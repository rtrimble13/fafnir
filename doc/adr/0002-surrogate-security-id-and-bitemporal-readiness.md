# ADR 0002: Surrogate security_id, symbol history, and bitemporal readiness

- Status: Accepted
- Date: 2026-06-27

## Context

Tickers are not stable identifiers: they are reused and renamed (FB→META, and
recycled across delisted and new companies). Keying facts on the ticker string
corrupts history. Separately, fundamentals (a fast-follow) are restated over time
and must be modeled bitemporally for point-in-time correctness.

## Decision

- Mint a surrogate `security_id` (`BIGINT GENERATED ALWAYS AS IDENTITY`) as the
  identity of every security. All facts join on `security_id`, never on the ticker.
- Keep `core.symbol_xref` mapping tickers to `security_id` over time
  (`valid_from`/`valid_to`), so a renamed/relisted ticker resolves correctly at any
  point in time.
- Retain delisted/inactive securities (`delisted_date` set, never deleted) to keep
  the universe free of survivorship bias.
- Design the temporal columns now (`*_date` observation, `loaded_at` process time)
  so the fundamentals tables can add `filing_date`/`valid_from` bitemporal
  versioning without reshaping the core.

## Consequences

- Joins are stable across renames; backtests see the correct entity.
- The security upsert uses a soft natural key `(source, primary_symbol, exchange)`
  for idempotency without making the ticker a global key.
- Adding bitemporal statement tables later is additive, not a grain change.
