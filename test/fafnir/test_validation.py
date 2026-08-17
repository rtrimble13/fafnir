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


# -- FMP field-name variants --------------------------------------------------
#
# The unadjusted endpoint (historical-price-eod/non-split-adjusted) labels its OHLC
# fields adjOpen/adjHigh/adjLow/adjClose, the same convention the dividend-adjusted
# payload uses. On the unadjusted feed the prefix is only a name -- the values are
# the prices as traded. Rejecting those bars would quarantine every row.


def test_adj_prefixed_ohlc_is_accepted():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "adjOpen": 39.0,
            "adjHigh": 39.5,
            "adjLow": 38.5,
            "adjClose": 39.2,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["close"] == 39.2
    assert row["open"] == 39.0
    assert row["high"] == 39.5
    assert row["low"] == 38.5


def test_plain_names_win_when_a_payload_carries_both():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "adjOpen": 1.0,
            "adjHigh": 1.0,
            "adjLow": 1.0,
            "adjClose": 1.0,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["close"] == 39.2


def test_bar_with_neither_spelling_is_quarantined():
    _, reason = _validate_bar({"date": "1990-01-02", "volume": 100})
    assert reason == "missing_or_nonnumeric_ohlc"


def test_null_plain_field_falls_back_to_the_adj_spelling():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "adjOpen": 39.0,
            "adjHigh": 39.5,
            "adjLow": 38.5,
            "adjClose": 39.2,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["close"] == 39.2


# -- volume spellings ---------------------------------------------------------
#
# core.daily_price is raw, and volume back-adjusts the opposite way to price (a 4:1
# split multiplies pre-split share counts by 4). An already-adjusted volume would be
# inflated by the split ratio squared, so where a payload offers `unadjustedVolume`
# -- raw by definition -- it wins over `volume`.


def test_unadjusted_volume_is_preferred_over_volume():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 112_000_000,
            "unadjustedVolume": 1_000_000,
        }
    )
    assert reason is None
    assert row["volume"] == 1_000_000


def test_volume_is_used_when_unadjusted_volume_is_absent():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 1_000_000,
        }
    )
    assert reason is None
    assert row["volume"] == 1_000_000


def test_null_unadjusted_volume_falls_back_to_volume():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 1_000_000,
            "unadjustedVolume": None,
        }
    )
    assert reason is None
    assert row["volume"] == 1_000_000


def test_a_bar_with_no_volume_at_all_is_zero_not_a_quarantine():
    row, reason = _validate_bar(
        {"date": "1990-01-02", "open": 39.0, "high": 39.5, "low": 38.5, "close": 39.2}
    )
    assert reason is None
    assert row["volume"] == 0
