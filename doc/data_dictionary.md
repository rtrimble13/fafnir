# Fafnir Data Dictionary

Every schema, table, view, materialized view and function fafnir stores or serves,
with its **grain**, **source**, **units**, **adjustment status** and **update
cadence**. This is the contract between the warehouse and everything that reads it.

It describes the schema at migration **0040** (`industry_aliases`, release v3.0.4).
It was checked against the live catalog (`pg_class`, `pg_attribute`,
`pg_constraint`, `pg_index`, `pg_proc`, the view definitions and the column comments)
on 2026-10-31. A migration that adds, drops or re-types a column updates this file in
the same pull request: the PR template's first box.

Conventions:
- Money/prices are exact `NUMERIC` — never floating point. The factor panel's ratios
  are the one deliberate exception (`double precision`, see
  [the factor panel](#the-factor-panel-materialized-view-and-functions)).
- Temporal columns are suffixed by meaning:
  - `*_date` is the observation;
  - `loaded_at`, `computed_at`, `fetched_at`, `created_at` and `updated_at` are
    process time;
  - `valid_from`/`valid_to` and `known_from`/`known_to` are the knowledge interval.
- "Adjustment status" states whether price values are **raw** (unadjusted) or
  **adjusted** (split/dividend back-adjusted).
- A `source` column says **who supplied the row**: `fmp`, `sharadar`, or `operator`
  for a row a person wrote through the CLI. ADR 0015 also reserves `tiingo`, and the
  vendor tables' CHECKs allow it, but Tiingo was not adopted (2026-10-30) and no row
  carries it. Only the statement tables constrain `source` with a CHECK. Everywhere
  else the value is the loader's.
- **Vendor text is data.** `company_name`, `core.company_profile.description` and the
  vendor master's names are third-party strings, stored as sent.

---

## Inventory

Seven schemas: the six below plus `public`, which holds only the `pg_stat_statements`
extension's two views and three functions (query statistics, no fafnir data). Row
counts are as of 2026-10-31, to give the scale: exact where a section below quotes a
count, the planner's estimate otherwise. They are not maintained.

| Relation | Kind | Grain | Written by | ≈ rows |
|---|---|---|---|---|
| [`core.security`](#coresecurity--security-master) | table | `security_id` | security-master loads, `vendor mint-delisted`, `security split-history` | 33.9k |
| [`core.symbol_xref`](#coresymbol_xref--ticker-history--cross-reference) | table | `(symbol, valid_from)` | security-master loads, renames | 40.2k |
| [`core.symbol_change`](#coresymbol_change--applied-ticker-renames) | table | `symbol_change_id` | `ingest symbol-changes`, `ingest sharadar-universe` | 243 |
| [`core.company_profile`](#corecompany_profile--long-form-description) | table | `security_id` | `ingest securities --enrich` | 20.6k |
| [`core.daily_price`](#coredaily_price--raw-daily-ohlcv---primary-fact) | partitioned table | `(security_id, trade_date)` | price loaders, `prices shift\|rescale` | ≈ 99M |
| [`core.corporate_action`](#corecorporate_action--splits--cash-dividends) | table | `(security_id, action_type, ex_date)` | action loaders, `actions add\|redate` | 931k |
| [`core.adjustment_factor`](#coreadjustment_factor--derived-back-adjustment-factors) | table | `(security_id, effective_date)` | `fafnir adjust` | 894k |
| [`core.vendor_security`](#corevendor_security--a-vendors-own-security-master-grain-vendor-vendor_table-vendor_key) | table | `(vendor, vendor_table, vendor_key)` | `ingest sharadar-master` | 61.2k |
| [`core.security_vendor_xref`](#coresecurity_vendor_xref--fafnirs-security--a-vendors-key-over-time-grain-vendor-vendor_key-valid_from) | table | `(vendor, vendor_key, valid_from)` | `vendor link`, `vendor mint-delisted`, minting | 27.5k |
| [`core.security_vendor_rejection`](#coresecurity_vendor_rejection--this-security-is-not-that-key-0032-63-grain-security_id-vendor-vendor_key) | table | `(security_id, vendor, vendor_key)` | `vendor link reject` | 0 |
| [`core.vendor_security_event`](#corevendor_security_event--a-vendors-lifecycle-events-grain-vendor-vendor_key-event_date-action-contra_ticker) | table | `(vendor, vendor_key, event_date, action, contra_ticker)` | `ingest sharadar-master` | 154k |
| [`core.income_statement`, `core.balance_sheet`, `core.cash_flow_statement`](#coreincome_statement-corebalance_sheet-corecash_flow_statement) | tables | `(security_id, period_type, fiscal_date, version_no, loaded_at)` | `ingest fundamentals` | 1.29M, 1.04M, 1.22M |
| [`core.v_security_classification`](#corev_security_classification--the-classification-readers-see-view) | view | `security_id` | — | — |
| [`mart.v_daily_price_raw`](#martv_daily_price_raw--unadjusted-ohlcv-view) | view | `(security_id, trade_date)` | — | — |
| [`mart.v_daily_price_adjusted`](#martv_daily_price_adjusted--adjusted-ohlcv-view) | view | `(security_id, trade_date)` | — | — |
| [`mart.v_symbol_lookup`](#martv_symbol_lookup--ticker--security_id-view) | view | `(symbol, valid_from)` | — | — |
| [`mart.v_security_profile`](#martv_security_profile--descriptive-profile-view) | view | `security_id` | — | — |
| [`mart.v_security_price_coverage`](#martv_security_price_coverage--what-is-held-per-security-view) | view | `security_id` with bars | — | — |
| [`mart.v_security_action_summary`](#martv_security_action_summary--corporate-actions-per-security-view) | view | `security_id` with actions | — | — |
| [`mart.v_corporate_action`](#martv_corporate_action--corporate-actions-one-row-per-event-view) | view | `(security_id, action_type, ex_date)` | — | — |
| [`mart.v_security_lifecycle`](#martv_security_lifecycle--when-a-security-traded-and-why-it-stopped-view) | view | `security_id` | — | — |
| [`mart.v_income_statement_versions`, `_balance_sheet_`, `_cash_flow_statement_`](#the-statements-point-in-time-views-and-functions) | views | the core table's | — | — |
| [`mart.v_statements_current`, `mart.v_fundamentals_ttm`](#the-statements-point-in-time-views-and-functions) | views | `(security_id, period_type)`; `(security_id, fiscal_date)` | — | — |
| [`mart.fundamental_panel`](#the-factor-panel-materialized-view-and-functions) | materialized view | `(security_id, as_of_date)` | `db refresh-marts` | 1.83M |
| [`mart.v_industry_fundamentals`](#martv_industry_fundamentals--the-panel-by-sector-and-industry-view) | view | `(group_level, as_of_date, sector_name, industry_name)` | — | — |
| [`mart.security_fundamentals_latest`](#martsecurity_fundamentals_latest--each-securitys-factors-now-materialized-view) | materialized view | `security_id` | `db refresh-marts` | 5.4k |
| [`mart.v_security_dq_open`](#martv_security_dq_open--open-dq-flags-per-security-view) | view | open `dq_flag_id` with a security | — | — |
| [`mart.security_latest`](#martsecurity_latest--screening-snapshot-materialized-view) | materialized view | `security_id` | `db refresh-marts` | 33.9k |
| [`mart.v_security_attributes`](#martv_security_attributes--the-screeners-attributes-by-precedence-view) | view | `security_id` | — | — |
| [`ref.exchange`](#refexchange--grain-exchange_code) | table | `exchange_code` | seed `0001_exchanges.sql` | 8 |
| [`ref.tracked_symbol`](#reftracked_symbol--declared-universe-grain-source-symbol) | table | `(source, symbol)` | `track add\|rm` | 7 |
| [`ref.sector`, `ref.industry`](#refsector--refindustry--the-sector-and-industry-names) | tables | `sector_id`; `industry_id` | security-master loads | 11; 111 |
| [`ref.statement_line_item`](#refstatement_line_item--the-fundamentals-taxonomy-grain-statement-column_name) | table | `(statement, column_name)` | migration 0035 | 58 |
| [`ref.trading_calendar`](#reftrading_calendar--grain-exchange_code-trade_date) | table | `(exchange_code, trade_date)` | `db seed`, the nightly horizon | 49.1k |
| [`ops.ingestion_run`](#opsingestion_run--lineage-grain-ingestion_run_id) | table | `ingestion_run_id` | every load and every operator edit | 635 |
| [`ops.data_quality_flag`](#opsdata_quality_flag--quarantineanomaly-queue-grain-dq_flag_id) | table | `dq_flag_id` | the loaders, `dq run`, dorq | 1.56M |
| [`ops.load_watermark`](#opsload_watermark--incremental-marks-grain-source-endpoint-security_id) | table | `(source, endpoint, security_id)` | the loaders, dorq | 128k |
| [`ops.operator_override`](#opsoperator_override--operator-corrections-grain-override_id) | table | `override_id` | `actions`, `prices`, `security split-history` | 343k |
| [`ops.vendor_correction`](#opsvendor_correction--rows-one-vendor-replaced-grain-correction_id) | table | `correction_id` | `vendor correct` | 704k |
| [`ops.dq_label`](#opsdq_label--labels-a-person-stated-grain-label_id) | table | `label_id` | `dq label`, `dq accept\|resolve --label` | 129 |
| [`landing.fmp_raw`](#landingfmp_raw--raw-immutable-payloads-grain-raw_id) | table | `raw_id` | the FMP loaders | 915k (6.2 GB) |
| [`landing.sharadar_file`](#landingsharadar_file--the-sharadar-file-ledger-grain-file_id) | table | `file_id` | `source sharadar-bulk\|delta\|prune` | 34 |
| [`meta.schema_migration`](#metaschema_migration--migration-bookkeeping-grain-version) | table | `version` | `db migrate` | 40 |

The mart functions are `mart.fn_statements_as_of` (two forms),
`mart.fn_fundamentals_ttm`, `mart.fn_fundamental_panel`,
`mart.fn_fundamental_snapshot`, `mart.fn_ratio`, `mart.fn_growth` and
`mart.fn_security_betas`, described with the views they serve.

## Roles and privileges

| Role | Schemas it can use | Reads | Writes |
|---|---|---|---|
| `fafnir_ingest` | all six | everything | `landing`, `core`, `ref`, `ops` (0001's default privileges). `meta` and the mart objects belong to the role that runs the migrations. |
| `fafnir_read` | `core`, `mart`, `ref` | the same | — |
| `fafnir_app` | `mart`, `ref` | the same | — |
| `fafnir_ops` | all six | everything (SELECT only; the agent tier, migration 0021, ADR 0010) | — |

- **`mart` is the read seam** ([ADR 0009](adr/0009-mart-is-the-read-seam.md)). Its
  views run with their owner's rights, so `fafnir_app` reads core-derived data without
  any grant on `core`. A `mart` view must never set `security_invoker = true`. Adding
  one is a grant to every reader.
- **The mart functions** run with their caller's rights. They read only mart views,
  so the same holds. EXECUTE is granted to `fafnir_read`, `fafnir_app` and
  `fafnir_ops`, and revoked from PUBLIC.
- **Login roles.** Per-person and per-agent login roles are members of these groups.
  They are deployment facts, not migrations.

---

## Schema: `core`

### `core.security` — security master
**Grain:** one row per minted `security_id`. **Source:** FMP `stock-list` / `etf-list`
(+ `profile` enrichment), and Sharadar's TICKERS for the securities it mints (the dead
issuers, #71; new listings since the cutover, #93). **Cadence:** nightly, by `ingest
sharadar-universe` in Sharadar mode and `ingest securities` in FMP mode; profile
fields on enrichment.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT IDENTITY PK | Surrogate identity. Join all facts on this. |
| `primary_symbol` | TEXT NOT NULL | Current/best-known ticker. **Not** an identifier. |
| `company_name` | TEXT | |
| `asset_type` | TEXT CHECK | `equity` / `etf` / `fund` / `other`. |
| `exchange_code` | TEXT → ref.exchange | Current listing venue. Mutable; **not** part of identity — a venue transfer keeps the `security_id`. |
| `sector_id` | INT → ref.sector | Set during profile enrichment. |
| `industry_id` | INT → ref.industry | Set during profile enrichment. |
| `currency` | TEXT | Default `USD`. |
| `country` | TEXT | |
| `cik` / `isin` / `cusip` | TEXT | External identifiers (nullable). Populated from the vendor profile, so a security never profiled carries none. A lookup key for `duk ph cik:51143` / `duk ls cusip:459200101`, matched on a normalised form (zero-padding, case and grouping ignored) and indexed as such by migration 0023. Not unique: one CIK covers every share class of an issuer. |
| `is_actively_trading` | BOOLEAN NOT NULL | Default TRUE. False for delisted/inactive. The vendor's view, copied by the nightly load: it is not evidence that a listed security stopped trading (FMP called ET inactive the week it moved to TXSE), so the nightly universes also poll a listed row with a bar in the last 30 days (ADR 0014). |
| `is_etf` / `is_fund` | BOOLEAN NOT NULL | The vendor's flags (default FALSE), independent of `asset_type`. **Every fund is stored `asset_type = 'equity'` with `is_fund = true`** (5,657 rows on 2026-10-31; none carries `asset_type = 'fund'`). The price loader treats a security as NAV-priced when `asset_type = 'fund'`, or `is_fund` and `asset_type <> 'etf'` (`daily_price._is_nav_priced`). It then reads a bar with no volume as a NAV strike: a closed-end fund with real volume is still a session. |
| `ipo_date` | DATE | |
| `market_cap_usd` | NUMERIC(24,2) | The vendor's market-cap snapshot: FMP's `company-screener`, refreshed by each FMP security-master load (moved here from `core.company_profile` by migration 0010), and frozen since the cutover for the securities Sharadar feeds. **Not history**: never use it in a backtest. `mart.v_security_attributes` derives its own where SF1 holds the shares (#109). |
| `beta` | NUMERIC(12,6) | The vendor's beta, from the same snapshot and with the same caveats. `mart.v_security_attributes` derives its own from a year of returns. |
| `venue_override` / `venue_override_left` | TEXT | An operator-recorded venue (`fafnir security venue-transfer`, migration 0027), and the venue the security left. While set, the nightly upsert keeps `exchange_code` and `is_actively_trading` as they are, until the vendor reports a venue other than `venue_override_left`; then both clear. ADR 0014. |
| `delisted_date` | DATE | NULL while listed; set (never deleted) on delist. |
| `source` | TEXT | The party that **minted** the security, not the one that feeds it now ([ADR 0015](adr/0015-multi-vendor-provenance-and-precedence.md)). Values: `fmp` (default), `operator` and `sharadar` (`tiingo` is reserved and unused). The FMP loaders feed `source = 'fmp'` rows and nothing else: their universes filter on it, and a per-symbol pull that resolves to any other row is refused, because FMP serves that ticker for a different issuer. `operator` is a security minted by `fafnir security split-history` to hold one issuer's history split off a reused ticker: delisted, carrying a closed `symbol_xref` period, fed by nobody. `sharadar` is a dead issuer minted by `fafnir vendor mint-delisted` (#71, ADR 0016 §3): delisted, `is_actively_trading` false, the vendor's ticker as `primary_symbol`, a closed `symbol_xref` period (`source = 'sharadar'`) per ticker it traded under, and a `minted`, feeding link to its permaticker. Its bars come from the Sharadar backfill (#75). Since the cutover `ingest sharadar-universe` also mints new listings as `sharadar` rows: active, with an open ticker period (#93). |
| `first_seen_at` / `updated_at` | TIMESTAMPTZ | Load process time. The nightly FMP load stamps `updated_at` on every row the screener still lists, so it is the record of what the screener returned (the screener payload is not kept in landing). |

Soft natural key: `UNIQUE (source, primary_symbol) WHERE delisted_date IS NULL`
(`ux_security_active_source_symbol`) makes the security upsert idempotent without
keying on the ticker globally. Scoped to *listed* rows (0009) so a reused ticker mints
a new `security_id` instead of overwriting a dead issuer. The **exchange is
deliberately not part of it** (0012): it is an attribute of the listing, not the
company, so a venue transfer (NYSE → NASDAQ) updates the security that already holds
the history rather than forking it into a second row.

**Constraints and indexes.**
- `asset_type` is CHECKed to `equity`, `etf`, `fund` and `other`.
- Foreign keys: `exchange_code` and `venue_override` → `ref.exchange`, `sector_id` →
  `ref.sector`, `industry_id` → `ref.industry`.
- `source` has no CHECK.
- Indexes: `primary_symbol`; `(primary_symbol, delisted_date)` for delisted rows;
  `is_actively_trading`; `first_seen_at DESC`; and the normalised `cik`, `isin` and
  `cusip` (0023).

**On 2026-10-31:** 33,889 rows. By minter: 21,850 `fmp`, 11,981 `sharadar` and 58
`operator`. 16,344 are delisted and 15,821 actively trading. 25,294 are `equity`,
8,592 `etf` and 3 `other`. 11 hold a venue override.

### `core.symbol_xref` — ticker history / cross-reference
**Grain:** `(symbol, valid_from)`, the primary key. **Source:** derived by the
security-master loads, renames, minting and `security split-history`. **Cadence:** on
security loads.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT NOT NULL → core.security | Indexed. |
| `symbol` | TEXT NOT NULL | External ticker. |
| `valid_from` | DATE NOT NULL | Start of validity (default `1900-01-01`). |
| `valid_to` | DATE | NULL = currently valid. `CHECK (valid_to IS NULL OR valid_to >= valid_from)`. |
| `is_primary` | BOOLEAN NOT NULL | Default TRUE. The security's primary ticker in the interval. |
| `source` | TEXT NOT NULL | Who wrote the period: `fmp` (default), `sharadar` (a minted dead issuer's tickers, a Sharadar rename or listing) or `operator` (`security split-history`). |
| `created_at` | TIMESTAMPTZ NOT NULL | When the row was written. |

Resolves a ticker to a `security_id` at any point in time (handles renames/relists).
A rename closes the old ticker's period the day *before* the change and opens a new
one for the new ticker against the **same** `security_id`, so the two periods are
contiguous and never both open. Resolution order (`resolve_security_id`, mirrored in
`duk.datasource.db`): the open period, then `core.security.primary_symbol`, then the
most recently closed period — so a reused ticker resolves to its live owner while a
company's former ticker still reaches its history.

**A ticker has at most one open period**, enforced since migration 0015
(`ux_symbol_xref_open`, unique on `symbol` where `valid_to IS NULL`). Resolution
reads only open periods, so a second one is not a second answer — it is a row the
resolver ignores while point-in-time queries still read it. 0015 also repaired the
warehouses that had accumulated them: a duplicate of the same security was deleted,
and a second security's claim was closed the day before the surviving period opens.

### `core.symbol_change` — applied ticker renames
**Grain:** one row per `(source, old_symbol, new_symbol, change_date)`. **Source:**
FMP `symbol-change` (`source = 'fmp'`) and, since the cutover, Sharadar's rename events
(`source = 'sharadar'`, `fafnir ingest sharadar-universe`). **Cadence:** nightly.

| Column | Type | Notes |
|---|---|---|
| `symbol_change_id` | BIGINT IDENTITY PK | |
| `old_symbol` / `new_symbol` | TEXT NOT NULL | `CHECK (old_symbol <> new_symbol)`. |
| `change_date` | DATE NOT NULL | Effective date; the xref period boundary. |
| `security_id` | BIGINT → core.security | The security the rename was applied to; NULL while unapplied. Also NULL once `fafnir security descope` has removed that security (ADR 0012): the row is kept, detached, so the sweep still knows the rename was recorded. |
| `company_name` | TEXT | As reported with the rename. |
| `status` | TEXT CHECK | `applied` / `conflict` / `ignored` / `dismissed` — see below. |
| `detail` | JSONB | Context: the `folded_security_id` of an absorbed duplicate, the `merged_security_id` of one merged by hand, or `dismissed_by` / `dismissed_note` / `dismissed_at`. A `conflict` refused because it would change the instrument carries that as `reason`. An applied rename records `old_company_name`, the security's name before the rename overwrote it: the security-master load declines a screener entry for the old ticker under that name (or the security's current name, or `company_name` above) as an echo instead of minting a copy of the company. `echo_names` lists further names it declines on that ticker: each one recorded by `security merge` from a re-minted echo it folded back in. |
| `source` | TEXT NOT NULL | `fmp` (default) or `sharadar`. Part of the natural key (`ux_symbol_change_natural`), so the two vendors' records of one rename are two rows. |
| `first_seen_at` / `updated_at` | TIMESTAMPTZ | |

Indexed on `old_symbol`, on `new_symbol`, and on `change_date DESC` where the status is
not `applied` (the sweep's retry list).

- `applied` — carried onto an existing `security_id` (terminal; never downgraded).
- `conflict` — the new ticker already belongs to another **listed** security that
  carries history. Nothing was changed; retried on every sweep and raised as a
  `symbol_change_conflict` DQ flag. Also the status, with `detail.reason`, of a
  rename that would change what the instrument is: a share onto its own warrant,
  right or unit ticker, or back (`ABCD -> ABCDU`, the vendor's pre-launch shuffle;
  ADR 0012, `fafnir.instruments.rename_changes_instrument`). Such a rename is never
  carried, so it waits for `fafnir security dismiss-rename`.
- `ignored` — the old ticker belongs to a delisted issuer, so this is ticker
  *reuse*, not a rename.
- `dismissed` (0018) — an operator judged the reported rename not to be a rename
  at all: a bad feed row. Terminal, so the sweep stops retrying it. Set only by
  `fafnir security dismiss-rename`, never by a loader, and the reasoning is kept in
  `detail`. It is **not** the way to close a rename that is real but blocked —
  that one is merged with `fafnir security merge-rename` and reaches `applied`.
  `fafnir security descope` also sets it, on a `conflict` naming a security it
  removes: the removal would otherwise leave the rename unresolvable, or let the
  sweep carry it onto the security that remains.

Renames of tickers fafnir does not track are counted but not stored — the feed is
global across every venue.

### `core.company_profile` — long-form description
**Grain:** one row per `security_id`. **Source:** FMP `profile`. **Cadence:** only when
`fafnir ingest securities --enrich` runs (one request per symbol, so it is optional).
Every profile payload is kept in `landing.fmp_raw`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT PK → core.security | |
| `description` | TEXT | The vendor's company description, as sent: third-party text. |
| `loaded_at` | TIMESTAMPTZ NOT NULL | When the description was loaded; `mart.v_security_profile` exposes it as `description_loaded_at`. |
| `source` | TEXT NOT NULL | `fmp` (default; every row on 2026-10-31). |

Migration 0010 trimmed this table to the description. It moved `market_cap_usd` and
`beta` to `core.security`, where the screener supplies them on every load, and dropped
`ceo`, `full_time_employees`, `website`, `last_dividend`, `price_range` and
`image_url`. Those fields survive only in the `profile` payloads in landing. 20,565
rows on 2026-10-31.

### `core.daily_price` — raw daily OHLCV  ⟵ primary fact
**Grain:** `(security_id, trade_date)`. **Source:** since the cutover (2026-10-22),
Sharadar's SEP and SFP for the securities it feeds (`source = 'sharadar'`, as traded);
FMP `historical-price-eod/non-split-adjusted`, the *unadjusted* endpoint, for what FMP
still feeds and for the history it loaded. `.../full` is already split-adjusted and
must not be used here; see [adr/0004](adr/0004-unadjusted-price-feed.md).
**Units:** price NUMERIC(20,6) in the security's currency; volume shares (BIGINT).
**Adjustment status:** **RAW (unadjusted).** **Cadence:** daily; immutable once the
day closes.

**Partitions.** Range-partitioned by `trade_date`, one partition a year:
`core.daily_price_y1990` … `core.daily_price_y2028`. `fafnir db` maintenance
(`ensure_horizon`) creates the next years' partitions and calendar ahead of time.
`core.daily_price_default` catches any date outside them: on 2026-10-31 it held two
FMP bars dated 1989-12-28 and 1989-12-29 (security 4864). The only index is the
primary key `(security_id, trade_date)`, created on every partition. A query that does
not bound `trade_date` or name a `security_id` touches all 40 partitions. The table
held about 99 million bars on 2026-10-31.

Because these are the prices as traded, the series contains split-sized jumps. That
is correct: a 4:1 split shows a ~75% drop with no gap in the data.

**NAV-priced securities** share this table and this grain: `asset_type = 'fund'`, or
`is_fund` on anything but an ETF (see `core.security.is_fund`). A fund is struck once
a day and does not trade, so its bar carries `open = high = low = close = NAV` and
`volume = 0`. That is the whole truth about the day, not three missing fields: the
price loader expands a NAV-only payload for a NAV-priced security and still
quarantines one for an equity. Distributions back-adjust through
`core.adjustment_factor` exactly as cash dividends do, so `mart.v_daily_price_adjusted`
returns a total-return NAV series with no special case.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT NOT NULL → core.security | |
| `trade_date` | DATE NOT NULL | Exchange trading day. |
| `open` `high` `low` `close` | NUMERIC(20,6) NOT NULL | **Raw** prices. Six decimal places: a price that rounds to zero at that scale is quarantined (`price_subresolution_price`), as is one too large for it (`price_price_out_of_range`). |
| `volume` | BIGINT NOT NULL | Default 0. **Raw** volume (shares), in the share count of the day it traded. Taken from `unadjustedVolume` where the payload offers it, else `volume`. |
| `vwap` | NUMERIC(20,6) | Volume-weighted average price (nullable). |
| `source` | TEXT | Who supplied the bar (a `sharadar` bar is **vendor-derived raw**: recovered from SEP/SFP's `closeunadj` and the ACTIONS split product by `fafnir ingest sharadar-prices`, #75 and ADR 0004's amendment): `fmp`, `sharadar`, `tiingo`, or `operator` for a vendor bar an operator re-dated or re-scaled with `fafnir prices shift` / `rescale` (see `ops.operator_override`, migration 0026). Writes follow ADR 0015 precedence (`operator > sharadar > tiingo > fmp`): a vendor revises its own bars and leaves every other party's alone; a higher-ranked vendor replaces a lower one's bar only in an explicit correction, which keeps the replaced bar in `ops.vendor_correction` (0029). No vendor overwrites an `operator` bar: a vendor bar on its date is set aside. |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | Lineage. NULL on an `operator` bar; the vendor bar it replaced, with its run, is kept in the override's `detail.row`. |
| `loaded_at` | TIMESTAMPTZ NOT NULL | |

Constraints reject impossible bars (`ck_daily_price_*`): `high>=low,open,close`;
`low<=open,close`; `volume>=0`; all prices `>0`. A bar that fails them never reaches
the table: the loader quarantines it as a `price_*` flag instead.

**Who wrote the bars.** The planner's statistics on 2026-10-31 show `fmp`,
`sharadar` and `operator` bars in every yearly partition from 1997 on, and `fmp` and
`operator` only in 1990–1996. The universe is survivorship-complete only from
1998-01-01 (ADR 0016; `mart.v_security_lifecycle.survivorship_complete_from`).

### `core.corporate_action` — splits & cash dividends
**Grain:** `(security_id, action_type, ex_date)`. **Source:** Sharadar's ACTIONS for
the securities it feeds (`ingest sharadar-actions`); for what FMP still feeds, FMP
`splits` / `dividends` (per security) or `splits-calendar` / `dividends-calendar`
(market-wide), per `actions_mode`. **Cadence:** daily. Never holds an `ex_date` in the future — a
declared-but-not-yet-ex event would back-adjust prices for something that has not
happened (ADR 0007).

| Column | Type | Notes |
|---|---|---|
| `corporate_action_id` | BIGINT IDENTITY PK | |
| `security_id` | BIGINT NOT NULL → core.security | |
| `action_type` | TEXT NOT NULL CHECK | `split` or `dividend`. |
| `ex_date` | DATE NOT NULL | Ex-date (the adjustment boundary). |
| `record_date` `payment_date` `declaration_date` | DATE | Dividend dates (nullable). |
| `split_numerator` / `split_denominator` | NUMERIC(20,6) | 2-for-1 ⇒ 2 / 1. Both `> 0` on a split (`ck_corp_action_split`). |
| `dividend_amount` | NUMERIC(20,6) | Cash per share. NOT NULL and `>= 0` on a dividend (`ck_corp_action_dividend`). |
| `currency` | TEXT NOT NULL | Default `USD`. |
| `source` | TEXT | Who supplied the row: `fmp`, `sharadar`, `tiingo`, or `operator` for a row written by `fafnir actions add` / `redate` (see `ops.operator_override`). Writes follow ADR 0015 precedence, as for `core.daily_price.source`: a replaced vendor row is kept in `ops.vendor_correction`. The loaders never overwrite an `operator` row, and the FMP reconciliation compares only `fmp` rows against its feed: an `operator` or `sharadar` row is never reported as drift. `sharadar` rows come from `fafnir ingest sharadar-actions` (#73): every split and dividend of a security Sharadar minted, and, fill-only, what an FMP security linked to Sharadar lacks. A Sharadar dividend is stored as declared (its ACTIONS value times the later splits' product). Spin-offs are not stored here, from either vendor: they are events. |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | The run that last **changed** this row, not the last that read it — an unchanged re-load leaves both lineage columns alone, which is what makes `fafnir adjust --changed` a small set. NULL on an `operator` row. |
| `loaded_at` | TIMESTAMPTZ | Last change, as above. |

Unique on `(security_id, action_type, ex_date)`, so a security holds at most one split
and one dividend per ex-date; also indexed on `(security_id, ex_date)`. On 2026-10-31:
912,080 dividends (815,947 `fmp`, 95,840 `sharadar`, 293 `operator`) and 18,580 splits
(13,609 `fmp`, 4,927 `sharadar`, 44 `operator`).

### `core.adjustment_factor` — derived back-adjustment factors
**Grain:** `(security_id, effective_date)`. **Source:** derived from
`core.corporate_action`. **Cadence:** recomputed by `fafnir adjust` after each
actions load. Deterministic and point-in-time stable.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT NOT NULL → core.security | Primary key `(security_id, effective_date)`. |
| `effective_date` | DATE NOT NULL | An ex-date boundary. |
| `cumulative_price_factor` | NUMERIC NOT NULL | Default 1.0. Multiply RAW price by this for `trade_date < effective_date`. Unconstrained scale (0013); `> 0` (`ck_adj_factor_positive`). |
| `cumulative_volume_factor` | NUMERIC NOT NULL | Default 1.0. Multiply RAW volume by this for `trade_date < effective_date`. Moves inversely to the price factor. `> 0`. |
| `computed_at` | TIMESTAMPTZ NOT NULL | |

A bar reads the factor of the **first boundary after its date**. A bar on or after
the latest ex-date has no factor row and reads 1.0 (`mart.v_daily_price_adjusted`).

---

### Vendor identity (migration 0031, ADR 0015)

A vendor's view of a security lives beside `core.security`, never in it. Migrations
0031 and 0032 create these tables empty. The Sharadar master and events load from TICKERS and ACTIONS
(#58). Links come from the linking engine (#62), manual review (#63) and minting
(#71). `fafnir_read` and `fafnir_ops` read them. No mart view exposes them directly:
`mart.v_security_lifecycle` reads the events, and `core.v_security_classification`
the master's sectors. The `vendor` column is CHECKed to `fmp`, `sharadar` and `tiingo`
on all four tables. On 2026-10-31 every row is `sharadar`.

#### `core.vendor_security` — a vendor's own security master. **Grain:** `(vendor, vendor_table, vendor_key)`.

| Column | Type | Notes |
|---|---|---|
| `vendor` | TEXT CHECK | `sharadar`, `tiingo`, `fmp`. |
| `vendor_table` | TEXT | Sharadar's modern table name the row describes (`stocks`, `funds`, `fundamentals`, `insiders`). TICKERS has one row per table a security is in, all with one permaticker. |
| `vendor_key` | TEXT | Sharadar's `permaticker`, as text. |
| `ticker` | TEXT | The vendor's ticker **today**. A dead issuer's reused ticker carries a numeric suffix (`CLA1`), and history is filed under it. |
| `name`, `exchange`, `category`, `is_delisted` | | As the vendor states them. |
| `cusips`, `related_tickers` | TEXT[] | Space-separated lists in TICKERS. |
| `figi`, `cik`, `sic_code` | TEXT | `cik` from the `secfilings` URL. |
| `sector`, `industry`, `fama_industry`, `currency`, `location` | TEXT | |
| `first_price_date` / `last_price_date` | DATE CHECK | The price history's span; last ≥ first. |
| `first_quarter` / `last_quarter` | DATE | The fundamentals' span. |
| `attributes` | JSONB | Vendor fields without a column (`scalemarketcap`, `scalerevenue`, `sicsector`, `sicindustry`, `firstadded`, `secfilings`, `companysite`). |
| `vendor_last_updated` / `ingestion_run_id` / `loaded_at` | | The vendor's `lastupdated`, the run and the load time. `ingestion_run_id` and `loaded_at` move only when a value changes. |

**Population (`fafnir ingest sharadar-master`, #58).** One row per TICKERS row:
- **`vendor_table`:** the file's `table` column is in legacy codes (`SEP`, `SFP`,
  `SF1`, `SF2`), which map to the modern names. `SF3B` rows (`holdings_investor`)
  list institutional investors, not securities, and are skipped.
- **Mapped fields:**
  - `isdelisted` `Y`/`N` becomes `is_delisted`;
  - `cik` is the digits of the `secfilings` URL's `CIK=` parameter, as the SEC
    writes them (ten, zero-padded);
  - `firstquarter` / `lastquarter`, which are text in the file, become dates.
- **Not mapped:** values are stored as the vendor states them. `exchange` stays
  `NYSEARCA`, for instance; `fafnir.ingest.sharadar_map` maps it where a loader
  needs fafnir's code ([ingestion.md](ingestion.md#sharadar-field-mapping)).
- **Upserted:** a row equal to the stored one is not written. Nothing is deleted, so a
  row the vendor stops sending stays.

Measured on the 2026-10-19 full file: 74,420 rows, 61,146 loaded, 13,274 investors
skipped. On 2026-10-31 the table held 21,025 `stocks`, 17,877 `fundamentals`, 12,270
`insiders` and 10,037 `funds` rows. Indexed on `(vendor, ticker)`.

**Cadence:** nightly in Sharadar mode. `fafnir ingest sharadar` downloads TICKERS and
ACTIONS whole each night (step 1) and loads them here (step 3,
[ingestion.md](ingestion.md#sharadar-as-primary-prices-and-actions-98)). TICKERS has
no delta: the whole file is under 0.1 GB with ACTIONS.

#### `core.security_vendor_xref` — fafnir's security ↔ a vendor's key, over time. **Grain:** `(vendor, vendor_key, valid_from)`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT NOT NULL FK → core.security | Indexed. |
| `vendor` / `vendor_key` | TEXT NOT NULL | |
| `valid_from` / `valid_to` | DATE | Open = `valid_to IS NULL`; `valid_to ≥ valid_from`. |
| `is_feed` | BOOLEAN NOT NULL | Default FALSE. This vendor loads the security's new rows (ADR 0015 §2). Only on an open link (`ck_svx_feed_open`). |
| `match_method` | TEXT CHECK | `minted` (#71); `ticker_span_price`, written by `fafnir vendor link` (#62): a shared ticker over overlapping spans, confirmed by ≥ 4 of 5 sampled raw closes (`evidence.basis = price_history`) or by the latest of them (`latest_price`, FMP's history being wrong); `identifier` (a shared CUSIP/FIGI/CIK alone; `fafnir vendor link` writes it when no sample decides and one candidate shares a CIK or CUSIP); `manual`, written by `fafnir vendor link accept` (#63) with `created_by` the person and `confidence` NULL. |
| `confidence` | NUMERIC(4,3) | 0–1. |
| `evidence` | JSONB | What the match rests on, so a link can be reviewed without re-running the matcher. For `fafnir vendor link`'s links: `basis` (`price_history`, `latest_price`, `identifier`), `samples` (date, `fmp`, `sharadar`), `agreed` of `of`, `ticker`, `matched_on`, `identifier`, `name_similarity`, the `others` considered and the `run_id`. Vendor data: never posted. |
| `created_by` / `created_at` / `note` | | `created_by` may not be blank. |

Its unique indexes:
- **One open link per vendor key.** A permaticker is one fafnir security at a time.
- **One open link per security and vendor.** A security is one identity per vendor.
- **One feeding link per security.** One vendor loads its new rows.

When a security goes away:
- **`security merge` and the stub fold** move the victim's links to the survivor. Both
  refuse when the two securities hold open links to the same vendor, or both have a
  feeding link: one has to be closed first (#63). `force` does not lift that refusal,
  and the automatic fold declines it instead of failing the load.
- **`security split-history --undo`** keeps a destination that holds links instead of
  deleting it.

**Cadence:** written by #62, #63 and #71, and by `ingest sharadar-universe` when it
mints a new listing.

On 2026-10-31 there were 27,549 Sharadar links: 15,368 `ticker_span_price`, 11,981
`minted`, 198 `identifier` and 1 `manual`. All but one are open and feeding; the
exception is a closed `ticker_span_price` link.

#### `core.security_vendor_rejection` — "this security is not that key" (0032, #63). **Grain:** `(security_id, vendor, vendor_key)`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT FK → core.security, ON DELETE CASCADE | A rejection is about one security, and goes with it. |
| `vendor` / `vendor_key` | TEXT | |
| `rejected_by` / `rejected_at` / `note` | | Who, when and why; `rejected_by` and `note` may not be blank. |
| `evidence` | JSONB | The link the rejection closed, if any. |

Indexed on `(vendor, vendor_key)`. Empty on 2026-10-31.

**Population:**
- **Written by** `fafnir vendor link reject`. Rejecting again restates the note
  and owner.
- **Removed by** `accept` of the same pair.
- **The linking engine** never proposes a rejected pair.

**Why not a row of `core.security_vendor_xref`:** a link means "this key was this
security over this span". A rejection is not a span of identity, and kept there it
would answer "who was this key on date D" wrongly.

#### `core.vendor_security_event` — a vendor's lifecycle events. **Grain:** `(vendor, vendor_key, event_date, action, contra_ticker)`.

| Column | Type | Notes |
|---|---|---|
| `vendor` | TEXT NOT NULL CHECK | `sharadar`, `tiingo`, `fmp`. |
| `vendor_key` | TEXT NOT NULL | The permaticker the row's ticker resolved to. |
| `event_date` | DATE NOT NULL | ACTIONS' `date`. Indexed. |
| `action` | TEXT NOT NULL | ACTIONS other than splits and dividends: `listed`, `delisted`, `tickerchangefrom`/`to`, `acquisitionby`, `mergerfrom`/`to`, `relation`, `initiated`, `spinoff`, … (54 distinct on 2026-10-31). |
| `ticker` | TEXT | The ticker as the row gave it. |
| `value` | NUMERIC | As given: a price, a deal value in millions, or NULL. |
| `contra_ticker`, `contra_name` | TEXT | The other party, as the vendor spells it. |
| `contra_vendor_key` | TEXT | The other party's permaticker, when it resolves. |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | |
| `loaded_at` | TIMESTAMPTZ NOT NULL | |

The table has **no primary key and no foreign key to `core.security`**: it is keyed by
the vendor's identity, and reaches a fafnir security only through
`core.security_vendor_xref`. The grain is the unique index `ux_vendor_security_event`,
with `COALESCE(contra_ticker, '')`, since most events have no contra party. ACTIONS
rows carry only a ticker, so #58 resolves each to its permaticker through TICKERS
before writing here. Splits and dividends go to `core.corporate_action` (#73).

**Population (`fafnir ingest sharadar-master`, #58).**
- **Which actions:** every ACTIONS action except `split` and `dividend`. The file
  uses about seventy action names (2026-10-19), among them a family of
  `acquisition*` variants, `exchangefrom`/`to`, `namechangefrom`/`to` and
  `spacmerger`. They are kept as given rather than filtered to a list.
- **Resolving a row:** the row's ticker resolves through the loaded master, in
  the order stocks, funds, fundamentals, insiders. A row whose ticker resolves
  nowhere is skipped and counted by action in the run's `params.skipped`; they are
  mostly acquirers outside the dataset. The contra ticker resolves the same way,
  into `contra_vendor_key`, which is NULL when it does not.
- **Duplicates on the grain:** the file repeats the grain for some acquisitions,
  differing only in `contra_name`. The loader keeps the name that sorts first and
  counts the rest as `duplicate`.

Measured on the 2026-10-19 full file: 718,464 rows → 142,852 events, after
skipping 571,824 splits and dividends, 3,134 unresolved rows and 654 duplicates.
Loading both files took 12.5 s; a re-run took 7.6 s and wrote nothing.

### Fundamentals (migration 0035, ADR 0018)

Statements from Sharadar SF1, one table per statement, written by `fafnir ingest
fundamentals` (#90; [ingestion.md](ingestion.md#statements-from-sf1-90)). The
backfill on the host (#92) ran on 2026-10-26 and 2026-10-27. The nightly delta has
run since 2026-10-28 (`ops.ingestion_run` endpoint `fundamentals:nightly`). **Adjustment status: as reported, in the share terms of
`known_from`.** No column is split-adjusted to today, and no per-share value is
stored (ADR 0019). The design is
[plans/fundamentals-sf1.md](plans/fundamentals-sf1.md) §4.

#### `core.income_statement`, `core.balance_sheet`, `core.cash_flow_statement`

**Grain:** `(security_id, period_type, fiscal_date, version_no, loaded_at)`, one row
per version of a period, per load. **Source:** `sharadar` only (a CHECK). **Cadence:**
the SF1 backfill, then the nightly delta (#106).

**A version in force at date D:** `superseded_at IS NULL AND known_from <= D AND
(known_to IS NULL OR D < known_to)`. A period's versions chain: each `known_to` is
the next version's `known_from`. Successive periods are not chained.

Every table shares these columns, then its line items, then `content_hash`,
`extra`, `source` and `ingestion_run_id`:

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT FK | The security linked to the SF1 row's permaticker (primary share class only): the link covering `fiscal_date`, else the open one. The foreign key refuses deleting a security that holds statements; `security merge` moves them, keeping the survivor's versions of a period both hold. |
| `vendor_key` | TEXT | The permaticker. |
| `period_type` / `fiscal_period` | TEXT CHECK | `annual` with `FY`; `quarter` with `Q1`–`Q4`. From SF1's dimension and `fiscalperiod`. |
| `fiscal_date` | DATE | SF1 `reportperiod`: the period's end (observation). |
| `calendar_date` | DATE | SF1 `calendardate`: the vendor's normalized period, for comparing companies. |
| `fiscal_year` | INTEGER | From `fiscalperiod` (`2025-Q4` → 2025), never derived from a date. |
| `period_start_date` | DATE | NULL: SF1 carries no period start. Before `fiscal_date` when set. |
| `version_no` | INTEGER | 1 = as first reported; each restatement adds one, and only an `observed` version can be later than 1. |
| `known_from` / `known_to` | DATE | The knowledge interval, half-open. `known_from >= fiscal_date`: the look-ahead tripwire. |
| `knowledge_source` | TEXT CHECK | `filed` (the SEC filing, `known_from = filing_date`), `observed` (the day fafnir first loaded a restatement, or the next day where the version it replaces became known that day) or `estimated` (a period with no first-reported row: the issuer's first filing for a period ending on or after it). |
| `filing_date` | DATE | SF1 `date` of the first-reported row; NULL on any other version. |
| `loaded_at` / `superseded_at` | TIMESTAMPTZ | The load timeline. `superseded_at` is set when a vendor correction replaced the row, which keeps its knowledge interval. |
| `reported_currency` | TEXT | TICKERS' `currency`, per security. |
| `content_hash` | TEXT CHECK | SHA-256 (64 hex) over the typed values and `filing_date`; share counts at 1,000-share precision, so a split's re-expression is no change. The loader also leaves a row whose share counts moved by no more than a recovery's rounding (1,000 shares, or a millionth). |
| `extra` | JSONB NOT NULL | SF1 fields without a column (`vendor_metric`: the 36 metrics, the date-measured columns, per-share values, `*usd` conversions; `unmapped`: any column SF1 adds later), once per SF1 row, on `core.income_statement`, its numbers exactly as SF1 wrote them. `{}` on the other two tables. **Never read by mart.** Measured at about 965 bytes a row, about 1.35 GB at full scale; `core.income_statement` was 1.96 GB with its indexes on 2026-10-31. |
| `source` | TEXT NOT NULL CHECK | Always `sharadar` (`ck_*_source`). |
| `ingestion_run_id` | BIGINT FK | The load. |

**CHECK constraints** (named `ck_inc_*`, `ck_bal_*`, `ck_cfs_*`), the same on all three
tables:

| Constraint | Rule |
|---|---|
| `_period_type`, `_fiscal_period`, `_period_label` | `period_type` is `annual` or `quarter`; `fiscal_period` is `FY` or `Q1`–`Q4`; `annual` ⇔ `FY`. |
| `_knowledge_source` | `filed`, `observed` or `estimated`. |
| `_filed` | `filed` ⇔ `filing_date IS NOT NULL`, and a `filed` row has `known_from = filing_date`. |
| `_known_after_period` | `known_from >= fiscal_date`: the look-ahead tripwire. |
| `_known_range` | `known_to IS NULL OR known_to > known_from`. |
| `_restatement_observed` | `version_no = 1 OR knowledge_source = 'observed'`. |
| `_version` | `version_no >= 1`. |
| `_period_start` | `period_start_date IS NULL OR period_start_date < fiscal_date`. |
| `_superseded` | `superseded_at IS NULL OR superseded_at >= loaded_at`. |
| `_content_hash` | 64 lowercase hex digits. |
| `_source` | `source = 'sharadar'`. |

Rows on 2026-10-31: about 1.29M income statements, 1.04M balance sheets and 1.22M cash
flows (planner estimates).

**Line items:** 19 on the income statement, 26 on the balance sheet, 13 on the cash
flow. Money is `NUMERIC(24,2)`; share counts (`weighted_average_shares`,
`weighted_average_shares_diluted`, `shares_outstanding`) are `NUMERIC(24,4)`, in the
version's share terms, recovered to the nearest share. `shares_outstanding` is the
first-reported row's cover count: a restated version carries its predecessor's, in
its own terms, and an estimated version has none. Each column's SF1 indicator, class
and sign are in `ref.statement_line_item`. Two conventions are SF1's own:
- **Absent and inapplicable.** An item the filing does not carry is **0** for 41
  indicators. An item that does not apply is NULL: for example, the current and
  non-current splits of a balance sheet that is not classified.
- **No minority interest in the balance sheet.** `total_equity_parent` excludes
  noncontrolling interest, and there is no balance item for it.

The line items, in column order, as `ref.statement_line_item` states them. **Sign** is
`sign_convention`: *inflow* = `positive_is_inflow`, *outflow* = `positive_is_outflow`.
**Σ** marks a subtotal (`is_subtotal`). **FMP** is the FMP field of the same meaning,
kept for cross-vendor checks; fafnir loads no FMP statements.

**`core.income_statement`** (19):

| Column | SF1 | FMP | Class | Sign | Σ | Meaning |
|---|---|---|---|---|---|---|
| `revenue` | `revenue` | `revenue` | flow | inflow | | Revenue for the period |
| `cost_of_revenue` | `cor` | `costOfRevenue` | flow | outflow | | Cost of the goods and services sold |
| `gross_profit` | `gp` | `grossProfit` | flow | signed | Σ | Revenue less cost of revenue |
| `research_and_development` | `rnd` | `researchAndDevelopmentExpenses` | flow | outflow | | R&D expense; 0 when the filing does not carry it |
| `selling_general_and_administrative` | `sgna` | `sellingGeneralAndAdministrativeExpenses` | flow | outflow | | SG&A expense |
| `operating_expenses` | `opex` | `operatingExpenses` | flow | outflow | Σ | SG&A, R&D and other operating costs |
| `operating_income` | `opinc` | `operatingIncome` | flow | signed | Σ | Operating income |
| `interest_expense` | `intexp` | `interestExpense` | flow | outflow | | Interest expense; 0 when the filing does not carry it |
| `ebit` | `ebit` | `ebit` | flow | signed | Σ | Net income + tax + interest |
| `income_before_tax` | `ebt` | `incomeBeforeTax` | flow | signed | Σ | Net income + tax |
| `income_tax_expense` | `taxexp` | `incomeTaxExpense` | flow | outflow | | Income tax; a benefit is negative |
| `consolidated_income` | `consolinc` | — | flow | signed | Σ | After tax, before noncontrolling interest |
| `net_income_noncontrolling` | `netincnci` | — | flow | signed | | Attributable to noncontrolling interests |
| `net_income_discontinued` | `netincdis` | `netIncomeFromDiscontinuedOperations` | flow | signed | | From discontinued operations |
| `net_income` | `netinc` | `netIncome` | flow | signed | Σ | Attributable to the parent |
| `preferred_dividends` | `prefdivis` | — | flow | outflow | | Preferred dividends charged against net income |
| `net_income_common` | `netinccmn` | — | flow | signed | Σ | Available to common shareholders: EPS's numerator |
| `weighted_average_shares` | `shareswa` | `weightedAverageShsOut` | share_count | signed | | Basic, in the version's share terms |
| `weighted_average_shares_diluted` | `shareswadil` | `weightedAverageShsOutDil` | share_count | signed | | Diluted, in the version's share terms |

**`core.balance_sheet`** (26). Every item is `signed`. The current and non-current
splits are NULL on a balance sheet that is not classified.

| Column | SF1 | FMP | Class | Σ | Meaning |
|---|---|---|---|---|---|
| `cash_and_equivalents` | `cashneq` | `cashAndCashEquivalents` | stock | | Cash and cash equivalents |
| `investments_current` | `investmentsc` | `shortTermInvestments` | stock | | Current investments |
| `investments_noncurrent` | `investmentsnc` | `longTermInvestments` | stock | | Non-current investments |
| `investments_total` | `investments` | `totalInvestments` | stock | Σ | Current and non-current |
| `receivables` | `receivables` | `netReceivables` | stock | | Trade and non-trade receivables |
| `inventory` | `inventory` | `inventory` | stock | | Inventory |
| `tax_assets` | `taxassets` | `taxAssets` | stock | | Tax assets |
| `total_current_assets` | `assetsc` | `totalCurrentAssets` | stock | Σ | Current assets |
| `property_plant_equipment_net` | `ppnenet` | `propertyPlantEquipmentNet` | stock | | PP&E, net |
| `goodwill_and_intangibles` | `intangibles` | `goodwillAndIntangibleAssets` | stock | | Goodwill and intangible assets |
| `total_noncurrent_assets` | `assetsnc` | `totalNonCurrentAssets` | stock | Σ | Non-current assets |
| `total_assets` | `assets` | `totalAssets` | stock | Σ | Total assets |
| `payables` | `payables` | `totalPayables` | stock | | Trade and non-trade payables |
| `deferred_revenue` | `deferredrev` | `deferredRevenue` | stock | | Deferred revenue |
| `deposit_liabilities` | `deposits` | — | stock | | Deposit liabilities (banks) |
| `debt_current` | `debtc` | `shortTermDebt` | stock | | Current debt |
| `debt_noncurrent` | `debtnc` | `longTermDebt` | stock | | Non-current debt |
| `total_debt` | `debt` | `totalDebt` | stock | Σ | Current and non-current debt |
| `tax_liabilities` | `taxliabilities` | — | stock | | Tax liabilities |
| `total_current_liabilities` | `liabilitiesc` | `totalCurrentLiabilities` | stock | Σ | Current liabilities |
| `total_noncurrent_liabilities` | `liabilitiesnc` | `totalNonCurrentLiabilities` | stock | Σ | Non-current liabilities |
| `total_liabilities` | `liabilities` | `totalLiabilities` | stock | Σ | Total liabilities |
| `accumulated_other_comprehensive_income` | `accoci` | `accumulatedOtherComprehensiveIncomeLoss` | stock | | AOCI (loss) |
| `retained_earnings` | `retearn` | `retainedEarnings` | stock | | Retained earnings (deficit) |
| `total_equity_parent` | `equity` | `totalStockholdersEquity` | stock | Σ | Equity attributable to the parent; no noncontrolling interest |
| `shares_outstanding` | `sharesbas` | — | share_count | | The filing's cover count, in the version's share terms; first-reported rows only |

**`core.cash_flow_statement`** (13). Every item is a `flow`, signed as cash moves
(`positive_is_inflow`): an outflow is negative.

| Column | SF1 | FMP | Σ | Meaning |
|---|---|---|---|---|
| `net_cash_operating` | `ncfo` | `netCashProvidedByOperatingActivities` | Σ | From operating activities |
| `depreciation_amortization` | `depamor` | `depreciationAndAmortization` | | D&A and accretion, added back |
| `share_based_compensation` | `sbcomp` | `stockBasedCompensation` | | Added back |
| `capital_expenditure` | `capex` | `capitalExpenditure` | | Capex; negative as spent |
| `net_cash_business_acquisitions` | `ncfbus` | `acquisitionsNet` | | Business acquisitions and disposals |
| `net_cash_investment_acquisitions` | `ncfinv` | — | | Purchases and sales of investments |
| `net_cash_investing` | `ncfi` | `netCashProvidedByInvestingActivities` | Σ | From investing activities |
| `net_debt_issuance` | `ncfdebt` | `netDebtIssuance` | | Issuing and repaying debt |
| `net_equity_issuance` | `ncfcommon` | `netCommonStockIssuance` | | Issuing and repurchasing equity; a buyback is negative |
| `dividends_paid` | `ncfdiv` | `netDividendsPaid` | | Dividends and other distributions; negative as paid |
| `net_cash_financing` | `ncff` | `netCashProvidedByFinancingActivities` | Σ | From financing activities |
| `fx_effect_on_cash` | `ncfx` | `effectOfForexChangesOnCash` | | Effect of exchange rates on cash |
| `net_change_in_cash` | `ncf` | `netChangeInCash` | Σ | Net change in cash and equivalents |

**Indexes:**
- `ux_<table>_current`: one current row per version;
- `ux_<table>_in_force`: one version in force per period;
- `ix_<table>_pit`, `ix_<table>_known`: point-in-time reads and panel sweeps;
- `ix_<table>_restated`: versions after the first.

Gaps and overlaps between a period's versions are checked by a standing test and a
DQ check, since the warehouse has no exclusion constraints.

**Privileges:** `fafnir_read` and `fafnir_ops` read them, `fafnir_ingest` writes
them, and `fafnir_app` reaches them only through mart views (#95).

## Schema: `mart`

### `mart.v_daily_price_adjusted` — adjusted OHLCV (VIEW)
**Grain:** `(security_id, trade_date)`. **Source:** `core.daily_price` ×
`core.adjustment_factor`. **Adjustment status:** **ADJUSTED (split + dividend),
derived on read.** Point-in-time stable.

| Column | Type | Notes |
|---|---|---|
| `security_id`, `trade_date` | | |
| `open` `high` `low` `close` | NUMERIC | Back-adjusted prices: the exact product of the raw price and the factor, unrounded, so a deep split history can neither round one to zero nor overflow on read (migrations 0008, 0013). |
| `volume` | NUMERIC | Back-adjusted volume (post-split share terms). Rounded to whole shares, not truncated. |
| `close_raw` | NUMERIC(20,6) | The unadjusted close, for reference. |
| `price_factor` / `volume_factor` | NUMERIC | The factor applied to this row. |

Both the factor and the adjusted price are unconstrained `NUMERIC` (migration 0013).
A cumulative factor is a *product* over a whole action history, so it spans orders of
magnitude a declared scale cannot hold in either direction: `NUMERIC(20,10)` overflowed
on a deep reverse-split history (which is what killed the 2026-08-27 backfill at step 5)
and rounded a deep forward-split history to `0.0000000000`. The arithmetic is exact
`Decimal` at 28 significant digits (`fafnir.ingest.adjustments.PRECISION`), pinned so
the factors are reproducible everywhere; ratios that terminate stay exact, and AAPL's
112:1 carries a relative error of ~1×10⁻²⁸. Display rounding is duk's job
(`duk.format_utils`), which never renders a non-zero price as zero.

### `mart.v_daily_price_raw` — unadjusted OHLCV (VIEW)
**Grain:** `(security_id, trade_date)`. **Source:** `core.daily_price`.
**Adjustment status:** **RAW — as traded**; a split shows as a real jump.

Columns: `security_id, trade_date, open, high, low, close, volume, vwap`. Lineage
columns (`source`, `ingestion_run_id`, `loaded_at`) are deliberately not exposed —
they are operational, not market data.

Added by migration 0020 so the unadjusted series has a `mart` name too. The explicit
name is the point: a client cannot read unadjusted prices while believing they are
adjusted. Partition pruning survives the view — a `security_id` + date-range query
plans identically to the same query against `core.daily_price`.

### `mart.v_symbol_lookup` — ticker → `security_id` (VIEW)
**Grain:** `(symbol, valid_from)`. **Source:** `core.symbol_xref` (passthrough).

Columns: `symbol, security_id, is_primary, valid_from, valid_to`. `NULL valid_to` =
currently valid. Deliberately **not** a resolver: the ladder (live ticker → primary
symbol → a ticker the security used to trade under) lives in the client, stated once
in `fafnir.db.repository` and copied once in `duk.datasource.db`. Its second rung
reads `mart.v_security_profile`, which is why resolution reads two views.

### `mart.v_security_profile` — descriptive profile (VIEW)
**Grain:** one row per `security_id`. **Source:** `core.security` + `ref.exchange` +
`core.v_security_classification` (with `ref.sector`, `ref.industry`) +
`core.company_profile`. Read live, unlike `security_latest`. Since 0039 (#109),
`sector_name` and `industry_name` are the feeding vendor's, as in `security_latest`;
the columns are unchanged.

Columns: `security_id, symbol` (`primary_symbol`), `company_name, asset_type,
exchange_code, exchange_name, sector_name, industry_name, currency, country, cik, isin,
cusip, is_actively_trading, is_etf, is_fund, ipo_date, delisted_date, market_cap_usd,
beta, source, first_seen_at, updated_at, description, description_loaded_at`.

Adds over `security_latest`: `exchange_name`, `cik`/`isin`/`cusip`, `ipo_date`,
`delisted_date`, `source`, `first_seen_at`, `updated_at`, `description` and
`description_loaded_at` (`core.company_profile.loaded_at`). It has no last bar. It is also
the relation `duk` resolves `cik:`/`isin:`/`cusip:` arguments against, which is why
the identifier columns belong on this view and not only on `core.security`. Its
`market_cap_usd` and `beta` are `core.security`'s: the vendor's company-screener
snapshot as last loaded (FMP's, frozen at the cutover for the securities Sharadar
feeds). **Not** history; do not use them in a backtest. `security_latest` derives its
own (#109).

### `mart.v_security_price_coverage` — what is held per security (VIEW)
**Grain:** one row per `security_id` with prices. **Source:** `core.daily_price`.

Columns: `first_trade_date, last_trade_date, bar_count, distinct_years,
calendar_span_days, min_close, max_close, zero_volume_bars`. Raw-series facts only.
Trailing returns, volatility and drawdown belong to the *adjusted* series and are
computed by the client (`duk.return_utils`), not here.

### `mart.v_security_action_summary` — corporate actions per security (VIEW)
**Grain:** one row per `security_id` with corporate actions. **Source:**
`core.corporate_action` + `core.adjustment_factor`.

Columns: `split_count`, `first_split_date`, `last_split_date`,
`last_split_numerator`/`_denominator`, `dividend_count`, `first_dividend_date`,
`last_dividend_date`, `last_dividend_amount`, `last_dividend_currency`,
`ttm_dividend_amount`, `adjustment_factor_rows`, `latest_factor_effective_date`.

`ttm_dividend_amount` is the 365 days ending at **the security's own latest
ex-date**, not at `now()` — so the row does not change with the clock, and a
delisted security reports what it last paid rather than zero.

References no *numeric* column of `core.adjustment_factor` (only `COUNT(*)` and
`MAX(effective_date)`) on purpose: migration 0013 had to drop
`mart.v_daily_price_adjusted` to re-type those columns, and a second dependent view
would double that cost next time. A client wanting back-adjustment depth reads
`price_factor` from `mart.v_daily_price_adjusted`.

### `mart.v_corporate_action` — corporate actions, one row per event (VIEW)
**Grain:** `(security_id, action_type, ex_date)`. **Source:** `core.corporate_action`
(passthrough). Added by migration 0033 (#174).

Columns: `corporate_action_id, security_id, action_type, ex_date, record_date,
payment_date, declaration_date, split_numerator, split_denominator, dividend_amount,
currency, source`. `action_type` is `split` or `dividend`; the ratios and the amount
are `NUMERIC(20,6)`, as stored. `source` says which vendor supplied the row, or
`operator` for one written by `fafnir actions add|redate` (ADR 0015 precedence).
Lineage (`ingestion_run_id`, `loaded_at`) is left out, as in `mart.v_daily_price_raw`.

The event-level counterpart of `mart.v_security_action_summary`, which aggregates and
cannot say which dividends went ex between two dates. It is how a mart-only reader
lists corporate actions: portable's `pt ca sync` reads it to compare its ledger with
the warehouse. Like every `mart` view it is a grant to every reader (ADR 0009), and
it depends on the three numeric columns, so re-typing them means recreating it too.

### `mart.v_security_lifecycle` — when a security traded, and why it stopped (VIEW)
**Grain:** one row per `security_id`. **Source:** `core.security`, `core.daily_price`,
`core.security_vendor_xref`, `core.vendor_security_event`. Added by migration 0034
(#79).

Columns: `security_id, symbol, company_name, minted_by, is_actively_trading,
first_trade_date, last_trade_date, delisted_date, delist_reason, delist_event_date,
acquirer_ticker, acquirer_name, acquirer_security_id, survivorship_complete_from`.

- `minted_by` is `core.security.source`: `fmp`, or `sharadar` for a dead issuer
  minted from Sharadar (#71).
- `delist_reason` is the vendor's terminal event, for a security that stopped
  trading (a `delisted_date`, or not actively trading). The values are
  `acquisitionby`, `mergerto`, `bankruptcyliquidation`, `regulatorydelisting`,
  `voluntarydelisting`, or a bare `delisted` when the vendor gives no reason. It
  is NULL for a live security, or one with no Sharadar link.
  - It is read from the security's Sharadar link: the open one, else the most
    recent.
  - A specific reason wins over `delisted` within 30 days of the last terminal
    event. An acquisition is recorded as both, on one date.
  - `mergerto` is the merged-away side; the survivor carries `mergerfrom`, which is
    not a reason.
- `acquirer_*` comes from an `acquisitionby` or `mergerto` event: the other party's
  ticker and name as the vendor spells them. `acquirer_security_id` is filled when
  fafnir holds the acquirer under an open Sharadar link. The vendor's `N/A` is NULL.
- `first_trade_date`, `last_trade_date` are per-security index probes, cheap on a
  `security_id` filter.
- `survivorship_complete_from` is `1998-01-01`
  ([ADR 0016](adr/0016-sharadar-identity-and-survivorship-floor.md)): from then on
  the universe holds the issuers that died as well as those that live.

The vendor keys, the link evidence and the other lifecycle events stay in `core`.
Like every `mart` view it is a grant to every reader (ADR 0009).

### The statements, point in time (VIEWS and FUNCTIONS)
Added by migration 0036 (#95; [ADR 0018](adr/0018-bitemporal-fundamentals.md), plan
[§5.1–5.2](plans/fundamentals-sf1.md#5-the-mart-migrations-0036-0038)). The design and
the rules for reading the statements are ADR 0018's; these are its read seam.

**`mart.v_income_statement_versions`, `mart.v_balance_sheet_versions`,
`mart.v_cash_flow_statement_versions`** (VIEWS). **Grain:** the core table's: one row
per version of a period, per load.
- **Columns:** `security_id, period_type, fiscal_date, calendar_date, fiscal_year,
  fiscal_period, version_no, known_from, known_to, knowledge_source, filing_date,
  loaded_at, superseded_at, reported_currency`, then the table's line items.
- **Left out:** `extra` (SF1's vendor metrics are never read through mart: ADR 0019),
  `vendor_key`, `content_hash`, `source` and `ingestion_run_id`.

**`mart.fn_statements_as_of(as_of DATE)`** (FUNCTION) returns, per
`(security_id, period_type)`, the latest `fiscal_date` with a version in force at
`as_of`.
- **Joining.** The three statements join on `fiscal_date`, never on knowledge dates. Each
  is read at its own version in force. A statement the period lacks is NULL.
- **Columns:**
  - `security_id, period_type, fiscal_date, calendar_date, fiscal_year, fiscal_period,
    reported_currency`;
  - `known_from`: the latest of the three, since the row was not knowable before it;
  - `knowledge_source`: the weakest of the three, `estimated` then `observed` then
    `filed`;
  - `has_income, has_balance_sheet, has_cash_flow`;
  - per statement, `<statement>_version_no, <statement>_known_from,
    <statement>_knowledge_source`, with prefixes `income`, `balance_sheet`, `cash_flow`;
  - then every line item.
- **Share counts** are in the terms of their statement's `_known_from` (ADR 0019).
- **For backtests:** nothing in it was knowable after `as_of`.

**`mart.fn_statements_as_of(as_of DATE, load_as_of TIMESTAMPTZ)`** is the same read, as
fafnir held the statements at `load_as_of`:
- the rows loaded by then and not yet superseded;
- a version loaded later is ignored, and its predecessor read as open.

It reproduces a past run.

**`mart.fn_fundamentals_ttm(as_of DATE)`** (FUNCTION) gives the trailing twelve months
for every quarter-end of the income statement in force at `as_of`.
- **Flows** sum four contiguous quarters, each at its version in force at `as_of`, or
  are NULL. A flow NULL in any of the four is NULL. There is never a partial sum.
- **Contiguous** means consecutive fiscal labels, each period end 8 to 18 weeks after the
  one before. A retailer's 16-week first quarter is included.
- **Stocks and share counts** take the latest quarter: a balance-sheet item is never
  summed.
- **No derived Q4 and no year-to-date de-cumulation:** SF1 files Q4 and its quarters are
  discrete (#83).
- **Columns:**
  - `security_id, fiscal_date, calendar_date, fiscal_year, fiscal_period,
    reported_currency`;
  - `ttm_quarters_used` (1–4, from the income statement) and `cash_flow_quarters_used`;
  - `has_balance_sheet`;
  - `known_from`: the latest of every constituent, since a TTM figure is not knowable
    before its last quarter;
  - `knowledge_source`: the weakest;
  - `income_known_from` and `balance_sheet_known_from`: the latest quarter's, the share
    terms of its counts;
  - then every line item.
- **Average balances** for ROE-like ratios are the panel's (#100).

**`mart.v_statements_current`** and **`mart.v_fundamentals_ttm`** (VIEWS) are the two
functions as of `current_date`: for screening and dashboards, **not for backtests**.

**How they run.**
- **Rights.** The functions are plain SQL, `STABLE`, and run with their caller's
  rights. They read only the definer-rights views above, so a mart-only role needs no
  `core`.
- **Inlining.** The planner inlines them, so a filter on `security_id` reaches the
  statement tables' indexes.
- **EXECUTE** is granted to `fafnir_read`, `fafnir_app` and `fafnir_ops`, and revoked from
  PUBLIC.

### The factor panel (MATERIALIZED VIEW and FUNCTIONS)
Added by migration 0037 (#100; plan
[§5.3](plans/fundamentals-sf1.md#53-the-factor-panel-100), ADR 0018, ADR 0019). The
factor definitions and their literature are in
[fundamentals.md](fundamentals.md#the-factor-panel-100).

**`mart.fundamental_panel`** (MATERIALIZED VIEW). **Grain:** `(security_id,
as_of_date)`, unique (`ux_fundamental_panel`); also indexed on `(as_of_date)` and
`(sector_name, as_of_date)`. **Cadence:** `fafnir db refresh-marts`
(`maintenance.MATVIEWS`, after `mart.security_latest`).
- **`as_of_date`** is the last trading day of each month from 1998-01 (decision 4):
  NYSE's in `ref.trading_calendar`, or the month's last weekday where the calendar
  does not reach.
- **A row** is a security that traded in the week to `as_of_date` (a raw close > 0) and
  had a basis period then. Dead issuers keep their rows while they traded. A secondary
  share class has no statements and no row.
- **Created empty.** A read is refused until the first refresh, which runs plainly;
  later ones run concurrently.
- **The standing invariant:** `count(*) WHERE known_from > as_of_date` is 0.

**`mart.fn_fundamental_panel(as_of_dates DATE[], extra_lag_days INTEGER DEFAULT 0)`**
(FUNCTION) is the panel at any dates. Every factor is defined in it once; the
matview and the snapshot select it.
- **Statements** are read as known at `as_of_date - extra_lag_days`; prices at
  `as_of_date`. A negative lag returns no rows.
- **The basis** is the security's latest period known then: the TTM of four contiguous
  quarters (`mart.fn_fundamentals_ttm`'s rule), or a fiscal year with its income
  statement where that is newer. The TTM wins a tie. A basis that ended more than 730
  days before gives no row.
- **Year-ago and three-years-ago** periods are the same basis labelled 4 and 12 quarters
  back, as known at the same date, within 730 and 1,826 days of the basis. Growth
  compares versions in force at one date.

**`mart.fn_fundamental_snapshot(as_of DATE, extra_lag_days INTEGER DEFAULT 0)`**
(FUNCTION) is the panel function at one date, for daily as-of reads.

**`mart.fn_ratio(num, den)`** and **`mart.fn_growth(cur, prior)`** (FUNCTIONS) are the
denominator rule, written once: `num / den` when `den > 0`, else NULL; `cur / prior - 1`
when `prior > 0`, else NULL. A numerator keeps its sign. Nothing is winsorized,
clipped or filled.

**Columns** (the panel's and both functions'):

| Group | Columns |
|---|---|
| Keys, context | `security_id, as_of_date, exchange_code, sector_name, industry_name, trading_currency, reported_currency, currency_mismatch` |
| The basis | `basis` (`ttm` or `annual`), `fiscal_date, fiscal_year, fiscal_period, known_from, knowledge_source, days_since_filing` (`as_of_date - known_from`), `ttm_quarters_used` (4, or NULL for a year) |
| Market state | `price_date` (the close's day, at most 6 days before), `close_raw, close_adjusted, shares_outstanding_as_of, market_cap, enterprise_value` |
| Value | `earnings_yield, book_to_price, sales_to_price, cash_flow_to_price, fcf_yield, ev_to_ebitda, ev_to_sales, ev_to_ebit, ev_to_fcf` |
| Quality | `roe, roa, roic, gross_profitability, gross_margin, operating_margin, net_margin, asset_turnover, accruals, net_operating_assets` |
| Safety | `debt_to_equity, net_debt_to_ebitda, interest_coverage, current_ratio, quick_ratio, altman_z, piotroski_f` (0–9) |
| Growth | `revenue_growth_yoy, eps_growth_yoy, revenue_cagr_3y, net_income_growth_yoy` |
| Investment, issuance | `asset_growth_1y, capex_to_sales, net_share_issuance_1y` |
| Payout | `dividend_yield_ttm, buyback_yield_ttm, shareholder_yield` |

- **Share terms** (ADR 0019 §3). `shares_outstanding_as_of` is the basis's count put in
  `as_of_date`'s terms with the warehouse's splits (`mart.v_corporate_action`), the list
  the raw prices follow. `market_cap` is the raw close times it: never a vendor
  snapshot. EPS and dividends per share are in the same terms.
- **`enterprise_value`** is market cap plus total debt less cash. It omits minority
  interest and preferred stock: SF1 carries no balance for either.
- **`currency_mismatch`**: the statements are not in the trading currency. Every ratio
  of a statement figure to the price is then NULL, as are `enterprise_value` and
  `altman_z`. Ratios within the statements stand.
- **The ratios are doubles**; the money columns are `numeric`.

**How they run.**
- **Rights.** Plain SQL with the caller's rights, reading only definer-rights mart
  views. EXECUTE goes to `fafnir_read`, `fafnir_app` and `fafnir_ops`, and is revoked
  from PUBLIC.
- **Once per state.** A TTM, or a joined year, changes only when a version under it
  starts or ends. The panel function computes each once per such state and reads every
  date from it, so many dates cost about what one does. A test holds its TTM equal to
  `mart.fn_fundamentals_ttm`'s at every month-end of a history with a restatement, a
  late filing and a missing quarter.
- **Planner settings.** The panel function runs with `enable_nestloop = off` and
  `jit = off` (function-level `SET`). Its materialized steps hide their row counts, and
  nested loops over them were quadratic. Steering off them lifts the plan's cost over
  the JIT thresholds, and compiling would take longer than running. Being `SET`, it is
  not inlined: a filter on `security_id` applies to its result.
- **Cost, on a synthetic load** of 1,000 securities, 1998–2026, 6.6 million daily bars
  and a third of quarters restated: the full refresh took **45 s** (291,430 rows), and
  one date about 3 s. Production's is recorded on #92, after the backfill (step 9). On
  2026-10-31 the production panel held about 1.83M rows (1.16 GB).

### `mart.v_industry_fundamentals` — the panel by sector and industry (VIEW)
Added by migration 0038 (#101; build prompt §5.4). **Grain:** `(group_level, as_of_date,
sector_name, industry_name)`. **Source:** `mart.fundamental_panel`, so it is as fresh as the
panel's last refresh.
- **`group_level`** is `sector` (`industry_name` NULL) or `industry` (both names). A security
  without a sector, or an industry, is in no group at that level.
- **`n_securities`**: the group's panel rows that month. **A group of fewer than three has no
  row.**
- **Per metric** (`market_cap, earnings_yield, book_to_price, sales_to_price, fcf_yield,
  ev_to_ebitda, roe, gross_profitability, operating_margin, accruals, debt_to_equity,
  interest_coverage, revenue_growth_yoy, asset_growth_1y, dividend_yield_ttm,
  shareholder_yield`):
  - `<metric>_n`: its non-NULL values;
  - `<metric>_p25`, `_median`, `_p75`: `percentile_cont` over those values. They are NULL
    when fewer than three are present, so a median of two companies never passes for a
    sector norm. `_n` still shows how many there were.
- **Classification is today's**, as in the panel: the security master's current sector and
  industry, not as they were at `as_of_date`. `market_cap` is in the trading currency.
- **Filters** on `as_of_date`, `sector_name` or `group_level` reach the panel's indexes. One
  month-end reads in milliseconds; the synthetic thousand securities' whole history (7,765
  groups) in about 0.1 s.

### `mart.security_fundamentals_latest` — each security's factors now (MATERIALIZED VIEW)
Added by migration 0038 (#101; build prompt §5.5). **Grain:** one row per `security_id`
(unique, `ux_security_fundamentals_latest`). **Source:**
`mart.fn_fundamental_snapshot(current_date)` joined to `core.security`. **Cadence:** `fafnir
db refresh-marts`, after the panel (`maintenance.MATVIEWS`).
- **Columns:** `security_id, symbol, company_name`, then the snapshot's columns after
  `security_id` (the panel's, above).
- **A row** is a security that traded in the week to the refresh and has a basis then.
- **For screens, not backtests:** it is as of the last refresh. `mart.security_latest`
  keeps its own columns (re-defined by 0039, #109): duk's screener reads it, and a screen
  opts into the factors by joining this on `security_id`.
- **Created empty.** A read is refused until the first refresh, which runs plainly; later
  ones run concurrently. About 3 s on the synthetic thousand securities.

### `mart.v_security_dq_open` — open DQ flags per security (VIEW)
**Grain:** one row per open `dq_flag_id` that names a security. **Source:**
`ops.data_quality_flag WHERE resolved_at IS NULL AND security_id IS NOT NULL`.

Columns: `dq_flag_id, security_id, check_name, severity, table_name, record_key,
detected_at`.

Flags with no security never appear: dorq's cross-sectional `dorq_cohort_*` and
`fundamentals_unmapped_fields`. Accepted flags never appear either, since an
accepted flag is resolved. The open queue's total is therefore
`ops.data_quality_flag`'s, not this view's.

The only relation exposing `ops` outside `fafnir_ingest`, and a deliberately narrow
one ([ADR 0009](adr/0009-mart-is-the-read-seam.md)). Open flags only; `record_key`
(dates and tickers) is included so a reader can name the offending bar; `detail` is
excluded because `adjustment_failed` writes a raw Python exception string into it.
`resolved_by`/`resolution_note` are kept off by the `resolved_at IS NULL` filter —
**that safety is in the `WHERE` clause**, so widening this view to resolved flags
means excluding those columns explicitly. Full triage stays with `fafnir dq list`.

### `mart.security_latest` — screening snapshot (MATERIALIZED VIEW)
**Grain:** one row per `security_id`. **Source:** `mart.v_security_attributes` (since
0039, #109; before it, `core.security` and the latest `daily_price`). **Cadence:**
refreshed on schedule (`fafnir db refresh-marts`), first of the matviews. duk's screener
and the MCP `screen_securities` read it.

The columns, names, types and order are as they were before #109 (duk's screener
contract). Where each comes from:

| Column | Source |
|---|---|
| `security_id`, `symbol`, `company_name`, `asset_type`, `exchange_code`, `currency`, `country`, `is_actively_trading`, `is_etf`, `is_fund` | `core.security` |
| `sector_name`, `industry_name` | the feeding vendor's: `core.v_security_classification` |
| `market_cap_usd` | **derived** where SF1 holds the shares (`mart.v_security_attributes`), else `core.security`'s vendor snapshot |
| `beta` | **derived** from a year of daily returns on SPY's where there are 120 of them, else `core.security`'s |
| `last_trade_date`, `last_close`, `last_volume` | the latest raw bar in `core.daily_price` |

`market_cap_usd` is cast to `NUMERIC(24,2)` and `beta` to `NUMERIC(12,6)`, the types
the contract always had. A derived value is therefore rounded here, unlike in
`mart.v_security_attributes`. Unique on `security_id` (`ux_security_latest_id`, which
lets the refresh run concurrently) and indexed on `symbol`. It holds every security,
delisted ones included (33,889 rows on 2026-10-31); `duk ls` and `screen_securities`
filter to `is_actively_trading` unless asked not to.

Which values are derived, and as of when, is in `mart.v_security_attributes`.

### `mart.v_security_attributes` — the screener's attributes by precedence (VIEW)
Added by 0039 (#109; [ADR 0015 §4](adr/0015-multi-vendor-provenance-and-precedence.md),
[ADR 0019 §5](adr/0019-derived-metrics-not-vendor-metrics.md)). **Grain:** one row per
`security_id`. **Source:** `core.security`, `core.v_security_classification`,
`core.daily_price`, `core.balance_sheet`, `mart.v_corporate_action` and
`mart.fn_security_betas(current_date)`. Read live, so it computes every security's beta when read:
for screens, read `mart.security_latest`, which holds it as of the last refresh. No column
of `core.security` is written.

- **Classification:** `sector_name`, `industry_name` and `attributes_source` (`sharadar`,
  or the `core.security.source` that wrote the pair), from the classification view below.
- **`market_cap_usd`:**
  - **How it is derived** (ADR 0019 §5, the panel's expression): the last raw close ×
    `shares_outstanding` of the latest balance sheet in force on that day, ended within
    730 days, put in that day's share terms with the warehouse's splits. It is derived
    for USD-traded securities only.
  - **The record:** `market_cap_source` is `derived`, `market_cap_as_of` the close's date,
    `market_cap_fiscal_date` the balance sheet's period.
  - **Otherwise** (no SF1 shares: funds, ETFs, secondary share classes, names Sharadar
    does not cover) it is `core.security.market_cap_usd`, and `market_cap_source` names
    its vendor. That is the vendor's last snapshot, frozen since the cutover for the
    securities Sharadar feeds.
- **`beta`:**
  - **How it is derived:** the slope of the security's daily adjusted returns over the
    last 365 days on SPY's over the same intervals. A security that skipped days is
    matched to the market's move over the same span.
  - **The record:** `beta_source` is `derived`, `beta_as_of` the last return's date,
    `beta_returns` how many there were.
  - **Otherwise:** with fewer than 120 returns, or no SPY in the warehouse, it is
    `core.security.beta`, and `beta_source` names its vendor.
- **`last_trade_date`, `last_close`, `last_volume`:** the latest raw bar.
- **Columns, in order:** `security_id, symbol, company_name, asset_type,
  exchange_code, sector_name, industry_name, attributes_source, currency, country,
  is_actively_trading, is_etf, is_fund, market_cap_usd` (numeric), `market_cap_source,
  market_cap_as_of, market_cap_fiscal_date, beta` (double precision), `beta_source,
  beta_as_of, beta_returns, last_trade_date, last_close, last_volume`.
- **A guard on beta:** a derived beta whose absolute value is 10⁶ or more is discarded
  for the vendor's (migration 0039): a slope too steep for `security_latest`'s
  `NUMERIC(12,6)` comes from a degenerate series, not a beta.

**`mart.fn_security_betas(as_of DATE)`** (FUNCTION, 0039) returns `security_id, beta,
returns, as_of_date`: the regression of each security's daily adjusted returns over the
365 days to `as_of` on SPY's over the same intervals, where there are at least 120 of
them.
- **It reads mart only:** `mart.v_daily_price_adjusted`, and `mart.v_symbol_lookup` for
  SPY. A mart reader may therefore call it, and EXECUTE is for the read roles only.
- **It is a function, not part of the view,** so no view depends on
  `mart.v_daily_price_adjusted`, which 0013 drops and re-creates.

### `core.v_security_classification` — the classification readers see (VIEW)
Added by 0039 (#109); 0040 completes its industry aliases. **Grain:** one row per
`security_id`. Columns: `security_id, sector_id, industry_id, attributes_source`.

- **The feeding vendor's.** A security Sharadar feeds (an open `is_feed` link in
  `core.security_vendor_xref`) takes Sharadar's TICKERS sector and industry
  (`core.vendor_security`, the `stocks` row, else `funds`). They are mapped onto fafnir's
  names as `sharadar_map.map_classification` maps them when it mints: case, commas and
  spacing ignored, and its industry aliases: every industry Sharadar spells otherwise
  than FMP's vocabulary (69 in the 2026-10-28 TICKERS). A test holds the two equal.
- **A pair.** Sector and industry come together from Sharadar when its sector maps, else
  from `core.security`, so a sector and an industry from different vendors are never
  mixed. Sharadar's industry may then be NULL, which the classification check flags.
- **Every other security** reads `core.security`'s pair.
- **Readers.** `mart.v_security_profile`, `mart.v_security_attributes` and the
  `security_missing_classification` check (`dq run`) read it. It is plain joins, so a
  filter on `security_id` reaches the indexes.

---

## Schema: `ref`

### `ref.exchange` — **Grain:** `exchange_code`.
`exchange_code` (PK), `exchange_name`, `country`, `timezone` (IANA, to recover local
market time from UTC), `is_active` (default TRUE), `created_at`, `updated_at`.
**Source:** the seed `sql/seeds/0001_exchanges.sql`, plus
`repository.ensure_exchange` for a venue first met in a payload. That call creates the
row bare and fills a missing attribute later, never overwriting one.

| Code | Name | Timezone | Calendar rows | Note |
|---|---|---|---|---|
| `NASDAQ` | Nasdaq Stock Market | America/New_York | yes | The calendar fallback (below). |
| `NYSE` | New York Stock Exchange | America/New_York | yes | The panel's month-ends are NYSE's sessions. |
| `AMEX` | NYSE American | America/New_York | yes | |
| `BATS` | Cboe BZX Exchange | America/New_York | yes | Seeded over 1990–2028, though BZX opened in 2008. |
| `OTC` | Over-the-Counter | America/New_York | yes | A pseudo-venue. |
| `TXSE` | Texas Stock Exchange | America/Chicago | **no** | Seeded after the calendar was. |
| `CBOE` | (none) | (none) | **no** | Created bare by a security-master load (`ensure_venue`); one of `SCREENER_EXCHANGES`. |
| `MUTF` | US open-end mutual funds (NAV, no venue) | America/New_York | **no** | Created by `ingest tracked` for a declared fund. See below. |

`MUTF` is a pseudo-venue for open-end mutual funds, which are struck at NAV and have
no trading venue; it is deliberately not one of `SCREENER_EXCHANGES`, which is what
keeps the delisting sweep away from funds. A venue with no calendar rows is read with
NASDAQ's sessions (`daily_price.CALENDAR_FALLBACK_EXCHANGE`).

### `ref.tracked_symbol` — declared universe. **Grain:** `(source, symbol)`.
**Source:** the operator (`fafnir track add`). **Cadence:** on demand; read nightly
by `fafnir ingest tracked`.

The symbols the security master must hold *regardless of the screener*. The nightly
universe is discovered from `company-screener`, which only returns exchange-listed
securities — a mutual fund has no venue and never appears there. This table is how
one is declared. The grain is deliberately the same soft key `upsert_security`
conflicts on, so a declared symbol and a screened one converge on one `security_id`
rather than forking. See [ADR 0006](adr/0006-curated-fund-universe.md).

| Column | Type | Notes |
|---|---|---|
| `source` | TEXT NOT NULL | Feed the symbol is declared against (default `fmp`). |
| `symbol` | TEXT NOT NULL | The ticker. Kept in step with `core.security.primary_symbol` — `ingest tracked` follows a rename rather than minting a second security. |
| `asset_type` | TEXT NOT NULL CHECK | `equity`/`etf`/`fund`/`other` (default `fund`). **Authoritative over the vendor profile**: the price loader reads it to decide whether a NAV-shaped bar is valid. |
| `exchange_code` | TEXT → ref.exchange | Venue to record. `MUTF` for open-end funds. |
| `note` | TEXT | Why this symbol is tracked — the one thing a hand-inserted `core.security` row could never carry. |
| `is_tracked` | BOOLEAN NOT NULL | FALSE stops the nightly pulls. It does **not** delist; retiring a security is `mark_delisted`'s job and history is retained either way. |
| `added_at` / `untracked_at` | TIMESTAMPTZ | Preserved across a re-declaration, so the row still answers "since when". `untracked_at` is set exactly when `is_tracked` is false (`ck_tracked_symbol_untracked`). |

Indexed on `symbol` where tracked. On 2026-10-31: seven rows, five tracked (three
ETFs, two equities); the one fund declared is untracked.

### `ref.sector` / `ref.industry` — the sector and industry names
`sector_id`/`industry_id` (IDENTITY PK), `*_name` (UNIQUE), `created_at`. FMP's
vocabulary: a security-master load adds a name the first time it meets one
(`get_or_create_sector` / `_industry`). Sharadar's names are mapped onto these, never
added (`core.v_security_classification`; `sharadar_map.map_classification` when
minting). On 2026-10-31: 11 sectors (Basic Materials, Communication Services,
Consumer Cyclical, Consumer Defensive, Energy, Financial Services, Healthcare,
Industrials, Real Estate, Technology, Utilities) and 111 industries. The ids are
sparse: an identity sequence also advances on a conflicting insert.

### `ref.statement_line_item` — the fundamentals taxonomy. **Grain:** `(statement, column_name)`.
**Source:** migration 0035, equal to `fafnir.ingest.fundamentals_map` (a test holds
them equal). One row per typed line item of the statement tables:

| Column | Notes |
|---|---|
| `statement` | `income`, `balance_sheet`, `cash_flow`. |
| `column_name` | The typed column. |
| `vendor_field` | The SF1 indicator (UNIQUE per statement). |
| `fmp_field` | The FMP spelling of its FMP build-prompt counterpart, where one exists, for a cross-vendor check. |
| `item_class` | `flow`, `stock`, `share_count` (`per_share` and `memo` are allowed and unused). **Load-bearing:** TTM sums a flow and takes the latest stock or share count. |
| `sign_convention` | `positive_is_inflow` (revenue; every cash-flow line, signed as cash moves, so capex is negative), `positive_is_outflow` (expenses), `signed` (subtotals, balances, share counts). |
| `is_subtotal`, `description` | |

`fafnir_app` reads it, as it reads the rest of `ref`.

### `ref.trading_calendar` — **Grain:** `(exchange_code, trade_date)`.
**Source:** generated by `fafnir.db.seed.seed_calendar` (`fafnir db seed`) for every
`ref.exchange` row with `country = 'US'` at the time. The nightly `ensure_horizon`
extends the tail year by year. **Used by** the gap, stale and sparse checks, dorq,
`dq recheck`, the price loader's session test and the panel's month-ends.

| Column | Type | Notes |
|---|---|---|
| `exchange_code` | TEXT NOT NULL → ref.exchange | |
| `trade_date` | DATE NOT NULL | A weekday. |
| `is_open` | BOOLEAN NOT NULL | Default TRUE. FALSE only on a known ad hoc closure (below). |
| `session_note` | TEXT | Unused: NULL on every row. A half-day is an open session. |

**How a closed day is represented.**
- **Recurring holidays:** by *absence*. The seed inserts open weekdays only, so no
  weekend and no recurring NYSE holiday has a row. A date with no row is closed
  inside the calendar's span and *unknown* outside it
  (`repository.open_sessions`).
- **Ad hoc closures:** the eleven 1990–2028 days with no annual rule are rows with
  `is_open = FALSE`, written by migration 0022: 1994-04-27, 2001-09-11 to 09-14,
  2004-06-11, 2007-01-02, 2012-10-29 and 10-30, 2018-12-05, 2025-01-09. A fresh install
  seeds after migrating, and the seed skips them (`seed.AD_HOC_CLOSURES`), so there
  they are absent rather than closed.
- **Every reader** filters on `is_open`, so the two forms read alike.

On 2026-10-31 the calendar covers AMEX, BATS, NASDAQ, NYSE and OTC, 1990-01-02 to
2028-12-29: 9,812 open sessions and 11 closed rows each. TXSE, CBOE and MUTF have
none and fall back to NASDAQ's.

**Known defect:** the seed closes Martin Luther King Jr. Day in 1990–1997, when NYSE
was open (it first closed for it in 1998). Those eight sessions are missing, so a bar
on one of them reads as a non-session date.

---

## Schema: `ops`

### `ops.ingestion_run` — lineage. **Grain:** `ingestion_run_id`.
One row per load, and per operator command that changes identity. Written through
`RunLog`; `fafnir_ops` reads it (MCP `ingestion_runs`).

| Column | Type | Notes |
|---|---|---|
| `ingestion_run_id` | BIGINT IDENTITY PK | Referenced by every lineage column (`core.daily_price`, `core.corporate_action`, the statements, the vendor tables, `ops.data_quality_flag`, `landing.*`). |
| `source` | TEXT NOT NULL | `fmp` (default), `sharadar`, `operator` or `dorq`. |
| `endpoint` | TEXT NOT NULL | What ran (below). |
| `params` | JSONB NOT NULL | The arguments, and what the run reports about itself: `params.progress` (checkpoints), `params.counts` and `params.through` (the Sharadar nightly), `params.mode` (`corporate-actions`: per-symbol or calendar), `params.by` and `params.changed` (`feed`), `params.skipped` (the master load). |
| `window_from` / `window_to` | DATE | The date window asked for, where there is one. |
| `symbols_requested` | INTEGER NOT NULL | |
| `rows_inserted` / `rows_updated` / `rows_quarantined` | INTEGER NOT NULL | |
| `bytes_downloaded` | BIGINT NOT NULL | Against FMP's 50 GB/month budget. |
| `response_hash` | TEXT | |
| `status` | TEXT NOT NULL CHECK | `started` (default) / `success` / `partial` / `failed`. Indexed where `failed` or `partial`. |
| `error_message` | TEXT | |
| `started_at` / `finished_at` | TIMESTAMPTZ | Indexed with `(source, endpoint)`. |

A long load can write its counts so far to its open run (`RunLog.checkpoint`); the
Sharadar scans do every minute, with `params.progress`. A `started` row with no
`finished_at` and no live process was killed outright: its counts are as of the last
checkpoint.

**The vocabulary**, as recorded by 2026-10-31. A command not yet run on a warehouse
has no rows.

| `source` | `endpoint` | Written by |
|---|---|---|
| `fmp` | `stock-list` | `ingest securities` (the screener universe) |
| `fmp` | `profile` | `ingest securities --enrich`, and `ingest tracked` |
| `fmp` | `historical-price-eod/non-split-adjusted` | `ingest prices` |
| `fmp` | `historical-price-eod/full` | the legacy split-adjusted feed, before 2026-08-26 ([ADR 0004](adr/0004-unadjusted-price-feed.md)) |
| `fmp` | `corporate-actions` | `ingest actions`, in `symbol`, `calendar` or `auto` mode (`params.mode`) |
| `fmp` | `symbol-change` | `ingest symbol-changes` |
| `fmp` | `delisted-companies` | `ingest delisted` |
| `fmp`, `sharadar` | `feed` | `vendor feed`: a security's feed moved to (`source`) that vendor |
| `sharadar` | `bulk:<table>`, `delta:<table>` | `source sharadar-bulk`, `source sharadar-delta` (`tickers`, `actions`, `stocks`, `funds`, `fundamentals`) |
| `sharadar` | `master:tickers`, `master:actions` | `ingest sharadar-master` |
| `sharadar` | `universe` | `ingest sharadar-universe` |
| `sharadar` | `prices`, `prices:nightly` | `ingest sharadar-prices`, bulk and `--nightly` |
| `sharadar` | `actions`, `actions:prune` | `ingest sharadar-actions`, and its prune of withdrawn rows |
| `sharadar` | `fundamentals`, `fundamentals:nightly` | `ingest fundamentals`, `--backfill` and nightly |
| `sharadar` | `link`, `link:new` | `vendor link`, over everything and over new keys only |
| `sharadar` | `mint-delisted` | `vendor mint-delisted` |
| `sharadar` | `correct` | `vendor correct` |
| `sharadar` | `overlap` | `vendor overlap` |
| `sharadar` | `prune` | `source sharadar-prune` |
| `operator` | `security-delist`, `security-relist`, `security-venue-transfer`, `security-descope`, `security-merge-across-vendors` | the `fafnir security` commands of those names |
| `dorq` | `dq-shadow`, `dq-run` | `dq run --engine dorq`, in shadow or queue mode |

### `ops.data_quality_flag` — quarantine/anomaly queue. **Grain:** `dq_flag_id`.

| Column | Type | Notes |
|---|---|---|
| `dq_flag_id` | BIGINT IDENTITY PK | |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | The load that raised it. NULL on every flag from `dq run`'s SQL checks and from `fafnir adjust`, which open no run. |
| `security_id` | BIGINT | The security it is about, or NULL for a cross-sectional or vendor-key-only condition. **No foreign key**, so a merge or descope is not blocked; `security merge` retargets a victim's flags. |
| `table_name` | TEXT | The relation the condition is in (below). NULL for the fundamentals loader's run-level flags. |
| `record_key` | JSONB | What tells one occurrence from another on the same security and check (below). Part of the open-condition key. |
| `check_name` | TEXT NOT NULL | Which check (the catalogue below). |
| `severity` | TEXT NOT NULL CHECK | `info`, `warn` (default) or `error`. |
| `detail` | JSONB | The check's evidence. Free-form per check, and it can hold a raw exception string (`adjustment_failed`), which is why no mart view exposes it. |
| `detected_at` | TIMESTAMPTZ NOT NULL | When first seen: re-detection does not write a second row. |
| `resolved_at` / `resolved_by` / `resolution_note` | | The closure (0017). Provenance only on a closed flag (`ck_dq_flag_resolution_provenance`). |
| `accepted_at` / `accepted_by` / `accepted_note` | | Set with `dq accept` (0024): the condition is real and permanent. Only on a resolved flag (`ck_dq_flag_accepted_is_resolved`); the checks skip an accepted condition, so it is not re-detected. |
| `judgement_basis` | TEXT CHECK | `per_flag`, `by_rule` or NULL (0028); only on a resolved flag (`ck_dq_flag_judgement_basis`). |

**Indexes.**
- `ux_dq_flag_open_condition`: unique on `(check_name, COALESCE(security_id, -1),
  COALESCE(record_key, '{}'))` where open and not `price_*`.
- `ix_dq_flag_open_condition`, `ix_dq_flag_accepted_condition`: the write path's
  look-ups.
- `ix_dq_flag_open`: `(check_name, detected_at DESC)` where open.
- `ix_dq_flag_resolved`: `resolved_at DESC` where closed.
- `ix_dq_flag_security`: `security_id` where set.

**Size.** On 2026-10-31: 1,562,483 rows (1.95 GB), of which 10,988 open and 462,829
accepted.

`judgement_basis` says how a closure was judged: `per_flag` (`dq accept|resolve
--per-flag`: this flag's own evidence was looked at), `by_rule` (`--by-rule`: closed as
one of a class), or NULL (not said). Only a closed flag carries one, and `dq reopen`
clears it with the rest of the resolution. 0028 marked `by_rule` the closures whose
note says so ("without per-flag verification", "Classified by rule", "not individually
verified": the 23,903 outlier acceptances of 2026-09-10/11). `dq export-labels` writes
it as a label's `verified`.

Read and worked with `fafnir dq list` / `fafnir dq resolve` (see
[operations.md](operations.md#working-the-dq-queue)). Since migration 0017 a
resolution records **who** closed the flag and **why**: `resolved_at` alone cannot
tell a gap that was genuinely explained from one closed to quiet the count, and the
judgement is the part worth keeping. `ck_dq_flag_resolution_provenance` holds the
other half of that — provenance exists only where a resolution does — so reopening a
flag (`fafnir dq reopen`) clears the note rather than leaving a decision that no
longer stands on an open row.

**One unresolved problem is one unresolved row.** The checks that write here run on
a schedule over the same data, so a standing condition is flagged once, not once per
run: a writer skips the insert when an unresolved flag with the same
`(security_id, check_name, record_key)` already exists. A different `record_key` — a
new gap date, another ex-date — is a different occurrence and is still recorded.
That is what makes `count(*) WHERE resolved_at IS NULL` (the `Open DQ` line in
`fafnir status`) a count of problems rather than a count of check runs.

`price_*` is the deliberate exception and **does** repeat, once per re-detection:
`daily_price` counts those rows for a `(security_id, date)` to decide when a
persistently-bad bar has held the ingestion watermark long enough
(`MAX_QUARANTINE_HOLDS`). Deduplicating them would freeze that counter at 1 and hold
the watermark behind the bar forever. New writers pick a side explicitly:
`repository.add_dq_flag_once` for a standing condition, `add_dq_flag` where each
detection is itself the signal.

`price_*` is the family an operator globs for, not a promise that every member is a
quarantine. `price_scale_collapse` shares the prefix but describes a bar that **was**
stored, so it is written with `add_dq_flag_once` (one row per corrupted bar, not one
per nightly overlap) and `repository.count_price_quarantines` excludes it by name via
`NON_QUARANTINE_PRICE_CHECKS`. Counting it would let a stored-but-corrupted bar spend
the watermark budget of a *rejected* bar on the same date and release ingestion past
one nobody had looked at. `price_restatement_conflict` (v1.5.0) is the second such
check: the nightly load kept the stored bar and the vendor's re-read disagrees with it
beyond `[prices] restatement_max_ratio` / `restatement_max_volume_ratio` (a volume
difference with both sides at or below `restatement_min_volume`, a round lot, is not
one); it is written once per open `(symbol, date)` and listed in
`NON_QUARANTINE_PRICE_CHECKS`.
`price_split_scale_mismatch` is a real quarantine (a new bar at a split-adjusted scale
was not stored) and repeats like the others. Anything added to that family later has
to make the same choice explicitly.

Since migration 0016 the schema holds the rule too — `ux_dq_flag_open_condition`,
unique on `(check_name, security_id, record_key)` where `resolved_at IS NULL` and the
check is not `price_*`. It is a backstop, not the mechanism: the guard stays in the
write path, because a constraint alone would turn a redundant flag (harmless noise)
into an exception that aborts whatever load raised it. 0016 also collapsed the
duplicates a running warehouse had already accumulated, keeping the earliest row of
each condition so `detected_at` still says when the problem was first seen.

`dorq_*` flags are dorq's (doc/dorq.md): `detail` is dorq's whole report, with
`p_error`, the hypotheses and evidence, the suggested repair and `dorq.version` /
`dorq.config_hash`. The cross-sectional ones (`dorq_cohort_gap`, `dorq_cohort_move`)
have a NULL `security_id`. They are written with the same open/accepted guards as
the SQL checks, by a run recorded in `ops.ingestion_run` with `source = 'dorq'`. A
cross-sectional row's `record_key` carries the cohort's split `ratio` beside its
`trade_date` (v1.5.0; dorq 0.10 writes it itself): two cohorts on one date are two
conditions.

Other checks added in v1.5.0: `split_duplicate_suspect` (`fafnir adjust`: two splits
of one ratio within ten sessions; keyed `{"ex_dates": [d1, d2]}`) and
`tracked_symbol_delisted` (`ingest tracked`: a declared ticker whose identity is a
delisted security; keyed `{"symbol": ...}`).

#### The check catalogue

Every `check_name` the code writes, with the relation it names and the shape of its
`record_key`. **What each one means, how to tell a defect from a market fact, and how
to close it** are in the DQ playbooks
([`.claude/skills/fafnir-dba/references/dq-playbooks.md`](../.claude/skills/fafnir-dba/references/dq-playbooks.md)).
A check marked † had written no row by 2026-10-31.

| `check_name` | Written by | `table_name` | `record_key` |
|---|---|---|---|
| `gap`, `outlier` | `dq run` | `core.daily_price` | `trade_date` |
| `stale` | `dq run` | `core.daily_price` | `last_date` |
| `sparse_coverage` | `dq run` | `core.daily_price` | `{}`: one per security |
| `security_duplicate_identity`, `security_missing_classification` | `dq run` | `core.security` | `symbol` |
| `price_<reason>`: `missing_or_nonnumeric_ohlc`†, `non_positive_price`, `price_out_of_range`†, `subresolution_price`, `cross_field_violation`, `volume_out_of_range`† | `ingest prices`, `ingest sharadar-prices` (a rejected bar) | `core.daily_price` | `symbol`, `date` |
| `price_scale_collapse`, `price_restatement_conflict`, `price_split_scale_mismatch`† | `ingest prices` | `core.daily_price` | `symbol`, `date` |
| `split_invalid`†, `dividend_invalid`† | `ingest actions` | `core.corporate_action` | `symbol`, `date` |
| `corporate_action_drift` | `ingest actions` (the calendar reconciliation) | `core.corporate_action` | `symbol` |
| `split_duplicate_suspect` | `fafnir adjust` | `core.corporate_action` | `ex_dates` (two) |
| `dividend_no_prior_close`, `dividend_exceeds_price` | `fafnir adjust` (`ingest sharadar-actions` closes those of a dividend it removes) | `core.adjustment_factor` | `ex_date` |
| `adjustment_factor_extreme` | `fafnir adjust` | `core.adjustment_factor` | `effective_date` |
| `adjustment_failed`† | `fafnir adjust` | `core.adjustment_factor` | none; `detail.error` is the exception |
| `security_company_name_drift`, `security_inactive_but_trading` | `ingest securities`; the second also `ingest sharadar-universe` | `core.security` | `symbol` |
| `security_market_cap_usd_out_of_range`†, `security_beta_out_of_range`† | `ingest securities` (value stored as NULL) | `core.security` | `symbol` |
| `security_delisting_contradicted` | `ingest delisted`, `ingest sharadar-universe`; `security venue-transfer` writes and accepts one to record its decision | `core.security` | `symbol`, `delisted_date` |
| `symbol_change_conflict` | `ingest symbol-changes`, `ingest sharadar-universe` | `core.security` | `old_symbol`, `new_symbol` |
| `tracked_symbol_delisted`†, `tracked_symbol_unknown_to_source`† | `ingest tracked` | `ref.tracked_symbol` | `symbol` |
| `vendor_xref_unmatched`, `vendor_xref_ambiguous` | `vendor link` | `core.security_vendor_xref` | `vendor` |
| `vendor_history_disagrees` | `vendor link` | `core.daily_price` | `vendor`, `vendor_key` |
| `vendor_history_overlap` | `vendor overlap` | `core.daily_price` | `kind`, `vendor`, `vendor_key` |
| `sharadar_split_ratio_mismatch` | `ingest sharadar-prices` | `core.daily_price` | `vendor`, `vendor_key` |
| `fundamentals_duplicate_period`, `fundamentals_filing_before_period`, `fundamentals_missing_period_label`†, `fundamentals_value_out_of_range`† | `ingest fundamentals` (a quarantined SF1 row) | `landing.sharadar_file` | `ticker`, `dimension`, `reportperiod` |
| `fundamentals_restatement`, `fundamentals_vendor_correction` | `ingest fundamentals`, once per security and run | NULL | `ingestion_run_id` |
| `fundamentals_estimated_knowledge_date` | `ingest fundamentals` | NULL | `vendor`, `vendor_key` |
| `fundamentals_unlinked` | `ingest fundamentals` (no security for the permaticker; `security_id` NULL) | `core.security_vendor_xref` | `vendor`, `vendor_key` |
| `balance_sheet_identity`, `cash_flow_tie` | `dq run` | `core.balance_sheet`, `core.cash_flow_statement` | `period_type`, `fiscal_date`; the rows of 2026-10-28 also carry `version_no` |
| `cash_balance_tie` | `dq run` | `core.cash_flow_statement`; or `core.balance_sheet`, keyed with `item` too | `period_type`, `fiscal_date` (+ `item`) |
| `fundamentals_negative_impossible` | `dq run` | `core.income_statement` | `statement`, `period_type`, `fiscal_date`, `version_no` |
| `fundamentals_suspicious_zero` | `dq run` | the statement's table | `statement`, `period_type`, `fiscal_date` |
| `fundamentals_implausible_filing_lag` | `dq run` | `core.income_statement` | `period_type`, `fiscal_date`, `version_no` |
| `fundamentals_gap` | `dq run` | `core.income_statement` | `from`, `to` |
| `fundamentals_stale` | `dq run` | `core.income_statement` | `latest_fiscal_date` |
| `fundamentals_ttm_incomplete` | `dq run` | `core.income_statement` | `fiscal_year` |
| `fundamentals_ytd_suspect` | `dq run` | the statement's table | `fiscal_year`, `item` |
| `fundamentals_currency_mismatch` | `dq run` | `core.income_statement` | `reported_currency` |
| `fundamentals_coverage`† | `dq run` | `core.income_statement` | `{}` |
| `fundamentals_unmapped_fields`† | `dq run` (no security) | `core.income_statement` | `field` |
| `fundamentals_version_chain`† | `dq run` | the statement's table | the period |
| `dorq_*` (`dorq_bad_print`, `dorq_scale_shift`, `dorq_stale_feed`, `dorq_cohort_gap`, `dorq_cohort_move`, …)† | `dq run --engine dorq --no-shadow` | `core.daily_price` | `trade_date`; a cohort row also `ratio`, with `security_id` NULL |

dorq has run in shadow mode throughout (`[dq] dorq_shadow = true`), so its findings
are in `<dorq_shadow_dir>/<as-of>.jsonl` and the queue holds no `dorq_*` row.

### `ops.load_watermark` — incremental marks. **Grain:** `(source, endpoint, security_id)`.
`source` (TEXT, default `fmp`), `endpoint`, `security_id` (BIGINT, default 0),
`last_loaded_date`, `last_run_at`, `updated_at`. `security_id = 0` denotes a
whole-endpoint (non per-symbol) mark. No foreign key: `security merge` deletes a
victim's marks.

| `source` | Endpoint | `security_id` | `last_loaded_date` means |
|---|---|---|---|
| `fmp` | `historical-price-eod/non-split-adjusted` | the security | bars are loaded through this date |
| `fmp` | `corporate-actions` | the security | its full action history has been pulled, as of this date |
| `fmp` | `corporate-actions-calendar` | `0` | the market-wide calendars have been read through this date |
| `sharadar` | `SEP`, `SFP` | the security | Sharadar's equity (`stocks`) or fund (`funds`) bars are applied through this date (`ingest sharadar-prices`) |
| `sharadar` | `SF1:ARQ`, `SF1:ARY`, `SF1:MRQ`, `SF1:MRY` | the security | the latest `reportperiod` applied from that SF1 dimension (`ingest fundamentals`): as-reported (`AR*`) or most-recent (`MR*`), quarterly or annual |
| `dorq` | `dq-run`, `dq-shadow` | `0` | dorq has judged bars through this as-of date, into the queue or the shadow file; the next nightly run reports from five sessions before it, and a re-run for the same date over the same span as that night's run |

On 2026-10-31: 21,705 FMP price marks and 15,914 FMP action marks, which stopped
advancing at the cutover for what Sharadar feeds. Sharadar had 15,751 `SEP`, 7,966
`SFP` and about 16,400–17,100 marks per SF1 dimension. dorq had only `dq-shadow`.

The `corporate-actions` row is a presence flag as much as a date: its *absence* is what
puts a newly minted security on the full-history path, so it is stamped with the run
date rather than the security's last ex-date (a security that has never paid anything
must still stop being re-pulled).

### `ops.operator_override` — operator corrections. **Grain:** `override_id`.
Written by `fafnir actions add|delete|redate`, `fafnir prices delete|shift|rescale`
and `fafnir security split-history`; undone by `fafnir override revoke`
(migrations 0025, 0026). Active means `revoked_at IS NULL`.

| Column | Type | Notes |
|---|---|---|
| `override_id` | BIGINT IDENTITY PK | |
| `security_id` | BIGINT | No FK, so merges and folds are not blocked; `security merge` retargets a victim's overrides. |
| `target` | TEXT CHECK | `corporate_action` or `daily_price`. |
| `action_type` | TEXT | `split` / `dividend` for a corporate action; NULL for a bar. |
| `key_date` | DATE | `ex_date` for an action, `trade_date` for a bar. |
| `operation` | TEXT CHECK | `delete`: the row was removed, and while active the loaders set aside a vendor row at this key. `add`: the operator wrote the row (`source = operator`). A bar is only ever added as a recorded transform of a vendor bar -- the table refuses a bar `add` without `detail.transform` -- and the price loader sets aside a vendor bar on the date of either operation. |
| `detail` | JSONB | `delete`: the row as it stood. `add`: the values written. A re-date pair names the other half (`redated_to_override` / `redated_from_override`). A bar shift or rescale carries `transform`: `kind` (`shift` / `rescale`), its parameters (`days`, or `price_factor` and `volume_factor`), the other half (`to_override` / `from_override`, `to_date` / `from_date`), and `edit` -- the id of the edit's first override, shared by every override of one command. A key moved by `security split-history` carries `split` = `{source_security_id, destination_security_id, kind}`, where `kind` is `moved`, `duplicate` (the destination already held it identically), or `restored` (an earlier `prices delete`, now also materialised on the destination); `--undo` reads exactly these. |
| `note` / `created_by` / `created_at` | TEXT / TEXT / TIMESTAMPTZ | The evidence and the owner. `note` may not be blank. |
| `reason` | TEXT CHECK (0028) | Why: `data_error` (the row was wrong; the default, and every row before 0028), `out_of_scope` (the security does not belong -- its data was removed, not repaired), `cleanup` (bookkeeping). Set with `--reason` on every command above; changed after the fact with `fafnir override set-reason`, which appends `{from, to, note, by, at}` to `detail.reason_history`. Only `data_error` overrides are exported as labels. |
| `revoked_at` / `revoked_by` / `revoked_note` | | Set by `override revoke`. Revoking a `delete` lifts the suppression only; revoking an `add` removes the operator's row. A bar shift or rescale is revoked whole: any of its overrides revokes every one of the `edit`, removes the operator bars and writes the vendor bars back from `detail.row`. A bar shift or rescale undone this way also puts back any corporate action a `--with-actions` shift re-dated. A key carrying a `split` marker is refused: revoking it alone would lift the suppression while the destination kept its copy, so a split is reversed whole by `security split-history --undo`. |

At most one active override per `(security_id, target, action_type, key_date,
operation)` (`ux_operator_override_active`): a `delete` and an `add` may share a key,
which is how a wrong vendor row is replaced by a corrected one. Indexed on
`security_id`.

On 2026-10-31: 342,685 overrides.
- **Bars:** 233,167 `delete`/`data_error`, 100,936 `add`/`data_error` (the shift and
  rescale transforms), 4,403 `delete`/`out_of_scope` and 388 `delete`/`cleanup`.
- **Actions:** 702 `delete`/`data_error`, 2,748 `delete`/`cleanup` and 341
  `add`/`data_error`.

### `ops.vendor_correction` — rows one vendor replaced. **Grain:** `correction_id`.
Every `core.daily_price` / `core.corporate_action` row a higher-ranked vendor
replaced in an explicit correction ([ADR 0015](adr/0015-multi-vendor-provenance-and-precedence.md)
§3), as it stood, written in the same transaction as the replacement (migration
0029). Listed and undone with `fafnir vendor corrections list|revert`. Active means
`reverted_at IS NULL`. `old_row` is the replaced vendor's data, so it is kept or
deleted under that vendor's licence, like `landing.fmp_raw`.

| Column | Type | Notes |
|---|---|---|
| `correction_id` | BIGINT IDENTITY PK | |
| `security_id` | BIGINT | No FK, as `operator_override`; `security merge` retargets a victim's corrections (a victim correction on a key the survivor already has an active one for stays with the victim's id). |
| `target` | TEXT CHECK | `daily_price` or `corporate_action`. |
| `action_type` | TEXT CHECK | `split` / `dividend` for an action; NULL for a bar. |
| `key_date` | DATE | `trade_date` for a bar, `ex_date` for an action. |
| `old_source` / `new_source` | TEXT CHECK | The party replaced and the one that replaced it. They differ, and `old_source` is never `operator`. |
| `old_row` | JSONB | The replaced row's value columns, `ingestion_run_id` and `loaded_at`: what a revert writes back. |
| `new_row` | JSONB | The values the correction wrote. A revert refuses when the row no longer holds them. |
| `reason` | TEXT CHECK | `reconcile_divergence`, written by `fafnir vendor correct` (#75) for an FMP bar Sharadar's disagreed with; `gap_fill` and `restatement` are reserved for the loaders that will use them. |
| `ingestion_run_id` | BIGINT | The correcting run, when it has one. |
| `corrected_at` | TIMESTAMPTZ | |
| `reverted_at` / `reverted_by` / `revert_note` | | Set by `vendor corrections revert`; `reverted_at` and `reverted_by` are set together. |

At most one active correction per `(security_id, target, action_type, key_date)`
(`ux_vendor_correction_active`); indexed on `security_id`. On 2026-10-31: 704,351
corrections, every one a `daily_price` bar FMP → Sharadar for `reconcile_divergence`;
57 of them reverted.

### `ops.dq_label` — labels a person stated. **Grain:** `label_id`.
What someone checked and found true about a stretch of one series, or a cohort, for
measuring dorq (doc/dorq.md, "Labels"). Written by `fafnir dq label add|import` and
`dq accept|resolve --label`; revoked by `dq label revoke` (and by `dq reopen`, for a
label stated with that flag's closure). Merged into `dq export-labels`, where a row
here wins over a derived label on the same security, overlapping dates and class.
Changes no data and no flag (migration 0028). Active means `revoked_at IS NULL`.

| Column | Type | Notes |
|---|---|---|
| `label_id` | BIGINT IDENTITY PK | |
| `security_id` | BIGINT | NULL for a cross-sectional label (dorq's series `*`). No FK, as `operator_override`. |
| `members` | BIGINT[] | A cohort label's securities: it covers only the cross-sectional rows naming one of them. |
| `first_date` / `last_date` | DATE | The span. |
| `class` | TEXT CHECK | `data_error`, `context_gap` or `market_fact`. |
| `kind` | TEXT | `feed_gap`, `odd_lots_only`, `distribution`, `dividend_units`, `wrong_instrument`, `bad_print`, `cohort_fact`, or free text. |
| `expect` / `codes` | TEXT | A fault's expected codes (`DQ202\|DQ205`); a market fact's code prefix (default `DQ2`). |
| `note`, `verified` | TEXT, BOOLEAN | What was checked; whether on the bars or an outside source. |
| `source` | TEXT | Free text: `dq_flag:893223`, `hand-2026-10-15`, `cli`. |
| `decided_by` / `decided_at` | TEXT / TIMESTAMPTZ | |
| `applies_until` | TIMESTAMPTZ | When a repair changed what dorq reads; a shadow night that ran after it is not judged by the label. NULL: always applies. |
| `revoked_at` / `revoked_by` / `revoked_note` | | |

---

## Schema: `landing`

### `landing.fmp_raw` — raw immutable payloads. **Grain:** `raw_id`.
Ground truth for any field whose meaning is later questioned. Kept or deleted under
FMP's licence.

| Column | Type | Notes |
|---|---|---|
| `raw_id` | BIGINT IDENTITY PK | |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | |
| `endpoint` | TEXT NOT NULL | FMP's path: `historical-price-eod/non-split-adjusted`, `splits`, `dividends`, `splits-calendar`, `dividends-calendar`, `profile`, `symbol-change`, and from v1.5.0 `delisted-companies`. |
| `params` | JSONB NOT NULL | The request's query, as sent. |
| `symbol` | TEXT | NULL for a market-wide endpoint (the calendars, the delisted sweep). |
| `http_status` | INTEGER | 200 on every row on 2026-10-31. |
| `payload` | JSONB | The response body, as received. |
| `payload_hash` | TEXT | Indexed, to find repeated payloads. |
| `bytes` | BIGINT NOT NULL | |
| `fetched_at` | TIMESTAMPTZ NOT NULL | Indexed with `(endpoint, symbol)`, newest first. |

**What it does not hold:** `stock-list`/`company-screener`, the security-master
universe. The screener cannot be audited after the fact; `core.security.updated_at`
is its trace. 914,989 rows, 6.2 GB, on 2026-10-31 (the largest relation outside
`core.daily_price`). Nothing new lands for the securities Sharadar feeds.

### `landing.sharadar_file` — the Sharadar file ledger. **Grain:** `file_id`.
One row per file downloaded from Sharadar (migration 0030, plan v2). The file is
on disk under `[sharadar] bulk_dir`, named by its content. There are no typed
landing tables: loaders read the file through `fafnir.sources.sharadar_files`
and write `core` directly. Unlike `landing.fmp_raw`, the ledger does not hold the
data. A restore of the database brings the ledger back without the files (see
[operations.md](operations.md#backups)).

| Column | Type | Notes |
|---|---|---|
| `file_id` | BIGINT IDENTITY PK | |
| `vendor_table` | TEXT CHECK | Sharadar's modern name: `stocks` (not `SEP`), `funds` (SFP), `fundamentals` (SF1), `tickers`, `actions`, `daily`, `metrics`, `descriptions`, `events`, `sp500`, `insiders`, `holdings`, `holdings_investor`, `holdings_ticker`. Only the first five have been fetched. |
| `kind` | TEXT CHECK | `full` (a bulk file, `years=5\|10\|full`) or `delta` (a slice: a date, a `lastupdated` window). |
| `filters` | JSONB | What was asked for, as sent. |
| `path` | TEXT CHECK | Relative to `bulk_dir`: `<sha256><suffix>`. Never absolute, never `..`. |
| `member` | TEXT | The CSV inside a zip; NULL for a plain or gzipped CSV. |
| `sha256` | TEXT UNIQUE | The file's content hash: a file already held is not recorded twice. |
| `bytes` / `rows` | BIGINT | Size on disk; data rows, NULL until first read. |
| `columns` | TEXT[] | The CSV header as found, for drift checks against the next file. |
| `fetched_at` / `ingestion_run_id` | | When, and the run that fetched it. |
| `status` / `status_at` | TEXT CHECK | The file's lifecycle: `downloaded` (default), `superseded` (a newer full of the table), `deleted` (gone from disk; `source sharadar-prune`). Not a loader's progress: several loaders read one file, and each keeps its own record. |

Indexed on `(vendor_table, kind, fetched_at DESC)`. On 2026-10-31: 34 rows. The
current full file of each of `stocks`, `funds`, `fundamentals`, `tickers` and
`actions`, older fulls `superseded` or `deleted`, and 22 nightly deltas (`stocks`,
`funds`, `fundamentals`). MCP `sharadar_files` reads it.

---

## Schema: `meta`

### `meta.schema_migration` — migration bookkeeping. **Grain:** `version`.
`version` (TEXT PK, `0001`…), `name`, `checksum` (SHA-256 of the up-migration),
`applied_at`. Written by `fafnir db migrate`, removed by `fafnir db rollback`. On
2026-10-31 it holds 0001–0040, the repository's full set (MCP `schema_state`).

---

## Source → table map

Since the cutover (2026-10-22) Sharadar is the primary vendor
(`[general] primary_price_vendor = "sharadar"`). FMP still feeds what Sharadar does
not: the `source = 'fmp'` securities with no feeding Sharadar link (`repo.fmp_fed`).
[ingestion.md](ingestion.md) has every command's request, target and natural key.

**Sharadar** (files under `[sharadar] bulk_dir`, ledger `landing.sharadar_file`):

| Sharadar table | Command | Target | Grain |
|---|---|---|---|
| TICKERS (`tickers`) | `ingest sharadar-master` | `core.vendor_security` | (vendor, vendor_table, vendor_key) |
| ACTIONS (`actions`), lifecycle events | `ingest sharadar-master` | `core.vendor_security_event` | the event grain |
| the master's events | `ingest sharadar-universe` | `core.security`, `core.symbol_xref`, `core.symbol_change` | security_id |
| TICKERS (dead issuers) | `vendor mint-delisted` | `core.security`, `core.symbol_xref`, `core.security_vendor_xref` | security_id |
| SEP (`stocks`), SFP (`funds`) | `ingest sharadar-prices` | `core.daily_price` (`source = 'sharadar'`, raw recovered) | (security_id, trade_date) |
| ACTIONS (splits, dividends) | `ingest sharadar-actions` | `core.corporate_action` (`source = 'sharadar'`) | (security_id, action_type, ex_date) |
| SF1 (`fundamentals`) | `ingest fundamentals` | `core.income_statement`, `core.balance_sheet`, `core.cash_flow_statement` | (security_id, period_type, fiscal_date, version_no, loaded_at) |
| SEP/SFP against FMP's bars | `vendor link`, `vendor correct` | `core.security_vendor_xref`; `core.daily_price` with `ops.vendor_correction` | — |

**FMP** (`stable/` endpoints; payloads in `landing.fmp_raw`):

| FMP endpoint | Command | Target | Grain |
|---|---|---|---|
| `company-screener` (`stock-list` run) | `ingest securities` | `core.security` (incl. `market_cap_usd`, `beta`), `core.symbol_xref`, `ref.sector`, `ref.industry` | security_id |
| `profile` | `ingest securities --enrich`, `ingest tracked` | `core.security` (identifiers), `core.company_profile` | security_id |
| `historical-price-eod/non-split-adjusted` | `ingest prices` | `core.daily_price` | (security_id, trade_date) |
| `splits`, `dividends` (per symbol); `splits-calendar`, `dividends-calendar` | `ingest actions` | `core.corporate_action` | (security_id, action_type, ex_date) |
| `symbol-change` | `ingest symbol-changes` | `core.symbol_change`, `core.symbol_xref` | (source, old, new, change_date) |
| `delisted-companies` | `ingest delisted` | `core.security.delisted_date` | security_id |

**Derived and operator-written:**

| Writer | Target |
|---|---|
| `fafnir adjust` | `core.adjustment_factor` |
| `fafnir db refresh-marts` | `mart.security_latest`, `mart.fundamental_panel`, `mart.security_fundamentals_latest` (`maintenance.MATVIEWS`, in that order) |
| `dq run` (SQL checks, dorq) | `ops.data_quality_flag`; dorq's shadow file and `ops.load_watermark` |
| `dq resolve\|accept\|reopen\|recheck`, `dq label` | `ops.data_quality_flag`, `ops.dq_label` |
| `actions add\|delete\|redate`, `prices delete\|shift\|rescale`, `override revoke\|set-reason` | `core.corporate_action` / `core.daily_price` with `ops.operator_override` |
| `security merge\|split-history\|venue-transfer\|delist\|relist\|descope` | `core.security` and everything keyed on it; an `operator` run in `ops.ingestion_run` |
| `track add\|rm` | `ref.tracked_symbol` |
| `vendor link accept\|reject`, `vendor feed` | `core.security_vendor_xref`, `core.security_vendor_rejection` |

Domains not built (economic series and the rest) are sketched in
[extending.md](extending.md).
