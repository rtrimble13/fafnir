# Ingestion

How data flows from FMP into the warehouse, and the guarantees that make it
research-grade.

## Principles

- **Land raw, then transform.** Every response is persisted to `landing.fmp_raw`
  (payload + hash) before transformation, so the original is always recoverable.
- **Validate at the boundary.** Each row is typed and sanity-checked (cross-field,
  ranges, nulls). Failures are **quarantined** into `ops.data_quality_flag`, never
  silently dropped.
- **Idempotent.** Loads upsert on the natural key (`ON CONFLICT ... DO UPDATE`).
  Re-pulling a window converges to the same state.
- **Incremental & resumable.** Per-symbol watermarks (`ops.load_watermark`) bound
  each pull to the new tail, with a configurable overlap (`overlap_days`, default 5)
  to absorb late corrections. A load that dies mid-universe resumes from its
  watermarks, not from scratch.
- **Lineage.** Every load opens an `ops.ingestion_run` row (params, window, counts,
  bytes, status).

## Respecting FMP limits (Professional plan)

The source client (`fafnir/sources/fmp.py`) throttles **proactively** to
`request_rate_per_min` (default 280, under the ~300/min ceiling), backs off with
exponential delay + jitter on HTTP 429 and 5xx, retries transient failures, and
**meters bytes** so bandwidth can be tracked against the 50 GB/month budget
(`ops.ingestion_run.bytes_downloaded`). Prefer comma-separated batch pulls where
an endpoint supports them.

## Endpoint → table map

All endpoints are on the `stable/` base, matching what is already proven against
the Professional plan in the original `duk`.

| Loader | FMP endpoint | Target table(s) | Natural key |
|---|---|---|---|
| `ingest securities` | `stock-list`, `etf-list` | `core.security`, `core.symbol_xref` | (source, primary_symbol, exchange) |
| `ingest securities --enrich` | `profile` | `core.security`, `core.company_profile` | security_id |
| `ingest prices` | `historical-price-eod/non-split-adjusted` | `core.daily_price` | (security_id, trade_date) |
| `ingest actions` | `splits`, `dividends` | `core.corporate_action` | (security_id, action_type, ex_date) |
| `adjust` | (derived) | `core.adjustment_factor` | (security_id, effective_date) |

> **Verify before the first production backfill:** the `splits` / `dividends`
> stable field names (`numerator`/`denominator`, `dividend`/`adjDividend`) should
> be confirmed against a live response for a known symbol (e.g. AAPL). The loader
> already tolerates the common alternates; the endpoint paths are centralized as
> constants in `FMPClient` for a one-line correction.

## Why the *unadjusted* price endpoint

`core.daily_price` is defined as raw, and `core.adjustment_factor` is the only
adjustment fafnir applies. Most FMP price payloads are adjusted before they arrive,
so the endpoint choice is load-bearing:

| Endpoint / field | Adjusted for |
|---|---|
| `historical-price-eod/full` → `close` | splits |
| `historical-price-eod/full` → `adjClose` | splits **and** dividends |
| `historical-price-eod/dividend-adjusted` | splits and dividends |
| **`historical-price-eod/non-split-adjusted`** | **nothing — prices as traded** |

Loading a pre-adjusted series adjusts it twice. AAPL has split 112:1 cumulatively
since 1990, so its true 1990-01-02 close of ~$39.20 arrives from `.../full` as
~$0.35; storing that as raw and applying the 1/112 factor again yields ~$0.003. The
symptom only appears on symbols that have split, and only in deep history.

Two details follow from the endpoint choice:

- The unadjusted payload names its OHLC fields `adjOpen`/`adjHigh`/`adjLow`/
  `adjClose`. That prefix is FMP's naming convention on this family of endpoints,
  **not** a second adjustment. The loader accepts either spelling (preferring the
  unprefixed one) and lands the payload verbatim.
- Dividends are taken from the as-declared `dividend` field, not the restated
  `adjDividend`, so the dividend and the raw prior close it divides into are quoted
  in the same share terms.

Full rationale and the migration consequences: [adr/0004](adr/0004-unadjusted-price-feed.md).

## The adjustment step

`fafnir adjust` recomputes `core.adjustment_factor` from `core.corporate_action`:

- split num:den → price × (den/num), volume × (num/den) for prior dates;
- cash dividend D vs prior close P → price × ((P−D)/P) for prior dates.

Cumulative factors are built from latest ex-date backwards. The adjusted view picks,
for each `trade_date`, the factor at the smallest `effective_date` greater than that
date — i.e. the product of every action that happened after it. Prices on/after the
latest ex-date get factor 1.0. This is why adjusted prices are **point-in-time
stable**: they are a deterministic function of the actions known as of a date, not a
frozen snapshot. See [adr/0001](adr/0001-raw-prices-plus-adjustment-factors.md).

## Order of operations (daily)

```
ensure-partitions → prices → actions → adjust → refresh-marts → dq run
```

Prices precede actions so dividend adjustment can value against fresh closes.
`scripts/daily_update.sh` encodes this order.

## Watermarks and the endpoint string

`ops.load_watermark` is keyed on `(source, endpoint, security_id)`, so the endpoint
path is part of ingestion state, not just a URL. Changing it retires every existing
watermark and makes each symbol look new — which on an incremental run means an
unbounded request, and an unbounded request is capped at 5000 bars (~19.8 years).

`load_prices` therefore refuses to run incrementally when watermarks exist only
under the retired `historical-price-eod/full` endpoint, directing the operator to a
re-backfill instead. If you ever change a loader's endpoint again, plan the
watermark migration at the same time.
