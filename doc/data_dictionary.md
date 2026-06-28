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
| `exchange_code` | TEXT → ref.exchange | Listing venue. |
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

Soft natural key: `UNIQUE (source, primary_symbol, COALESCE(exchange_code,''))`
makes the security upsert idempotent without keying on the ticker.

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
**Grain:** `(security_id, trade_date)`. **Source:** FMP `historical-price-eod/full`.
**Units:** price NUMERIC(20,6) in the security's currency; volume shares (BIGINT).
**Adjustment status:** **RAW (unadjusted).** **Cadence:** daily; immutable once the
day closes. Range-partitioned by `trade_date` (yearly).

| Column | Type | Notes |
|---|---|---|
| `security_id` | BIGINT → core.security | |
| `trade_date` | DATE | Exchange trading day. |
| `open` `high` `low` `close` | NUMERIC(20,6) | **Raw** prices. |
| `volume` | BIGINT | **Raw** volume (shares). |
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
| `cumulative_price_factor` | NUMERIC(20,10) | Multiply RAW price by this for `trade_date < effective_date`. |
| `cumulative_volume_factor` | NUMERIC(20,10) | Multiply RAW volume by this for `trade_date < effective_date`. |
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
| `open` `high` `low` `close` | NUMERIC(20,6) | Back-adjusted prices. |
| `volume` | BIGINT | Back-adjusted volume (post-split share terms). |
| `close_raw` | NUMERIC(20,6) | The unadjusted close, for reference. |
| `price_factor` / `volume_factor` | NUMERIC | The factor applied to this row. |

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
(`info`/`warn`/`error`), `detail` (JSONB), `detected_at`, `resolved_at`.

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
| `historical-price-eod/full` | `core.daily_price` | (security_id, trade_date) |
| `splits` | `core.corporate_action` (split) | (security_id, split, ex_date) |
| `dividends` | `core.corporate_action` (dividend) | (security_id, dividend, ex_date) |
| `available-sectors` / `available-industries` | `ref.sector` / `ref.industry` | name |
| (derived) | `core.adjustment_factor`, `mart.*` | — |

Fast-follow domains (fundamentals, economic series) are mapped in
[extending.md](extending.md).
