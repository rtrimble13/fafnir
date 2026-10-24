"""
`fafnir source probe-fundamentals --vendor sharadar`: measure SF1 before the
fundamentals schema is written (plan SA-0705, #83).

The fundamentals build prompt (``doc/plans/fundamentals-build-prompt-fmp.md``) was
written for FMP. Its knowledge-date model, restatement versioning, TTM rules and
per-share rules rest on facts about the vendor, and SF1's are not FMP's. This probe
asks SF1 each of them, for a fixed probe set with long histories (``AAPL, MSFT, GE,
KO, JPM, XOM``: two fiscal calendars, one bank) plus random dead issuers and
issuers that report outside USD,
and gives each a verdict, as :mod:`fafnir.sources.sharadar_probe` does for prices:

* **PASS** -- the fact is as the design assumes;
* **NOTE** -- a fact the design must accommodate, with the consequence stated;
* **BLOCK** -- the fact breaks the knowledge-date model (a filing date before its
  period end, filing dates that look imputed, one that disagrees with the public
  record). The command exits non-zero.

The items (#83's numbering, plus share terms, which the first measurement forced):

1. Coverage -- dimensions, row counts, earliest period, rows before the floor.
2. Knowledge dates -- ``date`` against ``reportperiod`` on AR and MR rows, filing
   lags by era, ``lastupdated``, and filings to compare with EDGAR.
3. Restatements -- AR (first reported) against MR (restated), MSFT FY2015 first.
4. Indicator inventory -- SF1's columns against the build prompt's §4.3 list.
5. Quarterly completeness -- Q4 rows, and quarters that sum to the year.
6. Null against zero -- GE's R&D, interest and D&A for 1998-2005 first.
7. Currency -- where the reporting currency is, and what ``fxusd`` converts.
8. Identity -- tickers to permatickers, share classes, reused tickers.
9. Vendor metrics -- the columns the loader routes to ``extra``.
10. Share terms -- as reported, or re-expressed at each later split (§4.7).

Read-only. It reads the ledger's current SF1 and ACTIONS files under ``[sharadar]
bulk_dir`` and the vendor master (``core.vendor_security``), so it lives here, not
in :mod:`fafnir.sources` (ADR 0017 §1). ``--from-api`` reads the same rows from the
API instead, one ticker per request, for a session with no ledger. Its ``--json``
report holds vendor rows as evidence: keep it on the host and post verdicts and
aggregates only (plan, working agreement 5).
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from fafnir.ingest import sharadar_ledger as ledger
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf
from fafnir.sources.sharadar_probe import BLOCK, NOTE, PASS, Item, blocking, decimals

logger = get_logger("ingest.sf1_probe")

__all__ = [
    "PROBE_SET",
    "ProbeError",
    "Sf1Data",
    "blocking",
    "draw",
    "format_fundamentals_report",
    "from_api",
    "from_ledger",
    "probe_fundamentals",
]

#: Long histories, two fiscal calendars (AAPL's September, MSFT's June) and a bank.
PROBE_SET = ("AAPL", "MSFT", "GE", "KO", "JPM", "XOM")
DEAD_SAMPLE = 20
#: Item 7 asks about reporting currencies (ADRs among them), and the probe set
#: reports in USD.
FOREIGN_SAMPLE = 3

#: The survivorship floor (ADR 0016): no vendor holds a delisting before it.
FLOOR = date(1998, 1, 1)

AR_DIMENSIONS = ("ARQ", "ARY", "ART")
MR_DIMENSIONS = ("MRQ", "MRY", "MRT")
DIMENSIONS = AR_DIMENSIONS + MR_DIMENSIONS
#: AR to its MR twin: first reported against including restatements.
TWIN = dict(zip(AR_DIMENSIONS, MR_DIMENSIONS))
TWIN_OF_MR = dict(zip(MR_DIMENSIONS, AR_DIMENSIONS))

# SF1's columns by the vendor's own classification: the ``indicators`` table, read
# 2026-10-24, where each description opens with its category.
ENTITY = (
    "ticker",
    "dimension",
    "calendardate",
    "date",
    "reportperiod",
    "fiscalperiod",
    "lastupdated",
    "price",
    "sharefactor",
    "sharesbas",
)
INCOME = (
    "consolinc",
    "cor",
    "dps",
    "ebit",
    "ebitusd",
    "eps",
    "epsdil",
    "epsusd",
    "gp",
    "intexp",
    "netinc",
    "netinccmn",
    "netinccmnusd",
    "netincdis",
    "netincnci",
    "opex",
    "opinc",
    "prefdivis",
    "revenue",
    "revenueusd",
    "rnd",
    "sgna",
    "shareswa",
    "shareswadil",
    "taxexp",
)
BALANCE = (
    "accoci",
    "assets",
    "assetsc",
    "assetsnc",
    "cashneq",
    "cashnequsd",
    "debt",
    "debtc",
    "debtnc",
    "debtusd",
    "deferredrev",
    "deposits",
    "equity",
    "equityusd",
    "intangibles",
    "inventory",
    "investments",
    "investmentsc",
    "investmentsnc",
    "liabilities",
    "liabilitiesc",
    "liabilitiesnc",
    "payables",
    "ppnenet",
    "receivables",
    "retearn",
    "taxassets",
    "taxliabilities",
)
CASH_FLOW = (
    "capex",
    "depamor",
    "ncf",
    "ncfbus",
    "ncfcommon",
    "ncfdebt",
    "ncfdiv",
    "ncff",
    "ncfi",
    "ncfinv",
    "ncfo",
    "ncfx",
    "sbcomp",
)
METRICS = (
    "assetsavg",
    "assetturnover",
    "bvps",
    "currentratio",
    "de",
    "divyield",
    "ebitda",
    "ebitdamargin",
    "ebitdausd",
    "ebt",
    "equityavg",
    "ev",
    "evebit",
    "evebitda",
    "fcf",
    "fcfps",
    "fxusd",
    "grossmargin",
    "invcap",
    "invcapavg",
    "marketcap",
    "netmargin",
    "payoutratio",
    "pb",
    "pe",
    "pe1",
    "ps",
    "ps1",
    "roa",
    "roe",
    "roic",
    "ros",
    "sps",
    "tangibles",
    "tbvps",
    "workingcapital",
)
STATEMENTS = {"income": INCOME, "balance_sheet": BALANCE, "cash_flow": CASH_FLOW}
KNOWN = set(ENTITY) | set(INCOME) | set(BALANCE) | set(CASH_FLOW) | set(METRICS)
#: What the knowledge-date model cannot do without.
KEY_COLUMNS = ("ticker", "dimension", "calendardate", "date", "reportperiod")

#: Measured on the row's ``date`` (the vendor: "the observation date used for
#: [Price] based data"), so an AR row and its MR twin differ here because their
#: dates differ, not because anything was restated. ``sharesbas`` is the count on
#: the cover of the filing.
AS_OF_DATE = (
    "price",
    "marketcap",
    "ev",
    "evebit",
    "evebitda",
    "pb",
    "pe",
    "pe1",
    "ps",
    "ps1",
    "divyield",
    "sharesbas",
)
#: Share counts and per-share values: in today's share terms (item 10), so a pair
#: that differs only here may differ by re-expression, not by a restatement.
SHARE_FIELDS = ("shareswa", "shareswadil", "eps", "epsdil", "epsusd", "dps")
#: The indicators whose description says "the value of 0 is used" when the filing
#: does not carry the item (the ``indicators`` table, 2026-10-24).
ZERO_FILLED = (
    "accoci",
    "capex",
    "cashneq",
    "cashnequsd",
    "cor",
    "debt",
    "debtc",
    "debtnc",
    "debtusd",
    "deferredrev",
    "depamor",
    "deposits",
    "divyield",
    "dps",
    "intangibles",
    "intexp",
    "inventory",
    "investments",
    "investmentsc",
    "investmentsnc",
    "ncfbus",
    "ncfcommon",
    "ncfdebt",
    "ncfdiv",
    "ncff",
    "ncfi",
    "ncfinv",
    "ncfx",
    "netincdis",
    "netincnci",
    "payables",
    "ppnenet",
    "prefdivis",
    "receivables",
    "revenue",
    "revenueusd",
    "rnd",
    "sgna",
    "taxassets",
    "taxexp",
    "taxliabilities",
)

# The build prompt's §4.3 columns, and the SF1 indicator a proposed field map
# would read for each. Input to #85, which owns the field map: only the coverage
# is a finding here.
BUILD_PROMPT_COLUMNS = {
    "income": (
        "revenue cost_of_revenue gross_profit research_and_development_expenses "
        "general_and_administrative_expenses selling_and_marketing_expenses "
        "selling_general_and_administrative_expenses other_expenses operating_expenses "
        "cost_and_expenses net_interest_income interest_income interest_expense "
        "depreciation_and_amortization ebitda ebit "
        "non_operating_income_excluding_interest operating_income "
        "total_other_income_expenses_net income_before_tax income_tax_expense "
        "net_income_from_continuing_operations net_income_from_discontinued_operations "
        "other_adjustments_to_net_income net_income net_income_deductions "
        "bottom_line_net_income eps eps_diluted weighted_average_shs_out "
        "weighted_average_shs_out_dil"
    ).split(),
    "balance_sheet": (
        "cash_and_cash_equivalents short_term_investments "
        "cash_and_short_term_investments net_receivables accounts_receivables "
        "other_receivables inventory prepaids other_current_assets "
        "total_current_assets property_plant_equipment_net goodwill intangible_assets "
        "goodwill_and_intangible_assets long_term_investments tax_assets "
        "other_non_current_assets total_non_current_assets other_assets total_assets "
        "total_payables account_payables other_payables accrued_expenses "
        "short_term_debt capital_lease_obligations_current tax_payables "
        "deferred_revenue other_current_liabilities total_current_liabilities "
        "long_term_debt capital_lease_obligations_non_current "
        "deferred_revenue_non_current deferred_tax_liabilities_non_current "
        "other_non_current_liabilities total_non_current_liabilities other_liabilities "
        "capital_lease_obligations total_liabilities treasury_stock preferred_stock "
        "common_stock retained_earnings additional_paid_in_capital "
        "accumulated_other_comprehensive_income_loss other_total_stockholders_equity "
        "total_stockholders_equity total_equity minority_interest "
        "total_liabilities_and_total_equity total_investments total_debt net_debt"
    ).split(),
    "cash_flow": (
        "net_income depreciation_and_amortization deferred_income_tax "
        "stock_based_compensation change_in_working_capital accounts_receivables "
        "inventory accounts_payables other_working_capital other_non_cash_items "
        "net_cash_provided_by_operating_activities "
        "investments_in_property_plant_and_equipment acquisitions_net "
        "purchases_of_investments sales_maturities_of_investments "
        "other_investing_activities net_cash_provided_by_investing_activities "
        "net_debt_issuance long_term_net_debt_issuance short_term_net_debt_issuance "
        "net_stock_issuance net_common_stock_issuance common_stock_issuance "
        "common_stock_repurchased net_preferred_stock_issuance net_dividends_paid "
        "common_dividends_paid preferred_dividends_paid other_financing_activities "
        "net_cash_provided_by_financing_activities effect_of_forex_changes_on_cash "
        "net_change_in_cash cash_at_end_of_period cash_at_beginning_of_period "
        "operating_cash_flow capital_expenditure free_cash_flow income_taxes_paid "
        "interest_paid"
    ).split(),
}
SF1_TO_BUILD_PROMPT: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("income", "revenue"),),
    "cor": (("income", "cost_of_revenue"),),
    "gp": (("income", "gross_profit"),),
    "rnd": (("income", "research_and_development_expenses"),),
    "sgna": (("income", "selling_general_and_administrative_expenses"),),
    "opex": (("income", "operating_expenses"),),
    "opinc": (("income", "operating_income"),),
    "intexp": (("income", "interest_expense"),),
    "ebit": (("income", "ebit"),),
    "ebitda": (("income", "ebitda"),),
    "ebt": (("income", "income_before_tax"),),
    "taxexp": (("income", "income_tax_expense"),),
    "netincdis": (("income", "net_income_from_discontinued_operations"),),
    "netinc": (("income", "net_income"), ("cash_flow", "net_income")),
    "eps": (("income", "eps"),),
    "epsdil": (("income", "eps_diluted"),),
    "shareswa": (("income", "weighted_average_shs_out"),),
    "shareswadil": (("income", "weighted_average_shs_out_dil"),),
    "depamor": (
        ("income", "depreciation_and_amortization"),
        ("cash_flow", "depreciation_and_amortization"),
    ),
    "cashneq": (("balance_sheet", "cash_and_cash_equivalents"),),
    "investmentsc": (("balance_sheet", "short_term_investments"),),
    "receivables": (("balance_sheet", "net_receivables"),),
    "inventory": (("balance_sheet", "inventory"),),
    "assetsc": (("balance_sheet", "total_current_assets"),),
    "ppnenet": (("balance_sheet", "property_plant_equipment_net"),),
    "intangibles": (("balance_sheet", "goodwill_and_intangible_assets"),),
    "investmentsnc": (("balance_sheet", "long_term_investments"),),
    "taxassets": (("balance_sheet", "tax_assets"),),
    "assetsnc": (("balance_sheet", "total_non_current_assets"),),
    "assets": (("balance_sheet", "total_assets"),),
    "payables": (("balance_sheet", "total_payables"),),
    "debtc": (("balance_sheet", "short_term_debt"),),
    "deferredrev": (("balance_sheet", "deferred_revenue"),),
    "liabilitiesc": (("balance_sheet", "total_current_liabilities"),),
    "debtnc": (("balance_sheet", "long_term_debt"),),
    "liabilitiesnc": (("balance_sheet", "total_non_current_liabilities"),),
    "liabilities": (("balance_sheet", "total_liabilities"),),
    "retearn": (("balance_sheet", "retained_earnings"),),
    "accoci": (("balance_sheet", "accumulated_other_comprehensive_income_loss"),),
    "equity": (("balance_sheet", "total_stockholders_equity"),),
    "investments": (("balance_sheet", "total_investments"),),
    "debt": (("balance_sheet", "total_debt"),),
    "ncfo": (
        ("cash_flow", "net_cash_provided_by_operating_activities"),
        ("cash_flow", "operating_cash_flow"),
    ),
    "capex": (
        ("cash_flow", "capital_expenditure"),
        ("cash_flow", "investments_in_property_plant_and_equipment"),
    ),
    "ncfbus": (("cash_flow", "acquisitions_net"),),
    "ncfi": (("cash_flow", "net_cash_provided_by_investing_activities"),),
    "ncfdebt": (("cash_flow", "net_debt_issuance"),),
    "ncfcommon": (("cash_flow", "net_common_stock_issuance"),),
    "ncfdiv": (("cash_flow", "net_dividends_paid"),),
    "ncff": (("cash_flow", "net_cash_provided_by_financing_activities"),),
    "ncfx": (("cash_flow", "effect_of_forex_changes_on_cash"),),
    "ncf": (("cash_flow", "net_change_in_cash"),),
    "sbcomp": (("cash_flow", "stock_based_compensation"),),
    "fcf": (("cash_flow", "free_cash_flow"),),
}

# Item 2: filings whose dates are public record (EDGAR), and the floor of a
# plausible filing lag (#102's fundamentals_implausible_filing_lag).
FILING_CASES = (
    ("AAPL", "2020-FY", date(2020, 10, 30)),  # 10-K for the year to 2020-09-26
    ("MSFT", "2015-FY", date(2015, 7, 31)),  # 10-K for the year to 2015-06-30
)
MIN_FILING_LAG_DAYS = 5
#: More than this share of one era's filings on a single lag looks imputed.
IMPUTED_LAG_SHARE = Decimal("0.5")
#: AR rows that may fail a date rule before it is systematic, not an issuer's row.
SYSTEMATIC_SHARE = Decimal("0.01")
EDGAR_CHECKS = 5
# Item 3: MSFT restated FY2015's balance sheet in its FY2016 10-K.
RESTATEMENT_CASE = ("MSFT", "2015-FY")
# Item 5: the flows summed across a fiscal year's quarters, and the tolerance.
FLOW_FIELDS = ("revenue", "netinc", "ncfo", "capex")
SUM_TOLERANCE = Decimal("0.01")
# Item 6: the FMP defect of the vendor assessment §3.4, asked of SF1.
NULL_ZERO_CASE = ("GE", 1998, 2005, ("rnd", "intexp", "depamor"))
# Item 8: one issuer's two share classes (public record), primary first.
SHARE_CLASS_CASES = (("GOOGL", "GOOG"), ("BRK.B", "BRK.A"))
# Item 10: a split moves the share count by at least this factor, and a ratio
# within this band of 1 (or of the split) decides which way the counts read.
SPLIT_MIN_FACTOR = Decimal("1.5")
SHARE_BAND = Decimal("0.25")


class ProbeError(ValueError):
    """The probe has nothing to read: no SF1 file, or no vendor master."""


@dataclass
class Sf1Data:
    """What the probe reads, from the ledger's files or from the API."""

    origin: str
    columns: list[str]
    rows: list[dict]
    #: TICKERS' fundamentals listing: ``permaticker, ticker, category,
    #: is_delisted, currency, first_quarter``.
    listing: list[dict]
    #: ``{ticker: [(ex_date, value), ...]}`` from ACTIONS.
    splits: dict[str, list[tuple[date, Decimal]]]
    sample: dict[str, list[str]]
    requests: int = 0
    bytes: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def _share(n: int, d: int) -> Decimal:
    return Decimal(n) / Decimal(d) if d else Decimal(0)


def _quantile(values: Sequence[int], q: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _ar(rows: Iterable[dict], dims: Sequence[str] = ("ARQ", "ARY")) -> list[dict]:
    return [r for r in rows if r.get("dimension") in dims]


def _fiscal(row: dict) -> tuple[Optional[str], Optional[str]]:
    """``("2025", "Q4")`` from ``fiscalperiod = "2025-Q4"``."""
    fp = row.get("fiscalperiod") or ""
    year, _, period = fp.partition("-")
    return (year or None), (period or None)


def _listed_word(listed: bool) -> str:
    return "listed" if listed else "absent"


def _era(day: date) -> str:
    if day < FLOOR:
        return f"before {FLOOR.year}"
    return f"{FLOOR.year}-2002" if day.year <= 2002 else "2003 on"


def _listed(row: Mapping[str, Any]) -> dict:
    """A TICKERS row (API spelling) or a ``core.vendor_security`` row, as one
    shape."""
    delisted = row.get("is_delisted", row.get("isdelisted"))
    if isinstance(delisted, str):
        delisted = delisted.upper() == "Y"
    return {
        "permaticker": str(row.get("vendor_key") or row.get("permaticker") or ""),
        "ticker": row.get("ticker"),
        "category": row.get("category") or "",
        "is_delisted": bool(delisted),
        "currency": row.get("currency"),
        "first_quarter": row.get("first_quarter") or row.get("firstquarter"),
    }


def draw(
    listing: Sequence[dict],
    symbols: Sequence[str] = PROBE_SET,
    *,
    dead: int = DEAD_SAMPLE,
    foreign: int = FOREIGN_SAMPLE,
    seed: int = 0,
) -> dict[str, list[str]]:
    """The probe set, ``dead`` delisted issuers SF1 covers and ``foreign`` listed
    issuers that report outside USD, drawn deterministically for ``seed`` from the
    listing in permaticker order."""
    covered = sorted(
        (r for r in listing if r.get("first_quarter") and r.get("ticker")),
        key=lambda r: r["permaticker"],
    )
    taken = set(symbols)
    dead_pool = [
        r["ticker"] for r in covered if r["is_delisted"] and r["ticker"] not in taken
    ]
    foreign_pool = [
        r["ticker"]
        for r in covered
        if not r["is_delisted"]
        and (r["currency"] or "USD") != "USD"
        and r["ticker"] not in taken
    ]
    rng = random.Random(seed)
    dead_pick = rng.sample(dead_pool, min(dead, len(dead_pool)))
    foreign_pick = rng.sample(foreign_pool, min(foreign, len(foreign_pool)))
    return {"probe_set": list(symbols), "dead": dead_pick, "foreign": foreign_pick}


def _sampled(sample: Mapping[str, Sequence[str]]) -> list[str]:
    seen: list[str] = []
    for tickers in sample.values():
        seen += [t for t in tickers if t not in seen]
    return seen


def _splits(actions: Iterable[dict]) -> dict[str, list[tuple[date, Decimal]]]:
    out: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for a in actions:
        if a.get("action") == "split" and a.get("value"):
            out[a["ticker"]].append((a["date"], Decimal(a["value"])))
    return {t: sorted(v) for t, v in out.items()}


# ---------------------------------------------------------------------------
# Collecting the rows
# ---------------------------------------------------------------------------


def from_ledger(
    db,
    bulk_dir,
    *,
    symbols: Sequence[str] = PROBE_SET,
    dead: int = DEAD_SAMPLE,
    foreign: int = FOREIGN_SAMPLE,
    seed: int = 0,
) -> Sf1Data:
    """The sample's rows from the ledger's current SF1 file, its splits from the
    current ACTIONS file, and the listing from ``core.vendor_security``. One pass
    over each file; nothing is written."""
    from fafnir.db import repository as repo

    sf1 = repo.current_sharadar_full(db, "fundamentals")
    if sf1 is None:
        raise ProbeError(
            "the ledger holds no current SF1 file: run "
            "`fafnir source sharadar-bulk --tables SF1`"
        )
    listing = [_listed(r) for r in repo.vendor_listing(db, "sharadar", "fundamentals")]
    if not listing:
        raise ProbeError(
            "core.vendor_security holds no fundamentals tickers: run "
            "`fafnir ingest sharadar-master`"
        )
    sample = draw(listing, symbols, dead=dead, foreign=foreign, seed=seed)
    wanted = {t: sf.ALL_DATES for t in _sampled(sample)}
    entry, path = ledger.checked_path(db, sf1["file_id"], bulk_dir)
    columns = sf.header(path, entry["member"])
    rows = list(ledger.select(db, sf1["file_id"], bulk_dir, wanted))
    actions = repo.current_sharadar_full(db, "actions")
    notes = []
    if actions is None:
        notes.append("no current ACTIONS file: item 10 has no splits to test")
        splits = {}
    else:
        splits = _splits(ledger.select(db, actions["file_id"], bulk_dir, wanted))
    return Sf1Data(
        origin=f"file {sf1['file_id']}",
        columns=columns,
        rows=rows,
        listing=listing,
        splits=splits,
        sample=sample,
        notes=notes,
    )


LISTING_FIELDS = (
    "permaticker",
    "ticker",
    "category",
    "isdelisted",
    "currency",
    "firstquarter",
)


def from_api(
    client,
    *,
    symbols: Sequence[str] = PROBE_SET,
    dead: int = DEAD_SAMPLE,
    foreign: int = FOREIGN_SAMPLE,
    seed: int = 0,
) -> Sf1Data:
    """The same rows from the API. One SF1 request per ticker: a list of tickers
    returns only about the last two years, and nothing for a ticker without them
    (measured 2026-10-24). Each request's rows are checked for its ticker, since
    an ignored filter is not refused. About two requests per sampled ticker."""
    listed = client.query(
        client.T_TICKERS,
        {"table": "fundamentals", "sort": "permaticker"},
        fields=list(LISTING_FIELDS),
    )
    by_key = {str(r.get("permaticker")): _listed(r) for r in listed}
    listing = list(by_key.values())
    sample = draw(listing, symbols, dead=dead, foreign=foreign, seed=seed)
    rows: list[dict] = []
    columns: list[str] = []
    actions: list[dict] = []
    for ticker in _sampled(sample):
        got = [
            r
            for r in client.query(client.T_SF1, {"ticker": ticker})
            if r.get("ticker") == ticker
        ]
        for r in got:
            columns += [c for c in r if c not in columns]
        rows += [sf.type_row("fundamentals", r) for r in got]
        acts = client.query(client.T_ACTIONS, {"ticker": ticker, "action": "split"})
        actions += [
            sf.type_row("actions", a)
            for a in acts
            if a.get("ticker") == ticker and a.get("action") == "split"
        ]
    return Sf1Data(
        origin="api",
        columns=columns,
        rows=rows,
        listing=listing,
        splits=_splits(actions),
        sample=sample,
        requests=client.request_count,
        bytes=client.bytes_downloaded,
    )


# ---------------------------------------------------------------------------
# The items, each a pure function of what was read
# ---------------------------------------------------------------------------


def coverage(rows: list[dict], sample: Mapping[str, Sequence[str]]) -> Item:
    by_dim = Counter(r.get("dimension") for r in rows)
    tickers = {r.get("ticker") for r in rows}
    wanted = _sampled(sample)
    if not rows:
        return Item(
            1,
            "Coverage",
            "no_rows",
            BLOCK,
            f"no SF1 row for any of {len(wanted)} tickers",
        )
    missing = [d for d in DIMENSIONS if not by_dim.get(d)]
    pre = sum(1 for r in rows if r["reportperiod"] < FLOOR)
    earliest = min(r["calendardate"] for r in rows)
    first_ar = {
        t: min(r["calendardate"] for r in _ar(rows) if r["ticker"] == t).isoformat()
        for t in sample.get("probe_set", ())
        if any(r["ticker"] == t for r in _ar(rows))
    }
    dead_rows = sum(1 for t in sample.get("dead", ()) if t in tickers)
    detail = (
        f"{len(rows):,} rows on {len(tickers & set(wanted))} of {len(wanted)} sampled "
        f"tickers ({dead_rows} of {len(sample.get('dead', ()))} dead issuers); by "
        f"dimension {dict(sorted(by_dim.items()))}; earliest calendardate {earliest}. "
        f"{pre:,} rows ({_pct(pre, len(rows))}) report a period before {FLOOR}, the "
        "survivorship floor (ADR 0016): whether the loader keeps them is #85's call."
    )
    if missing:
        verdict, level = "dimensions_missing", NOTE
        detail += f" Missing: {', '.join(missing)}."
    elif pre:
        verdict, level = "history_before_floor", NOTE
    else:
        verdict, level = "six_dimensions", PASS
    return Item(
        1,
        "Coverage",
        verdict,
        level,
        detail,
        {"first_ar_period": first_ar, "by_dimension": dict(by_dim)},
    )


def knowledge_dates(rows: list[dict]) -> Item:
    ar = _ar(rows)
    name = "Knowledge dates"
    if not ar:
        return Item(
            2, name, "no_ar_rows", BLOCK, "no ARQ or ARY row to read a filing date from"
        )
    lags: dict[tuple[str, str], list[int]] = defaultdict(list)
    before = same_day = quick = late = 0
    for r in ar:
        lag = (r["date"] - r["reportperiod"]).days
        before += lag < 0
        same_day += lag == 0
        quick += 0 < lag < MIN_FILING_LAG_DAYS
        late += lag > 366
        kind = "annual" if r["dimension"] == "ARY" else "quarter"
        lags[(kind, _era(r["reportperiod"]))].append(lag)
    eras = {}
    imputed = []
    for (kind, era), values in sorted(lags.items()):
        mode, count = Counter(values).most_common(1)[0]
        share = _share(count, len(values))
        eras[f"{kind}, {era}"] = {
            "rows": len(values),
            "median": _quantile(values, 0.5),
            "p95": _quantile(values, 0.95),
            "modal_lag": mode,
            "modal_share": f"{share:.2f}",
        }
        if len(values) >= 20 and share > IMPUTED_LAG_SHARE:
            imputed.append(f"{kind}, {era}")
    mr = [r for r in rows if r.get("dimension") in MR_DIMENSIONS]
    mr_period = sum(1 for r in mr if r["date"] == r["reportperiod"])
    per_ticker: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        per_ticker[r["ticker"]][r.get("lastupdated")] += 1
    restamped = sum(
        1
        for c in per_ticker.values()
        if c.most_common(1)[0][1] >= 0.9 * sum(c.values())
    )
    cases, disagree = [], []
    for ticker, fp, filed in FILING_CASES:
        hit = next(
            (
                r
                for r in ar
                if r["ticker"] == ticker
                and r["dimension"] == "ARY"
                and r.get("fiscalperiod") == fp
            ),
            None,
        )
        if hit is None:
            continue
        cases.append(
            {
                "ticker": ticker,
                "fiscalperiod": fp,
                "edgar": filed.isoformat(),
                "sf1": hit["date"].isoformat(),
            }
        )
        if hit["date"] != filed:
            disagree.append(f"{ticker} {fp}")
    latest: dict[str, dict] = {}
    for r in ar:
        if r["dimension"] == "ARY" and (
            r["ticker"] not in latest
            or r["reportperiod"] > latest[r["ticker"]]["reportperiod"]
        ):
            latest[r["ticker"]] = r
    edgar = [
        {
            "ticker": r["ticker"],
            "fiscalperiod": r.get("fiscalperiod"),
            "reportperiod": r["reportperiod"].isoformat(),
            "date": r["date"].isoformat(),
        }
        for r in sorted(latest.values(), key=lambda r: r["ticker"])[:EDGAR_CHECKS]
    ]
    evidence = {
        "lags_by_era": eras,
        "public_record": cases,
        "compare_with_edgar": edgar,
        "before_period_end": before,
        "within_5_days": quick,
        "after_366_days": late,
    }
    detail = (
        f"AR `date` against `reportperiod` on {len(ar):,} ARQ/ARY rows: {before} before "
        f"the period end, {same_day} on it, {quick} within {MIN_FILING_LAG_DAYS} days, "
        f"{late} over a year after. {len(cases) - len(disagree)} of {len(cases)} "
        "public-record filings agree. "
        + (
            f"Lags look imputed in: {', '.join(imputed)}. "
            if imputed
            else "No era's filings sit on one lag (they are not imputed). "
        )
        + f"MR `date` is the period end on {mr_period:,} of {len(mr):,} MR rows. "
        f"`lastupdated` is one date for 90% or more of the rows of {restamped} of "
        f"{len(per_ticker)} tickers. Compare the {len(edgar)} filings in the "
        "evidence with EDGAR by hand."
    )
    if _share(before, len(ar)) > SYSTEMATIC_SHARE:
        return Item(2, name, "filing_before_period_end", BLOCK, detail, evidence)
    if _share(same_day, len(ar)) > SYSTEMATIC_SHARE:
        return Item(2, name, "ar_date_is_not_a_filing_date", BLOCK, detail, evidence)
    if imputed:
        return Item(2, name, "filing_dates_look_imputed", BLOCK, detail, evidence)
    if disagree:
        detail += f" Disagreeing: {', '.join(disagree)}."
        return Item(
            2, name, "filing_date_disagrees_with_public_record", BLOCK, detail, evidence
        )
    detail += (
        " So an AR row's knowledge date is its filing date; an MR row carries none of"
        " its own (#85 decides a restatement's), and `lastupdated` is a restamp, a"
        " delta trigger only."
    )
    verdict = "ar_filed_mr_undated" if mr and mr_period == len(mr) else "ar_filed"
    return Item(2, name, verdict, NOTE, detail, evidence)


def _statement_fields(columns: Iterable[str]) -> list[str]:
    cols = set(columns)
    return [
        c
        for group in STATEMENTS.values()
        for c in group
        if c in cols and c not in AS_OF_DATE
    ]


def restatements(rows: list[dict], columns: Sequence[str]) -> Item:
    fields = _statement_fields(columns)
    keyed = {(r["ticker"], r["dimension"], r["calendardate"]): r for r in rows}
    pairs: Counter = Counter()
    differ: Counter = Counter()
    mr_only: Counter = Counter()
    moved_fields: Counter = Counter()
    share_only = 0
    for (ticker, dim, cal), r in keyed.items():
        if dim in AR_DIMENSIONS:
            twin = keyed.get((ticker, TWIN[dim], cal))
            if twin is None:
                continue
            pairs[dim] += 1
            changed = {c for c in fields if r.get(c) != twin.get(c)}
            differ[dim] += bool(changed)
            if dim == "ARQ":
                moved_fields.update(changed)
                share_only += bool(changed) and changed <= set(SHARE_FIELDS)
        elif dim in TWIN_OF_MR and (ticker, TWIN_OF_MR[dim], cal) not in keyed:
            mr_only[dim] += 1
    ticker, fp = RESTATEMENT_CASE
    ar = next(
        (
            r
            for r in rows
            if r["ticker"] == ticker
            and r["dimension"] == "ARY"
            and r.get("fiscalperiod") == fp
        ),
        None,
    )
    mr = ar and keyed.get((ticker, "MRY", ar["calendardate"]))
    moved = [c for c in fields if ar and mr and ar.get(c) != mr.get(c)]
    evidence = {
        "pairs": dict(pairs),
        "differ": dict(differ),
        "mr_without_ar": dict(mr_only),
        "arq_fields_moved": dict(moved_fields.most_common()),
        "case": {
            "fields": moved,
            "ar_date": ar and ar["date"].isoformat(),
            "mr_date": mr and mr["date"].isoformat(),
        },
    }
    name = "Restatements"
    if not pairs:
        return Item(
            3,
            name,
            "no_pairs",
            NOTE,
            "no AR row has its MR twin in the sample",
            evidence,
        )
    shares = ", ".join(
        f"{d} {_pct(differ[d], pairs[d])} of {pairs[d]}"
        for d in AR_DIMENSIONS
        if pairs[d]
    )
    if ar is None or mr is None:
        case = f"{ticker} {fp} is not in the sample. "
        verdict = "case_absent"
    elif moved:
        case = (
            f"{ticker} {fp}: {len(moved)} statement fields differ between ARY (dated "
            f"{ar['date']}, the filing) and MRY (dated {mr['date']}, the period end). "
        )
        verdict = "mr_carries_restatements"
    else:
        case = f"{ticker} {fp}: ARY and MRY agree on every statement field. "
        verdict = "case_not_restated"
    top = ", ".join(f"{c} {n}" for c, n in moved_fields.most_common(5))
    detail = (
        case
        + f"AR/MR pairs that differ in a statement field (the as-of-date columns left "
        f"out): {shares}. Quarterly, the fields that move most are {top}; "
        f"{share_only} pairs differ only in share counts and per-share values. "
        f"{sum(mr_only.values())} MR rows have no AR twin (a period never filed on "
        "time, or before coverage). AR is first-reported and becomes version 1 at its "
        "filing date; an MR row that differs is a restatement (a later filing's "
        "comparatives, mostly reclassifications) whose knowledge date SF1 does not give."
    )
    return Item(3, name, verdict, NOTE, detail, evidence)


def inventory(columns: Sequence[str]) -> Item:
    cols = list(columns)
    unknown = [c for c in cols if c not in KNOWN]
    absent = sorted(KNOWN - set(cols))
    missing_keys = [c for c in KEY_COLUMNS if c not in cols]
    covered: dict[str, set[str]] = defaultdict(set)
    for indicator, targets in SF1_TO_BUILD_PROMPT.items():
        if indicator in cols:
            for statement, column in targets:
                covered[statement].add(column)
    per_statement = {
        s: f"{len(covered[s])}/{len(c)}" for s, c in BUILD_PROMPT_COLUMNS.items()
    }
    uncovered = {
        s: [c for c in cs if c not in covered[s]]
        for s, cs in BUILD_PROMPT_COLUMNS.items()
    }
    unmapped = [
        c
        for g in STATEMENTS.values()
        for c in g
        if c in cols and c not in SF1_TO_BUILD_PROMPT
    ]
    n_cov = sum(len(v) for v in covered.values())
    n_all = sum(len(v) for v in BUILD_PROMPT_COLUMNS.values())
    counts = {s: sum(1 for c in g if c in cols) for s, g in STATEMENTS.items()}
    detail = (
        f"{len(cols)} columns: {sum(counts.values())} statement indicators (income "
        f"{counts['income']}, balance sheet {counts['balance_sheet']}, cash flow "
        f"{counts['cash_flow']}), {sum(1 for c in METRICS if c in cols)} vendor "
        f"metrics, {sum(1 for c in ENTITY if c in cols)} entity fields, "
        f"{len(unknown)} the vendor's classification does not list"
        + (f" ({', '.join(unknown)})" if unknown else "")
        + (f", {len(absent)} listed but absent ({', '.join(absent)})" if absent else "")
        + f". {n_cov} of the build prompt's {n_all} §4.3 columns have an SF1 "
        f"counterpart ({', '.join(f'{k} {v}' for k, v in per_statement.items())}); "
        "the rest have none, so SF1's indicators become the taxonomy, as #85 proposes."
    )
    evidence = {
        "uncovered": uncovered,
        "sf1_unmapped": unmapped,
        "unknown": unknown,
        "absent": absent,
    }
    if missing_keys:
        return Item(
            4,
            "Indicator inventory",
            "key_columns_missing",
            BLOCK,
            detail + f" Missing key columns: {', '.join(missing_keys)}.",
            evidence,
        )
    return Item(4, "Indicator inventory", "sf1_taxonomy", NOTE, detail, evidence)


def quarters(rows: list[dict]) -> Item:
    quarterly: dict[tuple, dict[str, dict]] = defaultdict(dict)
    annual: dict[tuple, dict] = {}
    for r in _ar(rows):
        year, period = _fiscal(r)
        if not year:
            continue
        if r["dimension"] == "ARY":
            annual[(r["ticker"], year)] = r
        elif period in ("Q1", "Q2", "Q3", "Q4"):
            quarterly[(r["ticker"], year)][period] = r
    name = "Quarterly completeness"
    if not annual:
        return Item(5, name, "no_fiscal_years", NOTE, "no ARY row with a fiscal period")
    complete = sum(1 for k in annual if len(quarterly.get(k, {})) == 4)
    # A year with its first three quarters and no Q4 is the case a derivation
    # (FY - Q1 - Q2 - Q3) would fill. Any other short year is a coverage edge: the
    # first year an issuer was covered, or its last.
    q4_missing = sum(
        1 for k in annual if set(quarterly.get(k, {})) == {"Q1", "Q2", "Q3"}
    )
    short = len(annual) - complete - q4_missing
    compared = agree = ytd = 0
    off = []
    for k, fy in annual.items():
        qs = quarterly.get(k, {})
        if len(qs) != 4:
            continue
        for f in FLOW_FIELDS:
            total = fy.get(f)
            if total is None or any(q.get(f) is None for q in qs.values()):
                continue
            summed = sum(q[f] for q in qs.values())
            compared += 1
            rel = abs(summed - total) / max(abs(total), Decimal(1))
            if rel <= SUM_TOLERANCE:
                agree += 1
                continue
            if total and abs(summed) >= Decimal("1.8") * abs(total):
                ytd += 1
            off.append(
                {
                    "ticker": k[0],
                    "fiscal_year": k[1],
                    "field": f,
                    "relative": f"{rel:.4f}",
                }
            )
    detail = (
        f"Of {len(annual)} fiscal years with an ARY row, {complete} have all four ARQ "
        f"rows, {q4_missing} have Q1-Q3 and no Q4, and {short} are short at a coverage "
        f"edge. Quarterly {', '.join(FLOW_FIELDS)} sum to the fiscal year within "
        f"{SUM_TOLERANCE:.0%} in {agree} of {compared} comparisons "
        f"({_pct(agree, compared)}), and {ytd} look year-to-date."
    )
    evidence = {"off": sorted(off, key=lambda o: -Decimal(o["relative"]))[:20]}
    if _share(q4_missing, complete + q4_missing) > SYSTEMATIC_SHARE:
        return Item(
            5,
            name,
            "q4_missing",
            NOTE,
            detail + " Q4 must be derived as FY - Q1 - Q2 - Q3 where it is missing.",
            evidence,
        )
    if _share(ytd, compared) > Decimal("0.05"):
        return Item(
            5,
            name,
            "ytd_quarters",
            NOTE,
            detail + " Some quarters are cumulative: TTM must de-cumulate them.",
            evidence,
        )
    if _share(agree, compared) >= Decimal("0.95"):
        detail += (
            " Quarters are discrete: TTM sums four ARQ rows, with no Q4 derivation and"
            " no year-to-date de-cumulation. The exceptions are in the evidence."
        )
        return Item(5, name, "discrete_quarters_with_q4", PASS, detail, evidence)
    return Item(5, name, "quarters_do_not_sum", NOTE, detail, evidence)


def nulls(rows: list[dict], columns: Sequence[str]) -> Item:
    fields = [c for g in STATEMENTS.values() for c in g if c in columns]
    ar = _ar(rows)
    tally = {c: Counter() for c in fields}
    for r in ar:
        for c in fields:
            v = r.get(c)
            tally[c]["null" if v is None else "zero" if v == 0 else "value"] += 1
    with_nulls = sorted(
        (c for c in fields if tally[c]["null"]), key=lambda c: -tally[c]["null"]
    )
    rows_with_null = sum(1 for r in ar if any(r.get(c) is None for c in fields))
    ticker, first, last, case_fields = NULL_ZERO_CASE
    case_rows = [
        r
        for r in ar
        if r["ticker"] == ticker
        and r["dimension"] == "ARY"
        and first <= r["reportperiod"].year <= last
    ]
    case = {
        f: dict(
            Counter(
                "null" if r.get(f) is None else "zero" if r.get(f) == 0 else "value"
                for r in case_rows
            )
        )
        for f in case_fields
    }
    evidence = {
        "case": case,
        "nulls_by_column": {c: tally[c]["null"] for c in with_nulls},
        "zero_share": {c: _pct(tally[c]["zero"], len(ar)) for c in fields},
    }
    case_text = (
        "; ".join(
            f"{f} zero in {case[f].get('zero', 0)} of {len(case_rows)}"
            for f in case_fields
        )
        if case_rows
        else f"{ticker} is not in the sample"
    )
    detail = (
        f"Sharadar writes 0 when a filing does not carry an item ('the value of 0 is "
        f"used', {len(ZERO_FILLED)} indicators), so SF1 cannot tell an absent item "
        f"from a reported zero there. {ticker}'s ARY {first}-{last}: {case_text}. "
        f"NULL is a second convention: {rows_with_null:,} of {len(ar):,} AR rows hold "
        f"one, in {len(with_nulls)} columns, most often "
        + (
            ", ".join(f"{c} {tally[c]['null']:,}" for c in with_nulls[:4])
            if with_nulls
            else "none"
        )
        + ". Sharadar leaves an item NULL where it does not apply, such as the current "
        "and non-current splits of a balance sheet that is not classified (banks, "
        "insurers). The loader keeps both as given; ratios guard "
        "zero denominators with NULLIF, and #102's fundamentals_suspicious_zero watches "
        "the items a company should report."
    )
    return Item(6, "Null against zero", "zero_for_absent", NOTE, detail, evidence)


def currency(
    rows: list[dict],
    columns: Sequence[str],
    listing: Sequence[dict],
    sample: Mapping[str, Sequence[str]],
) -> Item:
    by_ticker = {r["ticker"]: r for r in listing}
    sampled = [t for t in _sampled(sample) if t in by_ticker]
    non_usd = {
        t: by_ticker[t]["currency"]
        for t in sampled
        if (by_ticker[t]["currency"] or "USD") != "USD"
    }
    converted = [
        r
        for r in rows
        if r.get("fxusd")
        and r["fxusd"] != 1
        and r.get("revenue")
        and r.get("revenueusd") is not None
    ]
    times = sum(
        1
        for r in converted
        if abs(r["revenue"] * r["fxusd"] - r["revenueusd"])
        <= abs(r["revenueusd"]) * SUM_TOLERANCE
    )
    divided = sum(
        1
        for r in converted
        if abs(r["revenue"] / r["fxusd"] - r["revenueusd"])
        <= abs(r["revenueusd"]) * SUM_TOLERANCE
    )
    listing_non_usd = Counter(
        r["currency"] for r in listing if (r["currency"] or "USD") != "USD"
    )
    detail = (
        (
            "SF1 rows carry no currency column: the reporting currency is TICKERS' "
            "`currency`, one per ticker, not per row. "
            if "currency" not in columns
            else "SF1 rows carry a currency column. "
        )
        + f"{len(non_usd)} of {len(sampled)} sampled tickers report outside USD"
        + (f" ({', '.join(sorted(set(non_usd.values())))})" if non_usd else "")
        + f", and {sum(listing_non_usd.values()):,} of the listing's {len(listing):,}. "
        f"On {len(converted)} rows with `fxusd` other than 1, revenueusd is revenue x "
        f"fxusd on {times} and revenue / fxusd on {divided}. §4.8's reported_currency "
        "comes from TICKERS; the `*usd` columns are vendor conversions, not statement "
        "items."
    )
    evidence = {
        "non_usd": non_usd,
        "listing_currencies": dict(listing_non_usd.most_common(10)),
    }
    return Item(7, "Currency", "currency_from_tickers", NOTE, detail, evidence)


def identity(rows: list[dict], listing: Sequence[dict]) -> Item:
    by_ticker = {r["ticker"]: r for r in listing}
    tickers = sorted({r["ticker"] for r in rows})
    unlisted = [
        t for t in tickers if t not in by_ticker or not by_ticker[t]["permaticker"]
    ]
    categories = Counter(r["category"] for r in listing)
    secondary = {c: n for c, n in categories.items() if "Secondary" in c}
    suffixed = sum(
        1 for r in listing if r["is_delisted"] and re.search(r"\d$", r["ticker"] or "")
    )
    cases = {
        f"{p}/{s}": {
            "primary_listed": p in by_ticker,
            "secondary_listed": s in by_ticker,
        }
        for p, s in SHARE_CLASS_CASES
    }
    detail = (
        f"{len(tickers) - len(unlisted)} of {len(tickers)} SF1 tickers in the sample "
        f"are in TICKERS' fundamentals listing, so they reach a permaticker. The "
        f"listing's {len(listing):,} tickers hold {sum(secondary.values())} of a "
        "secondary share class"
        + (f" ({secondary})" if secondary else "")
        + ": SF1 holds one class per issuer, the primary "
        + ", ".join(
            f"({p} {_listed_word(v['primary_listed'])}, "
            f"{s} {_listed_word(v['secondary_listed'])})"
            for (p, s), v in zip(SHARE_CLASS_CASES, cases.values())
        )
        + ". A secondary class's security gets no statements unless #85 attaches the "
        f"issuer's; its permaticker is another one. {suffixed:,} delisted tickers end "
        "in a digit, the reused-ticker suffix #49 found in SEP."
    )
    evidence = {
        "unlisted": unlisted,
        "categories": dict(categories),
        "share_classes": cases,
    }
    if unlisted:
        return Item(
            8,
            "Identity",
            "tickers_without_permaticker",
            NOTE,
            detail + f" Unlisted: {', '.join(unlisted)}.",
            evidence,
        )
    verdict = "primary_class_only" if not secondary else "permaticker_per_ticker"
    return Item(8, "Identity", verdict, NOTE, detail, evidence)


def vendor_metrics(columns: Sequence[str]) -> Item:
    metrics = [c for c in METRICS if c in columns]
    as_of = [c for c in AS_OF_DATE if c in columns]
    aggregates = [c for c in ("ebitda", "ebt", "fcf") if c in metrics]
    detail = (
        f"{len(metrics)} columns are the vendor's own metrics by its classification "
        f"(ratios, averages, EV, margins, per-share values, fxusd) and {len(as_of)} are "
        f"measured on the row's `date` ({', '.join(as_of)}): those differ between an AR "
        "row and its MR twin only because the dates differ. Both go to `extra`, never "
        "read by mart (#86's derived-metrics ADR)"
        + (
            f"; {', '.join(aggregates)} are tagged metrics but are statement aggregates"
            " the field map may type"
            if aggregates
            else ""
        )
        + "."
    )
    return Item(
        9,
        "Vendor metrics",
        "vendor_metrics_identified",
        PASS,
        detail,
        {"metrics": metrics, "as_of_date": as_of},
    )


def share_terms(
    rows: list[dict], splits: Mapping[str, Sequence[tuple[date, Decimal]]]
) -> Item:
    quarterly: dict[str, list[dict]] = defaultdict(list)
    for r in _ar(rows, ("ARQ",)):
        if r.get("shareswa"):
            quarterly[r["ticker"]].append(r)
    today = as_reported = unclear = 0
    tested = []
    for ticker, events in splits.items():
        qs = sorted(quarterly.get(ticker, ()), key=lambda r: r["reportperiod"])
        for ex, value in events:
            if not (value >= SPLIT_MIN_FACTOR or value * SPLIT_MIN_FACTOR <= 1):
                continue
            before = [q for q in qs if q["reportperiod"] < ex]
            after = [q for q in qs if q["reportperiod"] >= ex]
            if (
                not before
                or not after
                or (after[0]["reportperiod"] - before[-1]["reportperiod"]).days > 200
            ):
                continue
            ratio = after[0]["shareswa"] / before[-1]["shareswa"]
            if abs(ratio - 1) <= SHARE_BAND:
                today += 1
                kind = "continuous"
            elif abs(ratio / value - 1) <= SHARE_BAND:
                as_reported += 1
                kind = "jumps"
            else:
                unclear += 1
                kind = "unclear"
            tested.append(
                {
                    "ticker": ticker,
                    "ex_date": ex.isoformat(),
                    "value": str(value),
                    "ratio": f"{ratio:.3f}",
                    "reads": kind,
                }
            )
    eps_dp = max(
        (decimals(r["eps"]) for r in _ar(rows) if r.get("eps") is not None), default=0
    )
    n = today + as_reported + unclear
    name = "Share terms"
    evidence = {"splits": tested, "eps_decimals": eps_dp}
    if not n:
        return Item(
            10,
            name,
            "no_split_in_sample",
            NOTE,
            "no split of a sampled ticker falls between two ARQ rows",
            evidence,
        )
    detail = (
        f"Across {n} splits of the sampled tickers, weighted shares run on without "
        f"the split's jump in {today} ({_pct(today, n)}) and jump by it in "
        f"{as_reported}" + (f"; {unclear} are unclear" if unclear else "") + ". "
    )
    if _share(today, n) >= Decimal("0.8"):
        detail += (
            "SF1's share counts and per-share values are in today's share terms, "
            "re-expressed at every later split, which breaks the build prompt's §4.7 "
            "rule 1 (store as reported). As reported is SF1's value / the product of "
            "the splits after `date`; the panel's market cap is the split-adjusted "
            "close x SF1's shares, or the raw close x the recovered count. Each later "
            "split also rewrites every earlier period's shares, which a content hash "
            f"would read as a restatement. EPS carries at most {eps_dp} decimals, so a "
            "recovered as-reported EPS is off by up to half a unit in that place x the "
            "product: prefer aggregate ratios (§4.7 rule 3)."
        )
        return Item(10, name, "share_terms_today", NOTE, detail, evidence)
    if _share(as_reported, n) >= Decimal("0.8"):
        detail += "Share counts are as reported, as §4.7 assumes."
        return Item(10, name, "share_terms_as_reported", PASS, detail, evidence)
    detail += "The share terms are mixed: #85 decides per security."
    return Item(10, name, "share_terms_mixed", NOTE, detail, evidence)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def probe_fundamentals(data: Sf1Data) -> dict[str, Any]:
    """Every item over what was read, and the report. A row missing one of the key
    columns cannot be placed on either timeline, so it is left out and counted."""
    started = time.monotonic()
    cols = data.columns
    rows = [r for r in data.rows if all(r.get(c) is not None for c in KEY_COLUMNS)]
    notes = list(data.notes)
    if len(rows) < len(data.rows):
        notes.append(
            f"{len(data.rows) - len(rows):,} rows without one of "
            f"{', '.join(KEY_COLUMNS)} were left out"
        )
    items = [
        coverage(rows, data.sample),
        knowledge_dates(rows),
        restatements(rows, cols),
        inventory(cols),
        quarters(rows),
        nulls(rows, cols),
        currency(rows, cols, data.listing, data.sample),
        identity(rows, data.listing),
        vendor_metrics(cols),
        share_terms(rows, data.splits),
    ]
    return {
        "probed_on": date.today().isoformat(),
        "origin": data.origin,
        "sample": {k: len(v) for k, v in data.sample.items()},
        "tickers": data.sample,
        "rows": len(rows),
        "requests": data.requests,
        "bytes": data.bytes,
        "elapsed_s": round(time.monotonic() - started, 1),
        "notes": notes,
        "items": [asdict(i) for i in items],
    }


def format_fundamentals_report(report: dict[str, Any]) -> str:
    """Verdicts and aggregates for the terminal: what may be posted. The --json
    report adds the evidence rows, which stay on the host."""
    s = report["sample"]
    lines = [
        f"SF1 probe ({report['probed_on']}), read from {report['origin']}",
        f"  sample: {s.get('probe_set', 0)} probe tickers, {s.get('dead', 0)} dead "
        f"issuers, {s.get('foreign', 0)} reporting outside USD; {report['rows']:,} rows",
    ]
    if report["requests"]:
        lines.append(
            f"  cost: {report['requests']} requests, {report['bytes']:,} bytes"
        )
    lines += [f"  note: {n}" for n in report.get("notes", ())]
    lines.append("")
    for i in report["items"]:
        lines.append(f"{i['number']:>2}. {i['name']}: {i['level']} {i['verdict']}")
        lines.append(f"    {i['detail']}")
    blocks = blocking(report)
    lines += [
        "",
        (
            f"BLOCKING: {', '.join(str(b['number']) + ' ' + b['verdict'] for b in blocks)}"
            if blocks
            else "No blocking verdict."
        ),
    ]
    return "\n".join(lines)
