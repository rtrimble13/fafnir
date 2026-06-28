# ADR 0003: Vanilla PostgreSQL now, TimescaleDB-ready

- Status: Accepted
- Date: 2026-06-27

## Context

Daily OHLCV grows steadily and intraday/economic series will grow faster. A
time-series extension (TimescaleDB) offers automatic partitioning, compression,
and continuous aggregates. But adopting it on day one adds operational surface for
a personal R&D warehouse that starts with daily bars.

## Decision

Run **vanilla PostgreSQL 16**, but shape the schema so TimescaleDB hypertables can
be adopted later **without a grain change**:

- `core.daily_price` is a single table **range-partitioned by `trade_date`**
  (yearly), with `trade_date` in the primary key — the same shape a hypertable
  requires.
- No logic depends on the partition layout; partitions are created ahead of time by
  `fafnir db ensure-partitions`.

## Consequences

- Zero extra extensions to start; maximum portability and simple ops.
- Range scans and retention touch only relevant partitions; old years archive
  cheaply.
- Converting `core.daily_price` to a hypertable later (`create_hypertable`) is a
  migration that preserves the grain, keys, and constraints — no application change.

## Decision drivers

- Initial volume (daily bars, US universe) is comfortably within vanilla Postgres.
- Keeping the grain hypertable-compatible avoids the one migration that would be
  painful to do later (changing a fact table's grain is a data-loss risk).
