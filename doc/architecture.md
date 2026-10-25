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
     │      security, symbol_xref, symbol_change, company_profile,
     │      daily_price (raw OHLCV), corporate_action, adjustment_factor
     ▼
 mart      derived read views/matviews
            v_daily_price_adjusted (adjusted-on-read), security_latest
```

Cross-cutting schemas:

- **ref** — exchanges, sectors, industries, trading calendar (gap detection),
  and `tracked_symbol`, the declared universe.
- **ops** — `ingestion_run` (lineage), `data_quality_flag` (quarantine/anomalies),
  `load_watermark` (incremental high-water marks).
- **meta** — `schema_migration` bookkeeping.

## The three timelines

Market data lives on three separate clocks; fafnir keeps them distinct:

- **observation date** — what period the data describes (`trade_date`, `ex_date`,
  `fiscal_date` for statements).
- **knowledge date** — when it became known. A statement is known from its SEC
  filing date (`filing_date`, real for SF1's first-reported rows), and each version
  of it holds over a knowledge interval, `known_from` to `known_to`: a restatement
  starts a new version on the day fafnir first loads it, and closes the one before
  ([ADR 0018](adr/0018-bitemporal-fundamentals.md)).
- **load date** — when fafnir ingested it (`loaded_at`, `ops.ingestion_run`). A
  statement row also has `superseded_at`, set when a vendor correction replaced it,
  so a past state of the warehouse can be read back.

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
[adr/0004-unadjusted-price-feed.md](adr/0004-unadjusted-price-feed.md). The one
exception is Sharadar's backfilled history, which no source observed raw: it is
recovered from the vendor's adjusted prices and its ACTIONS splits, and guarded by a
DQ check (ADR 0004's amendment). Adjusted series are computed **on read** in `mart.v_daily_price_adjusted`.
Because factors are derived deterministically from corporate actions, the adjusted
series is **point-in-time stable and reproducible**. See
[adr/0001-raw-prices-plus-adjustment-factors.md](adr/0001-raw-prices-plus-adjustment-factors.md).

## Identity & survivorship

- A surrogate `security_id` is the identity of every security. **Tickers are never
  keys** — they are reused and renamed (FB→META). `core.symbol_xref` maps tickers
  to `security_id` over time.
- Delisted/inactive securities are **retained** (`delisted_date` set, never
  deleted), so backtests are free of survivorship bias **from 1998-01-01**. Before
  that date no vendor records delistings, so a cross-section is survivor-only. From
  1998 the issuers that died before fafnir first ran are minted from Sharadar
  (`source = 'sharadar'`).
- **Sharadar's permaticker is the identity authority.** A security's open link in
  `core.security_vendor_xref` names its permaticker, and from cutover Sharadar's
  TICKERS and ACTIONS drive listings, renames and delistings; `security_id` stays
  the key every table uses. A security with no link (mutual funds, and the few
  `vendor link` could not place) keeps its FMP identity. See
  [adr/0016-sharadar-identity-and-survivorship-floor.md](adr/0016-sharadar-identity-and-survivorship-floor.md).
- The universe has two halves. The **discovered** one maintains itself: the nightly
  job re-reads the screener, so a security that lists today enters scope today (and,
  having no watermark, gets its full history on the same run). The **declared** one
  (`ref.tracked_symbol`) covers what no screener returns — an open-end mutual fund
  has no listing venue, so it is not screenable and must be declared. Both write
  through the same upsert on the same key, so a symbol in both converges on one
  `security_id`. See
  [adr/0006-curated-fund-universe.md](adr/0006-curated-fund-universe.md). Before that it applies ticker renames to the
  security that already holds the history, because to the screener a rename is
  indistinguishable from a new listing — and minting it as one would fork a
  company's identity into two `security_id`s. See
  [adr/0005-automatic-universe-maintenance.md](adr/0005-automatic-universe-maintenance.md).
- **More than one vendor writes to the same security.** Every fact row carries the
  party that supplied it in `source` (`fmp`, `sharadar`, `tiingo`, `fred`,
  `operator`); on `core.security` that is the vendor that *minted* it, while which
  vendor *feeds* its new rows is an explicit assignment. When two parties hold the
  same key the higher one wins, `operator > sharadar > tiingo > fmp`: a vendor never
  writes over a higher-ranked row, and replaces a lower-ranked one only through an
  explicit correction recorded in `ops.vendor_correction`, so it can be reviewed and
  reverted. See
  [adr/0015-multi-vendor-provenance-and-precedence.md](adr/0015-multi-vendor-provenance-and-precedence.md).

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
 ref.tracked_symbol ─────┤   (declared universe: funds and other unscreenable names)
               │         │
        core.security ───┼─ ref.industry
          │   │   │      │
          │   │   └── core.company_profile
          │   │
          │   └── core.symbol_xref            (ticker history)
          │
          ├── core.security_vendor_xref ──► core.vendor_security
          │       (a vendor's key over time;      (the vendor's own master)
          │        is_feed = who loads new rows)  core.vendor_security_event
          │                                       (its lifecycle events)
          │
          ├── core.daily_price                (raw OHLCV, partitioned)
          │       └► mart.v_daily_price_adjusted (× adjustment_factor)
          │
          └── core.corporate_action ──► core.adjustment_factor

 ops.ingestion_run ◄── every loader      landing.fmp_raw ◄── raw payloads
 ops.data_quality_flag ◄── quarantine/DQ ops.load_watermark ◄── incremental marks
 ops.vendor_correction ◄── replaced rows landing.sharadar_file ◄── Sharadar files on disk
```

A vendor's view of a security never overwrites `core.security` (ADR 0015): it lives
in `core.vendor_security`, keyed by the vendor's own id, and
`core.security_vendor_xref` says which fafnir security that id is, from when to when.

See the [data dictionary](data_dictionary.md) for every table and column.
