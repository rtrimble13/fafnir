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
