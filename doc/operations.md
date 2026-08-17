# Operations Runbook

Day-to-day upkeep of a fafnir deployment (bare-metal Postgres on Linux).

## One-time setup

Provisioning a cloud host from nothing? Use
**[install_hetzner.md](install_hetzner.md)** — it walks the whole path (server, OS
hardening, PostgreSQL 16 install and tuning, service user, schedule, backups). The
condensed version:

```bash
# 1. PostgreSQL 16 running locally; create the roles, database and schema.
#    setup_db.sh uses FAFNIR_ADMIN_DSN (a superuser conn) to create the three roles
#    and a database owned by FAFNIR_DB_OWNER (default fafnir_ingest), then migrates
#    and seeds as that ordinary role -- no superuser beyond the admin connection.
export FAFNIR_ADMIN_DSN="host=localhost dbname=postgres user=postgres"
export FAFNIR_DSN="host=localhost dbname=fafnir user=fafnir_ingest"
export PGPASSWORD=...                # ingest role password; FAFNIR_DB_PASSWORD is
                                     # ignored when FAFNIR_DSN is set (see backfill.md §3)
export FMP_API_KEY=...
export FAFNIR_SQL_DIR=/opt/fafnir/sql

scripts/setup_db.sh

# 2. Assign role passwords (out of band) and harden grants as needed.
#    psql -c "ALTER ROLE fafnir_ingest PASSWORD '...';" etc.

# 3. Initial historical backfill (resumable).
scripts/initial_backfill.sh 2010-01-01
```

## Daily upkeep

A single cron entry runs `scripts/daily_update.sh` after US settlement. It is
incremental and idempotent — a missed day is caught up on the next run. Install:

```bash
crontab etc/crontab.example   # edit paths/times first
```

The daily job performs, in order:
`ensure-horizon → ingest prices → ingest actions → adjust → refresh-marts → dq run`.

## Monitoring

```bash
fafnir status                 # securities, price rows, latest date, open DQ flags
scripts/run_dq_checks.sh      # gaps/outliers/freshness + open-flag summary
```

Things to watch:
- **Freshness** — `fafnir status` latest date should track the last trading day.
  Stale securities raise `stale` DQ flags.
- **Quarantine spikes** — a jump in `ops.data_quality_flag` (warn/error) on a load
  signals a source change or bad data. Investigate the `landing.fmp_raw` payload.
  A load where *every* bar quarantines as `price_missing_or_nonnumeric_ohlc` points
  at FMP renaming the OHLC fields; the loader accepts `open…close` and
  `adjOpen…adjClose`, so a third spelling would need adding to `_OHLC_ALIASES`.
- **Price levels, not just counts** — a wrongly-adjusted feed is internally
  consistent and passes every structural check. The deep-history spot check in
  [backfill.md §7](backfill.md#7-verify) is the one that catches it; it is worth
  re-running after any change to the price loader.
- **Bandwidth** — sum `ops.ingestion_run.bytes_downloaded` over the month against
  the 50 GB FMP budget:
  ```sql
  SELECT date_trunc('month', started_at) AS m, sum(bytes_downloaded)
  FROM ops.ingestion_run GROUP BY 1 ORDER BY 1;
  ```
- **Failed runs** —
  ```sql
  SELECT * FROM ops.ingestion_run WHERE status IN ('failed','partial')
  ORDER BY started_at DESC LIMIT 20;
  ```

## Reconciliation

`scripts/reconcile.sh AAPL,MSFT,SPY` re-pulls a sample live and diffs against the
warehouse to catch silent drift (re-adjustments, late corrections). Run weekly over
a rotating sample. It reports differences; it does not auto-overwrite.

It compares **raw** closes, and both sides are unadjusted — `core.daily_price` on one
side, FMP's `historical-price-eod/non-split-adjusted` on the other — so a split in
the compared window is not by itself a reason for the two to disagree.

## Maintenance tasks

- **Partitions & calendar horizon** — kept rolling automatically by
  `fafnir db ensure-horizon` in the daily job (extends to
  `max(current_year + horizon_extra_years, calendar_end_year)`), so no yearly config
  edits are needed. To force a specific target: `fafnir db ensure-horizon --through-year 2035`;
  to create an explicit fixed range instead: `fafnir db ensure-partitions --start-year 2028 --end-year 2030`.
- **Mart refresh** — `fafnir db refresh-marts` (also part of the daily job).
- **Re-run adjustments** — `fafnir adjust` (whole universe) or `fafnir adjust --symbol AAPL`.
  Run it after any price reload as well as after an actions load: dividend factors
  are valued against the prior raw close, so new prices can change them.

## Recovery

- **Interrupted backfill** — just re-run `scripts/initial_backfill.sh`; watermarks
  resume per symbol.
- **Suspected bad load** — the raw payload is in `landing.fmp_raw` (by endpoint +
  symbol + `fetched_at`); re-pull the window to upsert corrected values.
- **Suspected bad *feed*** — `fafnir source probe-prices` checks that FMP's
  unadjusted endpoint really is unadjusted (3 requests, writes nothing). See
  [backfill.md](backfill.md#confirming-the-price-feed).
- **Reload that must replace, not top up** — `scripts/reset_data.sh --scope <scope>`
  clears prices / actions / landing / everything, keeping migrations, partitions and
  reference data. Dry run by default; `--yes` to execute. Scope table in
  [backfill.md](backfill.md#clearing-data-for-a-reload).
- **Schema rollback** — `fafnir db rollback --steps N` (uses the `.down.sql`).
  Never roll back in a way that drops `core.daily_price` history without a backup.

## Backups

The whole warehouse is rebuildable from `landing` + the sources, but back up at
least `core` and `landing`:

```bash
pg_dump -Fc -n core -n landing -n ref fafnir > fafnir_$(date +%F).dump
```
