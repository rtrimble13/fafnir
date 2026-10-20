"""
`fafnir ingest sharadar-prices` (plan SA-0601, #75; the integration tests need
FAFNIR_TEST_DSN).

Sharadar's bars, raw recovered: the whole history of a security it minted, and,
fill-only, the dates a linked FMP security lacks. Every key, ticker and price is
synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sharadar_prices as sp

D = Decimal
Y = date
integration = pytest.mark.integration

PRICES = "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"
ACTIONS = "date,action,ticker,name,value,contraticker,contraname\n"


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_ratio_agrees_within_the_two_closes_rounding():
    assert sp.ratio_agrees(D("50.000"), D("100.000"), D("2"))
    # A 3-dp close of a low-priced bar: 0.333 x 3 = 0.999, inside the rounding.
    assert sp.ratio_agrees(D("0.333"), D("0.999"), D("3"))
    assert not sp.ratio_agrees(D("50.000"), D("150.000"), D("2"))


def test_recovered_bar_uses_actions_where_it_agrees():
    splits = [(Y(2005, 6, 1), D("2"))]
    row = {
        "ticker": "ZZ",
        "date": Y(2005, 3, 1),
        "open": D("49"),
        "high": D("51"),
        "low": D("48"),
        "close": D("50"),
        "volume": D("200"),
        "closeunadj": D("100"),
    }
    bar, agrees = sp.recovered_bar(row, splits)
    assert agrees is True
    assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (
        D("98.000000"),
        D("102.000000"),
        D("96.000000"),
        D("100.000000"),
        100,
    )
    # ACTIONS says 2, the vendor's own ratio 3: the ratio wins, and it is reported.
    bar, agrees = sp.recovered_bar(dict(row, closeunadj=D("150")), splits)
    assert agrees is False and bar["open"] == D("147.000000")
    # No close to recover from.
    bar, agrees = sp.recovered_bar(dict(row, close=None), splits)
    assert (bar["close"], agrees) == (None, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _file(db, tmp_path, table, body):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind="full",
        filters={},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )


def _bar(ticker, day, close, unadj, *, low=None):
    lo = low if low is not None else close
    return f"{ticker},{day},{close},{close},{lo},{close},100,{close},{unadj},{day}\n"


def _vendor(db, key, ticker):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', %s, %s)",
        (key, ticker),
    )


def _security(db, symbol, *, source):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        is_actively_trading=source == "fmp",
        delisted_date=None if source == "fmp" else Y(2006, 1, 3),
        source=source,
    )


def _link(db, sid, key, valid_from, *, feed):
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


def _bars(db, sid):
    return {
        r["trade_date"]: r
        for r in db.fetchall(
            "SELECT * FROM core.daily_price WHERE security_id = %s", (sid,)
        )
    }


def _flags(db, check):
    return db.fetchall(
        "SELECT security_id, record_key, detail FROM ops.data_quality_flag "
        "WHERE check_name = %s AND resolved_at IS NULL",
        (check,),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@integration
def test_backfill_loads_a_dead_issuers_raw_history(db, tmp_path):
    dead = _security(db, "ZZD1", source="sharadar")
    _vendor(db, "900001", "ZZD1")
    _link(db, dead, "900001", Y(2005, 1, 3), feed=True)
    _file(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _bar("ZZOTHER", "2005-03-01", "10", "10")  # not in scope
        + _bar("ZZD1", "2005-03-01", "50", "100")  # before the 2:1
        + _bar("ZZD1", "2005-03-02", "50", "100", low="60")  # low above close
        + _bar("ZZD1", "2005-07-01", "55", "55")  # after it
        + _bar("ZZD1", "2030-01-02", "55", "55"),  # not yet
    )
    _file(db, tmp_path, "actions", ACTIONS + "2005-06-01,split,ZZD1,ZZD1 Co,2,,\n")
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert (result.securities, result.written, result.mismatched_securities) == (
        1,
        2,
        0,
    )
    assert result.counts["quarantined:cross_field_violation"] == 1
    bars = _bars(db, dead)
    assert set(bars) == {Y(2005, 3, 1), Y(2005, 7, 1)}
    assert (bars[Y(2005, 3, 1)]["close"], bars[Y(2005, 3, 1)]["volume"]) == (
        D("100"),
        50,
    )
    assert {b["source"] for b in bars.values()} == {"sharadar"}
    assert repo.get_watermark(db, "sharadar", "SEP", dead) == Y(2005, 7, 1)
    (flag,) = _flags(db, "price_cross_field_violation")
    assert flag["record_key"] == {"symbol": "ZZD1", "date": "2005-03-02"}
    again = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert again.written == 0


@integration
def test_a_split_missing_from_actions_is_flagged_and_the_ratio_used(db, tmp_path):
    dead = _security(db, "ZZR1", source="sharadar")
    _vendor(db, "900002", "ZZR1")
    _link(db, dead, "900002", Y(2005, 1, 3), feed=True)
    _file(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _bar("ZZR1", "2005-03-01", "50", "100")
        + _bar("ZZR1", "2005-03-02", "50", "100")
        + _bar("ZZR1", "2005-07-01", "55", "55"),
    )
    _file(db, tmp_path, "actions", ACTIONS)  # no split recorded
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=Y(2026, 1, 2))
    assert result.mismatched_securities == 1
    assert _bars(db, dead)[Y(2005, 3, 1)]["close"] == D("100")
    (flag,) = _flags(db, "sharadar_split_ratio_mismatch")
    assert flag["security_id"] == dead
    assert flag["detail"] == {"bars": 2, "first": "2005-03-01", "last": "2005-03-02"}


@integration
def test_gap_fill_adds_only_the_dates_fmp_lacks_from_the_link(db, tmp_path):
    fmp = _security(db, "ZZL", source="fmp")
    _vendor(db, "900003", "ZZL")
    _link(db, fmp, "900003", Y(2010, 1, 4), feed=False)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": fmp,
                "trade_date": Y(2010, 1, 5),
                "open": D("20"),
                "high": D("20"),
                "low": D("20"),
                "close": D("20"),
                "volume": 7,
            }
        ],
    )
    repo.upsert_corporate_action(
        db,
        security_id=fmp,
        action_type="dividend",
        ex_date=Y(2010, 1, 7),
        dividend_amount=D("0.2"),
    )
    # An operator deleted this bar: the vendor's copy is set aside.
    db.execute(
        "INSERT INTO ops.operator_override (security_id, target, key_date, "
        "operation, note, created_by) VALUES (%s, 'daily_price', '2010-01-08', "
        "'delete', 'bad', 't')",
        (fmp,),
    )
    _file(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _bar("ZZL", "2009-12-31", "10", "10")  # before the link
        + _bar("ZZL", "2010-01-05", "21", "21")  # FMP holds it
        + _bar("ZZL", "2010-01-06", "21", "21")  # missing: filled
        + _bar("ZZL", "2010-01-08", "21", "21"),  # suppressed
    )
    _file(db, tmp_path, "actions", ACTIONS)
    plan = sp.load(
        db, tmp_path, backfill=False, gap_fill=True, dry_run=True, as_of=Y(2026, 1, 2)
    )
    assert (plan.counts["to_write"], plan.counts["held_or_outside"]) == (1, 2)
    assert plan.counts["suppressed"] == 1 and plan.run_id is None
    assert set(_bars(db, fmp)) == {Y(2010, 1, 5)}  # the dry run wrote nothing
    result = sp.load(db, tmp_path, backfill=False, gap_fill=True, as_of=Y(2026, 1, 2))
    bars = _bars(db, fmp)
    assert {d: b["source"] for d, b in bars.items()} == {
        Y(2010, 1, 5): "fmp",
        Y(2010, 1, 6): "sharadar",
    }
    assert bars[Y(2010, 1, 5)]["close"] == D("20")  # FMP's, untouched
    assert result.adjusted == 1  # it holds a dividend: factors recomputed
    assert repo.get_watermark(db, "sharadar", "SEP", fmp) is None  # FMP feeds it


@integration
def test_a_missing_price_file_is_an_error(db, tmp_path):
    dead = _security(db, "ZZM1", source="sharadar")
    _vendor(db, "900004", "ZZM1")
    _link(db, dead, "900004", Y(2005, 1, 3), feed=True)
    with pytest.raises(sp.PricesError, match="no current stocks file"):
        sp.load(db, tmp_path, backfill=True, gap_fill=False)


@integration
def test_the_command(db, tmp_path, monkeypatch):
    dead = _security(db, "ZZC1", source="sharadar")
    _vendor(db, "900005", "ZZC1")
    _link(db, dead, "900005", Y(2005, 1, 3), feed=True)
    _file(db, tmp_path, "stocks", PRICES + _bar("ZZC1", "2005-03-01", "10", "10"))
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "ingest", "sharadar-prices", *args]
        )

    assert "Give --backfill, --gap-fill or both" in run().output
    out = run("--backfill", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. 1 securities in scope; 1 rows read; 1 bars" in (
        out.output
    )
    out = run("--backfill")
    assert out.exit_code == 0, out.output
    assert "1 written" in out.output
    assert len(_bars(db, dead)) == 1
