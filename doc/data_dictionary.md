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
| `cik` / `isin` / `cusip` | TEXT | External identifiers (nullable). |
| `is_actively_trading` | BOOLEAN | False for delisted/inactive. |
| `is_etf` / `is_fund` | BOOLEAN | |
| `ipo_date` | DATE | |
| `delisted_date` | DATE | NULL while listed; set (never deleted) on delist. |
| `source` | TEXT | Origin feed (default `fmp`). |
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
| `security_id` | BIGINT → core.security | The security the rename was applied to; NULL while unapplied. |
| `company_name` | TEXT | As reported with the rename. |
| `status` | TEXT CHECK | `applied` / `conflict` / `ignored` / `dismissed` — see below. |
| `detail` | JSONB | Context: the `folded_security_id` of an absorbed duplicate, the `merged_security_id` of one merged by hand, or `dismissed_by` / `dismissed_note` / `dismissed_at`. |
| `source` | TEXT | |
| `first_seen_at` / `updated_at` | TIMESTAMPTZ | |

- `applied` — carried onto an existing `security_id` (terminal; never downgraded).
- `conflict` — the new ticker already belongs to another **listed** security that
  carries history. Nothing was changed; retried on every sweep and raised as a
  `symbol_change_conflict` DQ flag.
- `ignored` — the old ticker belongs to a delisted issuer, so this is ticker
  *reuse*, not a rename.
- `dismissed` (0018) — an operator judged the reported rename not to be a rename
  at all: a bad feed row. Terminal, so the sweep stops retrying it. Set only by
  `fafnir security dismiss-rename`, never by a loader, and the reasoning is kept in
  `detail`. It is **not** the way to close a rename that is real but blocked —
  that one is merged with `fafnir security merge-rename` and reaches `applied`.

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

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT → core.security | |
| `trade_date` | DATE | Exchange trading day. |
| `open` `high` `low` `close` | NUMERIC(20,6) | **Raw** prices. |
| `volume` | BIGINT | **Raw** volume (shares), in the share count of the day it traded. Taken from `unadjustedVolume` where the payload offers it, else `volume`. |
| `vwap` | NUMERIC(20,6) | Volume-weighted average price (nullable). |
| `source` | TEXT | |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | Lineage. |
| `loaded_at` | TIMESTAMPTZ | |

Constraints reject impossible bars: `high>=low,open,close`; `low<=open,close`;
`volume>=0`; all prices `>0`.

### `core.corporate_action` — splits & cash dividends
**Grain:** `(security_id, action_type, ex_date)`. **Source:** FMP `splits` /
`dividends`. **Cadence:** daily.

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
| `source` | TEXT | |
| `ingestion_run_id` | BIGINT → ops.ingestion_run | |
| `loaded_at` | TIMESTAMPTZ | |

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
`created_at`, `updated_at`.

### `ref.sector` / `ref.industry` — FMP taxonomy.
`sector_id`/`industry_id` (IDENTITY PK), `*_name` (UNIQUE), `created_at`.

### `ref.trading_calendar` — **Grain:** `(exchange_code, trade_date)`.
Generated US calendar (weekdays minus NYSE holidays). `is_open` BOOLEAN,
`session_note` (e.g. half-day). Used for gap detection and EOD DATE semantics.

---

## Schema: `ops`

### `ops.ingestion_run` — lineage. **Grain:** `ingestion_run_id`.
`source`, `endpoint`, `params` (JSONB), `window_from/to`, `symbols_requested`,
`rows_inserted/updated/quarantined`, `bytes_downloaded` (vs 50 GB/mo budget),
`response_hash`, `status` (`started`/`success`/`partial`/`failed`), `error_message`,
`started_at`, `finished_at`.

### `ops.data_quality_flag` — quarantine/anomaly queue. **Grain:** `dq_flag_id`.
`ingestion_run_id`, `security_id`, `table_name`, `record_key` (JSONB),
`check_name` (`gap`/`outlier`/`stale`/`price_*`/`split_invalid`/...), `severity`
(`info`/`warn`/`error`), `detail` (JSONB), `detected_at`, `resolved_at`,
`resolved_by`, `resolution_note`.

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

Since migration 0016 the schema holds the rule too — `ux_dq_flag_open_condition`,
unique on `(check_name, security_id, record_key)` where `resolved_at IS NULL` and the
check is not `price_*`. It is a backstop, not the mechanism: the guard stays in the
write path, because a constraint alone would turn a redundant flag (harmless noise)
into an exception that aborts whatever load raised it. 0016 also collapsed the
duplicates a running warehouse had already accumulated, keeping the earliest row of
each condition so `detected_at` still says when the problem was first seen.

### `ops.load_watermark` — incremental marks. **Grain:** `(source, endpoint, security_id)`.
`last_loaded_date`, `last_run_at`, `updated_at`. `security_id = 0` denotes a
whole-endpoint (non per-symbol) mark.

---

## Schema: `landing`

### `landing.fmp_raw` — raw immutable payloads. **Grain:** `raw_id`.
`ingestion_run_id`, `endpoint`, `params` (JSONB), `symbol`, `http_status`,
`payload` (JSONB), `payload_hash`, `bytes`, `fetched_at`. Ground truth for any
field whose meaning is later questioned.

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
| `available-sectors` / `available-industries` | `ref.sector` / `ref.industry` | name |
| (derived) | `core.adjustment_factor`, `mart.*` | — |

Fast-follow domains (fundamentals, economic series) are mapped in
[extending.md](extending.md).
