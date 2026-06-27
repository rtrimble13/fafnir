"""Integration tests for duk's db datasource against fafnir (needs FAFNIR_TEST_DSN)."""

from __future__ import annotations

import datetime as dt
import os

import pytest

from duk.datasource import db as ds_db
from fafnir.db import repository as repo
from fafnir.ingest import adjustments

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


def _seed(db, symbol="ZZZ"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name="Z Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "security_id": sid,
                "trade_date": dt.date(2023, 6, 2),
                "open": 50,
                "high": 51,
                "low": 49,
                "close": 50,
                "volume": 2000,
            },
        ],
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=2,
        split_denominator=1,
    )
    adjustments.compute_for_security(db, sid)
    return sid


def test_db_price_history_raw_and_adjusted(db):
    _seed(db)
    raw = ds_db.price_history(
        dsn=DSN,
        symbol="ZZZ",
        start_date=None,
        end_date=None,
        frequency="day",
        limit=None,
        fields=None,
        adjusted=False,
    )
    assert list(raw.columns) == ["open", "high", "low", "close", "volume"]
    assert raw.loc["2023-05-31", "close"] == 100.0

    adj = ds_db.price_history(
        dsn=DSN,
        symbol="ZZZ",
        start_date=None,
        end_date=None,
        frequency="day",
        limit=None,
        fields=["close"],
        adjusted=True,
    )
    assert list(adj.columns) == ["close"]
    assert adj.loc["2023-05-31", "close"] == 50.0  # back-adjusted for 2:1 split


def test_db_price_history_unknown_symbol_returns_empty(db):
    out = ds_db.price_history(
        dsn=DSN,
        symbol="NOPE",
        start_date=None,
        end_date=None,
        frequency="day",
        limit=None,
        fields=None,
        adjusted=False,
    )
    assert out.empty
