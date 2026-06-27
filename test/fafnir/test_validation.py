"""Unit tests for boundary validation of price bars."""

from __future__ import annotations

import datetime as dt

from fafnir.ingest.daily_price import _validate_bar


def test_valid_bar():
    row, reason = _validate_bar(
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["trade_date"] == dt.date(2023, 6, 1)
    assert row["close"] == 10.0


def test_cross_field_violation_rejected():
    _, reason = _validate_bar(
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 8,
            "low": 9,
            "close": 10,
            "volume": 100,
        }
    )
    assert reason == "cross_field_violation"


def test_non_positive_price_rejected():
    _, reason = _validate_bar(
        {
            "date": "2023-06-01",
            "open": 0,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
        }
    )
    assert reason == "non_positive_price"


def test_negative_volume_rejected():
    _, reason = _validate_bar(
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": -5,
        }
    )
    assert reason == "negative_volume"


def test_unparseable_date_rejected():
    _, reason = _validate_bar(
        {"date": "not-a-date", "open": 10, "high": 11, "low": 9, "close": 10}
    )
    assert reason == "unparseable_date"


def test_missing_ohlc_rejected():
    _, reason = _validate_bar({"date": "2023-06-01", "open": 10, "high": 11})
    assert reason == "missing_or_nonnumeric_ohlc"
