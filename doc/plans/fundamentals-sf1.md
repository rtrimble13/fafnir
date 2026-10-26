# Plan: fundamentals on Sharadar SF1

- Status: **signed off** (#85, GO on 2026-10-25). The five decisions in *A. Decisions* and the choices
  listed after them were taken by the operator on 2026-10-25. They are recorded as
  [ADR 0018](../adr/0018-bitemporal-fundamentals.md) and
  [ADR 0019](../adr/0019-derived-metrics-not-vendor-metrics.md) (#86); the schema (#87) builds on them.
- Retargets the FMP build prompt, [fundamentals-build-prompt-fmp.md](fundamentals-build-prompt-fmp.md)
  (2026-08-27). Its architecture carries over; its source, field map, timelines, share terms, cadence
  and numbering do not. This document follows its section numbers, restates every section SF1
  changes, and says where a section stands unchanged. Where the two disagree, this one wins.
- Evidence: the SF1 probe (#83), [backfill.md › Confirming the SF1 feed](../backfill.md#confirming-the-sf1-feed).
  Related ADRs: [0015](../adr/0015-multi-vendor-provenance-and-precedence.md) (provenance and
  precedence), [0016](../adr/0016-sharadar-identity-and-survivorship-floor.md) (permaticker, the 1998
  floor), [0017](../adr/0017-vendor-access-is-one-library-not-a-distribution-per-vendor.md) (vendor
  code), [0018](../adr/0018-bitemporal-fundamentals.md) (this plan's knowledge model) and
  [0019](../adr/0019-derived-metrics-not-vendor-metrics.md) (derived metrics), both #86.
- Tracking: [#41](https://github.com/rtrimble13/fafnir/issues/41), phase P4 of the
  [Sharadar adoption plan](sharadar-adoption.md).

## A. What SF1 changes

### What the probe measured (2026-10-24)

| Fact | Consequence for the build |
|---|---|
| On first-reported rows (ARQ, ARY, ART) `date` is the SEC filing date; on restated rows (MRQ, MRY, MRT) it is the period end. `lastupdated` is restamped on whole histories | A first-reported row has a real knowledge date. A restatement has none: decision 1 |
| SF1 keeps only the latest restated value of a period | Restatements before fafnir's first load are one version, whatever happened in between |
| Share counts and per-share values are in today's share terms, re-expressed at every later split | The FMP prompt's §4.7 rule 1 (store as reported) does not hold as given: decisions 2 and 3 |
| Q4 is its own row; quarters are discrete and sum to the year | TTM sums four quarters: no Q4 derivation, no year-to-date de-cumulation |
| An item a filing does not carry is 0 (41 indicators); an item that does not apply is NULL | NULL ≠ 0 holds only for inapplicable items; ratios guard zero denominators |
| No currency column; `fxusd` is foreign units per USD | The reporting currency is TICKERS' `currency` |
| One share class per issuer, the primary | Decision 5 |
| History back to 1991, 11.7% of rows before 1998 | Decision 4 |
| 112 columns: 66 statement indicators, 36 vendor metrics, 10 entity fields; 58 of the FMP prompt's 123 §4.3 columns have an SF1 counterpart | SF1's indicators are the taxonomy (§4.3) |

### Decisions (operator, 2026-10-25)

1. **A restatement is known from the day fafnir first loads it** (`knowledge_source = 'observed'`).
   First-reported values are known from their filing date. Neither SF1's restated-row `date` (the
   period end) nor `lastupdated` is ever a knowledge date. Each version carries a **knowledge
   interval**, `known_from` to `known_to`: a restatement sets the previous version's `known_to` to its
   own `known_from`. Intervals chain the versions of one period; successive periods are not chained.
   Consequence: point-in-time history before the backfill reads as first reported, and restatements
   gain real dates from the backfill on.
2. **A version is stored in the share terms of its `known_from`.** A first-reported version is SF1's
   value divided by the product of Sharadar's splits after the filing date. A restated version is
   stored as given, since today's terms are its terms. Per-share values are not typed: the mart
   derives them from aggregates.
3. **A split's re-expression is not a version.** After a later split, the recovered values are
   unchanged within rounding, so the content hash (share counts at 1,000-share precision) sees no
   change.
4. **Rows before 1998 load into `core`; the factor panel starts on 1998-01-01**, the survivorship
   floor (ADR 0016).
5. **Statements attach to the primary share class only.** The panel holds one row per issuer. `duk`
   and the MCP tools answer a secondary class with its issuer's primary-class statements, through the
   shared CIK, and say so.

### Choices this document makes (review at sign-off)

- **Two timelines of versions.** A restatement (a restated row differs from the period's current
  version) is a new *knowledge* version. A vendor correction (a first-reported row changes in SF1:
  the filing did not) replaces the row on the *load* timeline and keeps its knowledge interval
  (§4.6).
- **A period with no first-reported row** (294 in the probe's sample: late filings, coverage edges)
  is known from the issuer's next first-reported filing after the period, `estimated`, with a DQ
  flag; with no later filing, from the load day, `observed` (§4.5).
- **ART and MRT are not loaded.** fafnir computes TTM from quarters (§5.2); SF1's trailing rows stay
  in the file.
- **Vendor metrics, the columns measured on the row's `date`, per-share values and `*usd`
  conversions go to `extra`**, once per SF1 row (on `core.income_statement`), never read by mart.
  If #87 measures them above 2 GB, they are not stored at all: they stay in the ledger's file.
  *Measured by #87:* about 965 bytes a row, about 1.35 GB at full scale, so they are stored.
- **`shares_outstanding` (the cover-page count, `sharesbas`) comes from first-reported rows only.**
  A restated row's value is measured on another date (the period end), so it is not a restatement.
- **Nightly, not weekly.** The SF1 delta is small (1,105 rows over four days), so fundamentals ride
  the Sharadar night (#106).

### Deferred, not built

- **An estimated restatement timeline** (restated values known at the filing date of the same period
  a year later), as a research mode beside the default. It would give historical growth rates a
  consistent basis, at a look-ahead risk SF1 cannot test, because its restated value is the latest
  one. Computable later from stored filing dates, so it needs no schema now.
- **A "latest period on file" interval view** (from a period's `known_from` to the next period's).
  The point-in-time function answers the question; the view is cheap to add if wanted.
- **Attaching secondary share classes** to their issuer's statements: an issuer entity, and a risk of
  double counting in cross sections.
- **`core.vendor_metric`**, as the FMP prompt's §1 already deferred.

---

## 1. Scope

### In scope

SF1's statement indicators, quarterly and annual, first-reported (ARQ, ARY) and restated (MRQ, MRY),
for every security with a Sharadar link, **dead issuers included**. Plus what makes them usable:
bitemporal versioning, a point-in-time read seam, TTM, and a factor panel.

### Out of scope

- **Vendor metrics** (SF1's 36 [Metrics] columns, the `daily` and `metrics` tables): fafnir derives
  its own on read (#86).
- SF1's trailing-twelve-month dimensions (ART, MRT); insiders and holdings (SF2, SF3); segments;
  estimates; transcripts.
- **Securities with no Sharadar link:** the 198 unlinked securities of 2026-10-22, and mutual funds,
  which SF1 does not cover.
- **Secondary share classes** (decision 5).
- Any change to price, corporate-action or adjustment-factor handling.

### Sources of every derived quantity

| Needed for | Comes from |
|---|---|
| Share count | `weighted_average_shares` / `_diluted` (income statement); `shares_outstanding` (cover page) |
| Market cap (PIT) | `shares_outstanding` in as-of-t terms (§4.7) × the raw close on t |
| Enterprise value | market cap + `total_debt` − `cash_and_equivalents`. SF1 carries no minority-interest or preferred-stock balance: EV omits them, and says so |
| EPS | `net_income_common` / `weighted_average_shares`, never SF1's `eps` (§4.7) |
| Dividend yield | `core.corporate_action` dividends (as declared, ADR 0001); `dividends_paid` for the cash view |
| Buyback yield | `net_equity_issuance` (SF1 nets issuance and repurchase) |
| Adjusted prices | `mart.v_daily_price_adjusted`: **do not re-derive** |

---

## 2. The probe

Done: `fafnir source probe-fundamentals` (#83). The verdict table is in
[backfill.md](../backfill.md#confirming-the-sf1-feed). Still on the host before #92:

- the probe against the ledger's SF1 file, its verdicts compared with the 2026-10-24 API run;
- the five listed filings, plus two before 1997, compared with EDGAR by hand.

---

## 3. Architectural commitments

The FMP prompt's nine hold, with these changes:

1. **Purely additive.** New migrations take the next free number, 0035 today.
2. **Land raw, then transform**, as plan v2 does it: SF1's files stay on disk under the ledger
   (`landing.sharadar_file`); there is no landing table. The loader reads the ledger's SF1 full
   file, the nightly SF1 delta files (#106) and the ACTIONS file through
   `fafnir.ingest.sharadar_ledger`. Not `landing.fmp_raw`.
3. **Validate at the boundary; quarantine, never drop.** Unchanged.
4. **Idempotent**, and a re-run creates no version, nor does a later split (decision 3).
5. **Exact arithmetic.** `sharadar_files` already types every value as `Decimal` from its text;
   quantize to the column's scale before judging.
6. **Derive on read.** Unchanged.
7. **Lineage on every load.** Unchanged: `RunLog`, `ingestion_run_id` on every row.
8. **Least privilege.** Unchanged.
9. **Commit at the unit of work:** one security, all its periods and versions.
10. **Vendor code stays in `fafnir.sources`** (ADR 0017): file typing and any client change there;
    the loader in `ingest/`.
11. **Provenance (ADR 0015):** `source = 'sharadar'` on every row. No other vendor writes statements.

---

## 4. Schema: migration 0035 (#87)

### 4.1 Physical shape

Unchanged: three statement tables, typed `NUMERIC` columns, `extra JSONB NOT NULL DEFAULT '{}'`,
never read by mart. Not partitioned; `fiscal_date` stays in the key so a partition is not a grain
change.

### 4.2 Common columns

```sql
security_id        BIGINT      NOT NULL REFERENCES core.security (security_id),
vendor_key         TEXT        NOT NULL,   -- the permaticker
period_type        TEXT        NOT NULL CHECK (period_type IN ('annual','quarter')),
fiscal_date        DATE        NOT NULL,   -- SF1 reportperiod: the period's end (observation)
calendar_date      DATE        NOT NULL,   -- SF1 calendardate: the vendor's normalized period
fiscal_year        INTEGER     NOT NULL,   -- from SF1 fiscalperiod ('2025-Q4' -> 2025), not derived
fiscal_period      TEXT        NOT NULL CHECK (fiscal_period IN ('FY','Q1','Q2','Q3','Q4')),
period_start_date  DATE,
version_no         INTEGER     NOT NULL,   -- 1 = as first reported
-- knowledge timeline: what the market could know, and from when
known_from         DATE        NOT NULL,
known_to           DATE,                   -- the next version's known_from; NULL = in force
knowledge_source   TEXT        NOT NULL CHECK (knowledge_source IN ('filed','observed','estimated')),
filing_date        DATE,                   -- SF1 `date` of the first-reported row; else NULL
-- load timeline: what fafnir held, and from when
loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
superseded_at      TIMESTAMPTZ,            -- set when a vendor correction replaced the row
reported_currency  TEXT,                   -- TICKERS' currency
content_hash       TEXT        NOT NULL,
extra              JSONB       NOT NULL DEFAULT '{}'::jsonb,
source             TEXT        NOT NULL DEFAULT 'sharadar',
ingestion_run_id   BIGINT      REFERENCES ops.ingestion_run (ingestion_run_id)
```

The FMP prompt's `accepted_date` is dropped: SF1 has none. `is_restatement` is `version_no > 1`.
`valid_from` / `valid_to` are not used: elsewhere in the warehouse they are inclusive dates (a ticker
period's `valid_to` is its last day), and these intervals are half-open.

**Key:** `(security_id, period_type, fiscal_date, version_no, loaded_at)`.

**Indexes:**

```sql
-- one current row per version (load timeline)
CREATE UNIQUE INDEX ux_<t>_current ON core.<t> (security_id, period_type, fiscal_date, version_no)
    WHERE superseded_at IS NULL;
-- one version in force per period (knowledge timeline)
CREATE UNIQUE INDEX ux_<t>_in_force ON core.<t> (security_id, period_type, fiscal_date)
    WHERE known_to IS NULL AND superseded_at IS NULL;
-- point-in-time lookup
CREATE INDEX ix_<t>_pit ON core.<t> (security_id, period_type, known_from DESC, fiscal_date DESC)
    WHERE superseded_at IS NULL;
-- panel builds sweep by knowledge date
CREATE INDEX ix_<t>_known ON core.<t> (known_from) WHERE superseded_at IS NULL;
-- restatement forensics
CREATE INDEX ix_<t>_restated ON core.<t> (security_id, fiscal_date) WHERE version_no > 1;
```

**Constraints:**

```sql
CHECK (known_to IS NULL OR known_to > known_from)
CHECK (known_from >= fiscal_date)                  -- the look-ahead tripwire
CHECK ((period_type = 'annual') = (fiscal_period = 'FY'))
CHECK (knowledge_source <> 'filed' OR known_from = filing_date)
CHECK (superseded_at IS NULL OR superseded_at >= loaded_at)
CHECK (period_start_date IS NULL OR period_start_date < fiscal_date)
```

The warehouse has no exclusion constraints, so "no gaps or overlaps" is a standing test and a DQ
check rather than a constraint: for every period, the current rows' versions chain, each `known_to`
equal to the next version's `known_from`, with only the last one open.

### 4.3 Typed line items: SF1's statement indicators

SF1's indicators become the taxonomy (58 of the FMP prompt's 123 columns have a counterpart; the rest
have none in SF1). Units: **money `NUMERIC(24,2)`, share counts `NUMERIC(24,4)`**. No per-share
column (§4.7). All nullable, and a NULL stays NULL. The map lives in
`fafnir.ingest.fundamentals_map`, one tuple per statement; migration 0035 inserts the same rows
into `ref.statement_line_item`, and a test holds the two equal.

**`core.income_statement`** (19): `revenue` (revenue), `cost_of_revenue` (cor), `gross_profit` (gp),
`research_and_development` (rnd), `selling_general_and_administrative` (sgna), `operating_expenses`
(opex), `operating_income` (opinc), `interest_expense` (intexp), `ebit` (ebit), `income_before_tax`
(ebt), `income_tax_expense` (taxexp), `consolidated_income` (consolinc), `net_income_noncontrolling`
(netincnci), `net_income_discontinued` (netincdis), `net_income` (netinc), `preferred_dividends`
(prefdivis), `net_income_common` (netinccmn), `weighted_average_shares` (shareswa),
`weighted_average_shares_diluted` (shareswadil).

**`core.balance_sheet`** (26): `cash_and_equivalents` (cashneq), `investments_current`
(investmentsc), `investments_noncurrent` (investmentsnc), `investments_total` (investments),
`receivables` (receivables), `inventory` (inventory), `tax_assets` (taxassets),
`total_current_assets` (assetsc), `property_plant_equipment_net` (ppnenet),
`goodwill_and_intangibles` (intangibles), `total_noncurrent_assets` (assetsnc), `total_assets`
(assets), `payables` (payables), `deferred_revenue` (deferredrev), `deposit_liabilities`
(deposits), `debt_current` (debtc), `debt_noncurrent` (debtnc), `total_debt` (debt),
`tax_liabilities` (taxliabilities), `total_current_liabilities` (liabilitiesc),
`total_noncurrent_liabilities` (liabilitiesnc), `total_liabilities` (liabilities),
`accumulated_other_comprehensive_income` (accoci), `retained_earnings` (retearn),
`total_equity_parent` (equity), `shares_outstanding` (sharesbas, from first-reported rows only).

**`core.cash_flow_statement`** (13): `net_cash_operating` (ncfo), `depreciation_amortization`
(depamor), `share_based_compensation` (sbcomp), `capital_expenditure` (capex),
`net_cash_business_acquisitions` (ncfbus), `net_cash_investment_acquisitions` (ncfinv),
`net_cash_investing` (ncfi), `net_debt_issuance` (ncfdebt), `net_equity_issuance` (ncfcommon),
`dividends_paid` (ncfdiv), `net_cash_financing` (ncff), `fx_effect_on_cash` (ncfx),
`net_change_in_cash` (ncf).

**`extra`**, once per SF1 row on `core.income_statement`, under `vendor_metric`: the 36 metrics, the
columns measured on the row's `date` (price, marketcap, ev, evebit, evebitda, pb, pe, pe1, ps, ps1,
divyield), `sharefactor`, the per-share values (eps, epsdil, epsusd, dps) and the `*usd` conversions.
A column SF1 adds later lands in `extra` too, and `fundamentals_unmapped_fields` reports it.

### 4.4 `ref.statement_line_item`

As the FMP prompt's §4.4, with `vendor_field` (the SF1 code) beside a nullable `fmp_field` (filled
where the probe's map found a counterpart, for cross-vendor checks). `item_class` is load-bearing (TTM
reads it): income and cash-flow items `flow`, balance-sheet items `stock`, share counts
`share_count`. **Sign conventions, as measured:** cash-flow items are signed as cash moves (capex and
dividends paid negative), so `positive_is_inflow`, like revenue; expenses (cost of revenue, R&D,
SG&A, interest, tax) are positive, `positive_is_outflow`, and a tax benefit is negative; subtotals,
balances and share counts are `signed`. The rows are inserted by the migration rather than a seed
file: `fafnir db seed` does not re-run on an upgraded host (0019 and 0027 did the same for
exchanges).

### 4.5 The three timelines

| Timeline | Columns | Meaning |
|---|---|---|
| Observation | `fiscal_date` | the period the numbers describe |
| Knowledge | `known_from` / `known_to` | when the market could know this version |
| Load | `loaded_at` / `superseded_at` | when fafnir held this row |

**`known_from`, in this order:**

1. **A first-reported row** (ARQ, ARY) is version 1, known from its SF1 `date`, the filing:
   `knowledge_source = 'filed'`.
2. **A restated row** (MRQ, MRY) that differs from the period's version in force, compared in that
   version's share terms and without `shares_outstanding`, is a new version, known from **the day
   fafnir loads it**: `'observed'`. The previous version's `known_to` becomes the same day.
3. **A period with a restated row and no first-reported row** is version 1, known from the filing date
   of the issuer's next first-reported row after `fiscal_date`, the first filing that could carry it:
   `'estimated'`, with a `fundamentals_estimated_knowledge_date` flag. With no later filing, it is
   known from the load day: `'observed'`.
4. **Never** SF1's `date` on a restated row (the period end: it would date a restatement before the
   original filing) and never `lastupdated` (a restamp).

**What it means.** At the backfill, every restatement SF1 holds is known from the backfill day:
history before it reads as first reported. 73% of quarterly periods in the probe's sample differ in
some statement field, mostly reclassifications in a later filing's comparatives; 10% of annual net
incomes move by more than 1%. From the backfill on, a restatement is dated within a day of Sharadar
publishing it. `knowledge_source` is in every point-in-time read and in the panel, so a researcher can
see and exclude `estimated` rows.

### 4.6 Versioning on two timelines

- **A restatement adds a knowledge version** (§4.5, rule 2), with a `fundamentals_restatement` flag
  (`warn`) carrying the fields that moved and by how much.
- **A vendor correction replaces a row on the load timeline.** When a first-reported row changes in
  SF1, the filing did not change; Sharadar fixed its own record. In one transaction: the row's
  `superseded_at` is set, and the corrected row is inserted with the same `version_no`, `known_from`
  and `known_to`. A `fundamentals_vendor_correction` flag (`info`). A corrected filing date is a
  correction too, and moves `known_from`.
- **Order within a security:** first-reported rows first (corrections), then restated rows
  (restatements), in one transaction. A correction that also changes the restated row then compares
  equal, and adds no version.
- **The content hash:** SHA-256 over one table's typed values, rendered as `Decimal` strings with
  `None` told from `0`, share counts rounded to 1,000 shares (decision 3), plus `filing_date` for
  first-reported rows. Equal hash: nothing is written, not even `loaded_at`.
- **Two in-place updates, and only two:** `known_to` (NULL to a date, once, in the transaction that
  inserts the next version) and `superseded_at` (NULL to a time, once). A value is never updated.
- **Reproducing a past state** at time T: the rows with `loaded_at <= T` and not superseded by T; a
  version loaded after T is ignored, and its predecessor's `known_to` read as open. `known_to` always
  equals the next version's `known_from`, so nothing is lost by storing it.

### 4.7 Share terms

1. **A version is stored in the share terms of its `known_from`.**
   - A first-reported version: SF1's value ÷ the product of Sharadar's splits with an ex-date after the
     filing date, from Sharadar's ACTIONS (the list SF1 was adjusted by), as exact ratios
     (`sharadar_map.split_ratio`: 1/30, not 0.0333).
   - An observed version: as given (its terms are today's).
   - An estimated version: ÷ the splits after its `known_from`.

   Measured: share counts recover to whole numbers in 335 of 350 forward-split cases, and reverse
   splits within the vendor's rounding. A recovered count is rounded to the nearest share.
2. **No per-share column.** A recovered EPS is off by a median of $0.005 and up to $0.52. The mart
   derives EPS from `net_income_common / weighted_average_shares`, and DPS from the dividends fafnir
   stores as declared. SF1's values stay in `extra`.
3. **A split's re-expression is not a version** (decision 3). After a later split SF1 restamps the
   history; the recovered values equal the stored ones within rounding; the hash sees no change. A
   test pins it: a synthetic split after load creates no version.
4. **One expression converts to as-of terms:** × the product of the warehouse's splits
   (`core.corporate_action`, the list the raw prices follow) with an ex-date in `(known_from, t]`.
   There are two split lists on purpose: Sharadar's undoes SF1's adjustment at load, and the
   warehouse's matches the raw prices at read.
5. **Aggregate-over-aggregate first** (the FMP prompt's rule 3), and the split-era test: AAPL's P/E
   between 1998 and 2000 within a plausible band.
6. **Market cap at t** = raw close(t) × `shares_outstanding` of the latest period known at t,
   converted to t's terms. For a multi-class issuer this is approximate: the primary class's price ×
   the issuer's count, as Sharadar's own `marketcap` does with `sharefactor`. Documented, not
   corrected.

### 4.8 Currency

`reported_currency` is TICKERS' `currency` (`core.vendor_security`, `vendor_table = 'fundamentals'`):
per security, not per row. 1,019 of 17,872 tickers report outside USD. Statements stay in that
currency. `fxusd` (foreign units per USD) and the `*usd` columns go to `extra`. The mart's
`currency_mismatch` rule stands as the FMP prompt wrote it: the row is kept and its price ratios are
NULL.

---

## 5. The mart: migrations 0036-0038

The FMP prompt's §5 holds, with these changes.

### 5.1 Point-in-time access (#95)

`mart.fn_statements_as_of(as_of DATE)` returns, per `(security_id, period_type)`, the latest
`fiscal_date` with a version in force at `as_of`: current rows (`superseded_at IS NULL`) with
`known_from <= as_of AND (known_to IS NULL OR as_of < known_to)`. The two-axis variant takes
`(as_of DATE, load_as_of TIMESTAMPTZ)` and reads the rows fafnir held then (§4.6). The three
statements join on `fiscal_date`, never on knowledge dates, each table at its own version in force;
missing statements are NULL, with `has_*` flags. `mart.v_statements_current` holds the versions in
force today: for screening, not backtests.

### 5.2 TTM (#95)

- Flows sum four quarters; stocks take the latest; never sum a balance-sheet item.
- Four contiguous quarters, or NULL; `ttm_quarters_used` is exposed.
- **No derived Q4 and no year-to-date de-cumulation:** the probe found Q4 filed and quarters
  discrete. `fundamentals_ytd_suspect` watches for either appearing.
- At t, each constituent quarter is read at its version in force at t, and the TTM's knowledge date
  is the latest of the four `known_from`.

### 5.3 The factor panel (#100)

As the FMP prompt's §5.3, with these changes:

- **`as_of_date` from 1998-01-01** (decision 4), the last trading day of each month per
  `ref.trading_calendar`. Earlier periods feed it, as TTM and growth inputs.
- **Every input is a version in force at `as_of_date`.** The standing invariant:
  `count(*) WHERE known_from > as_of_date` is 0. `fundamentals_panel_extra_lag_days` (default 0)
  stays.
- **One row per issuer:** secondary share classes have no statements, so they have no row.
- Columns as listed there, except:
  - `is_derived_q4` is dropped;
  - `knowledge_date` becomes `known_from`, and `knowledge_source` and `version_no` stay;
  - EPS-based factors use the derived EPS;
  - `shares_outstanding_as_of` replaces `shares_outstanding_as_reported`;
  - EV omits minority interest and preferred stock (§1).
- **Growth factors compare versions in force at `as_of_date`.** Before the backfill, that is first
  reported against first reported. Where a company reclassified, the two years are on different bases;
  `doc/fundamentals.md` says so.
- Denominator discipline, the unique index and the refresh hold as written.

### 5.4-5.6

Unchanged: industry aggregates (#101, built: §5.9); `mart.security_latest` untouched by this build (#109 re-defines
it separately, with the same columns); `refresh_marts` iterates a list and stops swallowing real
failures (#96, which can merge before any of this). *Built (#96):* `maintenance.MATVIEWS`, to which
#100 appends the panel.

### 5.7 As built: point-in-time access and TTM (#95)

Migration 0036 ([data dictionary](../data_dictionary.md#the-statements-point-in-time-views-and-functions))
settles these points:

- **The seam.** Three definer-rights `mart` views hold every version of every period. They
  leave out `extra`, the vendor key and the hash. The functions are plain SQL with the caller's
  rights, reading only those views. They are inlined, so a filter on `security_id` reaches
  the indexes. `EXECUTE` goes to the read roles, not PUBLIC (an ADR 0009 status note).
- **The latest period** is the latest any statement knows at `as_of`. A statement that does
  not yet have it is NULL for it, rather than an older period's.
- **A joined row** is known from the latest of its statements' `known_from`. Its
  `knowledge_source` is the weakest of theirs: `estimated`, then `observed`, then `filed`.
- **TTM rows** are per income-statement quarter-end. The cash flow is counted on its own
  (`cash_flow_quarters_used`).
- **TTM contiguity** means consecutive fiscal labels, with period ends 8 to 18 weeks apart.
  The FMP prompt's 80–100 days would drop a retailer's 16-week first quarter.
- **A NULL flow** in any of the four quarters makes the TTM NULL.
- **Share terms.** Share counts take the latest quarter, in the terms of
  `income_known_from` / `balance_sheet_known_from`.
- **Not built:** a two-axis TTM. The two-axis read is `fn_statements_as_of`'s. Average
  balances for ROE-like ratios are the panel's (#100).

### 5.8 As built: the factor panel (#100)

Migration 0037 ([data dictionary](../data_dictionary.md#the-factor-panel-materialized-view-and-functions),
[definitions](../fundamentals.md#the-factor-panel-100)) builds §5.3. Where it departs from the
FMP prompt's §5.3, or adds a rule it did not have:

- **One function, any dates.** `mart.fn_fundamental_panel(as_of_dates[], extra_lag_days)`
  defines every factor. The matview and `fn_fundamental_snapshot(as_of, extra_lag_days)` select
  it. The prompt's one-date function would have been evaluated once per month-end, and each
  evaluation rebuilds every TTM in history: the cost grows with months times history.
- **Once per state.** A TTM, or a joined year, changes only when a version under it starts or
  ends. Each is computed once per such state, from the quarters it can reach, and every date
  reads the state it falls in. The same synthetic load refreshes in 45 s. A test holds the TTM
  equal to `mart.fn_fundamentals_ttm`'s at every month-end of a history with a restatement, a
  late filing and a missing quarter.
- **The extra lag is an argument, not a config key.** A matview cannot read `fafnir.toml`. The
  panel is built at 0; a researcher calls the function with a lag. A negative lag returns
  nothing.
- **A two-year horizon (new).** A basis that ended more than 730 days before the knowledge date
  gives no row: the company has stopped reporting, and its figures against today's price would
  mislead. It also bounds what one date reads. Stricter staleness rules are a research choice
  downstream (`fiscal_date`, `days_since_filing`).
- **The basis** is a TTM, or a fiscal year where that is newer (a filer of annual reports only);
  the TTM wins a tie. Year-ago and three-years-ago periods are the same basis labelled 4 and 12
  quarters back, within 730 and 1,826 days of it.
- **Rows** need a raw close in the week to `as_of_date`. A month whose calendar NYSE does not
  cover takes its last weekday.
- **Columns.**
  - Dropped: `is_derived_q4` (§5.3); `is_actively_trading`, which is today's flag, so a
    look-ahead in a history; and `version_no`, since a TTM basis rests on up to nine versions.
    `known_from` and `knowledge_source` describe them all.
  - `period_type` becomes `basis` (`ttm` or `annual`).
  - `sector_id` and `industry_id` become the names, as `mart.v_security_profile` exposes them.
    They are the security master's current classification, not point in time.
  - Added: `trading_currency`, `fiscal_year`, `fiscal_period`, `price_date`.
- **Planner settings.** The panel function runs with `enable_nestloop = off` and `jit = off`,
  for the reasons the [data dictionary](../data_dictionary.md#the-factor-panel-materialized-view-and-functions)
  gives.
- **#96's test** expected one view in `MATVIEWS`. It now expects both; the first refresh of the
  empty panel is plain.

### 5.9 As built: the aggregates and the latest snapshot (#101)

Migration 0038 builds §5.4–5.5
([data dictionary](../data_dictionary.md#martv_industry_fundamentals--the-panel-by-sector-and-industry-view)):

- **`mart.v_industry_fundamentals`** is a view over the panel, by sector and by sector and
  industry. Names, not ids: the panel carries the names. An industry is grouped under its
  sector, so the two levels nest.
- **The named subset** is sixteen factors: one or two per family, and market cap for size.
- **Suppression.** A group of fewer than three securities has no row, as §5.4 says. The same
  rule applies per factor: fewer than three values give NULL quartiles, with `_n` still shown.
- **`mart.security_fundamentals_latest`** is `fn_fundamental_snapshot(current_date)` joined to
  `core.security` for symbol and name, refreshed after the panel. `mart.security_latest` is not
  touched; #109 re-defines it separately.
- **#96's refresh test** now expects the third view, as it did the second for #100.

---

## 6. Ingestion: `src/fafnir/ingest/fundamentals.py` (#90)

### 6.1 Reading

- **The backfill** reads the ledger's current SF1 full file through
  `sharadar_ledger.select_by_ticker`, one batch per ticker.
- **The nightly** reads the SF1 delta files the night fetches (#106: SF1 added to `source
  sharadar-delta`, never paged by offset).
- **Splits** come from the ledger's ACTIONS file.
- **Applied state.** The run records the file ids it applied (`params.through`, as the price nightly
  does), and the file prune reads it (#106).

### 6.2 Identity

A row's ticker resolves to a permaticker through `core.vendor_security` (`vendor_table =
'fundamentals'`). The permaticker resolves to the security whose Sharadar link covers `fiscal_date`,
else to its open link. A row with neither is quarantined `fundamentals_unlinked` (`info`) and
counted. Never by bare ticker.

### 6.3 Validation and quarantine

| `check_name` | Condition |
|---|---|
| `fundamentals_filing_before_period` | `known_from < fiscal_date` |
| `fundamentals_missing_period_label` | `fiscalperiod` absent, or not `YYYY-FY` / `YYYY-Qn` |
| `fundamentals_value_out_of_range` | a value its column cannot hold |
| `fundamentals_duplicate_period` | two rows for one ticker, dimension and period in one file |
| `fundamentals_unlinked` | no security for the permaticker (§6.2) |

A value that does not parse fails the file in `sharadar_files`, which names its line. Reject the
**row**, not the security.

### 6.4 Cadence

Nightly: `fafnir ingest fundamentals` runs after `fafnir ingest sharadar`, `upkeep`-wrapped, so a
fundamentals failure never blocks prices (#106). The FMP prompt's weekly job and its request budget
do not apply.

### 6.5 DQ checks (#102)

The FMP prompt's §6.4 table, with these changes:

- **`balance_sheet_identity`** tolerates noncontrolling interest. SF1 has no balance item for it, so
  `total_assets − total_liabilities − total_equity_parent` is the noncontrolling interest where
  `net_income_noncontrolling ≠ 0`: `info` there, `error` elsewhere.
- **`cash_flow_tie`:** `ncfo + ncfi + ncff + ncfx = ncf`.
- **`cash_balance_tie`:** the change in `cash_and_equivalents` against `net_change_in_cash`, `warn`
  (restricted cash makes small gaps).
- **New:**
  - `fundamentals_suspicious_zero` (a zero where the company reports the item in neighbouring
    periods: probe item 6);
  - `fundamentals_implausible_filing_lag` (a filing under 5 days after the period end);
  - `fundamentals_vendor_correction` and `fundamentals_restatement` (counts and magnitudes);
  - `fundamentals_ytd_suspect`;
  - `fundamentals_version_chain` (a gap or an overlap in a period's knowledge intervals).
- **Changed:** `fundamentals_estimated_knowledge_date` keeps its name but now counts the §4.5 rule-3
  periods, the only estimated knowledge dates left.

`run_all` stays backward compatible: keys are added, `include_fundamentals=True`, and the checks
no-op without the tables.

### 6.6 As built (#90)

`fafnir ingest fundamentals` ([ingestion.md](../ingestion.md#statements-from-sf1-90)) settles
five points this plan left open:

- **The next filing** (§4.5, rule 3) is the issuer's earliest filing for a period ending on or
  after the estimated one. A 10-K for the same period end counts, since it carries the fourth
  quarter. A filing for an earlier period, filed late, does not.
- **An interval is never empty.** A restatement of a version known from the load day itself is
  known from the next day.
- **Rounding is not a change.** A share count that moved by no more than a recovery's rounding is
  unchanged: 1,000 shares, or a millionth of the count. This covers a count recovered across a
  deep reverse split, which carries the vendor's rounding times the ratio. The 1,000-share hash
  alone flips at a boundary.
- **Flags are per security and run.** Restatements and corrections are flagged once per security
  and run, with the largest moves. A restatement is `warn` from the nightly and `info` from a
  backfill, which dates the whole of SF1's restated history to one day. A flag per period would
  put the backfill's hundreds of thousands of restatements in the DQ queue.
- **Merges move statements.** `security merge` moves a victim's statements, keeping the
  survivor's versions of a period both hold. A fold refuses a security that holds statements,
  and a descope deletes them with it.

---

## 7. Backfill (#92)

There is no request budget: the backfill reads one file of 3,218,845 rows (a 632 MB zip, read
through in about 180 s, #52).

- **Steps:** dump; deploy and migrate; re-download the SF1 file if stale (`fafnir source
  sharadar-bulk --tables SF1 --force`); `fafnir ingest fundamentals --backfill`.
- **Resumable:** a commit per security, and a re-run is a no-op by hash.
- **An explicit backfill that writes nothing fails.**
- **Size, to measure before the production run** (#87 and #92 record it): the quarterly and annual
  first-reported and restated rows are about two thirds of SF1. With 1.7 versions per period across
  three tables, that is on the order of 6M rows, plus `extra`. The database was 22 GB on 2026-10-22.
  *Projected from a synthetic load (2026-10-26):* 3.3–5.8 GB with indexes, and about 40 minutes
  at the test machine's speed
  ([backfill.md](../backfill.md#the-fundamentals-backfill-92)).
- **Record:** rows per table, versions, restatements, corrections, quarantines, `knowledge_source`
  by decade, and the identity pass rate.

---

## 8. CLI surface

```
fafnir source probe-fundamentals [--from-api] [--json]   # shipped (#83)

fafnir ingest fundamentals
    [--backfill]                       # the ledger's full file; else the nightly deltas
    [--symbols AAPL,MSFT]              # default: every security with a Sharadar link
    [--period annual|quarter|both]     # default: both
    [--limit N] [--dry-run]

fafnir db refresh-marts                # unchanged flags; refreshes the panel too
fafnir dq run                          # unchanged flags; runs the fundamentals checks
fafnir status                          # appends fundamentals lines; existing lines byte-identical
```

`duk fs` and `duk fa` (#107) as the FMP prompt's §8, with three additions:

- `-S fmp` and `-S sharadar` refuse with the db-only message.
- `TICKER@date` is accepted (#80); it names the security, and `--as-of` bounds what was known.
- A secondary class answers with its issuer's primary-class statements, through the shared CIK,
  saying so (decision 5).

---

## 9. Non-breaking guarantees

The FMP prompt's ten hold, with the current numbers:

- New migrations from 0035, each with a tested `.down.sql`.
- The upgrade path is from 0034.
- `test_migrations_least_privilege.py` is extended: `fafnir_read` reads the new `core` tables, and
  `fafnir_app` reads the new `mart` objects and no new `core` table.
- **New config,** in a `[fundamentals]` section with defaults: `panel_extra_lag_days` (0) and
  `panel_start` (1998-01-01). The FMP prompt's filing-lag keys are not needed: no knowledge date
  is a statutory-lag estimate any more.

---

## 10. Testing

The FMP prompt's lists hold, with these additions:

**Unit:**
- the SF1 map, from a row;
- fiscal labels from `fiscalperiod` (AAPL's September year, MSFT's June);
- `known_from` by every rule of §4.5;
- share-term recovery: forward, reverse and rounded ratios;
- the hash at 1,000-share precision;
- routing: a first-reported change is a correction, a restated one a restatement.

**Integration:**
- **Idempotency.**
- **A restatement:** two versions, chained intervals, a flag.
- **A vendor correction:** two rows of one version, one current, same interval.
- **A split after load:** no version.
- **A period with no first-reported row:** `estimated`, from the next filing.
- **The look-ahead invariant,** and the version-chain invariant, as standing tests.
- **The split-era P/E band:** AAPL 1998-2000.
- **A secondary class:** no rows; `duk fs` answers through the CIK.
- **Pre-1998 rows** load, and the panel starts in 1998.
- **Golden values:** AAPL FY2020 revenue = 274,515,000,000; MSFT FY2015 in two versions, the
  restatement from the load day.

---

## 11. Documentation

As the FMP prompt's §11, with these changes:

- **The ADRs** are #86's, at the next free numbers (0018 and 0019 today).
- **The data dictionary** states the adjustment status as "as reported, in the share terms of
  `known_from`".
- **`doc/fundamentals.md`** (#108) explains the as-first-reported convention, what it does to growth
  factors, and the deferred research mode.

---

## 12. Sequence

| Step | Issue | Contents | Gate |
|---|---|---|---|
| 1 | #83 | The probe | Done (af4c776) |
| 2 | #85 | This document | Operator sign-off |
| 3 | #86 | ADRs 0018 (bitemporal fundamentals on SF1) and 0019 (derived metrics) | Merged and indexed |
| 4 | #87 | Migration 0035: three tables, `ref.statement_line_item` and its seed | Round-trips under a least-privilege owner |
| 5 | #90, #91 | The loader and its integration tests | Idempotency, restatement, correction, split no-op |
| 6 | #92 | The backfill, on the host | Counts recorded; `knowledge_source` by decade |
| 7 | #95, #96 | Point-in-time access and TTM (0036); `refresh_marts` | TTM edge cases |
| 8 | #100, #101 | The panel (0037), aggregates (0038) | Look-ahead invariant 0 on production |
| 9 | #102 | DQ checks, `fafnir status` | Existing DQ keys unchanged |
| 10 | #106 | The nightly: SF1 delta, prune, cadence | Timing recorded |
| 11 | #107, #108 | `duk fs/fa`, MCP tools, `doc/fundamentals.md` | `duk` regression green |
| 12 | #109 part 2 | Market cap and beta derived | |

A minor release closes P4 (the plan's P4 row).

---

## 13. Acceptance criteria

- [ ] The probe run on the host's ledger file agrees with the 2026-10-24 run; the EDGAR comparison is
      recorded.
- [ ] The backfill loads every linked security, dead issuers included, from the file, resumably.
- [ ] Re-running any load is a true no-op: zero new rows, versions, or interval changes. So is a
      synthetic split after load.
- [ ] `count(*)` of panel inputs with `known_from > as_of_date` is **0**, by a standing test.
- [ ] Every period's versions chain without a gap or an overlap, by a standing test.
- [ ] A restatement fixture produces two versions, the second known from its load day; a correction
      fixture produces two rows of one version.
- [ ] The AAPL split-era P/E test passes.
- [ ] The balance-sheet identity holds for **> 99%** of rows, noncontrolling interest aside; every
      exception carries a flag.
- [ ] `knowledge_source` is reported by decade.
- [ ] `fafnir_app` reads every new mart object and no new core table.
- [ ] Every existing test passes unmodified; a 0034 database migrates with byte-identical existing
      tables.

---

## 14. Ask before you assume

Stop and ask if:

- the host's probe run disagrees with the API run on any verdict;
- the EDGAR comparison finds a first-reported `date` that is not a filing date, or the pre-1997 ones
  look estimated;
- recovered share counts are fractional for more than a token share of forward-split periods;
- the backfill's restatement share is far beyond the probe's (over 90% of periods), which would mean
  a field that always differs;
- the size projection exceeds what the host's disk can take;
- anything here conflicts with an ADR: the ADR wins until superseded in writing.

### Departures from #85's text

#85 asked to keep the FMP prompt's §3, §4.5–4.8, §5, §9, §10 and §13 verbatim. SF1 changes §4.5–4.8
(knowledge dates, versioning, share terms, currency) and parts of §5 and §10, so those are restated
here; everything carried over is named as such. Its "expected 0031–0034" migrations are now 0035–0038.
