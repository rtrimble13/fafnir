# ADR 0001: Raw prices + adjustment factors (derive adjusted on read)

- Status: Accepted
- Date: 2026-06-27

## Context

FMP exposes several adjustment variants of its EOD price history. Adjusted values
are correct for return calculations but are **not stable**: every new split or
dividend re-scales the entire historical adjusted series. A stored adjusted close
therefore silently drifts and is not point-in-time reproducible — fatal for
backtesting.

> **Correction (2026-08-17).** This ADR originally described FMP as returning "both
> raw `close` and an adjusted close". That is wrong, and the error propagated into
> the loader: on `historical-price-eod/full`, `close` is already **split-adjusted**
> and `adjClose` is split- *and* dividend-adjusted. Neither is raw. Which endpoint
> actually delivers raw prices is settled in
> [ADR 0004](0004-unadjusted-price-feed.md); the decision below is unchanged.

## Decision

Store **raw OHLCV only** in `core.daily_price` (immutable once the day closes),
plus a `core.corporate_action` table and a derived `core.adjustment_factor` table.
Expose adjusted OHLCV through `mart.v_daily_price_adjusted`, computed **on read**
by multiplying raw values by the cumulative factor for all corporate actions after
each trade date.

## Consequences

- **Point-in-time stable & reproducible.** Adjusted series are a deterministic
  function of the corporate actions known as of a date, not a frozen snapshot.
- Raw history is never overwritten by re-adjustment; corrections arrive as new
  versions of corporate actions and a factor recompute (`fafnir adjust`).
- A small read-time cost (one LATERAL lookup per row); acceptable, and removable
  later with a materialized adjusted table if a hot path needs it.
- The `close_raw`/`price_factor` columns on the view keep the raw vs adjusted
  distinction impossible to miss.

## Alternatives considered

- **Store FMP's adjusted close as a refreshed snapshot.** Simpler but not
  point-in-time stable; rejected for research use. Documented as an acceptable
  fallback only if clearly labeled as a moving snapshot.
