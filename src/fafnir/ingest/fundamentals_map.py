"""
The SF1 statement map (plan ``fundamentals-sf1.md`` §4.3-4.4, ADR 0018; #87).

Which SF1 indicator fills which typed column of ``core.income_statement``,
``core.balance_sheet`` and ``core.cash_flow_statement``, and how TTM and the mart read
each one. Migration 0035 seeds ``ref.statement_line_item`` with the same rows, and a
test holds the two equal: a correction here needs a migration, and cannot drift.

What is typed and what is not (ADR 0019):

* **Typed:** SF1's statement indicators, money as ``NUMERIC(24,2)`` and share counts
  as ``NUMERIC(24,4)``. ``shares_outstanding`` is the filing's cover-page count
  (``sharesbas``), from first-reported rows only.
* **Not typed:** per-share values (``eps``, ``epsdil``, ``epsusd``, ``dps``), which
  SF1 holds in today's share terms to 3 decimals, so the mart derives them; the
  ``*usd`` conversions; and SF1's metrics, including the columns measured on a row's
  ``date``. Those go to ``extra``.

``item_class`` decides what TTM may sum: a ``flow`` sums over four quarters, a
``stock`` takes the latest quarter, and a ``share_count`` takes the latest quarter.
``sign_convention`` is as measured (#83): cash-flow lines are signed as cash moves (a
capital expenditure is negative), and expenses are positive.

``fmp_field`` is the FMP payload's spelling of the build prompt's §4.3 counterpart,
where the probe's map found one, kept for a cross-vendor check. It is the camelCase of
that column, not read from an FMP payload: a check that relies on it confirms it first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: The statements, as ``ref.statement_line_item.statement`` names them, and their tables.
TABLES = {
    "income": "core.income_statement",
    "balance_sheet": "core.balance_sheet",
    "cash_flow": "core.cash_flow_statement",
}

ITEM_CLASSES = ("flow", "stock", "per_share", "share_count", "memo")
SIGN_CONVENTIONS = ("positive_is_inflow", "positive_is_outflow", "signed")

#: Column types by item class.
MONEY = "NUMERIC(24,2)"
SHARES = "NUMERIC(24,4)"


@dataclass(frozen=True)
class LineItem:
    column: str
    vendor_field: str
    item_class: str
    sign_convention: str
    is_subtotal: bool
    fmp_field: Optional[str]
    description: str

    @property
    def sql_type(self) -> str:
        return SHARES if self.item_class == "share_count" else MONEY


def _item(
    column: str,
    vendor_field: str,
    item_class: str,
    sign: str,
    description: str,
    *,
    subtotal: bool = False,
    fmp: Optional[str] = None,
) -> LineItem:
    return LineItem(column, vendor_field, item_class, sign, subtotal, fmp, description)


IN, OUT, SIGNED = SIGN_CONVENTIONS

INCOME = (
    _item("revenue", "revenue", "flow", IN, "Revenue for the period", fmp="revenue"),
    _item(
        "cost_of_revenue",
        "cor",
        "flow",
        OUT,
        "Cost of the goods and services sold",
        fmp="costOfRevenue",
    ),
    _item(
        "gross_profit",
        "gp",
        "flow",
        SIGNED,
        "Revenue less cost of revenue",
        subtotal=True,
        fmp="grossProfit",
    ),
    _item(
        "research_and_development",
        "rnd",
        "flow",
        OUT,
        "Research and development expense; 0 when the filing does not carry it",
        fmp="researchAndDevelopmentExpenses",
    ),
    _item(
        "selling_general_and_administrative",
        "sgna",
        "flow",
        OUT,
        "Selling, general and administrative expense",
        fmp="sellingGeneralAndAdministrativeExpenses",
    ),
    _item(
        "operating_expenses",
        "opex",
        "flow",
        OUT,
        "Operating expenses: SG&A, R&D and other operating costs",
        subtotal=True,
        fmp="operatingExpenses",
    ),
    _item(
        "operating_income",
        "opinc",
        "flow",
        SIGNED,
        "Operating income",
        subtotal=True,
        fmp="operatingIncome",
    ),
    _item(
        "interest_expense",
        "intexp",
        "flow",
        OUT,
        "Interest expense; 0 when the filing does not carry it",
        fmp="interestExpense",
    ),
    _item(
        "ebit",
        "ebit",
        "flow",
        SIGNED,
        "Earnings before interest and tax (net income + tax + interest)",
        subtotal=True,
        fmp="ebit",
    ),
    _item(
        "income_before_tax",
        "ebt",
        "flow",
        SIGNED,
        "Earnings before tax (net income + tax)",
        subtotal=True,
        fmp="incomeBeforeTax",
    ),
    _item(
        "income_tax_expense",
        "taxexp",
        "flow",
        OUT,
        "Income tax expense; a benefit is negative",
        fmp="incomeTaxExpense",
    ),
    _item(
        "consolidated_income",
        "consolinc",
        "flow",
        SIGNED,
        "Income after tax of the consolidated entity, before noncontrolling interest",
        subtotal=True,
    ),
    _item(
        "net_income_noncontrolling",
        "netincnci",
        "flow",
        SIGNED,
        "Net income attributable to noncontrolling interests",
    ),
    _item(
        "net_income_discontinued",
        "netincdis",
        "flow",
        SIGNED,
        "Net income (loss) from discontinued operations",
        fmp="netIncomeFromDiscontinuedOperations",
    ),
    _item(
        "net_income",
        "netinc",
        "flow",
        SIGNED,
        "Net income attributable to the parent",
        subtotal=True,
        fmp="netIncome",
    ),
    _item(
        "preferred_dividends",
        "prefdivis",
        "flow",
        OUT,
        "Preferred dividends charged against net income",
    ),
    _item(
        "net_income_common",
        "netinccmn",
        "flow",
        SIGNED,
        "Net income available to common shareholders; the numerator of EPS",
        subtotal=True,
    ),
    _item(
        "weighted_average_shares",
        "shareswa",
        "share_count",
        SIGNED,
        "Weighted average basic shares of the period, in the version's share terms",
        fmp="weightedAverageShsOut",
    ),
    _item(
        "weighted_average_shares_diluted",
        "shareswadil",
        "share_count",
        SIGNED,
        "Weighted average diluted shares of the period, in the version's share terms",
        fmp="weightedAverageShsOutDil",
    ),
)

BALANCE_SHEET = (
    _item(
        "cash_and_equivalents",
        "cashneq",
        "stock",
        SIGNED,
        "Cash and cash equivalents",
        fmp="cashAndCashEquivalents",
    ),
    _item(
        "investments_current",
        "investmentsc",
        "stock",
        SIGNED,
        "Current investments",
        fmp="shortTermInvestments",
    ),
    _item(
        "investments_noncurrent",
        "investmentsnc",
        "stock",
        SIGNED,
        "Non-current investments",
        fmp="longTermInvestments",
    ),
    _item(
        "investments_total",
        "investments",
        "stock",
        SIGNED,
        "Current and non-current investments",
        subtotal=True,
        fmp="totalInvestments",
    ),
    _item(
        "receivables",
        "receivables",
        "stock",
        SIGNED,
        "Trade and non-trade receivables",
        fmp="netReceivables",
    ),
    _item("inventory", "inventory", "stock", SIGNED, "Inventory", fmp="inventory"),
    _item("tax_assets", "taxassets", "stock", SIGNED, "Tax assets", fmp="taxAssets"),
    _item(
        "total_current_assets",
        "assetsc",
        "stock",
        SIGNED,
        "Current assets; NULL for a balance sheet that is not classified",
        subtotal=True,
        fmp="totalCurrentAssets",
    ),
    _item(
        "property_plant_equipment_net",
        "ppnenet",
        "stock",
        SIGNED,
        "Property, plant and equipment, net",
        fmp="propertyPlantEquipmentNet",
    ),
    _item(
        "goodwill_and_intangibles",
        "intangibles",
        "stock",
        SIGNED,
        "Goodwill and intangible assets",
        fmp="goodwillAndIntangibleAssets",
    ),
    _item(
        "total_noncurrent_assets",
        "assetsnc",
        "stock",
        SIGNED,
        "Non-current assets; NULL for a balance sheet that is not classified",
        subtotal=True,
        fmp="totalNonCurrentAssets",
    ),
    _item(
        "total_assets",
        "assets",
        "stock",
        SIGNED,
        "Total assets",
        subtotal=True,
        fmp="totalAssets",
    ),
    _item(
        "payables",
        "payables",
        "stock",
        SIGNED,
        "Trade and non-trade payables",
        fmp="totalPayables",
    ),
    _item(
        "deferred_revenue",
        "deferredrev",
        "stock",
        SIGNED,
        "Deferred revenue",
        fmp="deferredRevenue",
    ),
    _item(
        "deposit_liabilities",
        "deposits",
        "stock",
        SIGNED,
        "Deposit liabilities (banks)",
    ),
    _item(
        "debt_current",
        "debtc",
        "stock",
        SIGNED,
        "Current debt; NULL for a balance sheet that is not classified",
        fmp="shortTermDebt",
    ),
    _item(
        "debt_noncurrent",
        "debtnc",
        "stock",
        SIGNED,
        "Non-current debt; NULL for a balance sheet that is not classified",
        fmp="longTermDebt",
    ),
    _item(
        "total_debt",
        "debt",
        "stock",
        SIGNED,
        "Current and non-current debt",
        subtotal=True,
        fmp="totalDebt",
    ),
    _item(
        "tax_liabilities",
        "taxliabilities",
        "stock",
        SIGNED,
        "Tax liabilities",
    ),
    _item(
        "total_current_liabilities",
        "liabilitiesc",
        "stock",
        SIGNED,
        "Current liabilities; NULL for a balance sheet that is not classified",
        subtotal=True,
        fmp="totalCurrentLiabilities",
    ),
    _item(
        "total_noncurrent_liabilities",
        "liabilitiesnc",
        "stock",
        SIGNED,
        "Non-current liabilities; NULL for a balance sheet that is not classified",
        subtotal=True,
        fmp="totalNonCurrentLiabilities",
    ),
    _item(
        "total_liabilities",
        "liabilities",
        "stock",
        SIGNED,
        "Total liabilities",
        subtotal=True,
        fmp="totalLiabilities",
    ),
    _item(
        "accumulated_other_comprehensive_income",
        "accoci",
        "stock",
        SIGNED,
        "Accumulated other comprehensive income (loss)",
        fmp="accumulatedOtherComprehensiveIncomeLoss",
    ),
    _item(
        "retained_earnings",
        "retearn",
        "stock",
        SIGNED,
        "Retained earnings (deficit)",
        fmp="retainedEarnings",
    ),
    _item(
        "total_equity_parent",
        "equity",
        "stock",
        SIGNED,
        "Shareholders' equity attributable to the parent; no noncontrolling interest",
        subtotal=True,
        fmp="totalStockholdersEquity",
    ),
    _item(
        "shares_outstanding",
        "sharesbas",
        "share_count",
        SIGNED,
        "Shares outstanding on the filing's cover, in the version's share terms; "
        "first-reported rows only",
    ),
)

CASH_FLOW = (
    _item(
        "net_cash_operating",
        "ncfo",
        "flow",
        IN,
        "Net cash from operating activities",
        subtotal=True,
        fmp="netCashProvidedByOperatingActivities",
    ),
    _item(
        "depreciation_amortization",
        "depamor",
        "flow",
        IN,
        "Depreciation, amortization and accretion, added back",
        fmp="depreciationAndAmortization",
    ),
    _item(
        "share_based_compensation",
        "sbcomp",
        "flow",
        IN,
        "Share-based compensation, added back",
        fmp="stockBasedCompensation",
    ),
    _item(
        "capital_expenditure",
        "capex",
        "flow",
        IN,
        "Capital expenditure; an outflow is negative",
        fmp="capitalExpenditure",
    ),
    _item(
        "net_cash_business_acquisitions",
        "ncfbus",
        "flow",
        IN,
        "Net cash from business acquisitions and disposals",
        fmp="acquisitionsNet",
    ),
    _item(
        "net_cash_investment_acquisitions",
        "ncfinv",
        "flow",
        IN,
        "Net cash from purchases and sales of investments",
    ),
    _item(
        "net_cash_investing",
        "ncfi",
        "flow",
        IN,
        "Net cash from investing activities",
        subtotal=True,
        fmp="netCashProvidedByInvestingActivities",
    ),
    _item(
        "net_debt_issuance",
        "ncfdebt",
        "flow",
        IN,
        "Net cash from issuing and repaying debt",
        fmp="netDebtIssuance",
    ),
    _item(
        "net_equity_issuance",
        "ncfcommon",
        "flow",
        IN,
        "Net cash from issuing and repurchasing equity; a buyback is negative",
        fmp="netCommonStockIssuance",
    ),
    _item(
        "dividends_paid",
        "ncfdiv",
        "flow",
        IN,
        "Dividends and other cash distributions paid; an outflow is negative",
        fmp="netDividendsPaid",
    ),
    _item(
        "net_cash_financing",
        "ncff",
        "flow",
        IN,
        "Net cash from financing activities",
        subtotal=True,
        fmp="netCashProvidedByFinancingActivities",
    ),
    _item(
        "fx_effect_on_cash",
        "ncfx",
        "flow",
        IN,
        "Effect of exchange-rate changes on cash",
        fmp="effectOfForexChangesOnCash",
    ),
    _item(
        "net_change_in_cash",
        "ncf",
        "flow",
        IN,
        "Net change in cash and equivalents",
        subtotal=True,
        fmp="netChangeInCash",
    ),
)

#: Every typed line item, by statement.
STATEMENTS: dict[str, tuple[LineItem, ...]] = {
    "income": INCOME,
    "balance_sheet": BALANCE_SHEET,
    "cash_flow": CASH_FLOW,
}

#: The columns every statement table shares (plan §4.2): these come first, the line
#: items next, and ``COMMON_TAIL`` last.
COMMON_HEAD = (
    "security_id",
    "vendor_key",
    "period_type",
    "fiscal_date",
    "calendar_date",
    "fiscal_year",
    "fiscal_period",
    "period_start_date",
    "version_no",
    "known_from",
    "known_to",
    "knowledge_source",
    "filing_date",
    "loaded_at",
    "superseded_at",
    "reported_currency",
)
COMMON_TAIL = ("content_hash", "extra", "source", "ingestion_run_id")


def columns(statement: str) -> tuple[str, ...]:
    """A statement table's columns, in table order."""
    items = tuple(i.column for i in STATEMENTS[statement])
    return COMMON_HEAD + items + COMMON_TAIL


def by_vendor_field(statement: str) -> dict[str, LineItem]:
    """``{sf1_indicator: LineItem}`` for one statement: the loader's lookup."""
    return {i.vendor_field: i for i in STATEMENTS[statement]}
