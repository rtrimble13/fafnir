"""
Integration tests for the ingestion core (needs FAFNIR_TEST_DSN):
idempotency, adjustment correctness, point-in-time stability, and DQ checks.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import adjustments

pytestmark = pytest.mark.integration


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
