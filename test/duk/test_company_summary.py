"""Unit tests for the company-summary assembly and rendering.

No database: `duk.company_summary` is pure functions over dicts, which is what
makes the whole report testable here rather than only where FAFNIR_TEST_DSN is
set. The integration half lives in test_db_company_summary.py.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from duk import company_summary as cs


def _closes(values, end=dt.date(2026, 8, 31)):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(values))
    return pd.DataFrame({"close": values}, index=idx)


def _raw(**overrides):
    raw = {
        "profile": {
            "security_id": 1,
            "symbol": "AAPL",
            "company_name": "Apple Inc.",
            "exchange_code": "NASDAQ",
            "exchange_name": "Nasdaq",
            "sector_name": "Technology",
            "industry_name": "Consumer Electronics",
            "currency": "USD",
            "country": "US",
            "cik": "0000320193",
            "is_actively_trading": True,
            "market_cap_usd": Decimal("3420000000000.00"),
            "beta": Decimal("1.24"),
            "ipo_date": dt.date(1980, 12, 12),
            "updated_at": dt.datetime(2026, 8, 29, 3, 0, 0),
        },
        "coverage": {
            "first_trade_date": dt.date(2019, 1, 2),
            "last_trade_date": dt.date(2026, 8, 31),
            "bar_count": 1999,
            "distinct_years": 8,
            "zero_volume_bars": 1,
        },
        "actions": {
            "split_count": 1,
            "last_split_date": dt.date(2020, 8, 31),
            "last_split_numerator": Decimal("4"),
            "last_split_denominator": Decimal("1"),
            "dividend_count": 4,
            "last_dividend_date": dt.date(2026, 8, 8),
            "last_dividend_amount": Decimal("0.25"),
            "ttm_dividend_amount": Decimal("0.99"),
            "adjustment_factor_rows": 5,
            "latest_factor_effective_date": dt.date(2026, 8, 8),
        },
        "last_bar": {
            "trade_date": dt.date(2026, 8, 31),
            "close": Decimal("100.00"),
            "volume": 41208300,
        },
        "dq_flags": [],
        "fundamentals": None,
        "adjusted_prices": _closes([100.0] * 300),
    }
    raw.update(overrides)
    return raw


class TestScalarFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (3_420_000_000_000, "3.42T"),
            (2_100_000_000, "2.10B"),
            (41_208_300, "41.21M"),
            (1_500, "1.50K"),
            (999, "999"),
            (-2_500_000, "-2.50M"),
            (None, "-"),
        ],
    )
    def test_fmt_big(self, value, expected):
        assert cs.fmt_big(value) == expected

    def test_missing_values_render_as_a_dash_not_blank(self):
        # A summary of a warehouse is largely a report about what is NOT held, so
        # absence has to be visible rather than an empty cell.
        assert cs.fmt_pct(None) == cs.MISSING
        assert cs.fmt_date(None) == cs.MISSING
        assert cs.fmt_int(None) == cs.MISSING
        assert cs.fmt_price(None) == cs.MISSING
        assert cs.fmt_ratio(None, 1) == cs.MISSING

    def test_sub_penny_price_keeps_significant_digits(self):
        # The rule `ph` already follows: a back-adjusted price too small for two
        # decimals must not print as 0.00, because that reports a real trade as
        # no trade.
        assert cs.fmt_price(0.0003123) != "0.00"
        assert "0.0003123" in cs.fmt_price(0.0003123)

    def test_fmt_ratio_renders_a_split(self):
        assert cs.fmt_ratio(Decimal("4"), Decimal("1")) == "4-for-1"
        assert cs.fmt_ratio(Decimal("1"), Decimal("10")) == "1-for-10"


class TestDerivedStatistics:
    def test_trailing_returns_against_a_known_series(self):
        # 100 -> 110 over the last year, flat before: +10% at 1Y.
        idx = pd.bdate_range(end=pd.Timestamp("2026-08-31"), periods=400)
        values = [100.0] * 399 + [110.0]
        closes = pd.Series(values, index=idx)
        out = cs.trailing_returns(closes, dt.date(2026, 8, 31))
        assert out["1Y"] == pytest.approx(0.10)
        assert out["1M"] == pytest.approx(0.10)

    def test_window_shorter_than_the_period_returns_none_not_a_wrong_number(self):
        # Eight months of history has no 1-year return. Inventing one from the
        # first available price would report a different measurement under the
        # same label.
        idx = pd.bdate_range(end=pd.Timestamp("2026-08-31"), periods=40)
        closes = pd.Series([100.0] * 40, index=idx)
        assert cs.trailing_returns(closes, dt.date(2026, 8, 31))["1Y"] is None

    def test_max_drawdown_is_peak_to_trough(self):
        closes = pd.Series([100.0, 120.0, 60.0, 90.0])
        assert cs.max_drawdown(closes) == pytest.approx(-0.5)

    def test_flat_series_has_zero_volatility_and_no_drawdown(self):
        closes = pd.Series([100.0] * 50)
        assert cs.annualised_volatility(closes) == pytest.approx(0.0)
        assert cs.max_drawdown(closes) == pytest.approx(0.0)

    def test_volatility_needs_at_least_two_returns(self):
        assert cs.annualised_volatility(pd.Series([100.0])) is None
        assert cs.annualised_volatility(pd.Series(dtype=float)) is None


class TestBuildSummary:
    def test_json_key_contract(self):
        # These keys are a public surface -- scripts branch on them.
        report = cs.build_summary(_raw())
        assert set(report) == {"meta", "prices", "actions", "fundamentals", "dq"}

    def test_decimals_become_floats_so_the_report_is_json_encodable(self):
        import json

        report = cs.build_summary(_raw())
        assert isinstance(report["meta"]["market_cap_usd"], float)
        assert isinstance(report["actions"]["ttm_dividend_amount"], float)
        json.dumps(report)  # must not raise

    def test_trailing_yield_is_ttm_over_last_close(self):
        report = cs.build_summary(_raw())
        assert report["actions"]["trailing_dividend_yield"] == pytest.approx(0.0099)

    def test_yield_is_none_rather_than_infinite_without_a_close(self):
        report = cs.build_summary(_raw(last_bar=None, adjusted_prices=pd.DataFrame()))
        assert report["actions"]["trailing_dividend_yield"] is None

    def test_a_security_with_no_prices_still_produces_a_report(self):
        report = cs.build_summary(
            _raw(coverage=None, last_bar=None, adjusted_prices=pd.DataFrame())
        )
        assert report["meta"]["symbol"] == "AAPL"
        assert report["prices"]["bar_count"] is None
        assert "No price history loaded" in cs.render_text(report)

    def test_dq_flags_group_by_check_and_keep_their_keys(self):
        flags = [
            {
                "check_name": "gap",
                "severity": "warn",
                "record_key": {"trade_date": "2026-07-14"},
                "detected_at": dt.datetime(2026, 7, 14),
            },
            {
                "check_name": "gap",
                "severity": "warn",
                "record_key": {"trade_date": "2026-08-02"},
                "detected_at": dt.datetime(2026, 8, 2),
            },
        ]
        report = cs.build_summary(_raw(dq_flags=flags))
        assert len(report["dq"]) == 1
        group = report["dq"][0]
        assert group["flags"] == 2
        assert group["first_detected"] == "2026-07-14"
        assert group["last_detected"] == "2026-08-02"
        # record_key is on the seam precisely so the report can name the bar.
        assert group["keys"] == ["2026-07-14", "2026-08-02"]


class TestFundamentals:
    def test_absent_fundamentals_render_the_planned_milestone_line(self):
        # Not an empty section: "not loaded" and "nothing to report" are
        # different claims about a warehouse.
        report = cs.build_summary(_raw())
        assert report["fundamentals"] is None
        text = cs.render_text(report)
        assert "FUNDAMENTALS" in text
        assert "Not loaded" in text

    def test_ratios_are_derived_from_the_statements_beside_them(self):
        ratios = cs.fundamental_ratios(
            {
                "period": "annual",
                "revenue": 400.0,
                "net_income": 100.0,
                "total_equity": 200.0,
            },
            market_cap=1000.0,
        )
        assert ratios["price_earnings"] == pytest.approx(10.0)
        assert ratios["price_sales"] == pytest.approx(2.5)
        assert ratios["price_book"] == pytest.approx(5.0)
        assert ratios["net_margin"] == pytest.approx(0.25)
        assert ratios["return_on_equity"] == pytest.approx(0.5)
        assert ratios["annualised"] is False

    def test_a_quarter_is_annualised_and_says_so(self):
        # Without the x4 a quarterly P/E overstates by roughly four; without the
        # flag, an annualised quarter would be mistaken for a filed TTM figure.
        ratios = cs.fundamental_ratios(
            {"period": "quarter", "net_income": 25.0}, market_cap=1000.0
        )
        assert ratios["price_earnings"] == pytest.approx(10.0)
        assert ratios["annualised"] is True

    def test_missing_or_zero_inputs_yield_none_not_zero(self):
        ratios = cs.fundamental_ratios(
            {"period": "annual", "revenue": 0.0, "total_equity": None},
            market_cap=1000.0,
        )
        assert ratios["price_sales"] is None
        assert ratios["price_book"] is None
        assert ratios["price_earnings"] is None

    def test_no_fundamentals_means_no_ratios(self):
        assert cs.fundamental_ratios(None, 1000.0) == {}


class TestRendering:
    def test_every_section_is_present_even_when_empty(self):
        text = cs.render_text(cs.build_summary(_raw()))
        for heading in (
            "PRICE HISTORY",
            "CORPORATE ACTIONS",
            "FUNDAMENTALS",
            "DATA QUALITY",
        ):
            assert heading in text

    def test_clean_queue_says_so_rather_than_omitting_the_section(self):
        text = cs.render_text(cs.build_summary(_raw()))
        assert "No open DQ flags." in text

    def test_price_star_flags_carry_the_repeat_caveat(self):
        flags = [
            {
                "check_name": "price_scale_collapse",
                "severity": "error",
                "record_key": {"trade_date": "2026-08-15"},
                "detected_at": dt.datetime(2026, 8, 15),
            }
        ]
        text = cs.render_text(cs.build_summary(_raw(dq_flags=flags)))
        # The same caveat `fafnir dq list` carries: without it the count reads as
        # a count of distinct problems, which for price_* it is not.
        assert "repeat per re-detection" in text

    def test_a_former_ticker_match_is_announced(self):
        profile = dict(_raw()["profile"])
        profile["matched_former_symbol"] = "APPL"
        profile["matched_former_valid_to"] = dt.date(2001, 5, 5)
        text = cs.render_text(cs.build_summary(_raw(profile=profile)))
        assert "Matched a former ticker" in text
        assert "APPL" in text

    def test_no_line_runs_off_a_narrow_terminal(self, monkeypatch):
        monkeypatch.setattr(cs, "_terminal_width", lambda default=100: 80)
        flags = [
            {
                "check_name": "price_scale_collapse",
                "severity": "error",
                "record_key": {"trade_date": "2026-08-15"},
                "detected_at": dt.datetime(2026, 8, 15),
            }
        ]
        text = cs.render_text(cs.build_summary(_raw(dq_flags=flags)))
        assert not [ln for ln in text.splitlines() if len(ln) > 88], text

    def test_flat_row_is_scalars_only_for_csv(self):
        flat = cs.render_flat(cs.build_summary(_raw()))
        assert all(
            v is None or isinstance(v, (str, int, float, bool)) for v in flat.values()
        ), flat
        assert flat["symbol"] == "AAPL"
        assert flat["open_dq_flags"] == 0

    def test_candidate_table_lists_every_match(self):
        text = cs.render_candidates(
            [
                {
                    "symbol": "APLD",
                    "company_name": "Applied Digital Corp",
                    "exchange_code": "NASDAQ",
                    "is_actively_trading": True,
                },
                {
                    "symbol": "MSFT",
                    "company_name": "Microsoft Corp",
                    "exchange_code": "NASDAQ",
                    "is_actively_trading": False,
                },
            ]
        )
        assert "APLD" in text and "MSFT" in text
        assert "active" in text and "delisted" in text
