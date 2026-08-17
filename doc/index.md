# Fafnir Documentation Index

Fafnir is a research-grade financial market data warehouse on PostgreSQL + Python,
read through the `duk` CLI.

## Start here

- [README](../README.md) — overview, quick start, project layout
- [Architecture](architecture.md) — layers, ERD, role model, key decisions
- [Data Dictionary](data_dictionary.md) — every table/column: grain, source, units,
  adjustment status, cadence

## Operating it

- [Install on a Hetzner Cloud Server](install_hetzner.md) — end-to-end fresh install:
  provision, harden, PostgreSQL 16, fafnir, schedule, backups
- [Initial Setup & Backfill](backfill.md) — stand up the DB and backfill history
  (single run, a few hours); also
  [confirming the price feed](backfill.md#confirming-the-price-feed) and
  [clearing data for a reload](backfill.md#clearing-data-for-a-reload)
- [Ingestion](ingestion.md) — FMP endpoint→table map, idempotency, watermarks, the
  adjustment step
- [Operations Runbook](operations.md) — setup, daily upkeep, monitoring,
  reconciliation, recovery, backups
- [duk CLI](duk.md) — reading the warehouse (db) vs the API (live)

## Growing it

- [Extending](extending.md) — add a source (FRED/BLS/BEA), fundamentals, MCP server
- Architecture Decision Records:
  - [ADR 0001 — Raw prices + adjustment factors](adr/0001-raw-prices-plus-adjustment-factors.md)
  - [ADR 0002 — Surrogate security_id & bitemporal readiness](adr/0002-surrogate-security-id-and-bitemporal-readiness.md)
  - [ADR 0003 — Postgres now, TimescaleDB later](adr/0003-postgres-now-timescale-later.md)
  - [ADR 0004 — Unadjusted price feed](adr/0004-unadjusted-price-feed.md)

## Reference

- Configuration template: [`etc/fafnirrc`](../etc/fafnirrc)
- Cron schedule: [`etc/crontab.example`](../etc/crontab.example)
- Migrations: [`sql/migrations/`](../sql/migrations/)
- Batch scripts: [`scripts/`](../scripts/)
