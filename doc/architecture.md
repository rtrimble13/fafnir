# Fafnir Architecture

## Purpose

Fafnir is a durable, research-grade financial market data warehouse. It is the
single source of truth that multiple independent apps and agentic/MCP clients
read from. The schema is the long-lived asset; queries and apps come and go.

Three pillars govern every design choice: **correctness**, **integrity**, and
**performance at scale**. In market data a wrong number is worse than a missing
one, so correctness is non-negotiable.

## Layered (medallion) architecture

Provenance flows one direction; every downstream layer is rebuildable from the
one upstream.

```
 sources (FMP, later FRED/BLS/BEA)
     │   throttled, retrying, bandwidth-metered loaders
     ▼
 landing   raw immutable payloads (landing.fmp_raw)  ── ground truth
     │   validate + type at the boundary; quarantine failures
     ▼
 core      modeled, constrained source of truth
     │      security, symbol_xref, company_profile,
     │      daily_price (raw OHLCV), corporate_action, adjustment_factor
     ▼
 mart      derived read views/matviews
            v_daily_price_adjusted (adjusted-on-read), security_latest
```

Cross-cutting schemas:

- **ref** — exchanges, sectors, industries, trading calendar (gap detection).
- **ops** — `ingestion_run` (lineage), `data_quality_flag` (quarantine/anomalies),
  `load_watermark` (incremental high-water marks).
- **meta** — `schema_migration` bookkeeping.

## The three timelines

Market data lives on three separate clocks; fafnir keeps them distinct:

- **observation date** — what period the data describes (`trade_date`, `ex_date`,
  later `fiscal_date`).
- **knowledge date** — when it became known (FMP feed timestamp; `filing_date`
  for statements in the fundamentals fast-follow).
- **load date** — when fafnir ingested it (`loaded_at`, `ops.ingestion_run`).

## Raw prices + derive-on-read adjustment (the key decision)

Adjusted price series are **not stable**: every new split or dividend re-scales the
entire history, so a stored adjusted close silently drifts and stops being
point-in-time reproducible.

Fafnir therefore stores **raw OHLCV only** (`core.daily_price`, immutable once the
day closes) plus a **corporate-actions** table and a derived **adjustment-factor**
table.

Getting genuinely raw prices takes care, because most of FMP's price payloads are
adjusted already. On `historical-price-eod/full`, `close` is adjusted **for splits**
and `adjClose` for splits *and* dividends — neither is raw. Prices are therefore
pulled from **`historical-price-eod/non-split-adjusted`**, the one endpoint that
returns prices as they actually traded. Feeding a pre-adjusted series into fafnir's
own factors would adjust it twice: AAPL's 1990-01-02 close would enter as ~$0.35
rather than its true ~$39.20, and the view would report ~$0.003. See
[adr/0004-unadjusted-price-feed.md](adr/0004-unadjusted-price-feed.md). Adjusted series are computed **on read** in `mart.v_daily_price_adjusted`.
Because factors are derived deterministically from corporate actions, the adjusted
series is **point-in-time stable and reproducible**. See
[adr/0001-raw-prices-plus-adjustment-factors.md](adr/0001-raw-prices-plus-adjustment-factors.md).

## Identity & survivorship

- A surrogate `security_id` is the identity of every security. **Tickers are never
  keys** — they are reused and renamed (FB→META). `core.symbol_xref` maps tickers
  to `security_id` over time.
- Delisted/inactive securities are **retained** (`delisted_date` set, never
  deleted), so backtests are free of survivorship bias.

## Partitioning & TimescaleDB-readiness

`core.daily_price` is a single table **range-partitioned by `trade_date`** (yearly),
with `trade_date` in the primary key. This serves contiguous-series reads, makes
old partitions cheap to archive, and — crucially — keeps the grain identical to a
TimescaleDB hypertable, so the extension can be adopted later without a grain
change. See [adr/0003-postgres-now-timescale-later.md](adr/0003-postgres-now-timescale-later.md).

## Role model (least privilege)

| Role | Grants | Used by |
|---|---|---|
| `fafnir_ingest` | write landing/core/ref/ops | the loaders |
| `fafnir_read` | read core/mart/ref | research, notebooks |
| `fafnir_app` | read **mart** (+ ref) only | external apps, MCP, `duk -S db` |

Reporting/research connects read-only. No application role owns `DROP`/`ALTER` in
production. Every query is parameterized.

## Entity overview

```
 ref.exchange ─┐         ┌─ ref.sector
               │         │
        core.security ───┼─ ref.industry
          │   │   │      │
          │   │   └── core.company_profile
          │   │
          │   └── core.symbol_xref            (ticker history)
          │
          ├── core.daily_price                (raw OHLCV, partitioned)
          │       └► mart.v_daily_price_adjusted (× adjustment_factor)
          │
          └── core.corporate_action ──► core.adjustment_factor

 ops.ingestion_run ◄── every loader      landing.fmp_raw ◄── raw payloads
 ops.data_quality_flag ◄── quarantine/DQ ops.load_watermark ◄── incremental marks
```

See the [data dictionary](data_dictionary.md) for every table and column.
