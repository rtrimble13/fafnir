"""
Integration tests for the ingestion core (needs FAFNIR_TEST_DSN):
idempotency, adjustment correctness, point-in-time stability, and DQ checks.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import adjustments
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


class _FakeFMP:
    """Minimal FMP stand-in returning canned bars (no network)."""

    bytes_downloaded = 0

    def __init__(self, bars):
        self._bars = bars

    def eod_full(self, symbol, from_date=None, to_date=None):
        return self._bars


def _mk_security(db, symbol="AAA"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name="Test",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _prices(sid, rows):
    return [{"security_id": sid, **r} for r in rows]


def test_price_upsert_is_idempotent(db):
    sid = _mk_security(db)
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 101,
                "high": 103,
                "low": 100,
                "close": 102,
                "volume": 1100,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_daily_prices(db, rows)  # second load: same window
    n = db.fetchval(
        "SELECT count(*) FROM core.daily_price WHERE security_id=%s", (sid,)
    )
    assert n == 2  # no duplication


def test_split_adjustment_is_correct_and_pit_stable(db):
    sid = _mk_security(db, "BBB")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 2),
                "open": 50,
                "high": 51,
                "low": 49,
                "close": 50,
                "volume": 2000,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=2,
        split_denominator=1,
    )
    adjustments.compute_for_security(db, sid)

    adj = repo.read_price_history(db, "BBB", None, None, adjusted=True)
    by_date = {r["date"]: r for r in adj}
    # Pre-split closes halved; ex-date and later unchanged.
    assert float(by_date[dt.date(2023, 5, 31)]["close"]) == pytest.approx(50.0)
    assert float(by_date[dt.date(2023, 6, 1)]["close"]) == pytest.approx(50.0)
    assert float(by_date[dt.date(2023, 6, 2)]["close"]) == pytest.approx(50.0)
    # Volume scaled into post-split share terms pre-split.
    assert int(by_date[dt.date(2023, 5, 31)]["volume"]) == 2000

    raw = repo.read_price_history(db, "BBB", None, None, adjusted=False)
    raw_by_date = {r["date"]: r for r in raw}
    assert float(raw_by_date[dt.date(2023, 5, 31)]["close"]) == pytest.approx(100.0)


def test_dividend_adjustment(db):
    sid = _mk_security(db, "CCC")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 99,
                "high": 99,
                "low": 99,
                "close": 99,
                "volume": 1000,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2023, 6, 1),
        dividend_amount=1.0,
    )
    adjustments.compute_for_security(db, sid)
    adj = {
        r["date"]: r
        for r in repo.read_price_history(db, "CCC", None, None, adjusted=True)
    }
    # Prior close 100, dividend 1 -> factor (100-1)/100 = 0.99 applied to 5/31.
    assert float(adj[dt.date(2023, 5, 31)]["close"]) == pytest.approx(99.0)
    assert float(adj[dt.date(2023, 6, 1)]["close"]) == pytest.approx(99.0)


def test_gap_check_flags_missing_day(db):
    sid = _mk_security(db, "DDD")
    # 2023-06-01 (Thu) and 2023-06-05 (Mon) present; 2023-06-02 (Fri, open) missing.
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
            {
                "trade_date": dt.date(2023, 6, 5),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    checks.check_gaps(db, exchange_code="NASDAQ")
    flags = db.fetchall(
        "SELECT record_key FROM ops.data_quality_flag WHERE check_name='gap' AND security_id=%s",
        (sid,),
    )
    flagged_dates = {f["record_key"]["trade_date"] for f in flags}
    assert "2023-06-02" in flagged_dates


def test_outlier_check_flags_unexplained_jump(db):
    sid = _mk_security(db, "EEE")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1,
            },
            {
                "trade_date": dt.date(2023, 6, 2),
                "open": 100,
                "high": 200,
                "low": 100,
                "close": 200,
                "volume": 1,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    n = checks.check_outliers(db, threshold=0.5)
    assert n >= 1


def test_ensure_year_partition_relocates_default_rows(db):
    # A row in a year with no dedicated partition lands in the DEFAULT partition;
    # creating that year's partition must succeed and relocate the stray row
    # (regression test for the attach-conflict bug).
    # Repeatable: the partition table persists across runs (TRUNCATE won't drop it).
    db.execute("DROP TABLE IF EXISTS core.daily_price_y2099")
    sid = _mk_security(db, "FFF")
    # 2099 has no dedicated partition -> goes to daily_price_default.
    far = dt.date(2099, 3, 15)
    repo.upsert_daily_prices(
        db,
        _prices(
            sid,
            [
                {
                    "trade_date": far,
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "volume": 1,
                }
            ],
        ),
    )
    in_default = db.fetchval(
        "SELECT count(*) FROM core.daily_price_default WHERE security_id=%s", (sid,)
    )
    assert in_default == 1

    # Creating the 2099 partition must not raise and must relocate the row.
    created = maintenance.ensure_year_partition(db, 2099)
    assert created is True
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price_default WHERE security_id=%s",
            (sid,),
        )
        == 0
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price_y2099 WHERE security_id=%s", (sid,)
        )
        == 1
    )
    # Row is still visible through the parent partitioned table.
    assert (
        db.fetchval(
            "SELECT close FROM core.daily_price WHERE security_id=%s AND trade_date=%s",
            (sid, far),
        )
        == 10
    )


def test_watermark_not_advanced_past_quarantined_bar(db):
    # 6/1 clean, 6/2 bad (high<low) -> quarantined, 6/5 clean. The watermark must
    # stay at 6/1 so the overlap re-fetches 6/2 next run (no permanent gap).
    sid = _mk_security(db, "GGG")
    bars = [
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-02",
            "open": 10,
            "high": 8,
            "low": 9,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-05",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
    ]
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(
            db,
            _FakeFMP(bars),
            "GGG",
            run=run,
            start_date=dt.date(2023, 6, 1),
            end_date=dt.date(2023, 6, 5),
        )
    # Clean bars were still written (6/1 and 6/5); 6/2 quarantined.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id=%s", (sid,)
        )
        == 2
    )
    # Watermark held at the last contiguous clean date before the quarantine.
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 1)


def test_resolvers_agree_on_ambiguous_symbol(db):
    # Two securities share primary_symbol with no xref row -> exercise the fallback.
    sid_fmp = repo.upsert_security(
        db, primary_symbol="DUP", company_name="A", source="fmp"
    )
    repo.upsert_security(db, primary_symbol="DUP", company_name="B", source="other")

    from_repo = repo.resolve_security_id(db, "DUP")

    import psycopg
    from psycopg.rows import dict_row

    from duk.datasource.db import _resolve_security_id

    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        from_duk = _resolve_security_id(cur, "DUP")

    # Both paths resolve identically, and to the fmp-source row (deterministic).
    assert from_repo == from_duk == sid_fmp
