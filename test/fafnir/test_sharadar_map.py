"""
Sharadar boundary mappings (plan SA-0307, #56): pure functions, no database.

Values are synthetic except where a public fact is the point (AAPL's 4-for-1 of
2020-08-31 and its $0.77 dividend of 2019-11-07, as #49 measured them).
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest

from fafnir.ingest import sharadar_map as sm

D = Decimal


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("NYSE", "NYSE"),
        ("NASDAQ", "NASDAQ"),
        ("NYSEMKT", "AMEX"),
        ("NYSEARCA", "AMEX"),
        ("BATS", "BATS"),
        ("TXSE", "TXSE"),
        ("OTC", "OTC"),
        (" nyse ", "NYSE"),
    ],
)
def test_every_measured_exchange_maps(code, expected):
    assert sm.map_exchange(code) == expected


def test_an_index_is_no_venue_and_an_unknown_code_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert sm.map_exchange("INDEX") is None
        assert sm.map_exchange(None) is None
        assert sm.map_exchange("") is None
    assert caplog.text == ""
    with caplog.at_level(logging.WARNING):
        assert sm.map_exchange("LSE") is None
    assert "'LSE' has no ref.exchange mapping" in caplog.text


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category, expected",
    [
        ("Domestic Common Stock", sm.EQUITY),
        ("Domestic Common Stock Primary Class", sm.EQUITY),
        ("Domestic Common Stock Secondary Class", sm.EQUITY),
        ("Domestic Preferred Stock", sm.EQUITY),
        ("ADR Common Stock", sm.EQUITY),
        ("ADR Common Stock Primary Class", sm.EQUITY),
        ("ADR Preferred Stock", sm.EQUITY),
        ("Canadian Common Stock", sm.EQUITY),
        ("Canadian Preferred Stock", sm.EQUITY),
        ("ETF", sm.EXCHANGE_TRADED),
        ("ETN", sm.EXCHANGE_TRADED),
        ("ETD", sm.EXCHANGE_TRADED),
        ("ETMF", sm.EXCHANGE_TRADED),
        ("CEF", sm.CLOSED_END),
        ("CEF Preferred", sm.CLOSED_END),
        ("MF", sm.NAV_FUND),
    ],
)
def test_every_measured_category_maps(category, expected):
    assert sm.map_category(category) == expected


def test_the_asset_classes_say_what_fafnir_means():
    assert sm.CLOSED_END == ("equity", False, True)  # as FMP stores its funds
    assert sm.NAV_FUND.asset_type == "fund"
    assert sm.EXCHANGE_TRADED == ("etf", True, False)


def test_out_of_scope_is_none_quietly_and_unknown_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert sm.map_category("UNIT") is None  # ADR 0012
        assert sm.map_category("IDX") is None
        assert sm.map_category(None) is None
    assert caplog.text == ""
    with caplog.at_level(logging.WARNING):
        assert sm.map_category("Warrant") is None
    assert "'Warrant' has no asset-type mapping" in caplog.text


# ---------------------------------------------------------------------------
# Sector and industry
# ---------------------------------------------------------------------------

SECTORS = ["Technology", "Financial Services", "Consumer Cyclical"]
INDUSTRIES = [
    "Consumer Electronics",
    "Drug Manufacturers - General",
    "Furnishings, Fixtures & Appliances",
    "Banks - Regional",
    "Medical - Devices",
    "Oil & Gas Exploration & Production",
    "Airlines, Airports & Air Services",
    "Regulated Electric",
]


def _cls(sector, industry):
    return sm.map_classification(
        sector, industry, sectors=SECTORS, industries=INDUSTRIES
    )


def test_names_already_in_the_vocabulary_map_to_its_spelling():
    assert _cls("Technology", "Consumer Electronics") == (
        "Technology",
        "Consumer Electronics",
    )
    assert _cls("financial services", "BANKS -  REGIONAL") == (
        "Financial Services",
        "Banks - Regional",
    )


def test_legacy_industry_names_map_through_the_reviewed_aliases():
    assert _cls(None, "Drug Manufacturers - Major")[1] == "Drug Manufacturers - General"
    assert _cls(None, "Furnishings")[1] == "Furnishings, Fixtures & Appliances"
    # A comma is not a difference.
    assert _cls(None, "Furnishings Fixtures & Appliances")[1] == (
        "Furnishings, Fixtures & Appliances"
    )


def test_sharadars_morningstar_names_map_onto_fmps():
    # Sharadar's TICKERS spells 69 industries otherwise than FMP's vocabulary
    # (2026-10-28); two of its names can share one of FMP's.
    assert _cls(None, "Medical Devices")[1] == "Medical - Devices"
    assert _cls(None, "Oil & Gas E&P")[1] == "Oil & Gas Exploration & Production"
    assert _cls(None, "Airlines")[1] == "Airlines, Airports & Air Services"
    assert _cls(None, "Airports & Air Services")[1] == (
        "Airlines, Airports & Air Services"
    )
    assert _cls(None, "Utilities - Regulated Electric")[1] == "Regulated Electric"


def test_the_aliases_are_normalized_names():
    """Keys and targets as _norm leaves them, so the view's list can be compared
    with this one as text; no alias points at another alias."""
    for alias, target in sm.INDUSTRY_ALIASES.items():
        assert alias == sm._norm(alias) and target == sm._norm(target), alias
        assert alias != target and target not in sm.INDUSTRY_ALIASES, alias


def test_anything_else_is_none_never_a_new_row():
    assert _cls("Crypto", "Mining Rigs") == (None, None)
    assert _cls("", "") == (None, None)
    assert _cls(None, None) == (None, None)


# ---------------------------------------------------------------------------
# Splits and dividends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (4, (4, 1)),  # AAPL 2020-08-31
        (0.25, (1, 4)),  # a 1-for-4 reverse split
        (1.5, (3, 2)),
        ("0.3333", (1, 3)),
        (0.1, (1, 10)),  # no float noise: Fraction(0.1) alone is 3602879701896397/...
        (112, (112, 1)),
        # Deep reverse splits: snapped to a denominator of 1,000 these were 0/1 (the
        # whole ACTIONS load refused, 2026-10-21) and 1/1000 (a wrong split, silently).
        ("0.0002", (1, 5000)),
        (D("0.0002"), (1, 5000)),  # as the file reader types it
        ("2E-4", (1, 5000)),
        ("0.0008", (1, 1250)),
        ("0.000666667", (1, 1500)),  # rounded, and still read as the round ratio
        ("0.00005", (1, 20000)),
    ],
)
def test_split_ratio(value, expected):
    assert sm.split_ratio(value) == expected


@pytest.mark.parametrize("value", [0, -2, "0"])
def test_a_non_positive_split_is_refused(value):
    with pytest.raises(ValueError, match="positive"):
        sm.split_ratio(value)


AAPL_SPLITS = [
    (date(2000, 6, 21), 2),
    (date(2005, 2, 28), 2),
    (date(2014, 6, 9), 7),
    (date(2020, 8, 31), 4),
]


def test_split_product_after():
    assert sm.split_product_after(AAPL_SPLITS, date(1999, 1, 4)) == 112
    assert sm.split_product_after(AAPL_SPLITS, date(2019, 11, 7)) == 4
    assert sm.split_product_after(AAPL_SPLITS, date(2020, 8, 31)) == 1  # ex-date
    assert sm.split_product_after([], date(2020, 1, 2)) == 1


def test_a_dividend_as_declared():
    product = sm.split_product_after(AAPL_SPLITS, date(2019, 11, 7))
    assert sm.as_declared_dividend(0.1925, product) == D("0.770000")
    assert sm.as_declared_dividend("0.5", 1) == D("0.500000")
    with pytest.raises(ValueError, match="positive"):
        sm.as_declared_dividend(0, 4)


# ---------------------------------------------------------------------------
# Raw prices
# ---------------------------------------------------------------------------


def _adjust(raw: dict, factor) -> dict:
    """What SEP holds for a raw bar with ``factor`` of later splits: prices
    divided and volume multiplied by it, ``closeunadj`` the raw close. Unrounded,
    so the round trip measures recover_raw, not the vendor's 3 decimals."""
    f = D(str(factor))
    return {
        "ticker": "ZZTEST",
        "date": "2001-01-02",
        "open": raw["open"] / f,
        "high": raw["high"] / f,
        "low": raw["low"] / f,
        "close": raw["close"] / f,
        "volume": D(raw["volume"]) * f,
        "closeadj": raw["close"] / f,
        "closeunadj": raw["close"],
    }


RAW = {
    "open": D("39.125000"),
    "high": D("40.500000"),
    "low": D("38.875000"),
    "close": D("39.200000"),
    "volume": 1_234_500,
}


@pytest.mark.parametrize("factor", [1, 2, 7, 112, D("0.05")])  # 1/20: a reverse split
@pytest.mark.parametrize("with_factor", [False, True])
def test_recover_raw_round_trips_at_6dp(factor, with_factor):
    bar = _adjust(RAW, factor)
    out = sm.recover_raw(bar, factor=factor if with_factor else None)
    assert {k: out[k] for k in RAW} == RAW
    assert (out["ticker"], out["date"], out["closeadj"]) == (
        "ZZTEST",
        "2001-01-02",
        bar["closeadj"],
    )


def test_recover_raw_uses_the_given_factor_over_the_rounded_ratio():
    # SEP as delivered for AAPL 1998-01-09 (#49): close 0.163 at 3 dp, closeunadj
    # 18.19, 112 of later splits. The ratio is 111.6 -- the close's rounding.
    bar = {
        "open": 0.16,
        "high": 0.165,
        "low": 0.159,
        "close": 0.163,
        "closeunadj": 18.19,
        "volume": 6_160_000,
    }
    by_ratio = sm.recover_raw(bar)
    by_factor = sm.recover_raw(bar, factor=112)
    assert by_ratio["close"] == by_factor["close"] == D("18.190000")
    assert by_factor["open"] == D("17.920000")  # 0.16 x 112
    assert by_ratio["open"] != by_factor["open"]
    assert by_factor["volume"] == 55_000


def test_recover_raw_rounds_half_up_and_keeps_nulls():
    bar = {
        "open": None,
        "high": None,
        "low": None,
        "close": 1,
        "closeunadj": 3,
        "volume": None,
    }
    out = sm.recover_raw(bar)
    assert (out["open"], out["volume"], out["close"]) == (None, None, D("3.000000"))
    half = sm.recover_raw({"close": 2, "closeunadj": 1, "volume": 1, "open": 1})
    assert half["volume"] == 2  # 1 x 2/1 -> 2; the volume divides by the ratio 0.5
    up = sm.recover_raw({"close": 1, "closeunadj": 4, "volume": 2})
    assert up["volume"] == 1  # 2 / 4 = 0.5 -> 1 (half up, never truncated to 0)


@pytest.mark.parametrize("close, unadj", [(0, 1), (1, 0), (-1, 1)])
def test_recover_raw_refuses_a_non_positive_close(close, unadj):
    with pytest.raises(ValueError, match="close must be positive"):
        sm.recover_raw(
            {"ticker": "ZZ", "date": "2001-01-02", "close": close, "closeunadj": unadj}
        )
