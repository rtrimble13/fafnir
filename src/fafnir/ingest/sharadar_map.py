"""
Boundary mappings from Sharadar's values to fafnir's (plan SA-0307, #56).

Pure functions every Sharadar loader needs, tested once; no database access. Each
follows what the paid feed showed, not what a document says:

* the probe's verdicts (#49): a split's ``value`` is new shares per old, a
  dividend's is **split-adjusted**, prices carry 3 decimal places and volume is
  split-adjusted;
* the first full TICKERS file (#52, 2026-10-19): the exchange codes, categories,
  sectors and industries it uses, recorded in the tables below.

An unknown exchange or category maps to ``None`` with a warning, and an unknown
sector or industry maps to ``None`` silently: the caller decides what an unmapped
security means, and the ``missing_classification`` DQ check flags a security left
without one. Nothing here invents a vocabulary row.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Iterable, NamedTuple, Optional

from fafnir.logging_config import get_logger

logger = get_logger("ingest.sharadar_map")

# NUMERIC(20, 6) and BIGINT, as daily_price._as_money and the volume column.
MONEY_SCALE = Decimal("0.000001")

# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------

#: Sharadar exchange code -> ref.exchange code. Measured on TICKERS (stocks and
#: funds, 2026-10-19): NASDAQ, NYSE, NYSEARCA, BATS, NYSEMKT, TXSE, OTC, INDEX.
#: NYSEARCA maps to AMEX because FMP files NYSE Arca listings under AMEX (its
#: screener returns SPY as exchangeShortName AMEX), and a security keeps one venue
#: whichever vendor described it. Confirm on the host before the first load:
#:   SELECT exchange_code, count(*) FROM core.security WHERE is_etf GROUP BY 1;
EXCHANGES = {
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
    "NYSEMKT": "AMEX",
    "NYSEARCA": "AMEX",
    "BATS": "BATS",
    "TXSE": "TXSE",
    "OTC": "OTC",
}
#: Codes that are not venues a security lists on: no exchange, no warning.
NOT_A_VENUE = frozenset({"INDEX"})


def map_exchange(code: Optional[str]) -> Optional[str]:
    """The ``ref.exchange`` code for a Sharadar exchange, or None."""
    key = (code or "").strip().upper()
    if key in EXCHANGES:
        return EXCHANGES[key]
    if key and key not in NOT_A_VENUE:
        logger.warning("Sharadar exchange %r has no ref.exchange mapping", code)
    return None


# ---------------------------------------------------------------------------
# Category -> asset type
# ---------------------------------------------------------------------------


class AssetClass(NamedTuple):
    asset_type: str  # core.security.asset_type: equity | etf | fund | other
    is_etf: bool
    is_fund: bool


EQUITY = AssetClass("equity", False, False)
EXCHANGE_TRADED = AssetClass("etf", True, False)
#: A closed-end fund is stored as FMP stores its funds in production: an equity
#: flagged is_fund. fafnir's 'fund' asset type is the declared NAV-priced mutual fund
#: (ADR 0006). Note that daily_price._is_nav_priced reads equity + is_fund as
#: NAV-priced, so a CEF bar with only a close is accepted, as FMP's are today (#75).
CLOSED_END = AssetClass("equity", False, True)
NAV_FUND = AssetClass("fund", False, True)

#: TICKERS categories (2026-10-19). Every common, preferred and ADR class of stock
#: is an equity; every exchange-traded product (ETF, ETN, ETD -- commodity pools
#: and the like -- and ETMF) trades like an ETF; CEFs as above; MF is a NAV-priced
#: mutual fund. UNIT is out of scope (ADR 0012) and IDX is an index, not a
#: security: both map to None, deliberately, without a warning.
CATEGORIES = {
    "ETF": EXCHANGE_TRADED,
    "ETN": EXCHANGE_TRADED,
    "ETD": EXCHANGE_TRADED,
    "ETMF": EXCHANGE_TRADED,
    "CEF": CLOSED_END,
    "CEF PREFERRED": CLOSED_END,
    "MF": NAV_FUND,
}
OUT_OF_SCOPE_CATEGORIES = frozenset({"UNIT", "IDX"})
_STOCK = re.compile(
    r"^(DOMESTIC|ADR|CANADIAN) (COMMON|PREFERRED) STOCK( (PRIMARY|SECONDARY) CLASS)?$"
)


def map_category(category: Optional[str]) -> Optional[AssetClass]:
    """``(asset_type, is_etf, is_fund)`` for a TICKERS category, or None."""
    key = " ".join((category or "").upper().split())
    if _STOCK.match(key):
        return EQUITY
    if key in CATEGORIES:
        return CATEGORIES[key]
    if key and key not in OUT_OF_SCOPE_CATEGORIES:
        logger.warning("Sharadar category %r has no asset-type mapping", category)
    return None


# ---------------------------------------------------------------------------
# Sector and industry
# ---------------------------------------------------------------------------

#: Sharadar's industry names (Morningstar's) and the same industry in FMP's
#: vocabulary (ref.industry), both normalized. Every name in Sharadar's TICKERS
#: (2026-10-28) that FMP's vocabulary does not spell the same way is here; each target
#: is the industry FMP gives most of the securities both vendors classify, by name
#: where those are too few to say (Computer Systems, Infrastructure Operations,
#: Industrial Metals & Minerals). Reviewed by hand; extend it only the same way, and
#: in the classification view's list (migration 0040), which a test holds equal.
#: Sharadar's sectors are FMP's eleven, verbatim, so sectors need none.
INDUSTRY_ALIASES = {
    "airlines": "airlines airports & air services",
    "airports & air services": "airlines airports & air services",
    "apparel manufacturing": "apparel - manufacturers",
    "apparel retail": "apparel - retail",
    "auto & truck dealerships": "auto - dealerships",
    "auto manufacturers": "auto - manufacturers",
    "auto parts": "auto - parts",
    "beverages - brewers": "beverages - alcoholic",
    "building materials": "construction materials",
    "building products & equipment": "construction materials",
    "capital markets": "financial - capital markets",
    "coking coal": "coal",
    "computer systems": "computer hardware",
    "confectioners": "food confectioners",
    "credit services": "financial - credit services",
    "diagnostics & research": "medical - diagnostics & research",
    "diversified industrials": "industrial - machinery",
    "drug manufacturers - major": "drug manufacturers - general",
    "electronic components": "hardware equipment & parts",
    "electronics & computer distribution": "technology distributors",
    "farm & heavy construction machinery": "agricultural - machinery",
    "farm products": "agricultural farm products",
    "financial conglomerates": "financial - conglomerates",
    "financial data & stock exchanges": "financial - data & stock exchanges",
    "footwear & accessories": "apparel - footwear & accessories",
    "furnishings": "furnishings fixtures & appliances",
    "gambling": "gambling resorts & casinos",
    "health information services": "medical - healthcare information services",
    "healthcare plans": "medical - healthcare plans",
    "home improvement retail": "home improvement",
    "industrial distribution": "industrial - distribution",
    "industrial metals & minerals": "industrial materials",
    "infrastructure operations": "industrial - infrastructure operations",
    "insurance brokers": "insurance - brokers",
    "internet retail": "specialty retail",
    "lodging": "travel lodging",
    "lumber & wood production": "paper lumber & forest products",
    "medical care facilities": "medical - care facilities",
    "medical devices": "medical - devices",
    "medical distribution": "medical - distribution",
    "medical instruments & supplies": "medical - instruments & supplies",
    "metal fabrication": "manufacturing - metal fabrication",
    "mortgage finance": "financial - mortgages",
    "oil & gas e&p": "oil & gas exploration & production",
    "other industrial metals & mining": "industrial materials",
    "other precious metals & mining": "other precious metals",
    "paper & paper products": "paper lumber & forest products",
    "personal services": "personal products & services",
    "pharmaceutical retailers": "medical - pharmaceuticals",
    "pollution & treatment controls": "industrial - pollution & treatment controls",
    "real estate services": "real estate - services",
    "recreational vehicles": "auto - recreational vehicles",
    "resorts & casinos": "gambling resorts & casinos",
    "savings & cooperative banks": "banks - regional",
    "scientific & technical instruments": "hardware equipment & parts",
    "semiconductor equipment & materials": "semiconductors",
    "shipping & ports": "marine shipping",
    "specialty chemicals": "chemicals - specialty",
    "specialty industrial machinery": "industrial - machinery",
    "telecom services": "telecommunications services",
    "textile manufacturing": "manufacturing - textiles",
    "thermal coal": "coal",
    "tools & accessories": "manufacturing - tools & accessories",
    "utilities - diversified": "diversified utilities",
    "utilities - independent power producers": "independent power producers",
    "utilities - regulated electric": "regulated electric",
    "utilities - regulated gas": "regulated gas",
    "utilities - regulated water": "regulated water",
    "utilities - renewable": "renewable utilities",
}


def _norm(name: str) -> str:
    """Case, commas and spacing do not distinguish two names:
    'Furnishings, Fixtures & Appliances' == 'Furnishings Fixtures & Appliances'."""
    return " ".join(name.replace(",", " ").lower().split())


def map_classification(
    sector: Optional[str],
    industry: Optional[str],
    *,
    sectors: Iterable[str],
    industries: Iterable[str],
) -> tuple[Optional[str], Optional[str]]:
    """Sharadar's sector and industry as names already in ``ref.sector`` /
    ``ref.industry`` (passed in), each None when it is not there."""
    by_norm_sector = {_norm(s): s for s in sectors}
    by_norm_industry = {_norm(i): i for i in industries}
    out_sector = by_norm_sector.get(_norm(sector)) if sector else None
    out_industry = None
    if industry:
        key = _norm(industry)
        out_industry = by_norm_industry.get(INDUSTRY_ALIASES.get(key, key))
    return out_sector, out_industry


# ---------------------------------------------------------------------------
# Splits and dividends
# ---------------------------------------------------------------------------


def split_ratio(value) -> tuple[int, int]:
    """``(numerator, denominator)`` of a split from its ACTIONS ``value``.

    The value is new shares per old (#49, item 4): a 4-for-1 is 4 -> (4, 1), a
    1-for-4 is 0.25 -> (1, 4), and a 3-for-2 is 1.5 -> (3, 2). Read from the text so
    a float's binary noise cannot leak into the fraction.

    A rounded value is snapped to the nearest fraction with a denominator up to
    1,000 (0.3333 -> 1/3), but only when that fraction is within half a unit of the
    text's last decimal. A deep reverse split is not snapped: the nearest such
    fraction to 0.0002 (a 1-for-5,000) is 0/1, which refused the whole ACTIONS load
    on 2026-10-21, and to 0.0008 (a 1-for-1,250) it is 1/1000, a wrong split stored
    silently. Those are read as written, to a denominator of at most 1,000,000.
    """
    text = Decimal(str(value))
    exact = Fraction(text)
    if exact <= 0:
        raise ValueError(f"a split ratio must be positive, not {value!r}")
    snapped = exact.limit_denominator(1000)
    half_unit = Fraction(1, 2) * Fraction(10) ** text.as_tuple().exponent
    ratio = (
        snapped
        if abs(snapped - exact) <= half_unit
        else exact.limit_denominator(1_000_000)
    )
    return ratio.numerator, ratio.denominator


def split_product_after(splits: Iterable[tuple], day) -> Decimal:
    """The product of the split values whose ex-date is strictly after ``day``:
    the factor a bar or dividend on ``day`` was divided by. A split's own ex-date
    already trades post-split."""
    product = Decimal(1)
    for ex_date, value in splits:
        if ex_date > day:
            product *= Decimal(str(value))
    return product


def as_declared_dividend(value, split_product) -> Decimal:
    """The dividend as declared, from ACTIONS' split-adjusted ``value`` (#49, item 5).

    ``split_product`` is :func:`split_product_after` the ex-date, from the same
    ACTIONS splits: AAPL's 2019-11-07 value 0.1925 x 4 = 0.77. That is exact where
    ``closeunadj / close`` would carry the close's 3-dp rounding. Quantized to
    NUMERIC(20, 6).
    """
    amount = Decimal(str(value)) * Decimal(str(split_product))
    if amount <= 0:
        raise ValueError(f"a dividend must be positive, not {value!r}")
    return amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Raw prices
# ---------------------------------------------------------------------------


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def recover_raw(bar: dict, *, factor=None) -> dict:
    """The raw bar from a SEP/SFP row: ``open/high/low/close/volume`` as traded.

    The close is ``closeunadj`` itself. The rest follow Sharadar's documented
    identities, ``OpenUnadj = Open x CloseUnadj / Close`` and ``VolumeUnadj = Volume
    x Close / CloseUnadj``, with the ratio ``closeunadj / close`` as the factor --
    unless ``factor`` is given: the cumulative split product after the bar's date,
    from ACTIONS (:func:`split_product_after`). That is exact, where the ratio
    carries the close's 3-dp rounding (up to 0.36% on an old low-priced bar, #49);
    #75 decides which a loader passes. Decimal throughout; prices quantized to
    NUMERIC(20, 6) and volume to a whole share, both ROUND_HALF_UP.
    """
    close = Decimal(str(bar["close"]))
    unadj = Decimal(str(bar["closeunadj"]))
    if close <= 0 or unadj <= 0:
        raise ValueError(
            f"{bar.get('ticker')} {bar.get('date')}: close must be positive"
        )
    f = Decimal(str(factor)) if factor is not None else unadj / close
    out = {
        k: v
        for k, v in bar.items()
        if k not in ("open", "high", "low", "close", "volume")
    }
    for field in ("open", "high", "low"):
        value = bar.get(field)
        out[field] = None if value is None else _money(Decimal(str(value)) * f)
    out["close"] = _money(unadj)
    volume = bar.get("volume")
    out["volume"] = (
        None
        if volume is None
        else int(
            (Decimal(str(volume)) / f).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
    )
    return out
