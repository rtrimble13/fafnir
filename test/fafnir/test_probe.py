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


# -- volume ------------------------------------------------------------------
#
# Volume back-adjusts the OPPOSITE way to price: a 112:1 split multiplies pre-split
# share counts by 112. So a volume that arrives already split-adjusted is inflated
# by 112**2, not collapsed toward zero -- there is no vanish-to-zero tell, and no DQ
# check covers volume. These pin the classifier, including the case it cannot decide.

RAW_VOL = Decimal("1000000")


def _bar_v(close, volume, prefixed=False, unadj=None, date="1990-01-02"):
    bar = _bar(close, prefixed=prefixed, date=date)
    bar["volume"] = volume
    if unadj is not None:
        bar["unadjustedVolume"] = unadj
    return bar


def test_volume_raw_when_feeds_differ_by_the_split_ratio():
    """full restates volume into today's shares; non-split-adjusted leaves it raw."""
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL * 112)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_raw_confirmed"
    assert r["unadjusted_volume"] == RAW_VOL
    assert r["split_adjusted_volume"] == RAW_VOL * 112
    assert "inflating stored volume" in r["volume_detail"]


def test_explicit_unadjusted_volume_is_preferred_and_conclusive():
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL * 112, prefixed=True, unadj=RAW_VOL)],
        [_bar_v(ADJ_CLOSE, RAW_VOL * 112, unadj=RAW_VOL)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_raw_confirmed"
    assert r["volume_key"] == "unadjustedVolume"
    assert r["unadjusted_volume_field"] == RAW_VOL


def test_matching_volumes_that_equal_unadjusted_volume_mean_fmp_never_adjusts():
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL, unadj=RAW_VOL)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_raw_confirmed"
    assert "does not split-adjust volume" in r["volume_detail"]


def test_matching_volumes_that_exceed_unadjusted_volume_are_caught():
    """Both feeds adjusted -- fafnir would inflate by the ratio a second time."""
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL * 112, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL * 112, unadj=RAW_VOL)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_adjusted"
    assert "inflate it again" in r["volume_detail"]


def test_matching_volumes_without_a_tiebreaker_are_reported_as_undecidable():
    """The honest case: identical signatures for 'never adjusts' and 'both adjust'."""
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_ambiguous"
    assert "outside source" in r["volume_detail"]


def test_volume_mismatch_matching_neither_is_flagged():
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL * 5)],
    )
    r = probe.probe_prices(fmp)
    assert r["volume_verdict"] == "volume_ratio_mismatch"


def test_volume_section_renders():
    fmp = _FMP(
        [_bar_v(RAW_CLOSE, RAW_VOL, prefixed=True)],
        [_bar_v(ADJ_CLOSE, RAW_VOL * 112)],
    )
    text = probe.format_report(probe.probe_prices(fmp))
    assert "Volume cross-check" in text
    assert "unadjusted feed `volume`" in text
    assert "`unadjustedVolume`" in text


# -- review findings (PR #11) -------------------------------------------------
#
# Each of these reproduces a defect the pre-merge review found by executing the
# code. They exist so the specific failure mode cannot come back.


def test_feeds_are_compared_on_the_same_trading_day():
    """Finding 1: selecting each feed's nearest bar independently compared
    different days and charged the price move between them to the split ratio,
    turning a correct 4:1 feed into a `ratio_mismatch` that blocks a backfill."""
    fmp = _FMP(
        [_bar(Decimal("40.00"), prefixed=True, date="1990-01-02")],
        [_bar(Decimal("10.60"), date="1990-01-03")],  # different day, +6% move
        [{"date": "2020-08-31", "numerator": 4, "denominator": 1}],
    )
    r = probe.probe_prices(fmp)
    assert r["verdict"] == "inconclusive"
    assert r["compared_date"] == dt.date(1990, 1, 2)
    assert "split-adjusted feed does not" in r["detail"]


def test_a_matched_pair_still_evaluates_normally():
    fmp = _FMP(
        [_bar(Decimal("40.00"), prefixed=True, date="1990-01-02")],
        [_bar(Decimal("10.00"), date="1990-01-02")],
        [{"date": "2020-08-31", "numerator": 4, "denominator": 1}],
    )
    r = probe.probe_prices(fmp)
    assert r["verdict"] == "unadjusted_confirmed"
    assert r["compared_date"] == dt.date(1990, 1, 2)


def test_duplicate_dates_do_not_raise():
    """Finding 3: min() over (date, bar) tuples fell through to comparing dicts.

    The bars must DIFFER -- identical dicts compare equal, so the tuples never
    reach `<` and a naive fixture misses the bug entirely.
    """
    dupes = [
        _bar_v(ADJ_CLOSE, 1000, date="1990-01-02"),
        _bar_v(ADJ_CLOSE + Decimal("0.01"), 1100, date="1990-01-02"),
    ]
    r = probe.probe_prices(_FMP([_bar(RAW_CLOSE, prefixed=True)], dupes))
    assert r["verdict"] == "unadjusted_confirmed"


def test_non_numeric_unadjusted_volume_is_a_failure_not_a_pass():
    """Finding 4: presence of the key was treated as proof of rawness, so the
    probe passed a payload the loader quarantines as nonnumeric_volume."""
    bad = _bar_v(RAW_CLOSE, RAW_VOL, prefixed=True, unadj="N/A")
    r = probe.probe_prices(_FMP([bad], [_bar_v(ADJ_CLOSE, RAW_VOL * 112)]))
    assert r["volume_verdict"] == "volume_unusable"
    assert "nonnumeric_volume" in r["volume_detail"]
    # And the loader agrees, which is the point.
    from fafnir.ingest.daily_price import _validate_bar

    assert _validate_bar(bad)[1] == "nonnumeric_volume"


def test_zero_unadjusted_volume_is_still_conclusive():
    """A genuine zero-volume day parses fine and must not read as unusable."""
    bar = _bar_v(RAW_CLOSE, 0, prefixed=True, unadj=0)
    r = probe.probe_prices(_FMP([bar], [_bar_v(ADJ_CLOSE, 0)]))
    assert r["volume_verdict"] == "volume_raw_confirmed"
