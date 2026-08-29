"""The fund NAV probe: does it actually catch an already-adjusted NAV series?

`probe_prices` settles "is the feed raw" for equities using splits. Funds rarely
split, so `probe_fund_nav` asks the same question of distributions: a raw NAV drops
by the distributed amount on the ex-date, an already-reinvested one does not.

Getting this wrong in the permissive direction is the expensive failure -- it would
wave through a total-return feed, and fafnir would then adjust every distribution a
second time. So the tests below pin the FAIL verdicts as hard as the PASS one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fafnir.sources import probe

EX_DATE = "2024-12-18"
NAV_BEFORE = 400.0
DISTRIBUTION = 20.0


class _FMP:
    def __init__(self, bars, dividends, splits=()):
        self._bars, self._dividends, self._splits = bars, dividends, list(splits)

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return self._bars

    def dividends(self, symbol):
        return self._dividends

    def splits(self, symbol):
        return self._splits


def _nav_bars(nav_after: float, *, before: float = NAV_BEFORE) -> list[dict]:
    """A NAV-shaped payload: a close and nothing else."""
    return [
        {"date": "2024-12-17", "close": before},
        {"date": EX_DATE, "close": nav_after},
        {"date": "2024-12-19", "close": nav_after + 1},
    ]


def _distributions(amount: float = DISTRIBUTION) -> list[dict]:
    return [
        {"date": "2024-06-20", "dividend": 0.5},  # a small income distribution
        {"date": EX_DATE, "dividend": amount},  # the December capital gain
    ]


def test_a_raw_nav_series_is_confirmed():
    """NAV falls by the distribution: fafnir's factors are the only adjustment."""
    fmp = _FMP(_nav_bars(NAV_BEFORE - DISTRIBUTION), _distributions())
    report = probe.probe_fund_nav(fmp, "VFIAX")

    assert report["verdict"] == "nav_raw_confirmed"
    assert report["ex_date"] == dt.date(2024, 12, 18)
    assert report["distribution"] == Decimal("20.0")
    assert report["nav_drop"] == Decimal("20.0")
    assert report["drop_share"] == Decimal("1")


def test_a_total_return_series_is_caught():
    """The expensive failure: NAV does not drop, so distributions are already in.

    Loading them as corporate actions on top of this would adjust every one of
    them twice -- ADR 0004's failure on a new asset class.
    """
    fmp = _FMP(_nav_bars(NAV_BEFORE), _distributions())
    report = probe.probe_fund_nav(fmp, "VFIAX")

    assert report["verdict"] == "nav_already_adjusted"
    assert "do not" in report["detail"].lower()


def test_a_nav_that_rises_across_the_ex_date_is_caught():
    """Reinvestment plus an up day. Still not a raw series."""
    fmp = _FMP(_nav_bars(NAV_BEFORE + 2), _distributions())
    assert probe.probe_fund_nav(fmp, "VFIAX")["verdict"] == "nav_already_adjusted"


def test_a_partial_drop_is_a_mismatch_not_a_pass():
    """Half the distribution is neither hypothesis -- say so rather than guess."""
    fmp = _FMP(_nav_bars(NAV_BEFORE - 8), _distributions())
    assert probe.probe_fund_nav(fmp, "VFIAX")["verdict"] == "ratio_mismatch"


def test_an_ordinary_market_move_does_not_break_the_pass():
    """The drop is the distribution PLUS that day's move; the band allows for it."""
    fmp = _FMP(_nav_bars(NAV_BEFORE - DISTRIBUTION - 4), _distributions())
    assert probe.probe_fund_nav(fmp, "VFIAX")["verdict"] == "nav_raw_confirmed"


def test_no_price_history_is_its_own_verdict():
    """Whether the endpoint serves funds at all is question one, and answerable."""
    report = probe.probe_fund_nav(_FMP([], _distributions()), "VFIAX")
    assert report["verdict"] == "no_price_history"
    assert probe.NAV_ENDPOINT in report["detail"]


def test_no_distributions_is_inconclusive_not_a_pass():
    report = probe.probe_fund_nav(_FMP(_nav_bars(400.0), []), "VFIAX")
    assert report["verdict"] == "inconclusive"
    assert report["ex_date"] is None


def test_an_immaterial_distribution_is_inconclusive():
    """A 0.1% distribution cannot be told from a normal day's move either way."""
    fmp = _FMP(_nav_bars(NAV_BEFORE - 0.4), _distributions(0.4))
    report = probe.probe_fund_nav(fmp, "VFIAX")
    assert report["verdict"] == "inconclusive"


def test_the_largest_distribution_is_the_one_probed():
    """Biggest, not most recent: only a large one clears the day's market move."""
    dividends = [
        {"date": EX_DATE, "dividend": DISTRIBUTION},
        {"date": "2025-03-20", "dividend": 0.6},  # more recent, far smaller
    ]
    report = probe.probe_fund_nav(_FMP(_nav_bars(380.0), dividends), "VFIAX")
    assert report["ex_date"] == dt.date(2024, 12, 18)
    assert report["distribution"] == Decimal("20")


def test_a_missing_bar_beside_the_ex_date_reports_nothing_rather_than_a_wrong_answer():
    bars = [{"date": "2024-12-19", "close": 380.0}]  # nothing before the ex-date
    report = probe.probe_fund_nav(_FMP(bars, _distributions()), "VFIAX")
    assert report["verdict"] == "inconclusive"
    assert report["nav_drop"] is None


def test_the_report_shows_that_the_asset_type_gate_is_load_bearing():
    """A NAV payload is accepted as a fund and quarantined as an equity.

    Both answers on the report, because they are different questions: it says
    whether the gate is doing work, or whether the payload never needed it.
    """
    fmp = _FMP(_nav_bars(NAV_BEFORE - DISTRIBUTION), _distributions())
    report = probe.probe_fund_nav(fmp, "VFIAX")
    assert report["loader_accepts_as_fund"] is True
    assert report["loader_accepts_as_equity"] is False
    assert report["quarantine_reason"] == "missing_or_nonnumeric_ohlc"
    assert report["ohlc_spelling"] == "close only"


def test_format_fund_report_renders_every_verdict_without_blowing_up():
    """Including the ones whose numeric fields are None -- this runs in a CLI."""
    cases = [
        _FMP(_nav_bars(NAV_BEFORE - DISTRIBUTION), _distributions()),
        _FMP(_nav_bars(NAV_BEFORE), _distributions()),
        _FMP([], _distributions()),
        _FMP(_nav_bars(400.0), []),
    ]
    for fmp in cases:
        text = probe.format_fund_report(probe.probe_fund_nav(fmp, "VFIAX"))
        assert "FMP fund NAV probe: VFIAX" in text
        assert "None" not in text.split("\n")[-1], "the detail line must read as prose"
