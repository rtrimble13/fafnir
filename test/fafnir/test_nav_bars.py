"""
Unit tests for the NAV-priced bar allowance (ADR 0006).

A mutual fund is struck once a day and does not trade, so FMP returns a bar with a
close and no open/high/low. That shape is the whole truth about the day for a fund
and a defect for an equity, which is why the allowance is gated on the security's
asset_type rather than on what the payload happens to omit. These tests pin both
halves of that gate, and the line between "absent" (stood in for) and "present but
unusable" (still quarantined).
"""

from __future__ import annotations

from decimal import Decimal

from fafnir.ingest.daily_price import NAV_ASSET_TYPES, _validate_bar


def test_nav_only_bar_expands_to_flat_ohlc():
    row, reason = _validate_bar({"date": "2024-06-03", "close": 412.55}, nav_only=True)
    assert reason is None
    assert (
        row["open"]
        == row["high"]
        == row["low"]
        == row["close"]
        == Decimal("412.550000")
    )
    # A fund reports no volume. The column defaults to 0 and the CHECK allows it.
    assert row["volume"] == 0


def test_the_same_bar_is_quarantined_for_an_equity():
    """The gate is the point: an OHLC-less equity bar is still a defect."""
    row, reason = _validate_bar({"date": "2024-06-03", "close": 412.55})
    assert row is None
    assert reason == "missing_or_nonnumeric_ohlc"


def test_nav_only_does_not_invent_a_close():
    """Nothing to stand in with. Rejected on every asset type alike."""
    _, reason = _validate_bar({"date": "2024-06-03", "volume": 0}, nav_only=True)
    assert reason == "missing_or_nonnumeric_ohlc"


def test_nav_only_still_rejects_a_present_but_unusable_close():
    """A zero close is bad data on a fund exactly as on an equity.

    The allowance stands in for an ABSENT field. Laundering an unusable close into
    three more copies of itself would hide the very thing the quarantine exists to
    surface.
    """
    _, reason = _validate_bar({"date": "2024-06-03", "close": 0}, nav_only=True)
    assert reason == "non_positive_price"


def test_nav_only_still_rejects_a_present_but_unusable_high():
    _, reason = _validate_bar(
        {"date": "2024-06-03", "close": 10, "high": 0}, nav_only=True
    )
    assert reason == "non_positive_price"


def test_nav_only_keeps_fields_that_are_present():
    """An allowance, not a rewrite: a fund payload that does carry OHLC is used."""
    row, reason = _validate_bar(
        {
            "date": "2024-06-03",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.25,
        },
        nav_only=True,
    )
    assert reason is None
    assert row["open"] == Decimal("10.000000")
    assert row["high"] == Decimal("10.500000")


def test_nav_only_still_enforces_cross_field_checks():
    """A fund bar with an inconsistent high is quarantined like any other."""
    _, reason = _validate_bar(
        {"date": "2024-06-03", "close": 10.0, "high": 9.0, "low": 8.0, "open": 8.5},
        nav_only=True,
    )
    assert reason == "cross_field_violation"


def test_nav_only_accepts_the_adj_prefixed_spelling():
    """The unadjusted endpoint prefixes its OHLC names; the fund path must too."""
    row, reason = _validate_bar(
        {"date": "2024-06-03", "adjClose": 412.55}, nav_only=True
    )
    assert reason is None
    assert row["close"] == Decimal("412.550000")


def test_fund_is_the_nav_asset_type():
    assert "fund" in NAV_ASSET_TYPES
    assert "equity" not in NAV_ASSET_TYPES
    assert "etf" not in NAV_ASSET_TYPES, (
        "an ETF trades through a session and has real OHLCV -- admitting the NAV "
        "shape for it would silence a genuinely broken payload"
    )


# ---------------------------------------------------------------------------
# NAV expansion vs. price_scale_collapse
# ---------------------------------------------------------------------------
#
# Both features put open = high = low = close into core.daily_price, and they
# landed independently. One is correct (a fund strikes one price a day, so there
# never was a range) and one is corruption (the money column's scale crushed a real
# range). price_scale_collapse tells them apart by reading the SOURCE bar: a NAV
# payload carries no open/high/low to compare, so it cannot evidence the range the
# check requires.
#
# That is quiet coupling. Teaching _source_price to fall back to the close would
# turn every fund bar in the warehouse into a flag overnight, and nothing else in
# either feature would fail. These are the tests that would.


def test_a_nav_bar_is_not_a_scale_collapse():
    from fafnir.ingest.daily_price import _scale_collapse_detail

    bar = {"date": "2024-03-01", "close": "18.42"}
    row, reason = _validate_bar(bar, nav_only=True)
    assert reason is None
    assert row["open"] == row["high"] == row["low"] == row["close"]
    assert _scale_collapse_detail(bar, row) is None


def test_a_nav_bar_priced_in_the_flattened_band_is_still_not_a_collapse():
    # The overlap that matters: a fund quoted inside the band where the column
    # genuinely does destroy ranges. It is still not a collapse, because a NAV bar
    # had no range to destroy.
    from fafnir.ingest.daily_price import _scale_collapse_detail

    bar = {"date": "2024-03-01", "close": "0.000000733"}
    row, reason = _validate_bar(bar, nav_only=True)
    assert reason is None
    assert str(row["close"]) == "0.000001"
    assert _scale_collapse_detail(bar, row) is None


def test_an_equity_in_the_same_band_is_still_a_collapse():
    # The other side of the guard: gating on the source bar must not have made the
    # check unreachable for the securities it was written for.
    from fafnir.ingest.daily_price import _scale_collapse_detail

    bar = {
        "date": "2016-10-31",
        "open": "0.000000788",
        "high": "0.000000801",
        "low": "0.000000732",
        "close": "0.000000733",
        "volume": "0",
    }
    row, reason = _validate_bar(bar)
    assert reason is None
    assert _scale_collapse_detail(bar, row) is not None


def test_a_fund_bar_that_does_carry_a_range_is_judged_on_it():
    # nav_only is an allowance, not an override: a fund payload that DOES carry
    # open/high/low is read from those fields, so the collapse check applies to it
    # exactly as it would to an equity.
    from fafnir.ingest.daily_price import _scale_collapse_detail

    bar = {
        "date": "2016-10-31",
        "open": "0.000000788",
        "high": "0.000000801",
        "low": "0.000000732",
        "close": "0.000000733",
        "volume": "0",
    }
    row, reason = _validate_bar(bar, nav_only=True)
    assert reason is None
    assert _scale_collapse_detail(bar, row) is not None
