"""The live price-feed probe: does it actually catch a split-adjusted feed?

These drive `probe_prices` with canned payloads standing in for FMP, so the logic
that would run against a real key is exercised without one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fafnir.sources import probe

# AAPL: 2:1, 2:1, 7:1, 4:1 after 1990 -> 112:1 cumulative.
SPLITS = [
    {"date": "2000-06-21", "numerator": 2, "denominator": 1},
    {"date": "2005-02-28", "numerator": 2, "denominator": 1},
    {"date": "2014-06-09", "numerator": 7, "denominator": 1},
    {"date": "2020-08-31", "numerator": 4, "denominator": 1},
    # Before the probe date: must NOT count toward the ratio.
    {"date": "1987-06-16", "numerator": 2, "denominator": 1},
]

RAW_CLOSE = Decimal("39.20")
ADJ_CLOSE = RAW_CLOSE / 112


class _FMP:
    def __init__(self, raw_bars, adj_bars, splits=SPLITS):
        self._raw, self._adj, self._splits = raw_bars, adj_bars, splits

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return self._raw

    def eod_split_adjusted(self, symbol, from_date=None, to_date=None):
        return self._adj

    def splits(self, symbol):
        return self._splits


def _bar(close, prefixed=False, date="1990-01-02"):
    if prefixed:
        return {
            "symbol": "AAPL",
            "date": date,
            "adjOpen": close,
            "adjHigh": close,
            "adjLow": close,
            "adjClose": close,
            "volume": 1000,
        }
    return {
        "symbol": "AAPL",
        "date": date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
    }


def test_cumulative_split_ratio_ignores_earlier_splits():
    assert probe.cumulative_split_ratio(SPLITS, dt.date(1990, 1, 2)) == 112
    # Standing after the 2014 split, only the 4:1 remains ahead.
    assert probe.cumulative_split_ratio(SPLITS, dt.date(2015, 1, 2)) == 4
    assert probe.cumulative_split_ratio(SPLITS, dt.date(2026, 1, 2)) == 1


def test_unadjusted_feed_is_confirmed():
    fmp = _FMP([_bar(RAW_CLOSE, prefixed=True)], [_bar(ADJ_CLOSE)])
    r = probe.probe_prices(fmp)
    assert r["verdict"] == "unadjusted_confirmed"
    assert r["ohlc_spelling"] == "adjOpen/adjHigh/adjLow/adjClose"
    assert r["loader_accepts"] is True
    assert r["split_ratio"] == 112
    assert "genuinely raw" in r["detail"]


def test_plain_field_names_are_reported_too():
    fmp = _FMP([_bar(RAW_CLOSE)], [_bar(ADJ_CLOSE)])
    r = probe.probe_prices(fmp)
    assert r["ohlc_spelling"] == "open/high/low/close"
    assert r["verdict"] == "unadjusted_confirmed"


def test_a_split_adjusted_feed_is_caught():
    """The regression this exists for: both endpoints returning the same price."""
    fmp = _FMP([_bar(ADJ_CLOSE, prefixed=True)], [_bar(ADJ_CLOSE)])
    r = probe.probe_prices(fmp)
    assert r["verdict"] == "feeds_agree"
    assert "do NOT ingest" in r["detail"]


def test_ratio_that_matches_neither_is_flagged():
    fmp = _FMP([_bar(RAW_CLOSE, prefixed=True)], [_bar(RAW_CLOSE / 8)])
    r = probe.probe_prices(fmp)
    assert r["verdict"] == "ratio_mismatch"


def test_a_date_with_no_later_splits_is_inconclusive():
    fmp = _FMP(
        [_bar(RAW_CLOSE, prefixed=True, date="2026-01-02")],
        [_bar(RAW_CLOSE, date="2026-01-02")],
    )
    r = probe.probe_prices(fmp, on_date=dt.date(2026, 1, 2))
    assert r["verdict"] == "inconclusive"
    assert "earlier --date" in r["detail"]


def test_missing_bars_are_inconclusive_not_a_crash():
    r = probe.probe_prices(_FMP([], []))
    assert r["verdict"] == "inconclusive"
    assert r["unadjusted_close"] is None


def test_unrecognized_field_names_are_reported_as_quarantining():
    odd = {"symbol": "AAPL", "date": "1990-01-02", "o": 1, "h": 1, "c": 1}
    fmp = _FMP([odd], [_bar(ADJ_CLOSE)])
    r = probe.probe_prices(fmp)
    assert r["ohlc_spelling"] == "unrecognized"
    assert r["loader_accepts"] is False
    assert r["quarantine_reason"] == "missing_or_nonnumeric_ohlc"


def test_report_renders_without_a_bar():
    # format_report must survive the inconclusive/no-data shape.
    text = probe.format_report(probe.probe_prices(_FMP([], [])))
    assert "INCONCLUSIVE" in text
    assert "implied ratio" in text
