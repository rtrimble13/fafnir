# Fafnir Data Dictionary

Every table and column in the initial release, with its **grain**, **source**,
**units**, **adjustment status**, and **update cadence**. This is the contract
between the warehouse and everything that reads it.

Conventions:
- Money/prices are exact `NUMERIC` — never floating point.
- Temporal columns are suffixed by meaning: `*_date` (observation), `loaded_at`
  / `computed_at` / `fetched_at` (process time), `valid_from`/`valid_to`
  (knowledge interval).
- "Adjustment status" states whether price values are **raw** (unadjusted) or
  **adjusted** (split/dividend back-adjusted).

---

## Schema: `core`

### `core.security` — security master
**Grain:** one row per minted `security_id`. **Source:** FMP `stock-list` / `etf-list`
(+ `profile` enrichment). **Cadence:** refreshed on security loads; profile fields
on enrichment.

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
| `is_actively_trading` | BOOLEAN | False for delisted/inactive. The vendor's view, copied by the nightly load: it is not evidence that a listed security stopped trading (FMP called ET inactive the week it moved to TXSE), so the nightly universes also poll a listed row with a bar in the last 30 days (ADR 0014). |
| `is_etf` / `is_fund` | BOOLEAN | |
| `ipo_date` | DATE | |
| `venue_override` / `venue_override_left` | TEXT | An operator-recorded venue (`fafnir security venue-transfer`, migration 0027), and the venue the security left. While set, the nightly upsert keeps `exchange_code` and `is_actively_trading` as they are, until the vendor reports a venue other than `venue_override_left`; then both clear. ADR 0014. |
| `delisted_date` | DATE | NULL while listed; set (never deleted) on delist. |
| `source` | TEXT | The party that **minted** the security, not the one that feeds it now ([ADR 0015](adr/0015-multi-vendor-provenance-and-precedence.md)). Values: `fmp` (default), `operator`, and, from the Sharadar work on, `sharadar` and `tiingo`. The FMP loaders feed `source = 'fmp'` rows and nothing else: their universes filter on it, and a per-symbol pull that resolves to any other row is refused, because FMP serves that ticker for a different issuer. `operator` is a security minted by `fafnir security split-history` to hold one issuer's history split off a reused ticker: delisted, carrying a closed `symbol_xref` period, fed by nobody. `sharadar` is a dead issuer minted by `fafnir vendor mint-delisted` (#71, ADR 0016 §3): delisted, `is_actively_trading` false, the vendor's ticker as `primary_symbol`, a closed `symbol_xref` period (`source = 'sharadar'`) per ticker it traded under, and a `minted`, feeding link to its permaticker. It holds no bars until the Sharadar backfill (#75). |
| `first_seen_at` / `updated_at` | TIMESTAMPTZ | Load process time. |

Soft natural key: `UNIQUE (source, primary_symbol) WHERE delisted_date IS NULL`
makes the security upsert idempotent without keying on the ticker globally. Scoped
to *listed* rows (0009) so a reused ticker mints a new `security_id` instead of
overwriting a dead issuer. The **exchange is deliberately not part of it** (0012):
it is an attribute of the listing, not the company, so a venue transfer
(NYSE → NASDAQ) updates the security that already holds the history rather than
forking it into a second row.

### `core.symbol_xref` — ticker history / cross-reference
**Grain:** `(symbol, valid_from)`. **Source:** derived. **Cadence:** on security loads.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT → core.security | |
| `symbol` | TEXT | External ticker. |
| `valid_from` | DATE | Start of validity (default `1900-01-01`). |
| `valid_to` | DATE | NULL = currently valid. |
| `is_primary` | BOOLEAN | The security's primary ticker in the interval. |
| `source` | TEXT | |

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
FMP `symbol-change`. **Cadence:** nightly (`fafnir ingest symbol-changes`).

| Column | Type | Notes |
|---|---|---|
| `symbol_change_id` | BIGINT IDENTITY PK | |
| `old_symbol` / `new_symbol` | TEXT NOT NULL | `CHECK (old_symbol <> new_symbol)`. |
| `change_date` | DATE NOT NULL | Effective date; the xref period boundary. |
| `security_id` | BIGINT → core.security | The security the rename was applied to; NULL while unapplied. Also NULL once `fafnir security descope` has removed that security (ADR 0012): the row is kept, detached, so the sweep still knows the rename was recorded. |
| `company_name` | TEXT | As reported with the rename. |
| `status` | TEXT CHECK | `applied` / `conflict` / `ignored` / `dismissed` — see below. |
| `detail` | JSONB | Context: the `folded_security_id` of an absorbed duplicate, the `merged_security_id` of one merged by hand, or `dismissed_by` / `dismissed_note` / `dismissed_at`. A `conflict` refused because it would change the instrument carries that as `reason`. An applied rename records `old_company_name`, the security's name before the rename overwrote it: the security-master load declines a screener entry for the old ticker under that name (or the security's current name, or `company_name` above) as an echo instead of minting a copy of the company. `echo_names` lists further names it declines on that ticker: each one recorded by `security merge` from a re-minted echo it folded back in. |
| `source` | TEXT | |
| `first_seen_at` / `updated_at` | TIMESTAMPTZ | |

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

### `core.company_profile` — descriptive attributes (current snapshot)
**Grain:** one row per `security_id`. **Source:** FMP `profile`. **Cadence:** on
enrichment. History is retained in `landing.fmp_raw`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT PK → core.security | |
| `description` | TEXT | |
| `ceo` | TEXT | |
| `full_time_employees` | BIGINT | |
| `website` | TEXT | |
| `beta` | NUMERIC(12,6) | |
| `market_cap_usd` | NUMERIC(24,2) | USD. |
| `last_dividend` | NUMERIC(20,6) | Per share. |
| `price_range` | TEXT | 52-week range string from FMP. |
| `image_url` | TEXT | |
| `loaded_at` | TIMESTAMPTZ | |
| `source` | TEXT | |

### `core.daily_price` — raw daily OHLCV  ⟵ primary fact
**Grain:** `(security_id, trade_date)`. **Source:** FMP
`historical-price-eod/non-split-adjusted` — the *unadjusted* endpoint. `.../full` is
already split-adjusted and must not be used here; see
[adr/0004](adr/0004-unadjusted-price-feed.md).
**Units:** price NUMERIC(20,6) in the security's currency; volume shares (BIGINT).
**Adjustment status:** **RAW (unadjusted).** **Cadence:** daily; immutable once the
day closes. Range-partitioned by `trade_date` (yearly).

Because these are the prices as traded, the series contains split-sized jumps. That
is correct: a 4:1 split shows a ~75% drop with no gap in the data.

**NAV-priced securities** (`asset_type = 'fund'`) share this table and this grain. A
fund is struck once a day and does not trade, so its bar carries
`open = high = low = close = NAV` and `volume = 0`. That is the whole truth about the
day, not three missing fields: the price loader expands a NAV-only payload for a
fund and still quarantines one for an equity. Distributions back-adjust through
`core.adjustment_factor` exactly as cash dividends do, so `mart.v_daily_price_adjusted`
returns a total-return NAV series with no special case.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT → core.security | |
| `trade_date` | DATE | Exchange trading day. |
| `open` `high` `low` `close` | NUMERIC(20,6) | **Raw** prices. |
| `volume` | BIGINT | **Raw** volume (shares), in the share count of the day it traded. Taken from `unadjustedVolume` where the payload offers it, else `volume`. |
| `vwap` | NUMERIC(20,6) | Volume-weighted average price (nullable). |
| `source` | TEXT | Who supplied the bar (a `sharadar` bar is **vendor-derived raw**: recovered from SEP/SFP's `closeunadj` and the ACTIONS split product by `fafnir ingest sharadar-prices`, #75 and ADR 0004's amendment): `fmp`, `sharadar`, `tiingo`, or `operator` for a vendor bar an operator re-dated or re-scaled with `fafnir prices shift` / `rescale` (see `ops.operator_override`, migration 0026). Writes follow ADR 0015 precedence (`operator > sharadar > tiingo > fmp`): a vendor revises its own bars and leaves every other party's alone; a higher-ranked vendor replaces a lower one's bar only in an explicit correction, which keeps the replaced bar in `ops.vendor_correction` (0029). No vendor overwrites an `operator` bar: a vendor bar on its date is set aside. |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | Lineage. NULL on an `operator` bar; the vendor bar it replaced, with its run, is kept in the override's `detail.row`. |
| `loaded_at` | TIMESTAMPTZ | |

Constraints reject impossible bars: `high>=low,open,close`; `low<=open,close`;
`volume>=0`; all prices `>0`.

### `core.corporate_action` — splits & cash dividends
**Grain:** `(security_id, action_type, ex_date)`. **Source:** FMP `splits` /
`dividends` (per security) or `splits-calendar` / `dividends-calendar` (market-wide),
per `actions_mode`. **Cadence:** daily. Never holds an `ex_date` in the future — a
declared-but-not-yet-ex event would back-adjust prices for something that has not
happened (ADR 0007).

| Column | Type | Notes |
|---|---|---|
| `corporate_action_id` | BIGINT IDENTITY PK | |
| `security_id` | BIGINT → core.security | |
| `action_type` | TEXT CHECK | `split` or `dividend`. |
| `ex_date` | DATE | Ex-date (the adjustment boundary). |
| `record_date` `payment_date` `declaration_date` | DATE | Dividend dates (nullable). |
| `split_numerator` / `split_denominator` | NUMERIC(20,6) | 2-for-1 ⇒ 2 / 1. |
| `dividend_amount` | NUMERIC(20,6) | Cash per share. |
| `currency` | TEXT | |
| `source` | TEXT | Who supplied the row: `fmp`, `sharadar`, `tiingo`, or `operator` for a row written by `fafnir actions add` / `redate` (see `ops.operator_override`). Writes follow ADR 0015 precedence, as for `core.daily_price.source`: a replaced vendor row is kept in `ops.vendor_correction`. The loaders never overwrite an `operator` row, and the FMP reconciliation compares only `fmp` rows against its feed: an `operator` or `sharadar` row is never reported as drift. `sharadar` rows come from `fafnir ingest sharadar-actions` (#73): every split and dividend of a security Sharadar minted, and, fill-only, what an FMP security linked to Sharadar lacks. A Sharadar dividend is stored as declared (its ACTIONS value times the later splits' product). Spin-offs are not stored here, from either vendor: they are events. |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | The run that last **changed** this row, not the last that read it — an unchanged re-load leaves both lineage columns alone, which is what makes `fafnir adjust --changed` a small set. NULL on an `operator` row. |
| `loaded_at` | TIMESTAMPTZ | Last change, as above. |

### `core.adjustment_factor` — derived back-adjustment factors
**Grain:** `(security_id, effective_date)`. **Source:** derived from
`core.corporate_action`. **Cadence:** recomputed by `fafnir adjust` after each
actions load. Deterministic and point-in-time stable.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT → core.security | |
| `effective_date` | DATE | An ex-date boundary. |
| `cumulative_price_factor` | NUMERIC | Multiply RAW price by this for `trade_date < effective_date`. |
| `cumulative_volume_factor` | NUMERIC | Multiply RAW volume by this for `trade_date < effective_date`. Moves inversely to the price factor. |
| `computed_at` | TIMESTAMPTZ | |

---

### Vendor identity (migration 0031, ADR 0015)

A vendor's view of a security lives beside `core.security`, never in it. These
tables arrive empty. The Sharadar master and events load from TICKERS and ACTIONS
(#58). Links come from the linking engine (#62), manual review (#63) and minting
(#71). `fafnir_read` and `fafnir_ops` read them; no mart view exposes them yet (#79).

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
skipped.

**Cadence:** each full TICKERS file the bulk download records. A nightly delta is
not built yet.

#### `core.security_vendor_xref` — fafnir's security ↔ a vendor's key, over time. **Grain:** `(vendor, vendor_key, valid_from)`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT FK → core.security | |
| `vendor` / `vendor_key` | TEXT | |
| `valid_from` / `valid_to` | DATE | Open = `valid_to IS NULL`; `valid_to ≥ valid_from`. |
| `is_feed` | BOOLEAN | This vendor loads the security's new rows (ADR 0015 §2). Only on an open link. |
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

**Cadence:** written by #62, #63 and #71.

#### `core.security_vendor_rejection` — "this security is not that key" (0032, #63). **Grain:** `(security_id, vendor, vendor_key)`.

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT FK → core.security, ON DELETE CASCADE | A rejection is about one security, and goes with it. |
| `vendor` / `vendor_key` | TEXT | |
| `rejected_by` / `rejected_at` / `note` | | Who, when and why; `rejected_by` and `note` may not be blank. |
| `evidence` | JSONB | The link the rejection closed, if any. |

**Population:**
- **Written by** `fafnir vendor link reject`. Rejecting again restates the note
  and owner.
- **Removed by** `accept` of the same pair.
- **The linking engine** never proposes a rejected pair.

**Why not a row of `core.security_vendor_xref`:** a link means "this key was this
security over this span". A rejection is not a span of identity, and kept there it
would answer "who was this key on date D" wrongly.

#### `core.vendor_security_event` — a vendor's lifecycle events. **Grain:** `(vendor, vendor_key, event_date, action, contra_ticker)`.

`vendor`, `vendor_key`, `event_date`, `action` (ACTIONS other than splits and
dividends: `listed`, `delisted`, `tickerchangefrom`/`to`, `acquisitionby`,
`mergerfrom`, `relation`, …), `ticker`, `value` (as given: a price, a deal value in
millions, or NULL), `contra_ticker`, `contra_name`, `contra_vendor_key` (when it
resolves), `ingestion_run_id`, `loaded_at`. The grain is a unique index with
`COALESCE(contra_ticker, '')`, since most events have no contra party. ACTIONS rows
carry only a ticker, so #58 resolves each to its permaticker through TICKERS
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

Statements from Sharadar SF1, one table per statement. They arrive empty; the loader
is #90 and the backfill #92. **Adjustment status: as reported, in the share terms of
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
| `security_id` | BIGINT FK | The security linked to the SF1 row's permaticker (primary share class only). The foreign key refuses deleting a security that holds statements. |
| `vendor_key` | TEXT | The permaticker. |
| `period_type` / `fiscal_period` | TEXT CHECK | `annual` with `FY`; `quarter` with `Q1`–`Q4`. From SF1's dimension and `fiscalperiod`. |
| `fiscal_date` | DATE | SF1 `reportperiod`: the period's end (observation). |
| `calendar_date` | DATE | SF1 `calendardate`: the vendor's normalized period, for comparing companies. |
| `fiscal_year` | INTEGER | From `fiscalperiod` (`2025-Q4` → 2025), never derived from a date. |
| `period_start_date` | DATE | NULL when not derivable; before `fiscal_date`. |
| `version_no` | INTEGER | 1 = as first reported; each restatement adds one, and only an `observed` version can be later than 1. |
| `known_from` / `known_to` | DATE | The knowledge interval, half-open. `known_from >= fiscal_date`: the look-ahead tripwire. |
| `knowledge_source` | TEXT CHECK | `filed` (the SEC filing, `known_from = filing_date`), `observed` (the day fafnir first loaded a restatement) or `estimated` (a period with no first-reported row: the issuer's next filing). |
| `filing_date` | DATE | SF1 `date` of the first-reported row; NULL on any other version. |
| `loaded_at` / `superseded_at` | TIMESTAMPTZ | The load timeline. `superseded_at` is set when a vendor correction replaced the row, which keeps its knowledge interval. |
| `reported_currency` | TEXT | TICKERS' `currency`, per security. |
| `content_hash` | TEXT CHECK | SHA-256 (64 hex) over the typed values; share counts at 1,000-share precision, so a split's re-expression is no change. |
| `extra` | JSONB | SF1 fields without a column (`vendor_metric`: the 36 metrics, the date-measured columns, per-share values, `*usd` conversions), once per SF1 row, on `core.income_statement`. **Never read by mart.** Measured at about 965 bytes a row, about 1.35 GB at full scale. |
| `ingestion_run_id` | BIGINT FK | The load. |

**Line items:** 19 on the income statement, 26 on the balance sheet, 13 on the cash
flow. Money is `NUMERIC(24,2)`; share counts (`weighted_average_shares`,
`weighted_average_shares_diluted`, `shares_outstanding`) are `NUMERIC(24,4)`, in the
version's share terms. Each column's SF1 indicator, class and sign are in
`ref.statement_line_item`. Two conventions are SF1's own:
- **Absent and inapplicable.** An item the filing does not carry is **0** for 41
  indicators. An item that does not apply is NULL: for example, the current and
  non-current splits of a balance sheet that is not classified.
- **No minority interest in the balance sheet.** `total_equity_parent` excludes
  noncontrolling interest, and there is no balance item for it.

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
`ref.sector` + `ref.industry` + `core.company_profile`. Read live, unlike
`security_latest`.

Adds over `security_latest`: `exchange_name`, `cik`/`isin`/`cusip`, `ipo_date`,
`delisted_date`, `source`, `first_seen_at`, `updated_at`, `description`. It is also
the relation `duk` resolves `cik:`/`isin:`/`cusip:` arguments against, which is why
the identifier columns belong on this view and not only on `core.security`. As with
`security_latest`, `market_cap_usd` and `beta` are a company-screener snapshot
refreshed with the security master — **not** history; do not use them in a backtest.

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

### `mart.v_security_dq_open` — open DQ flags per security (VIEW)
**Grain:** one row per open `dq_flag_id`. **Source:** `ops.data_quality_flag`.

Columns: `dq_flag_id, security_id, check_name, severity, table_name, record_key,
detected_at`.

The only relation exposing `ops` outside `fafnir_ingest`, and a deliberately narrow
one ([ADR 0009](adr/0009-mart-is-the-read-seam.md)). Open flags only; `record_key`
(dates and tickers) is included so a reader can name the offending bar; `detail` is
excluded because `adjustment_failed` writes a raw Python exception string into it.
`resolved_by`/`resolution_note` are kept off by the `resolved_at IS NULL` filter —
**that safety is in the `WHERE` clause**, so widening this view to resolved flags
means excluding those columns explicitly. Full triage stays with `fafnir dq list`.

### `mart.security_latest` — screening snapshot (MATERIALIZED VIEW)
**Grain:** one row per `security_id`. **Source:** `core.security` + `company_profile`
+ latest `daily_price`. **Cadence:** refreshed on schedule (`fafnir db refresh-marts`).

Columns: `security_id, symbol, company_name, asset_type, exchange_code,
sector_name, industry_name, currency, country, is_actively_trading, is_etf,
is_fund, market_cap_usd, beta, last_trade_date, last_close, last_volume`.

---

## Schema: `ref`

### `ref.exchange` — **Grain:** `exchange_code`.
`exchange_code` (PK), `exchange_name`, `country`, `timezone` (IANA), `is_active`,
`created_at`, `updated_at`. `MUTF` is a pseudo-venue for open-end mutual funds,
which are struck at NAV and have no trading venue; it is deliberately not one of
`SCREENER_EXCHANGES`, which is what keeps the delisting sweep away from funds.

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
| `source` | TEXT | Feed the symbol is declared against (default `fmp`). |
| `symbol` | TEXT | The ticker. Kept in step with `core.security.primary_symbol` — `ingest tracked` follows a rename rather than minting a second security. |
| `asset_type` | TEXT CHECK | `equity`/`etf`/`fund`/`other`. **Authoritative over the vendor profile**: the price loader reads it to decide whether a NAV-shaped bar is valid. |
| `exchange_code` | TEXT → ref.exchange | Venue to record. `MUTF` for open-end funds. |
| `note` | TEXT | Why this symbol is tracked — the one thing a hand-inserted `core.security` row could never carry. |
| `is_tracked` | BOOLEAN | FALSE stops the nightly pulls. It does **not** delist; retiring a security is `mark_delisted`'s job and history is retained either way. |
| `added_at` / `untracked_at` | TIMESTAMPTZ | Preserved across a re-declaration, so the row still answers "since when". |

### `ref.sector` / `ref.industry` — FMP taxonomy.
`sector_id`/`industry_id` (IDENTITY PK), `*_name` (UNIQUE), `created_at`.

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
Generated US calendar (weekdays minus NYSE holidays). `is_open` BOOLEAN,
`session_note` (e.g. half-day). Used for gap detection and EOD DATE semantics.

---

## Schema: `ops`

### `ops.ingestion_run` — lineage. **Grain:** `ingestion_run_id`.
`source`, `endpoint`, `params` (JSONB), `window_from/to`, `symbols_requested`,
`rows_inserted/updated/quarantined`, `bytes_downloaded` (vs 50 GB/mo budget),
`response_hash`, `status` (`started`/`success`/`partial`/`failed`), `error_message`,
`started_at`, `finished_at`. A long load can write its counts so far to its open run
(`RunLog.checkpoint`); the Sharadar scans do every minute, with `params.progress`.
A `started` row with no `finished_at` and no live process was killed outright: its
counts are as of the last checkpoint.

### `ops.data_quality_flag` — quarantine/anomaly queue. **Grain:** `dq_flag_id`.
`ingestion_run_id`, `security_id`, `table_name`, `record_key` (JSONB),
`check_name` (`gap`/`outlier`/`stale`/`price_*`/`split_invalid`/`dorq_*`/...), `severity`
(`info`/`warn`/`error`), `detail` (JSONB), `detected_at`, `resolved_at`,
`resolved_by`, `resolution_note`, `accepted_at`/`accepted_by`/`accepted_note` (0024),
`judgement_basis` (0028).

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

### `ops.load_watermark` — incremental marks. **Grain:** `(source, endpoint, security_id)`.
`last_loaded_date`, `last_run_at`, `updated_at`. `security_id = 0` denotes a
whole-endpoint (non per-symbol) mark.

| Endpoint | `security_id` | `last_loaded_date` means |
|---|---|---|
| `historical-price-eod/non-split-adjusted` | the security | bars are loaded through this date |
| `corporate-actions` | the security | its full action history has been pulled, as of this date |
| `corporate-actions-calendar` | `0` | the market-wide calendars have been read through this date |
| `dq-run`, `dq-shadow` (source `dorq`) | `0` | dorq has judged bars through this as-of date, into the queue or the shadow file; the next nightly run reports from five sessions before it, and a re-run for the same date over the same span as that night's run |

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
operation)`: a `delete` and an `add` may share a key, which is how a wrong vendor row
is replaced by a corrected one.

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

At most one active correction per `(security_id, target, action_type, key_date)`.

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
`ingestion_run_id`, `endpoint`, `params` (JSONB), `symbol`, `http_status`,
`payload` (JSONB), `payload_hash`, `bytes`, `fetched_at`. Ground truth for any
field whose meaning is later questioned.

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
| `vendor_table` | TEXT CHECK | Sharadar's modern name: `stocks` (not `SEP`), `funds`, `fundamentals`, `tickers`, `actions`, … |
| `kind` | TEXT CHECK | `full` (a bulk file, `years=5\|10\|full`) or `delta` (a slice: a date, a `lastupdated` window). |
| `filters` | JSONB | What was asked for, as sent. |
| `path` | TEXT CHECK | Relative to `bulk_dir`: `<sha256><suffix>`. Never absolute, never `..`. |
| `member` | TEXT | The CSV inside a zip; NULL for a plain or gzipped CSV. |
| `sha256` | TEXT UNIQUE | The file's content hash: a file already held is not recorded twice. |
| `bytes` / `rows` | BIGINT | Size on disk; data rows, NULL until first read. |
| `columns` | TEXT[] | The CSV header as found, for drift checks against the next file. |
| `fetched_at` / `ingestion_run_id` | | When, and the run that fetched it. |
| `status` / `status_at` | TEXT CHECK | The file's lifecycle: `downloaded`, `superseded` (a newer full of the table), `deleted` (gone from disk). Not a loader's progress: several loaders read one file, and each keeps its own record. |

---

## Schema: `meta`

### `meta.schema_migration` — migration bookkeeping. **Grain:** `version`.
`version` (PK), `name`, `checksum` (SHA-256 of the up-migration), `applied_at`.

---

## Source → table map (initial release)

| FMP endpoint (stable/) | Target | Grain |
|---|---|---|
| `stock-list`, `etf-list` | `core.security`, `core.symbol_xref` | security_id |
| `profile` | `core.security` (enrich), `core.company_profile` | security_id |
| `historical-price-eod/non-split-adjusted` | `core.daily_price` | (security_id, trade_date) |
| `splits` | `core.corporate_action` (split) | (security_id, split, ex_date) |
| `dividends` | `core.corporate_action` (dividend) | (security_id, dividend, ex_date) |
| `splits-calendar` | `core.corporate_action` (split) | (security_id, split, ex_date) |
| `dividends-calendar` | `core.corporate_action` (dividend) | (security_id, dividend, ex_date) |
| `available-sectors` / `available-industries` | `ref.sector` / `ref.industry` | name |
| (derived) | `core.adjustment_factor`, `mart.*` | — |

Fast-follow domains (fundamentals, economic series) are mapped in
[extending.md](extending.md).
