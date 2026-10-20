"""
`fafnir ingest sharadar-actions` (plan SA-0602, #73; the integration tests need
FAFNIR_TEST_DSN).

Sharadar's splits and dividends: every one for a security it minted, fill-only for
an FMP security linked to it, never a future ex-date, never a second copy of an
event another source holds. Every key, ticker and amount is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sharadar_actions as sa

D = Decimal
Y = date
integration = pytest.mark.integration

HEADER = "date,action,ticker,name,value,contraticker,contraname\n"


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_planned_converts_splits_and_as_declared_dividends():
    skipped: Counter = Counter()
    actions = {
        "split": [(Y(2005, 6, 1), D("2")), (Y(2005, 6, 1), D("2")), (Y(2006, 1, 3), D("0"))],
        "dividend": [
            # Before the 2:1: ACTIONS carries it split-adjusted, so 0.25 was 0.50.
            (Y(2005, 3, 10), D("0.25")),
            # A regular and a special on one ex-date sum; an identical copy does not.
            (Y(2005, 9, 9), D("0.5")),
            (Y(2005, 9, 9), D("0.1")),
            (Y(2005, 9, 9), D("0.5")),
            (Y(2005, 12, 9), None),
        ],
    }  # fmt: skip
    planned = sa.planned_for(7, actions, skipped)
    by = {(p.action_type, p.ex_date): p for p in planned}
    split = by[("split", Y(2005, 6, 1))]
    assert (split.split_numerator, split.split_denominator) == (2, 1)
    assert by[("dividend", Y(2005, 3, 10))].dividend_amount == D("0.500000")
    assert by[("dividend", Y(2005, 9, 9))].dividend_amount == D("0.600000")
    assert len(planned) == 3
    assert skipped == {"duplicate": 2, "invalid": 2, "summed": 1}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _actions_file(db, tmp_path, rows):
    body = (
        HEADER + "".join(f"{d},{a},{t},{t} Co,{v},," "\n" for d, a, t, v in rows)
    ).encode()
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(body)
    repo.record_sharadar_file(
        db,
        vendor_table="actions",
        kind="full",
        filters={},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(body),
        rows=len(rows),
        columns=None,
        ingestion_run_id=None,
    )


def _vendor(db, key, ticker):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', %s, %s)",
        (key, ticker),
    )


def _sec(db, symbol, start, end, *, close=D("100"), **kw):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        **kw,
    )
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            rows.append(
                {
                    "security_id": sid,
                    "trade_date": d,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100,
                }
            )
        d += timedelta(days=1)
    if rows:
        repo.upsert_daily_prices(db, rows, source=kw.get("source", "fmp"))
    return sid


def _link(db, sid, key, valid_from, *, feed=False):
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=valid_from,
        match_method="minted" if feed else "manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=feed,
    )


def _actions(db, sid):
    return {
        (r["action_type"], r["ex_date"]): r
        for r in db.fetchall(
            "SELECT * FROM core.corporate_action WHERE security_id = %s", (sid,)
        )
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@integration
def test_a_dead_issuer_gets_its_split_and_dividends_and_the_right_factors(db, tmp_path):
    dead = _sec(
        db,
        "ZZD1",
        Y(2005, 1, 3),
        Y(2005, 12, 30),
        delisted_date=Y(2006, 1, 3),
        is_actively_trading=False,
        source="sharadar",
    )
    _vendor(db, "900001", "ZZD1")
    _link(db, dead, "900001", Y(2005, 1, 3), feed=True)
    _actions_file(
        db,
        tmp_path,
        [
            ("2005-03-10", "dividend", "ZZD1", "0.5"),  # 1.00 as declared
            ("2005-06-01", "split", "ZZD1", "2"),
            ("2005-09-09", "dividend", "ZZD1", "1"),
            ("2005-06-01", "listed", "ZZD1", ""),  # not an action: ignored
        ],
    )
    result = sa.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert (result.written, result.adjusted, result.awaiting_prices) == (3, 1, 0)
    rows = _actions(db, dead)
    assert {k: r["source"] for k, r in rows.items()} == {
        ("dividend", Y(2005, 3, 10)): "sharadar",
        ("split", Y(2005, 6, 1)): "sharadar",
        ("dividend", Y(2005, 9, 9)): "sharadar",
    }
    assert rows[("dividend", Y(2005, 3, 10))]["dividend_amount"] == D("1")
    assert rows[("split", Y(2005, 6, 1))]["split_numerator"] == D("2")
    # The raw close is a flat 100: before 2005-03-10 the factor is the split's 1/2
    # times both dividends' (100 - 1) / 100.
    adj = db.fetchone(
        "SELECT close, close_raw FROM mart.v_daily_price_adjusted "
        "WHERE security_id = %s AND trade_date = '2005-02-01'",
        (dead,),
    )
    assert adj["close_raw"] == D("100")
    assert round(adj["close"], 6) == D("49.005000")
    after = db.fetchval(
        "SELECT close FROM mart.v_daily_price_adjusted "
        "WHERE security_id = %s AND trade_date = '2005-10-03'",
        (dead,),
    )
    assert after == D("100")
    # A re-run writes nothing.
    again = sa.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert again.written == 0


@integration
def test_a_future_ex_date_is_never_stored(db, tmp_path):
    dead = _sec(db, "ZZF1", Y(2005, 1, 3), Y(2005, 3, 31), source="sharadar")
    _vendor(db, "900002", "ZZF1")
    _link(db, dead, "900002", Y(2005, 1, 3), feed=True)
    _actions_file(
        db,
        tmp_path,
        [
            ("2005-02-10", "dividend", "ZZF1", "1"),
            ("2030-02-10", "dividend", "ZZF1", "1"),
        ],
    )
    result = sa.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert result.skipped["future"] == 1
    assert set(_actions(db, dead)) == {("dividend", Y(2005, 2, 10))}


@integration
def test_gap_fill_adds_only_what_fmp_lacks_inside_the_link(db, tmp_path):
    fmp = _sec(db, "ZZL", Y(2008, 1, 2), Y(2012, 12, 31))
    _vendor(db, "900003", "ZZL")
    _link(db, fmp, "900003", Y(2010, 1, 4))
    repo.upsert_corporate_action(
        db,
        security_id=fmp,
        action_type="dividend",
        ex_date=Y(2011, 3, 10),
        dividend_amount=D("1"),
    )
    # An operator deleted this key: the vendor's copy is set aside.
    db.execute(
        "INSERT INTO ops.operator_override (security_id, target, action_type, "
        "key_date, operation, note, created_by) VALUES "
        "(%s, 'corporate_action', 'dividend', '2012-03-09', 'delete', 'bad', 't')",
        (fmp,),
    )
    _actions_file(
        db,
        tmp_path,
        [
            ("2009-03-10", "dividend", "ZZL", "1"),  # before the link
            ("2011-03-14", "dividend", "ZZL", "1"),  # FMP's, four days apart
            ("2011-06-10", "dividend", "ZZL", "1"),  # missing from FMP
            ("2012-03-09", "dividend", "ZZL", "1"),  # suppressed
            ("2013-03-11", "dividend", "ZZL", "1"),  # after the last bar
        ],
    )
    # A minted security is out of scope for --gap-fill.
    dead = _sec(db, "ZZM1", Y(2005, 1, 3), Y(2005, 3, 31), source="sharadar")
    _vendor(db, "900004", "ZZM1")
    _link(db, dead, "900004", Y(2005, 1, 3), feed=True)
    plan = sa.load(
        db, tmp_path, backfill=False, gap_fill=True, dry_run=True, as_of=Y(2026, 1, 2)
    )
    assert plan.securities == 1 and plan.run_id is None
    assert [(p.action_type, p.ex_date) for p in plan.planned] == [
        ("dividend", Y(2011, 6, 10))
    ]
    assert plan.skipped == {"outside_span": 2, "near_existing": 1, "suppressed": 1}
    assert len(_actions(db, fmp)) == 1  # the dry run wrote nothing
    result = sa.load(db, tmp_path, backfill=False, gap_fill=True, as_of=Y(2026, 1, 2))
    rows = _actions(db, fmp)
    assert {k: r["source"] for k, r in rows.items()} == {
        ("dividend", Y(2011, 3, 10)): "fmp",
        ("dividend", Y(2011, 6, 10)): "sharadar",
    }
    assert result.adjusted == 1
    # A re-run: its own row is not "near" another source's, and nothing changes.
    assert sa.load(db, tmp_path, backfill=False, gap_fill=True).written == 0


@integration
def test_no_actions_file_is_an_error(db):
    with pytest.raises(sa.ActionsError, match="no current actions file"):
        sa.load(db, "/nonexistent", backfill=True, gap_fill=False)


@integration
def test_the_command(db, tmp_path, monkeypatch):
    dead = _sec(db, "ZZC1", Y(2005, 1, 3), Y(2005, 3, 31), source="sharadar")
    _vendor(db, "900005", "ZZC1")
    _link(db, dead, "900005", Y(2005, 1, 3), feed=True)
    _actions_file(db, tmp_path, [("2005-02-10", "dividend", "ZZC1", "1")])
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "ingest", "sharadar-actions", *args]
        )

    assert "Give --backfill, --gap-fill or both" in run().output
    out = run("--backfill", "--dry-run")
    assert out.exit_code == 0, out.output
    assert (
        "Dry run: nothing written. 1 securities in scope; 0 splits and 1 dividends"
        in (out.output)
    )
    out = run("--backfill")
    assert out.exit_code == 0, out.output
    assert "1 new or changed" in out.output
    assert "factors recomputed for 1 securities; 0 await their prices" in out.output
