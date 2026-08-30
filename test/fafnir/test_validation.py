"""Unit tests for boundary validation of price bars."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

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
    assert row["close"] == Decimal("10.0")


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
    assert row["close"] == Decimal("39.2")
    assert row["open"] == Decimal("39.0")
    assert row["high"] == Decimal("39.5")
    assert row["low"] == Decimal("38.5")


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
    assert row["close"] == Decimal("39.2")


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
    assert row["close"] == Decimal("39.2")


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


# -- review finding 5 (PR #11) ------------------------------------------------
#
# _ohlc returned the first PRESENT value, so a zero in the preferred spelling
# quarantined the bar even when the other spelling carried a real price. It now
# returns the first USABLE value, while still reporting the precise reason when
# nothing is usable.


def test_zero_in_the_preferred_spelling_falls_through_to_a_valid_one():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 0,
            "adjOpen": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["open"] == Decimal("39.0")


def test_junk_in_the_preferred_spelling_falls_through_to_a_valid_one():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": "n/a",
            "adjOpen": 39.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["open"] == Decimal("39.0")


def test_an_all_zero_bar_still_reports_non_positive_price():
    """The precise quarantine reason must survive the fallback logic."""
    _, reason = _validate_bar(
        {"date": "1990-01-02", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 1}
    )
    assert reason == "non_positive_price"


def test_an_all_junk_bar_still_reports_nonnumeric():
    _, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": "x",
            "high": "x",
            "low": "x",
            "close": "x",
            "volume": 1,
        }
    )
    assert reason == "missing_or_nonnumeric_ohlc"


def test_plain_name_still_wins_when_both_spellings_are_usable():
    row, reason = _validate_bar(
        {
            "date": "1990-01-02",
            "open": 39.0,
            "adjOpen": 1.0,
            "high": 39.5,
            "low": 38.5,
            "close": 39.2,
            "volume": 100,
        }
    )
    assert reason is None
    assert row["open"] == Decimal("39.0")


# ---------------------------------------------------------------------------
# The bar must survive the COLUMN, not just Python. core.daily_price stores money
# as NUMERIC(20, 6) and volume as BIGINT; Postgres rounds to the column's scale on
# insert and only then evaluates ck_daily_price_positive. Validating with
# `float(v) > 0` let a positive-but-sub-resolution price through, which landed as
# 0.000000 and aborted the whole batch mid-backfill.
# ---------------------------------------------------------------------------


def _bar(**overrides) -> dict:
    return {
        "date": "2023-06-01",
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "volume": 100,
    } | overrides


def test_sub_resolution_price_is_quarantined_not_inserted():
    """Regression: a sub-penny shell at 1e-7 rounds to 0.000000 in NUMERIC(20, 6)."""
    _, reason = _validate_bar(
        {
            "date": "2024-08-14",
            "open": 1e-7,
            "high": 1e-7,
            "low": 1e-7,
            "close": 1e-7,
            "volume": 3795,
        }
    )
    assert reason == "subresolution_price"


def test_sub_resolution_is_distinguished_from_a_feed_zero():
    """Different data, different remedy -- the DQ queue must not conflate them."""
    assert _validate_bar(_bar(open=0.0000004))[1] == "subresolution_price"
    assert _validate_bar(_bar(open=0))[1] == "non_positive_price"


def test_a_price_on_the_rounding_boundary_is_kept():
    """Half a micro-dollar rounds UP, so the column holds it as positive."""
    row, reason = _validate_bar(
        _bar(open="0.0000005", high="0.0000005", low="0.0000005", close="0.0000005")
    )
    assert reason is None
    assert row["close"] == Decimal("0.000001")


def test_a_sub_resolution_price_does_not_shadow_a_usable_alias():
    """The alias fallback must prefer the field that has a *storable* price."""
    row, reason = _validate_bar(
        _bar(open=1e-7, adjOpen=39.0, high=39.5, low=38.5, close=39.2)
    )
    assert reason is None
    assert row["open"] == Decimal("39.0")


def test_price_wider_than_the_column_is_quarantined():
    """NUMERIC(20, 6) leaves 14 integer digits; more raised NumericValueOutOfRange."""
    assert _validate_bar(_bar(close=1e20, high=1e20))[1] == "price_out_of_range"
    assert _validate_bar(_bar(close=10**14, high=10**14))[1] == "price_out_of_range"
    assert _validate_bar(_bar(close="99999999999999", high="99999999999999"))[1] is None


def test_volume_beyond_bigint_is_quarantined():
    assert _validate_bar(_bar(volume=2**63))[1] == "volume_out_of_range"
    assert _validate_bar(_bar(volume=2**63 - 1))[1] is None


def test_non_finite_values_are_quarantined_rather_than_raising():
    """float('inf') used to escape int() as an uncaught OverflowError."""
    assert _validate_bar(_bar(volume=float("inf")))[1] == "nonnumeric_volume"
    assert (
        _validate_bar(_bar(close=float("nan"), high=float("nan")))[1]
        == "missing_or_nonnumeric_ohlc"
    )


def test_prices_are_exact_decimals_not_floats():
    """core.daily_price is exact NUMERIC: no float round-trip after validation."""
    row, reason = _validate_bar(_bar(close=0.1, low=0.05))
    assert reason is None
    assert isinstance(row["close"], Decimal)
    assert row["close"] == Decimal("0.1")


def test_an_unusable_vwap_is_dropped_without_failing_the_bar():
    """vwap is nullable and unconstrained -- losing it must not cost the whole bar."""
    assert _validate_bar(_bar(vwap="N/A")) == (
        _validate_bar(_bar())[0],
        None,
    )
    row, reason = _validate_bar(_bar(vwap=1e20))
    assert reason is None
    assert row["vwap"] is None


# ---------------------------------------------------------------------------
# The quiet half of the resolution problem
# ---------------------------------------------------------------------------
#
# _reject_reason catches a price BELOW what the money column can hold and the bar is
# quarantined, loudly. ROUND_HALF_UP at six places puts that cliff at 5e-7 -- so a
# security quoted just above it passes every check and is stored with
# open = high = low = close, its real intraday range replaced by a zero-range
# session that nothing downstream can tell from a genuine no-trade day.
#
# The numbers below are real bars from the production warehouse (HIND 2016-10-31,
# ORIG 2011-09-19, PPCB 2026-08-28, ELOX 2024-08-05), which is the only reason it
# was found at all: `price_subresolution_price` was flagging the narrow band either
# side of it while this band went by silently.


def _collapse(bar):
    from fafnir.ingest.daily_price import _scale_collapse_detail

    row, reason = _validate_bar(bar)
    assert reason is None, f"expected a stored bar, got price_{reason}"
    return _scale_collapse_detail(bar, row)


def test_a_flattened_bar_is_detected():
    detail = _collapse(
        {
            "date": "2016-10-31",
            "open": "0.000000788",
            "high": "0.000000801",
            "low": "0.000000732",
            "close": "0.000000733",
            "volume": "0",
        }
    )
    assert detail is not None
    assert detail["stored"] == "0.000001"
    assert Decimal(detail["source_range"]) > 0


def test_the_detail_carries_the_range_the_column_lost():
    # The whole point of the flag: the source values are gone from core.daily_price
    # once the bar is stored, so an operator cannot otherwise tell how much was lost.
    detail = _collapse(
        {
            "date": "2011-09-19",
            "open": "0.000001626087",
            "high": "0.000001733696",
            "low": "0.000001517391",
            "close": "0.000001680435",
            "volume": "84640000",
        }
    )
    assert detail["source_high"] == "0.000001733696"
    assert detail["source_low"] == "0.000001517391"
    assert detail["stored"] == "0.000002"


def test_an_ordinary_bar_is_not_flagged():
    assert (
        _collapse(
            {
                "date": "2026-08-28",
                "open": "1.93",
                "high": "1.94",
                "low": "1.31",
                "close": "1.35",
                "volume": "4056907",
            }
        )
        is None
    )


def test_a_genuinely_flat_session_is_not_flagged():
    # A halted or one-trade session is flat at the source. The test is that a range
    # EXISTED and the column lost it -- not that the stored bar is flat, which would
    # flag every no-trade day in the warehouse.
    assert (
        _collapse(
            {
                "date": "2024-08-05",
                "open": "0.06363636",
                "high": "0.06363636",
                "low": "0.06363636",
                "close": "0.06363636",
                "volume": "0",
            }
        )
        is None
    )
    assert (
        _collapse(
            {
                "date": "2024-08-05",
                "open": "12.5",
                "high": "12.5",
                "low": "12.5",
                "close": "12.5",
                "volume": "10",
            }
        )
        is None
    )


def test_a_quarantined_bar_never_reaches_the_collapse_check():
    # Below the cliff the bar is rejected outright, so the two conditions partition
    # the problem rather than double-reporting the same bar.
    row, reason = _validate_bar(
        {
            "date": "2024-08-05",
            "open": "0.0000001",
            "high": "0.0000001",
            "low": "0.0000001",
            "close": "0.0000001",
            "volume": "1",
        }
    )
    assert row is None and reason == "subresolution_price"
