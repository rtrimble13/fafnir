"""
The precedence write path (ADR 0015 §3; needs FAFNIR_TEST_DSN).

Before this, the price upsert overwrote whatever it found and relabelled the row as
its own, so the first Sharadar load would have silently replaced FMP's bars. Now:

  * a writer revises its own rows;
  * it never writes over a row a higher-ranked party holds -- every column of that
    row, lineage included, stays exactly as it was, and the key is counted;
  * with ``correct=True`` it replaces a lower-ranked party's row, keeping the row as
    it stood in ops.vendor_correction, and a revert writes it back exactly;
  * an operator's row, or a key an operator edited, is never written by a vendor.

Ranks: operator > sharadar > tiingo > fmp.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

DAY = dt.date(2024, 1, 2)
EX = dt.date(2024, 1, 5)


@pytest.fixture()
def sid(db):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    security_id = repo.upsert_security(
        db, primary_symbol="ACME", company_name="Acme", exchange_code="NASDAQ"
    )
    repo.upsert_symbol_xref(db, security_id=security_id, symbol="ACME")
    return security_id


def _run_id(db, source="fmp"):
    return int(
        db.fetchval(
            "INSERT INTO ops.ingestion_run (source, endpoint, status) "
            "VALUES (%s, 'test', 'success') RETURNING ingestion_run_id",
            (source,),
        )
    )


def _bar(sid, close, day=DAY, volume=1000):
    c = Decimal(str(close))
    return {
        "security_id": sid,
        "trade_date": day,
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": volume,
    }


def _stored_bar(db, sid, day=DAY):
    return db.fetchone(
        "SELECT * FROM core.daily_price WHERE security_id = %s AND trade_date = %s",
        (sid, day),
    )


def _dividend(db, sid, amount, *, source, run_id=None, **kw):
    return repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=EX,
        dividend_amount=amount,
        ingestion_run_id=run_id,
        source=source,
        **kw,
    )


def _stored_action(db, sid):
    return db.fetchone(
        "SELECT * FROM core.corporate_action "
        "WHERE security_id = %s AND action_type = 'dividend' AND ex_date = %s",
        (sid, EX),
    )


def _corrections(db):
    return db.fetchall("SELECT * FROM ops.vendor_correction ORDER BY correction_id")


# ---------------------------------------------------------------------------
# Bars: a vendor never writes over another party's row
# ---------------------------------------------------------------------------


def test_a_sharadar_load_leaves_an_fmp_bar_byte_identical(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)], ingestion_run_id=_run_id(db))
    before = _stored_bar(db, sid)

    result = repo.upsert_daily_prices(
        db,
        [_bar(sid, 101)],
        ingestion_run_id=_run_id(db, "sharadar"),
        source="sharadar",
    )

    assert _stored_bar(db, sid) == before  # source, ingestion_run_id, loaded_at too
    assert result == 0
    assert result.skipped_foreign == 1
    assert _corrections(db) == []


def test_a_vendor_revises_its_own_bar(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)], source="sharadar")
    result = repo.upsert_daily_prices(db, [_bar(sid, 101)], source="sharadar")
    assert result == 1 and result.updated == 1
    assert _stored_bar(db, sid)["close"] == Decimal("101")


def test_fmp_keeps_its_old_behaviour_on_its_own_rows(db, sid):
    rows = [_bar(sid, 100), _bar(sid, 100, day=DAY + dt.timedelta(days=1))]
    first = repo.upsert_daily_prices(db, rows)
    again = repo.upsert_daily_prices(db, [_bar(sid, 102)])
    assert (first, first.inserted) == (2, 2)
    assert (again, again.updated) == (1, 1)
    assert _stored_bar(db, sid)["close"] == Decimal("102")


def test_a_lower_rank_cannot_correct_a_higher_one(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)], source="sharadar")
    result = repo.upsert_daily_prices(db, [_bar(sid, 90)], correct=True)  # fmp
    assert result.skipped_foreign == 1
    assert _stored_bar(db, sid)["source"] == "sharadar"
    assert _stored_bar(db, sid)["close"] == Decimal("100")


def test_an_operator_bar_is_never_replaced(db, sid):
    db.execute(
        "INSERT INTO core.daily_price (security_id, trade_date, open, high, low, close,"
        " volume, source) VALUES (%s, %s, 5, 5, 5, 5, 10, 'operator')",
        (sid, DAY),
    )
    result = repo.upsert_daily_prices(
        db, [_bar(sid, 100)], source="sharadar", correct=True
    )
    assert result.skipped_foreign == 1
    assert _stored_bar(db, sid)["source"] == "operator"


# ---------------------------------------------------------------------------
# Bars: corrections, and reverting them
# ---------------------------------------------------------------------------


def test_a_correction_replaces_the_fmp_bar_and_keeps_it(db, sid):
    fmp_run = _run_id(db)
    repo.upsert_daily_prices(db, [_bar(sid, 100)], ingestion_run_id=fmp_run)
    before = _stored_bar(db, sid)
    sharadar_run = _run_id(db, "sharadar")

    result = repo.upsert_daily_prices(
        db,
        [_bar(sid, 25)],  # FMP had it at the scale of a split not yet gone ex
        ingestion_run_id=sharadar_run,
        source="sharadar",
        correct=True,
    )

    assert (result, result.corrected) == (1, 1)
    now = _stored_bar(db, sid)
    assert (now["source"], now["close"]) == ("sharadar", Decimal("25"))
    assert now["ingestion_run_id"] == sharadar_run
    [c] = _corrections(db)
    assert (c["target"], c["key_date"], c["action_type"]) == ("daily_price", DAY, None)
    assert (c["old_source"], c["new_source"]) == ("fmp", "sharadar")
    assert c["reason"] == "reconcile_divergence"
    assert Decimal(str(c["old_row"]["close"])) == before["close"]
    assert c["old_row"]["ingestion_run_id"] == fmp_run
    assert Decimal(str(c["new_row"]["close"])) == Decimal("25")


def test_a_revert_restores_the_fmp_bar_exactly(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)], ingestion_run_id=_run_id(db))
    before = _stored_bar(db, sid)
    repo.upsert_daily_prices(db, [_bar(sid, 25)], source="sharadar", correct=True)
    [c] = _corrections(db)

    repo.revert_vendor_correction(db, c["correction_id"], by="tester", note="why")

    assert _stored_bar(db, sid) == before  # values, source, run and loaded_at
    [c] = _corrections(db)
    assert c["reverted_by"] == "tester" and c["reverted_at"] is not None


def test_a_revert_refuses_a_row_changed_since(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)])
    repo.upsert_daily_prices(db, [_bar(sid, 25)], source="sharadar", correct=True)
    repo.upsert_daily_prices(db, [_bar(sid, 26)], source="sharadar")  # a revision
    [c] = _corrections(db)

    with pytest.raises(repo.RevertRefused, match="changed since"):
        repo.revert_vendor_correction(db, c["correction_id"], by="t")
    assert _stored_bar(db, sid)["close"] == Decimal("26")
    assert _corrections(db)[0]["reverted_at"] is None


def test_a_revert_refuses_twice(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)])
    repo.upsert_daily_prices(db, [_bar(sid, 25)], source="sharadar", correct=True)
    cid = _corrections(db)[0]["correction_id"]
    repo.revert_vendor_correction(db, cid, by="t")
    with pytest.raises(repo.RevertRefused, match="already reverted"):
        repo.revert_vendor_correction(db, cid, by="t")


def test_a_correction_never_writes_a_key_an_operator_edited(db, sid):
    repo.upsert_daily_prices(db, [_bar(sid, 100)])
    db.execute("DELETE FROM core.daily_price WHERE security_id = %s", (sid,))
    repo._insert_override(
        db,
        security_id=sid,
        target="daily_price",
        action_type=None,
        key_date=DAY,
        operation="delete",
        detail={},
        note="bar on a closure",
        created_by="tester",
    )
    result = repo.upsert_daily_prices(
        db, [_bar(sid, 25)], source="sharadar", correct=True
    )
    assert (result, result.skipped_suppressed) == (0, 1)
    assert _stored_bar(db, sid) is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"source": "polygon"}, "unknown source"),
        ({"source": "operator", "correct": True}, "operator row"),
        ({"source": "sharadar", "correct": True, "reason": "hunch"}, "reason"),
    ],
)
def test_the_writer_is_checked(db, sid, kwargs, message):
    with pytest.raises(ValueError, match=message):
        repo.upsert_daily_prices(db, [_bar(sid, 1)], **kwargs)


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------


def test_a_sharadar_action_leaves_an_fmp_action_byte_identical(db, sid):
    assert _dividend(db, sid, 0.48, source="fmp", run_id=_run_id(db)) is True
    before = _stored_action(db, sid)
    counts: dict = {}

    changed = _dividend(db, sid, 0.12, source="sharadar", counts=counts)

    assert changed is False
    assert counts == {"skipped_foreign": 1}
    assert _stored_action(db, sid) == before
    assert _corrections(db) == []


def test_a_vendor_revises_its_own_action(db, sid):
    assert _dividend(db, sid, 0.48, source="sharadar") is True
    assert _dividend(db, sid, 0.48, source="sharadar") is False  # unchanged
    assert _dividend(db, sid, 0.50, source="sharadar") is True


def test_an_agreeing_action_is_not_a_correction(db, sid):
    _dividend(db, sid, 0.48, source="fmp")
    assert _dividend(db, sid, 0.480000, source="sharadar", correct=True) is False
    assert _corrections(db) == []
    assert _stored_action(db, sid)["source"] == "fmp"


def test_an_action_correction_and_its_revert(db, sid):
    _dividend(db, sid, 0.48, source="fmp", run_id=_run_id(db))
    before = _stored_action(db, sid)
    counts: dict = {}

    assert _dividend(db, sid, 0.12, source="sharadar", correct=True, counts=counts)
    assert counts == {"corrected": 1}
    now = _stored_action(db, sid)
    assert (now["source"], now["dividend_amount"]) == ("sharadar", Decimal("0.12"))
    [c] = _corrections(db)
    assert (c["target"], c["action_type"], c["key_date"]) == (
        "corporate_action",
        "dividend",
        EX,
    )

    repo.revert_vendor_correction(db, c["correction_id"], by="t")
    assert _stored_action(db, sid) == before


def test_no_vendor_overwrites_an_operator_action(db, sid):
    _dividend(db, sid, 0.30, source="operator")
    counts: dict = {}
    for vendor in ("fmp", "sharadar"):
        assert (
            _dividend(db, sid, 0.99, source=vendor, correct=True, counts=counts)
            is False
        )
    assert counts == {"skipped_foreign": 2}
    assert _stored_action(db, sid)["source"] == "operator"


def test_an_action_correction_never_writes_a_key_an_operator_deleted(db, sid):
    repo._insert_override(
        db,
        security_id=sid,
        target="corporate_action",
        action_type="dividend",
        key_date=EX,
        operation="delete",
        detail={},
        note="the feed invented it",
        created_by="tester",
    )
    counts: dict = {}
    assert (
        _dividend(db, sid, 0.12, source="sharadar", correct=True, counts=counts)
        is False
    )
    assert counts == {"skipped_suppressed": 1}
    assert _stored_action(db, sid) is None


# ---------------------------------------------------------------------------
# A merge carries corrections to the survivor
# ---------------------------------------------------------------------------


def test_a_merge_carries_the_victims_corrections(db, sid):
    victim = repo.upsert_security(
        db,
        primary_symbol="ACME",
        company_name="Acme",
        exchange_code="NASDAQ",
        delisted_date=dt.date(2025, 1, 1),
        is_actively_trading=False,
    )
    later = DAY + dt.timedelta(days=1)
    repo.upsert_daily_prices(db, [_bar(sid, 100)])
    repo.upsert_daily_prices(db, [_bar(victim, 100, day=later)])
    repo.upsert_daily_prices(
        db, [_bar(victim, 25, day=later)], source="sharadar", correct=True
    )
    repo.merge_security(db, victim_id=victim, survivor_id=sid, force=True)
    assert [c["security_id"] for c in _corrections(db)] == [sid]


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_the_cli_lists_and_reverts(db, sid, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    repo.upsert_daily_prices(db, [_bar(sid, 100)])
    repo.upsert_daily_prices(db, [_bar(sid, 25)], source="sharadar", correct=True)
    cid = str(_corrections(db)[0]["correction_id"])

    listed = CliRunner().invoke(cli.main, ["vendor", "corrections", "list"])
    assert listed.exit_code == 0, listed.output
    assert "fmp -> sharadar" in listed.output and "active" in listed.output

    dry = CliRunner().invoke(
        cli.main, ["vendor", "corrections", "revert", cid, "-m", "x", "--dry-run"]
    )
    assert dry.exit_code == 0 and "Dry run" in dry.output
    assert _stored_bar(db, sid)["source"] == "sharadar"

    out = CliRunner().invoke(
        cli.main, ["vendor", "corrections", "revert", cid, "-m", "x", "--yes"]
    )
    assert out.exit_code == 0, out.output
    assert "fmp's row restored" in out.output
    assert _stored_bar(db, sid)["source"] == "fmp"

    again = CliRunner().invoke(
        cli.main, ["vendor", "corrections", "revert", cid, "-m", "x", "--yes"]
    )
    assert again.exit_code != 0 and "already reverted" in again.output
