> Imported from the Claude project (`claude/fafnir-fundamentals-build-prompt.md`, 2026-08-27). Written for FMP;
> retargeted to Sharadar SF1 by `doc/plans/fundamentals-sf1.md` (SA-0801). Migration and ADR numbers in
> this document are historical.

# Build Prompt — `fafnir` Fundamentals Expansion (v0.6.0)

> **Role.** You are the database engineer for `fafnir`, a research-grade financial
> market data warehouse (PostgreSQL 16 + Python 3.11+). Repository:
> `github.com/rtrimble13/fafnir`.
>
> **Task.** Ship the **Fundamentals** milestone from `doc/extending.md`: bitemporal
> company financial statements sourced from FMP, back to 1990 where FMP carries
> them, plus a derived point-in-time factor layer in `mart`.
>
> **Non-negotiable.** This expansion is **purely additive**. No existing table,
> column, view, grant, CLI command, or `duk` behaviour may change semantics. Every
> existing test must still pass unmodified except where this document explicitly
> says otherwise.

---

## 0. Read before you write

Do not begin coding until you have read and can restate the commitments in:

| File | Why it binds this work |
|---|---|
| `doc/architecture.md` | Medallion layering, the **three timelines**, role model |
| `doc/adr/0002-surrogate-security-id-and-bitemporal-readiness.md` | This build is the promise that ADR made |
| `doc/adr/0001-raw-prices-plus-adjustment-factors.md` | Derive-on-read is the house style; ratios follow it |
| `doc/adr/0004-unadjusted-price-feed.md` | The double-adjustment failure mode. **It has an exact analogue in fundamentals** (§4.6) |
| `doc/data_dictionary.md` | The contract format your new tables must be documented in |
| `doc/ingestion.md` | Land-raw → validate → quarantine → upsert → watermark |
| `src/fafnir/ingest/daily_price.py` | The loader you are mirroring, including its `Decimal` discipline |
| `src/fafnir/sources/fmp.py` | Throttling, byte metering, endpoint constants, the row-cap lesson |
| `src/fafnir/ingest/runlog.py`, `src/fafnir/db/repository.py` | Lineage and upsert idioms |
| `sql/migrations/0010_trim_company_profile.up.sql` | The most recent migration; yours start at `0011` |

---

## 1. Scope

### In scope

Three standardized FMP statement families, **annual and quarterly**, for the full
US universe already in `core.security` (**including delisted names** — omitting
them reintroduces the survivorship bias the warehouse exists to avoid):

- `income-statement`
- `balance-sheet-statement`
- `cash-flow-statement`

Plus everything needed to make them usable: bitemporal versioning, a point-in-time
read seam, TTM rollups, and a factor panel.

### Explicitly out of scope (do not build)

- **Vendor-computed metrics** (`key-metrics`, `ratios`, `financial-growth`,
  `enterprise-values`, `*-ttm`). fafnir derives its own metrics from the statements
  it stores, on read, in `mart`. Vendor ratios are not point-in-time stable (the
  vendor silently recomputes history) and cannot be reconciled, which puts them in
  conflict with the correctness pillar. **Design the schema so they could be added
  later as a clearly-labelled `core.vendor_metric` table, but do not add one now.**
- Analyst estimates, earnings surprises, segment data, shares-float, transcripts.
- Economic series (FRED/BLS/BEA) — a separate milestone.
- Any change to price, corporate-action, or adjustment-factor handling.

### Sources of every derived quantity

Because vendor metrics are out of scope, every ratio must be computable from
`core` alone. Confirm this holds as you map fields:

| Needed for | Comes from |
|---|---|
| Share count | `weighted_average_shs_out` / `..._dil` on the income statement |
| Market cap (PIT) | shares × `core.daily_price.close` **in matching share terms** (§4.6) |
| Enterprise value | market cap + `total_debt` + `minority_interest` + preferred − `cash_and_short_term_investments` |
| Dividend / buyback yield | cash-flow `common_dividends_paid`, `common_stock_repurchased` |
| Adjusted prices | existing `mart.v_daily_price_adjusted` — **do not re-derive** |

If any planned factor cannot be sourced this way, say so and stop rather than
inventing a proxy.

---

## 2. Before the first line of schema: probe FMP

The repo's own precedent (`fafnir source probe-prices`, and the "Verify before the
first production backfill" note in `doc/ingestion.md`) is that vendor behaviour is
established empirically, not assumed. Do the same here.

**Deliverable: `fafnir source probe-fundamentals`** — a read-only, few-request
diagnostic in `src/fafnir/sources/probe.py` (mirroring `probe_prices` /
`format_report`) that reports, for a probe symbol set of at least
`AAPL, MSFT, GE, KO, JPM, XOM` (long histories, different fiscal calendars, one
financial):

1. **History depth.** Earliest `date` returned for annual and for quarterly, per
   statement. **This answers the 1990 question empirically.** Report per symbol
   and as a summary verdict: `reaches_1990` / `reaches_<year>` / `shallow`.
2. **Row-cap behaviour.** The exact analogue of `EOD_MAX_ROWS`. Call the endpoint
   with no `limit`, with `limit=10`, and with `limit=400`; report row counts.
   Determine (a) the **default** limit when the parameter is omitted, and (b)
   whether a large `limit` is honoured or silently capped. Establish this
   definitively — a silently-truncated statement history is indistinguishable from
   a company that simply has not existed for long.
3. **Field inventory.** The full set of JSON keys returned, per statement,
   diffed against the canonical column map in §4.2. Report `unmapped_keys` and
   `missing_expected_keys`. These become the seed for `ref.statement_line_item`.
4. **Date semantics.** Whether `filingDate` and `acceptedDate` are populated, and
   **how far back they remain populated** — pre-EDGAR-electronic filings routinely
   have null or synthetic filing dates. Report the earliest fiscal year with a
   non-null filing date per symbol. This drives the fallback rule in §4.5.
5. **Fiscal calendar.** `fiscalYear`/`calendarYear` vs `period` vs `date` for a
   non-December fiscal year-end (`AAPL` FY ends September). Confirm you can
   reconstruct the fiscal label without inferring it from the date.
6. **Quarterly completeness.** Whether Q4 is returned as its own quarterly record
   or must be derived as `FY − Q1 − Q2 − Q3` (§5.2).
7. **Cash-flow period semantics.** Whether quarterly cash-flow figures are
   **discrete-quarter or year-to-date cumulative**. Some filers report YTD. Test
   by checking whether `Q1+Q2+Q3+Q4 ≈ FY` for operating cash flow. Report the
   verdict per symbol.
8. **Bulk endpoints.** Probe for `income-statement-bulk` (and siblings) taking
   `year` + `period`. If available on the Professional plan, this collapses
   ~126,000 per-symbol requests into a few hundred, which changes the backfill
   plan entirely (§7). Report availability, response shape, and whether the bulk
   payload's fields match the per-symbol payload's.
9. **Reported currency.** How many probe symbols report in a currency other than
   their trading currency.

The probe must **write nothing** and must exit non-zero on a verdict that would
make the backfill unsafe (row-cap unresolved, or field inventory wildly divergent
from §4.2).

**Report the probe's findings back before proceeding.** If depth stops at, say,
1996 rather than 1990, that is the answer — record it in the docs and set the
backfill floor accordingly rather than pretending otherwise.

---

## 3. Architectural commitments this build must honour

1. **Purely additive.** New migrations `0011`+. Never edit an applied migration
   (`fafnir db migrate` detects checksum drift; the `SUPERSEDED_CHECKSUMS` escape
   hatch in `src/fafnir/db/migrate.py` applies only to revisions that leave an
   already-migrated database *identical* — this build qualifies for none of them).
2. **Land raw, then transform.** `landing.fmp_raw` is already generic
   (`endpoint`, `symbol`, `params`, `payload`). **Reuse it. Do not add a landing
   table.**
3. **Validate at the boundary; quarantine, never drop.** Every rejected record
   writes an `ops.data_quality_flag` with a specific `check_name`.
4. **Idempotent.** Re-running any load converges to the same state — including
   **not** creating a new bitemporal version when nothing changed (§4.4).
5. **Exact arithmetic.** `NUMERIC` everywhere; parse via `Decimal(str(value))`
   exactly as `daily_price._decimal` does. **Never** hand psycopg a float.
   Quantize to the target column's scale *before* judging a value, because
   Postgres rounds to scale and only then evaluates `CHECK` constraints — this is
   the sub-resolution bug documented in `daily_price.py`; the same trap exists for
   `NUMERIC(24,2)` money columns.
6. **Derive on read.** No stored ratio is the source of truth. Every metric is a
   `mart` view or function over `core`. The one exception is the materialized
   factor panel (§5.3), which is a **cache of a view**, rebuildable and refreshed
   on schedule — the same status `mart.security_latest` already has.
7. **Lineage on every load.** `RunLog` context manager; `ingestion_run_id` on
   every row.
8. **Least privilege.** New `core`/`mart` objects inherit `SELECT` for
   `fafnir_read`/`fafnir_app` from the `ALTER DEFAULT PRIVILEGES` in `0001` —
   *only if created by the same role*. Add **explicit, idempotent `GRANT SELECT`**
   statements anyway as belt-and-braces, and note that default privileges cover
   `TABLES` and `SEQUENCES` but **not `FUNCTIONS`** (Postgres grants `EXECUTE` to
   `PUBLIC` by default, so the set-returning function in §5.1 is reachable given
   schema `USAGE`, but state this explicitly in the migration comment rather than
   leaving it to be rediscovered).
9. **Commit at the unit of work.** One symbol (its landing payload, rows, DQ
   flags, watermark) is the unit, exactly as `load_prices` does — that is what
   makes a multi-hour backfill genuinely resumable.

---

## 4. Schema — migration `0011_fundamentals.up.sql` / `.down.sql`

### 4.1 Physical shape: hybrid typed columns + JSONB overflow

Each statement gets its own table with **named, typed `NUMERIC` columns** for the
stable contract, plus an `extra JSONB NOT NULL DEFAULT '{}'` column holding any
vendor field not in the canonical map.

Rationale (record it in the migration header):

- Typed columns give the constrained contract, index-ability, and cross-field
  `CHECK`s that make this warehouse research-grade. An EAV/long table would
  surrender all three and force a pivot into every query.
- The JSONB overflow means a vendor adding a field **never breaks a load and never
  loses data** — it lands in `extra` and surfaces in a DQ report (§6) so the
  taxonomy can be promoted to a typed column in a later additive migration.
- **Never** read `extra` from a `mart` view. It is a staging area for fields not
  yet part of the contract, not a second contract.

**Not partitioned.** Expected size is ~5–15M rows per statement table across a
21k-symbol universe × 36 years × (annual + quarterly) × restatement versions —
comfortably a single table. But keep `fiscal_date` in the primary key so a future
range partition (or TimescaleDB hypertable) is a migration and not a grain change,
exactly as ADR 0003 arranged for `core.daily_price`. State this in the comment.

### 4.2 Common columns (identical across all three statement tables)

```sql
security_id        BIGINT      NOT NULL REFERENCES core.security (security_id),
period_type        TEXT        NOT NULL CHECK (period_type IN ('annual','quarter')),
fiscal_date        DATE        NOT NULL,   -- period END date (observation timeline)
fiscal_year        INTEGER     NOT NULL,   -- vendor's fiscalYear/calendarYear, NOT derived
fiscal_period      TEXT        NOT NULL CHECK (fiscal_period IN ('FY','Q1','Q2','Q3','Q4')),
period_start_date  DATE,                   -- NULL when not derivable; see §5.2
filing_date        DATE,                   -- vendor knowledge timeline
accepted_date      TIMESTAMPTZ,            -- finer-grained knowledge timestamp
knowledge_date     DATE        NOT NULL,   -- resolved PIT date; see §4.5
knowledge_source   TEXT        NOT NULL    -- 'accepted' | 'filed' | 'estimated'
                     CHECK (knowledge_source IN ('accepted','filed','estimated')),
reported_currency  TEXT,
cik                TEXT,
valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),   -- fafnir load timeline
valid_to           TIMESTAMPTZ,                          -- NULL = current version
version_no         INTEGER     NOT NULL DEFAULT 1,
content_hash       TEXT        NOT NULL,   -- SHA-256 over the canonical value set
is_restatement     BOOLEAN     NOT NULL DEFAULT FALSE,   -- version_no > 1
extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
source             TEXT        NOT NULL DEFAULT 'fmp',
ingestion_run_id   BIGINT      REFERENCES ops.ingestion_run (ingestion_run_id),
loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Grain / primary key:**
`(security_id, period_type, fiscal_date, valid_from)`

with a **partial unique index** enforcing exactly one live version per period:

```sql
CREATE UNIQUE INDEX ux_<t>_current
    ON core.<t> (security_id, period_type, fiscal_date)
    WHERE valid_to IS NULL;
```

**Required indexes** (justify each in a comment; do not add speculative ones):

```sql
-- point-in-time lookup: "latest version known as of D"
CREATE INDEX ix_<t>_pit ON core.<t> (security_id, period_type, knowledge_date DESC, fiscal_date DESC);
-- panel builds sweep by knowledge date across the universe
CREATE INDEX ix_<t>_knowledge ON core.<t> (knowledge_date) WHERE valid_to IS NULL;
-- restatement forensics
CREATE INDEX ix_<t>_restated ON core.<t> (security_id, fiscal_date) WHERE is_restatement;
```

**Constraints:**

```sql
CHECK (valid_to IS NULL OR valid_to > valid_from)
CHECK ((period_type = 'annual') = (fiscal_period = 'FY'))
CHECK (knowledge_date >= fiscal_date)                      -- a filing cannot precede its period end
CHECK (period_start_date IS NULL OR period_start_date < fiscal_date)
```

The `knowledge_date >= fiscal_date` check is a **look-ahead tripwire at the
storage layer**. If FMP ever returns a filing date before the period it reports
on, that record is quarantined rather than silently poisoning every backtest built
on the panel.

### 4.3 Typed line items

Map FMP's stable payload to snake_case columns. **Money: `NUMERIC(24,2)`**
(matching `core.security.market_cap_usd`; comfortably holds a $4T balance sheet).
**Share counts: `NUMERIC(24,4)`** — weighted averages are fractional.
**Per-share: `NUMERIC(20,6)`** (matching price scale).

All line-item columns are **nullable** — a genuinely absent line item is not the
same as zero, and conflating them corrupts every ratio with that item in the
denominator. Never `COALESCE(x, 0)` at the ingest boundary.

> **Verify every field name against the §2 probe before committing the migration.**
> The list below is the target contract, not a confirmed inventory. Centralize the
> field→column map in one module-level dict per statement so a correction is a
> one-line change (the pattern `FMPClient` already uses for endpoint constants).

**`core.income_statement`** — revenue, cost_of_revenue, gross_profit,
research_and_development_expenses, general_and_administrative_expenses,
selling_and_marketing_expenses, selling_general_and_administrative_expenses,
other_expenses, operating_expenses, cost_and_expenses, net_interest_income,
interest_income, interest_expense, depreciation_and_amortization, ebitda, ebit,
non_operating_income_excluding_interest, operating_income,
total_other_income_expenses_net, income_before_tax, income_tax_expense,
net_income_from_continuing_operations, net_income_from_discontinued_operations,
other_adjustments_to_net_income, net_income, net_income_deductions,
bottom_line_net_income, eps, eps_diluted, weighted_average_shs_out,
weighted_average_shs_out_dil.

**`core.balance_sheet`** — cash_and_cash_equivalents, short_term_investments,
cash_and_short_term_investments, net_receivables, accounts_receivables,
other_receivables, inventory, prepaids, other_current_assets,
total_current_assets, property_plant_equipment_net, goodwill, intangible_assets,
goodwill_and_intangible_assets, long_term_investments, tax_assets,
other_non_current_assets, total_non_current_assets, other_assets, total_assets,
total_payables, account_payables, other_payables, accrued_expenses,
short_term_debt, capital_lease_obligations_current, tax_payables,
deferred_revenue, other_current_liabilities, total_current_liabilities,
long_term_debt, capital_lease_obligations_non_current,
deferred_revenue_non_current, deferred_tax_liabilities_non_current,
other_non_current_liabilities, total_non_current_liabilities, other_liabilities,
capital_lease_obligations, total_liabilities, treasury_stock, preferred_stock,
common_stock, retained_earnings, additional_paid_in_capital,
accumulated_other_comprehensive_income_loss, other_total_stockholders_equity,
total_stockholders_equity, total_equity, minority_interest,
total_liabilities_and_total_equity, total_investments, total_debt, net_debt.

**`core.cash_flow_statement`** — net_income, depreciation_and_amortization,
deferred_income_tax, stock_based_compensation, change_in_working_capital,
accounts_receivables, inventory, accounts_payables, other_working_capital,
other_non_cash_items, net_cash_provided_by_operating_activities,
investments_in_property_plant_and_equipment, acquisitions_net,
purchases_of_investments, sales_maturities_of_investments,
other_investing_activities, net_cash_provided_by_investing_activities,
net_debt_issuance, long_term_net_debt_issuance, short_term_net_debt_issuance,
net_stock_issuance, net_common_stock_issuance, common_stock_issuance,
common_stock_repurchased, net_preferred_stock_issuance, net_dividends_paid,
common_dividends_paid, preferred_dividends_paid, other_financing_activities,
net_cash_provided_by_financing_activities, effect_of_forex_changes_on_cash,
net_change_in_cash, cash_at_end_of_period, cash_at_beginning_of_period,
operating_cash_flow, capital_expenditure, free_cash_flow, income_taxes_paid,
interest_paid.

### 4.4 `ref.statement_line_item` — the line-item taxonomy

A small reference table, seeded via `sql/seeds/0002_statement_line_items.sql`,
that makes the contract self-describing for researchers, DQ checks, and the future
MCP server:

```sql
statement        TEXT NOT NULL,   -- 'income' | 'balance_sheet' | 'cash_flow'
column_name      TEXT NOT NULL,
fmp_field        TEXT,            -- vendor spelling; NULL for fafnir-only columns
item_class       TEXT NOT NULL CHECK (item_class IN ('flow','stock','per_share','share_count','memo')),
sign_convention  TEXT NOT NULL CHECK (sign_convention IN ('positive_is_inflow','positive_is_outflow','signed')),
is_subtotal      BOOLEAN NOT NULL DEFAULT FALSE,
description      TEXT,
PRIMARY KEY (statement, column_name)
```

`item_class` is **load-bearing, not documentation**: the TTM rollup (§5.2) reads
it to decide what may be summed. `sign_convention` matters because FMP reports
`capital_expenditure` and `common_stock_repurchased` as negatives — a factor that
assumes positive magnitudes silently flips sign.

### 4.5 The three timelines, resolved

`doc/architecture.md` already names them. Bind them explicitly here:

| Timeline | Column | Meaning |
|---|---|---|
| Observation | `fiscal_date` | the period the numbers describe |
| Knowledge | `accepted_date` → `filing_date` → `knowledge_date` | when the market could have known |
| Load | `valid_from` / `valid_to` / `loaded_at` | when *fafnir* learned it |

**`knowledge_date` resolution, in strict order:**

1. `date(accepted_date)` if present — the SEC acceptance timestamp is the truest
   "the market can see this now" moment. Set `knowledge_source='accepted'`.
2. else `filing_date`. Set `knowledge_source='filed'`.
3. else `fiscal_date + fundamentals_filing_lag_days` (new config key, default
   **90** for annual and **45** for quarterly — the statutory large-filer deadlines,
   deliberately conservative). Set `knowledge_source='estimated'` **and** write an
   `ops.data_quality_flag` (`check_name='fundamentals_estimated_knowledge_date'`,
   severity `info`).

**`knowledge_source='estimated'` must be visible in every PIT read**, and the
factor panel must expose it as a column so a researcher can exclude estimated-date
rows and measure how much the result moves. Expect this to bite hardest in the
early 1990s, precisely the history being backfilled. Silently guessing a filing
date and not saying so is the single most likely way this build produces
plausible, wrong, unfalsifiable backtests.

### 4.6 Restatement versioning (SCD-2 on the load timeline)

**FMP's statement endpoints return only the current version of each period** —
they do not carry restatement history. So the bitemporal history is something
fafnir *accumulates*, not something it downloads. This must be said plainly in the
ADR: history before fafnir's first load is single-versioned by construction, and
`version_no = 1` on an old period means "as FMP reports it today", not "as
originally filed".

Load algorithm, per `(security_id, period_type, fiscal_date)`:

1. Compute `content_hash` = SHA-256 over the canonical, ordered set of typed
   line-item values **plus `filing_date`/`accepted_date`** (a re-file with
   identical numbers is still a new knowledge event), rendered as
   `Decimal` strings with `None` distinguished from `0`.
2. Look up the live row (`valid_to IS NULL`).
3. **No live row** → insert `version_no = 1`, `is_restatement = FALSE`.
4. **Live row, same `content_hash`** → **do nothing**. Not an UPDATE, not a
   `loaded_at` bump. This is what makes a re-run a true no-op and keeps the daily
   job from manufacturing spurious restatements.
5. **Live row, different `content_hash`** → in one transaction:
   `UPDATE ... SET valid_to = now() WHERE valid_to IS NULL`, then `INSERT` the new
   version with `version_no = prev + 1`, `is_restatement = TRUE`, and write a DQ
   flag (`check_name='fundamentals_restatement'`, severity `warn`, `detail`
   carrying the fields that moved and their percentage change).

**Never `UPDATE` a line-item value in place.** Restatements version; they do not
overwrite. A `mart` reader filtering `valid_to IS NULL` always sees fafnir's
current best view; a reader filtering `valid_from <= T AND (valid_to IS NULL OR
valid_to > T)` reconstructs what fafnir believed at time `T`.

### 4.7 The per-share adjustment trap (read ADR 0004 first)

This is the fundamentals analogue of the double-adjustment bug, and it is the
highest-risk correctness issue in this build.

`eps`, `eps_diluted`, and `weighted_average_shs_out` are **as-reported, in the
share terms of the period they describe**. `core.daily_price` is **raw**, in the
share terms of the day it traded. `mart.v_daily_price_adjusted` is **back-adjusted
to today's share terms**.

Therefore:

- `price_raw(t) / eps_as_reported(period)` is **correct** when `t` sits in the same
  split era as the period — and wrong across any intervening split.
- `price_adjusted(t) / eps_as_reported(period)` is **wrong by the cumulative split
  ratio**. For AAPL (112:1 since 1990) that is a 112× error in P/E, and it produces
  a plausible-looking number, not an obvious one.

**Rules:**

1. Store per-share and share-count figures **exactly as reported. Do not adjust at
   ingest.** (Same reasoning as storing raw OHLCV.)
2. Any per-share metric in `mart` must apply `core.adjustment_factor` to the
   **reported figure**, using the factor effective at the statement's
   `fiscal_date`, so that the price and the per-share figure are expressed in the
   same share terms. Write this as one reusable expression, once.
3. **Prefer aggregate-over-aggregate ratios**, which are split-immune by
   construction: `market_cap / net_income` instead of `price / eps`;
   `market_cap / total_stockholders_equity` instead of `price / book_per_share`.
   Make these the panel's primary definitions and expose per-share variants only
   where convention demands them.
4. Compute point-in-time market cap as
   `raw_close(t) × shares_outstanding_as_reported(latest period known at t)`, both
   in as-of-`t` share terms — **not** from `core.security.market_cap_usd`, which
   is a vendor snapshot explicitly commented "do not use for backtests".
5. **Ship a test that fails on this specific bug**: assert AAPL's computed P/E for
   a 1995 as-of date is within a plausible band (single- to low-double-digit), so a
   regression that mixes share eras is caught by CI rather than by a strategy that
   looks brilliant in backtest.

### 4.8 Currency

`reported_currency` may differ from the security's trading currency (ADRs report
in EUR/JPY/GBP). **Dividing a EUR revenue by a USD market cap is silent garbage.**

- Store statements in their reported currency. Do **not** convert (no FX series
  exists in this warehouse yet, and inventing one is out of scope).
- Every `mart` object that combines a statement value with a price value must
  either restrict to `reported_currency = core.security.currency` or expose a
  `currency_mismatch BOOLEAN` column that defaults the row's ratios to `NULL`.
  **Choose the second** — dropping the rows hides the problem; nulling the ratios
  while keeping the row makes the coverage gap measurable.
- DQ check: count and flag mismatches (§6).

---

## 5. The `mart` layer — migration `0012_fundamentals_marts.up.sql`

The read seam. `fafnir_app` (MCP, apps, `duk -S db`) sees only this.

### 5.1 Point-in-time statement access

**`mart.fn_statements_as_of(as_of DATE)`** — a `STABLE` set-returning function
returning, per `(security_id, period_type)`, the **latest fiscal period whose
`knowledge_date <= as_of`**, in the version fafnir believed at that time. A view
cannot take a parameter and the as-of date is the whole point, so this is a
function. Add `mart.fn_statements_as_of(as_of DATE, knowledge_as_of TIMESTAMPTZ)`
as the two-axis variant for reproducing a prior fafnir state.

**`mart.v_statements_current`** — the convenience view: `valid_to IS NULL`, latest
`fiscal_date` per `(security_id, period_type)`. For screening and dashboards, **not
for backtests**; say so in the `COMMENT ON VIEW`.

Both must join income + balance sheet + cash flow **on `fiscal_date`, never on
`knowledge_date`** — the three statements of one 10-K are one observation even when
their filing metadata differs slightly. Where one statement is missing for a
period, return the row with that statement's columns `NULL` (an outer join), and
expose `has_income`/`has_balance_sheet`/`has_cash_flow` booleans.

### 5.2 TTM rollups — `mart.v_fundamentals_ttm`

Trailing-twelve-month aggregation is where naive implementations break. Get these
right:

1. **Flows sum; stocks do not.** Read `item_class` from `ref.statement_line_item`.
   Income and cash-flow line items are `flow` → sum the last four quarters. Balance
   sheet items are `stock` → take the **most recent** quarter's value (and, where a
   ratio needs an average balance — ROE, ROA, asset turnover — use the average of
   the current and four-quarters-ago values, and say which in the column comment).
   **Never sum a balance-sheet item.**
2. **Require four contiguous, non-overlapping quarters.** Verify by `fiscal_date`
   spacing (roughly 80–100 days apart) and by four distinct `fiscal_period` labels.
   If the window is incomplete, the TTM value is **`NULL`, not a partial sum** — a
   3-quarter "TTM" is a 25% understatement that looks like a fundamentals shock.
   Expose `ttm_quarters_used` so the gap is visible.
3. **Derived Q4.** If the §2 probe shows Q4 is not filed as its own quarterly
   record, derive it for **flow items only** as `FY − Q1 − Q2 − Q3`, mark the row
   `is_derived_q4 = TRUE`, and **never** derive a balance-sheet Q4 (a stock value
   is not a residual). If Q4 is filed directly, prefer the filed record.
4. **YTD-cumulative cash flow.** If the probe finds filers reporting quarterly cash
   flow as year-to-date, de-cumulate (`Qn = YTD_n − YTD_{n-1}`) and flag those rows.
   If the probe finds this does not occur, add a DQ check that would catch it
   appearing later, and say in the doc that it was checked.
5. **Knowledge date of a TTM figure** is the **maximum** `knowledge_date` of its
   four constituent quarters. Not the minimum, not the latest quarter's. A TTM
   number is not knowable until its last component is filed.

### 5.3 The factor panel — `mart.fundamental_panel` (MATERIALIZED)

The deliverable that makes this build worth doing.

**Grain: `(security_id, as_of_date)` at month-end.** Month-end, not daily:
21k securities × 12 months × 36 years ≈ **9M rows**, which refreshes nightly
`CONCURRENTLY` in minutes. A daily grain would be ~190M rows and could not.
For daily as-of needs, ship **`mart.fn_fundamental_snapshot(as_of DATE)`** — the
same expression set evaluated on demand for one date. Define the factor
expressions **once** in the function or a shared view and have the matview select
from it, so the two can never drift.

`as_of_date` is the last **trading** day of each month per `ref.trading_calendar`,
not the calendar last day.

**Every panel row is built with `knowledge_date <= as_of_date`.** No exceptions,
no "close enough". Additionally support a configurable extra reporting lag
(`fundamentals_panel_extra_lag_days`, default **0**, since `knowledge_date` is
already a real filing date) so a researcher can test sensitivity to a conservative
lag without rebuilding the panel logic.

**Panel columns:**

*Keys & context:* `security_id`, `as_of_date`, `sector_id`, `industry_id`,
`exchange_code`, `is_actively_trading`, `reported_currency`, `currency_mismatch`,
`fiscal_date`, `period_type`, `knowledge_date`, `knowledge_source`,
`days_since_filing`, `is_derived_q4`, `ttm_quarters_used`, `version_no`.

*Market state:* `close_raw`, `close_adjusted`, `shares_outstanding_as_reported`,
`market_cap`, `enterprise_value`.

*Value:* `earnings_yield` (TTM net income / market cap), `book_to_price`,
`sales_to_price`, `cash_flow_to_price`, `fcf_yield`, `ev_to_ebitda`,
`ev_to_sales`, `ev_to_ebit`, `ev_to_fcf`.

*Quality:* `roe`, `roa`, `roic`, `gross_profitability` (gross profit / total
assets — Novy-Marx), `gross_margin`, `operating_margin`, `net_margin`,
`asset_turnover`, `accruals` (Sloan: (net income − operating cash flow) / average
total assets), `net_operating_assets`.

*Safety / leverage:* `debt_to_equity`, `net_debt_to_ebitda`, `interest_coverage`,
`current_ratio`, `quick_ratio`, `altman_z`, `piotroski_f`.

*Growth:* `revenue_growth_yoy`, `eps_growth_yoy`, `revenue_cagr_3y`,
`net_income_growth_yoy` — all computed from **periods known as of `as_of_date`**.

*Investment / issuance:* `asset_growth_1y`, `capex_to_sales`,
`net_share_issuance_1y` (Daniel–Titman; from the change in as-reported share count
**adjusted to common share terms** per §4.7 — an unadjusted share-count change
reads a 4:1 split as a 300% equity issuance).

*Payout:* `dividend_yield_ttm`, `buyback_yield_ttm`, `shareholder_yield`.

**Denominator discipline** — write this rule once and apply it mechanically:

- Any ratio whose denominator is `NULL`, zero, or **negative where negative is
  economically meaningless** (market cap, total assets, revenue, book equity for
  `book_to_price`) yields `NULL`.
- Negative *numerators* are kept as-is. Negative earnings yield is information;
  a negative P/E is not, which is why the panel expresses value factors as
  **yields (fundamental / price)** rather than multiples. Keep it that way.
- Do **not** winsorize, clip, or fill. Outlier treatment is a research decision
  made downstream on a documented raw panel, not a warehouse decision baked
  irreversibly into stored data.

`REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a unique index — create
`ux_fundamental_panel ON mart.fundamental_panel (security_id, as_of_date)`, plus
`(as_of_date)` and `(sector_id, as_of_date)` for cross-sectional sweeps.

### 5.4 Industry research — `mart.v_industry_fundamentals`

Cross-sectional aggregates over the panel by `(sector_id, as_of_date)` and
`(industry_id, as_of_date)`: `n_securities`, and median + 25th/75th percentile for
a named subset of panel factors. Use `percentile_cont` over non-`NULL` values only,
and expose `n_non_null` per metric so a median over four companies is not mistaken
for a sector norm. Suppress any group with `n_securities < 3`.

### 5.5 Do **not** modify `mart.security_latest`

`duk`'s screener (`src/duk/datasource/db.py::screen`) selects named columns from
it. Adding fundamentals there risks the screener and invites a matview rebuild in a
migration for no benefit. Instead add **`mart.security_fundamentals_latest`** — a
separate matview joining `core.security` to the current TTM figures — and let
`duk` opt into it. Additive by construction.

### 5.6 `refresh_marts` must be extended carefully

`src/fafnir/db/maintenance.py::refresh_marts` currently hardcodes
`mart.security_latest` and wraps it in a bare `except Exception` that falls back to
a non-concurrent refresh. Extend it to iterate a module-level list of matviews so
new ones are refreshed too — and while you are there, **narrow the exception
handling**: the current form would swallow a genuine failure on a new matview and
report success. Catch the specific "cannot refresh concurrently" case, fall back,
and re-raise anything else. Refresh in dependency order (`security_latest` →
`fundamental_panel` → `security_fundamentals_latest`). This is a behaviour
*improvement* to existing code — call it out explicitly in the PR description and
cover it with a test, since it is the one place this build touches a shipped path.

---

## 6. Ingestion — `src/fafnir/ingest/fundamentals.py`

Mirror `ingest/daily_price.py` in structure and rigour.

### 6.1 Source client additions (`sources/fmp.py`)

Add endpoint constants (`EP_INCOME_STATEMENT`, `EP_BALANCE_SHEET`,
`EP_CASH_FLOW`, and the bulk variants if the probe found them) and methods
returning parsed payloads. **Always pass an explicit `limit`.** After each call,
if `len(rows) >= requested_limit`, log a warning naming the symbol and statement —
this is the `EOD_MAX_ROWS` lesson: a silent truncation of deep history is
indistinguishable from a young company, and the price loader already learned it the
hard way.

### 6.2 Watermarks

`ops.load_watermark` is keyed `(source, endpoint, security_id)` — reuse it, with
the endpoint string including the period type
(e.g. `income-statement:annual`), because the loads are independent. Note in
`doc/ingestion.md` that, as with prices, **the endpoint string is ingestion state**:
changing it retires every watermark.

Statements have no date tail to extend, so the incremental strategy differs from
prices: store `last_loaded_date = max(fiscal_date)` and, on an incremental run,
request the most recent **N periods** (config `fundamentals_recent_periods`,
default **8** quarterly / **3** annual). That window re-pulls recent periods every
run, which is exactly what surfaces restatements — and costs nothing extra thanks
to the content-hash no-op in §4.6.

### 6.3 Validation, quarantine, and cadence

Per record: parse dates, coerce every numeric through `Decimal(str(v))` then
quantize to the column scale before judging, reject and quarantine on:

| `check_name` | Condition |
|---|---|
| `fundamentals_unparseable_date` | `date` missing/unparseable |
| `fundamentals_filing_before_period` | `knowledge_date < fiscal_date` |
| `fundamentals_missing_period_label` | `period` absent or not in `FY,Q1..Q4` |
| `fundamentals_value_out_of_range` | a value the `NUMERIC(24,2)` column cannot hold |
| `fundamentals_nonnumeric_value` | present but not a finite number |
| `fundamentals_duplicate_period` | two live records for one `(security_id, period_type, fiscal_date)` |

Reject the **record**, not the whole symbol; land the payload regardless.

**Cadence.** Fundamentals do not change nightly. Wire `fafnir ingest fundamentals`
into a **weekly** slot in `etc/crontab.example` plus a **daily light pass** over
symbols with a recent earnings date if one is cheaply derivable; if not, weekly
alone is correct and honest. **Do not add it to the nightly `daily_update.sh`
critical path** — a 21k-symbol statement sweep does not belong in front of
`fafnir adjust` and `refresh-marts`. Add `scripts/weekly_fundamentals.sh`
following the shape of `daily_update.sh`, and have `daily_update.sh` refresh the
new marts only (cheap) without re-ingesting.

### 6.4 New DQ checks in `src/fafnir/dq/checks.py`

Set-based SQL, following the existing style. Each writes flags rather than failing:

| Check | Rule |
|---|---|
| `balance_sheet_identity` | \|assets − (liabilities + total_equity)\| > max(0.5% of assets, $1M) → `error` |
| `cash_flow_tie` | \|(op + inv + fin + fx) − net_change_in_cash\| beyond tolerance → `warn` |
| `cash_balance_tie` | `cash_at_beginning + net_change ≠ cash_at_end` → `warn` |
| `fundamentals_gap` | a security with prices in a period but no statement covering it → `info` |
| `fundamentals_stale` | latest `fiscal_date` more than 200 days behind `as_of` for an actively-trading name → `warn` |
| `fundamentals_restatement` | written by the loader (§4.6); report magnitude |
| `fundamentals_estimated_knowledge_date` | written by the loader (§4.5); report count by decade |
| `fundamentals_currency_mismatch` | `reported_currency <> core.security.currency` → `info` |
| `fundamentals_unmapped_fields` | keys landing in `extra`, aggregated by key name → `info`. **This is the taxonomy's growth signal** |
| `fundamentals_ttm_incomplete` | TTM windows with `< 4` quarters, counted by year |
| `fundamentals_negative_impossible` | negative `total_assets`, `revenue`, or share count → `error` |
| `fundamentals_coverage` | % of the active universe with a statement known as of today, by sector |

`run_all` must remain backward compatible: existing callers pass
`exchange_code`/`outlier_threshold` positionally-or-by-keyword and expect the
existing keys in the returned dict. **Add** keys; do not rename or remove any.
Gate the new checks behind a `include_fundamentals: bool = True` parameter so a
deployment mid-migration is not broken by missing tables — and have them no-op
gracefully if `core.income_statement` does not exist.

---

## 7. Backfill plan and the FMP budget

The backfill is the operationally risky part. Plan it explicitly before running it.

**Per-symbol path (if no bulk endpoint):** 3 statements × 2 period types × ~21,000
securities ≈ **126,000 requests**. At the configured 280 req/min that is roughly
**7.5 hours** of wall clock. Estimate payload size from the probe (measure, do not
guess) and project total bytes against the **50 GB/month** budget, reporting the
projection *before* starting. `ops.ingestion_run.bytes_downloaded` already meters
this; use it.

**Bulk path (if the probe finds `*-bulk`):** ~36 years × 5 period values × 3
statements ≈ **540 requests**. If available, make it the default backfill path and
keep the per-symbol path for incremental runs and repairs. Confirm the bulk payload
carries `filingDate`/`acceptedDate` — if it does not, it is unusable for a PIT
warehouse regardless of how much cheaper it is, and you fall back to per-symbol.

**Requirements either way:**

- **Resumable.** Commit per symbol (or per bulk page). A re-run after an
  interruption skips completed work via watermarks.
- **Ordered.** Annual before quarterly (cheaper, and it establishes fiscal-calendar
  facts that make quarterly validation stronger).
- **`--include-inactive` on the backfill.** A fundamentals history containing only
  survivors is worse than no history, because it looks complete.
- **Fail loudly on an empty load.** Copy the `load_prices` stance: an explicit
  window that returns zero rows across the universe raises rather than reporting a
  successful no-op. A backfill that "succeeded" having written nothing is the most
  expensive failure mode here.
- Add `scripts/backfill_fundamentals.sh` (mirroring `initial_backfill.sh`), and a
  `--from-year` defaulting to **1990** but clamped to whatever the probe proved is
  actually available, with the effective floor logged.

---

## 8. CLI surface (all additive)

```
fafnir source probe-fundamentals [--symbols AAPL,MSFT,...] [--json]

fafnir ingest fundamentals
    [--symbols AAPL,MSFT]              # default: universe from core.security
    [--statements income,balance,cash] # default: all three
    [--period annual|quarter|both]     # default: both
    [--from-year 1990]                 # backfill floor; else incremental
    [--include-inactive]               # required for an unbiased backfill
    [--limit N]                        # cap symbols, for testing
    [--bulk/--no-bulk]                 # use bulk endpoints when available

fafnir db refresh-marts                # unchanged flag surface; now refreshes more
fafnir dq run                          # unchanged flag surface; now runs more checks
fafnir status                          # ADD fundamentals lines; keep existing lines byte-identical
```

`fafnir status` additions: statement row counts by table, earliest and latest
`fiscal_date`, count of live versions vs total versions, restatement count,
`knowledge_source` distribution, panel row count and latest `as_of_date`. **Append;
do not reformat or reorder the existing four lines** — scripts and the operator's
eye both depend on them.

### `duk` additions (optional, additive, gated behind `-S db`)

- `duk fs SYMBOL [--period annual|quarter] [--statement income|balance|cash] [--as-of DATE] [--limit N]`
  — print a statement, PIT-correct when `--as-of` is given.
- `duk fa SYMBOL [--as-of DATE]` — the factor row for one security from the panel.

Follow `duk`'s existing precision/formatting conventions
(`apply_precision_to_dataframe`) and add read functions to
`src/duk/datasource/db.py` rather than writing SQL in the CLI. `-S live` for these
commands should raise the same "db mode only" style of message `yc` already uses
for the inverse case.

---

## 9. Non-breaking guarantees — verify each one

Treat this as a checklist to be *demonstrated*, not asserted:

1. **No `ALTER`/`DROP` on any existing table, column, view, or index.** The one
   permitted category is adding new objects. `mart.security_latest`,
   `mart.v_daily_price_adjusted`, `core.daily_price`, `core.security`,
   `core.company_profile`, `core.corporate_action`, `core.adjustment_factor`,
   `ops.*`, `landing.*`, `ref.*` are untouched.
2. **New migrations only** (`0011`, `0012`, and `0013` for seeds/backfill DDL if
   needed), each with a complete, tested `.down.sql`. `test_rollback_then_remigrate`
   rolls back the **last** migration and re-applies it — so whichever migration ends
   up last must round-trip cleanly. Verify by running it.
3. **No edits to applied migrations.** Do not add entries to
   `SUPERSEDED_CHECKSUMS`; nothing here qualifies.
4. **Grants.** Run `test_migrations_least_privilege.py` and extend it to assert
   `fafnir_read` can `SELECT` the new `core` tables and `fafnir_app` can `SELECT`
   the new `mart` objects **and cannot** `SELECT` the new `core` tables. Confirm
   the new migrations apply as a `NOSUPERUSER`/`NOCREATEROLE` owner.
5. **`refresh_marts` contract.** Existing callers
   (`scripts/daily_update.sh`, `fafnir db refresh-marts`) must keep working with no
   flag changes. Cover the narrowed exception handling (§5.6) with a test.
6. **`dq.run_all` contract.** Existing return keys preserved; new keys added.
7. **`duk` regression.** `duk ph`, `duk ls`, `duk rc`, `duk ti`, `duk yc` behave
   identically in both `-S db` and `-S live`. Run the existing `duk` tests unchanged.
8. **Config.** New keys (`fundamentals_filing_lag_days_annual`,
   `..._quarter`, `fundamentals_recent_periods`, `fundamentals_panel_extra_lag_days`,
   `fundamentals_start_year`) added to `FafnirConfig` **with defaults**, so an
   existing `~/.fafnirrc` keeps working untouched. Document them in `etc/fafnirrc`.
9. **Fresh-install path.** `scripts/setup_db.sh` → `migrate` → `seed` succeeds on
   an empty database with the new migrations present.
10. **Upgrade path.** A database currently at `0010` with real data migrates to the
    new head without touching a single existing row. Prove it: capture
    `count(*)` and a checksum of `core.daily_price` and `core.security` before and
    after, and assert equality.

---

## 10. Testing

Unit (no database):

- Field mapping: a fixture payload → expected typed row, including `extra`
  overflow for an unknown key.
- `Decimal` discipline: no float ever reaches the row dict; a value beyond
  `NUMERIC(24,2)` is quarantined rather than silently rounded.
- `knowledge_date` resolution across all three branches, including the
  `estimated` flag.
- `content_hash` stability: same payload → same hash; `None` vs `0` → different
  hash; key reordering → same hash.
- Fiscal period labelling for a non-December year-end.
- TTM: complete window, 3-quarter window → `NULL`, derived Q4 for flows,
  refusal to derive a balance-sheet Q4, YTD de-cumulation.
- Sign conventions from `ref.statement_line_item`.

Integration (needs `FAFNIR_TEST_DSN`):

- **Idempotency:** load the same payload twice → identical row count, no new
  version, `valid_from` unchanged.
- **Restatement:** load, mutate one value, reload → old row closed with `valid_to`,
  new row `version_no=2`, `is_restatement`, DQ flag written.
- **Look-ahead guard (the critical one):** build a panel row for `as_of = D` from a
  fixture where a statement has `knowledge_date = D + 1`; assert that statement's
  values are **absent** from the row. Then assert the same for the whole panel with
  a set-based query: `SELECT count(*) FROM mart.fundamental_panel p JOIN ... WHERE
  knowledge_date > as_of_date` **must be 0**. Make this a standing invariant test.
- **Split-era consistency (§4.7):** the AAPL 1995 P/E band test.
- **Currency:** a EUR-reporting fixture yields `currency_mismatch = TRUE` and
  `NULL` price-based ratios.
- **Golden values:** at least two hand-verified figures from a real 10-K
  (e.g. AAPL FY2020 revenue = 274,515,000,000; MSFT FY2015 net income, which was
  restated — good for the version test) asserted against the loaded rows.
- **Grants** and the **upgrade path** from §9.

Add a `pytest` marker if the fundamentals integration tests need one; otherwise
reuse `integration`. Keep `make test` (unit, no DB) green and fast.

---

## 11. Documentation (part of "done", not a follow-up)

- `doc/data_dictionary.md` — every new table/column in the existing format:
  **grain, source, units, adjustment status, cadence**. For statements, adjustment
  status is **"as-reported, unadjusted — see ADR 0005"**.
- `doc/architecture.md` — extend the entity-overview ASCII diagram; add
  fundamentals to the layer diagram; expand the three-timelines section now that
  `filing_date` is real rather than promised.
- `doc/ingestion.md` — endpoint→table map rows; the row-cap/limit lesson; the
  watermark-endpoint-string note; the weekly cadence.
- `doc/extending.md` — mark **Fundamentals ✅ shipped**; replace the sketch in
  "Adding fundamentals (bitemporal)" with a pointer to what was actually built,
  including the correction that FMP supplies no restatement history.
- `doc/backfill.md` — a fundamentals section: request/byte projection, ordering,
  resumability, the 1990-depth finding from the probe.
- `doc/operations.md` — weekly job, what a restatement flag means, how to
  investigate a balance-sheet-identity failure.
- `doc/fundamentals.md` **(new)** — the researcher-facing guide: how to run a
  point-in-time query, the look-ahead rules, what each factor means and its
  literature reference, known coverage limits by decade, and a worked example
  building a value-factor decile from `mart.fundamental_panel`.
- **`doc/adr/0005-bitemporal-fundamentals.md`** — the knowledge-date model, why
  SCD-2 on the load timeline, the estimated-filing-date fallback and its risk, and
  the fact that pre-first-load history is single-versioned.
- **`doc/adr/0006-derived-metrics-not-vendor-metrics.md`** — why fafnir computes
  its own ratios; the as-reported per-share/adjusted-price trap (§4.7) and the
  aggregate-over-aggregate preference.
- `README.md` — status line, quick-start commands, docs table rows.

---

## 12. Suggested PR sequence

Each PR must be independently reviewable, green in CI (`lint`, `build-test`,
`security` workflows), and leave `main` deployable.

| PR | Contents | Gate |
|---|---|---|
| 1 | `probe-fundamentals` + probe tests. No schema. | **Report findings; get sign-off before PR 2.** Everything downstream depends on what it finds. |
| 2 | Migration `0011` (tables, indexes, constraints, `ref.statement_line_item` + seed), data-dictionary entries, migration tests | Applies and rolls back cleanly under a least-privilege owner |
| 3 | `sources/fmp.py` methods + `ingest/fundamentals.py` + `repository` upserts + CLI `ingest fundamentals`; unit + integration tests | Idempotency and restatement tests pass |
| 4 | Migration `0012` (`fn_statements_as_of`, `v_statements_current`, `v_fundamentals_ttm`) + tests | TTM edge cases pass |
| 5 | Migration `0013` (`fundamental_panel`, `fn_fundamental_snapshot`, `v_industry_fundamentals`, `security_fundamentals_latest`) + `refresh_marts` extension + the look-ahead invariant test | Look-ahead invariant returns 0 |
| 6 | DQ checks, `fafnir status` additions, `scripts/weekly_fundamentals.sh`, `scripts/backfill_fundamentals.sh`, crontab entry | Existing DQ keys unchanged |
| 7 | `duk fs` / `duk fa`, remaining docs, ADRs 0005 + 0006, README | Full `duk` regression green |

---

## 13. Acceptance criteria

- [ ] Probe report published, with a definitive answer on **history depth** (does
      FMP reach 1990, and for which statements/period types), the **row cap**, and
      **bulk-endpoint availability**.
- [ ] Backfill loads the full universe including delisted names, from the proven
      floor year, resumably, within the FMP request and bandwidth budget — with the
      actual bytes consumed reported against the 50 GB/month allowance.
- [ ] Re-running any load is a **true no-op**: zero new rows, zero new versions,
      zero `valid_to` changes.
- [ ] `SELECT count(*) FROM mart.fundamental_panel p WHERE p.knowledge_date >
      p.as_of_date` returns **0**, enforced by a standing test.
- [ ] A restatement fixture produces exactly two versions, one live, with a DQ flag.
- [ ] The AAPL split-era P/E test passes (guards §4.7).
- [ ] Balance-sheet identity holds for **> 99%** of loaded records; every exception
      carries a DQ flag rather than being silently accepted.
- [ ] `knowledge_source` distribution is reported by decade, so the estimated-date
      exposure in early history is a known, published number.
- [ ] `fafnir_app` can read every new `mart` object and **no** new `core` table.
- [ ] Every pre-existing test passes unmodified; a `0010` database with real data
      migrates with **byte-identical** existing tables (proven by before/after
      counts and checksums).
- [ ] `make lint` and `make test` clean; `make test-int` clean against a test DSN.

---

## 14. Ask before you assume

Stop and ask rather than guessing if:

- The probe shows history materially shallower than 1990, or the row cap cannot be
  established conclusively.
- Bulk endpoints exist but omit `filingDate`/`acceptedDate`.
- More than a token fraction of pre-2000 records need an **estimated**
  `knowledge_date` (this changes what the early history can honestly support).
- FMP's field inventory diverges substantially from §4.3.
- The projected backfill exceeds the monthly bandwidth budget.
- Any requirement here appears to conflict with an existing ADR — the ADR wins
  until it is superseded in writing.
