# Initial Setup & Historical Backfill

How to stand up fafnir and backfill **active US equities + ETFs** from a chosen
start date (e.g. 1990) to present. Target host: Linux + PostgreSQL 16 + Python 3.11+,
with an FMP **Professional** key.

> **Standing up a cloud server from scratch?** Follow
> [install_hetzner.md](install_hetzner.md) instead — it covers provisioning, OS
> hardening, cluster tuning, service users, and scheduling, then hands off to §6 here
> for the load itself.

> **Scope:** `ingest securities` loads the *active* universe (FMP `stock-list` +
> `etf-list`). Retaining *delisted* names (survivorship-bias-free) needs the
> delisted endpoint — a documented fast-follow. So this backfill covers active
> securities.

---

## 0. Prerequisites

```bash
sudo apt-get update && sudo apt-get install -y postgresql-16 python3.11 python3-pip git
pg_isready    # expect: accepting connections
```

Have your FMP key ready. **Never commit it** — it lives in the environment or a
root-only env file.

## 1. Install

```bash
git clone https://github.com/rtrimble13/fafnir.git /opt/fafnir
cd /opt/fafnir
pip install -e .          # installs the `fafnir` and `duk` console scripts + psycopg
fafnir --version && duk --version
```

## 2. Bootstrap Postgres roles + database (once, as the `postgres` superuser)

Pre-creating the roles lets migration `0001` skip its `CREATE ROLE`, and makes
`fafnir_ingest` the database owner — which it must be, so the nightly
`fafnir db ensure-horizon` can attach new partitions to `core.daily_price`:

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE fafnir_ingest LOGIN PASSWORD 'CHANGE_ME_ingest';
CREATE ROLE fafnir_read   LOGIN PASSWORD 'CHANGE_ME_read';
CREATE ROLE fafnir_app    LOGIN PASSWORD 'CHANGE_ME_app';
CREATE DATABASE fafnir OWNER fafnir_ingest;
SQL
```

## 3. Configure `~/.fafnirrc` and the environment

```bash
cp /opt/fafnir/etc/fafnirrc ~/.fafnirrc
```

The one setting that matters for a deep backfill is **`calendar_start_year`** — set
it to your backfill start so partitions and the trading calendar cover that far back:

```toml
[general]
universe = "us-equity-etf"
request_rate_per_min = 280       # under FMP Pro's ~300/min
overlap_days = 5

calendar_start_year = 1990       # <-- backfill start (earliest year to build)
calendar_end_year   = 2027       # minimum/initial horizon (auto-extended; see below)
horizon_extra_years = 2          # stay this many years ahead of "now"
```

> **You do not bump `calendar_end_year` every year.** The nightly job runs
> `fafnir db ensure-horizon`, which auto-extends partitions **and** the trading
> calendar to a rolling `max(current_year + horizon_extra_years, calendar_end_year)`.
> `calendar_end_year` is just a floor. (See [operations.md](operations.md).)

Secrets via the environment (put in a root-only `/etc/fafnir/fafnir.env`, `chmod 600`):

```bash
export FAFNIR_DSN="host=localhost dbname=fafnir user=fafnir_ingest"
export PGPASSWORD="CHANGE_ME_ingest"      # see the note below
export FMP_API_KEY="your_fmp_pro_key"
export FAFNIR_SQL_DIR="/opt/fafnir/sql"   # so the migrator finds sql/ outside a checkout
```

> **`FAFNIR_DB_PASSWORD` is ignored when `FAFNIR_DSN` is set** — the DSN is used
> verbatim, and the password is only merged in when the DSN is assembled from the
> `[database]` parts in `~/.fafnirrc`. With `FAFNIR_DSN` exported, supply the password
> via `PGPASSWORD` (libpq reads it), a `~/.pgpass` entry (`chmod 600`), or
> `password=` inside the DSN itself — otherwise the connection fails with
> `fe_sendauth: no password supplied`.

## 4. Create the schema (migrate + seed + horizon)

The database and roles already exist (step 2), so run the schema steps directly — all
of them as the ordinary, non-superuser `fafnir_ingest`:

```bash
cd /opt/fafnir
fafnir db migrate          # applies all migrations
fafnir db seed             # exchanges + trading calendar (calendar_start_year..calendar_end_year)
fafnir db ensure-horizon   # creates yearly partitions + extends calendar to the rolling horizon
fafnir db status           # all migrations 'applied'
```

Migration `0001` needs no elevation: step 2 pre-created the roles, and its three
`COMMENT ON ROLE` statements (catalog documentation, which PostgreSQL restricts to
superusers) are best-effort — skipped, and logged as a `WARNING`, when the migrating
role cannot set them. Confirm the role stayed unprivileged:

```bash
sudo -u postgres psql -tAc "SELECT rolsuper, rolcreaterole FROM pg_roles
  WHERE rolname='fafnir_ingest';"                                     # f|f
```

Checkpoint:

```bash
psql "$FAFNIR_DSN" -c "SELECT count(*) AS partitions FROM pg_inherits
  JOIN pg_class p ON p.oid=inhparent WHERE p.relname='daily_price';"
psql "$FAFNIR_DSN" -c "SELECT min(trade_date), max(trade_date) FROM ref.trading_calendar;"
```

## 5. Pre-flight smoke test (before the full run)

Validate the FMP key, the split/dividend field mappings, and the whole pipeline on
a tiny slice first:

```bash
fafnir ingest securities --limit 50
fafnir ingest prices  --symbols AAPL,MSFT --from 1990-01-01
fafnir ingest actions --symbols AAPL,MSFT
fafnir adjust --symbol AAPL
fafnir db refresh-marts
fafnir status
duk -S db ph AAPL --adj --close -n 5
```

If split/dividend counts look off, confirm the FMP `stable/splits` & `stable/dividends`
field names against a live response (paths are constants in `FMPClient`; see
[ingestion.md](ingestion.md)).

## 6. Full backfill — a single run of a few hours

For **daily** OHLCV the full 1990→present backfill is modest on both axes:

| | Estimate |
|---|---|
| FMP requests | ~8k symbols × {price + splits + dividends (+profile)} ≈ **24k–32k** |
| Wall-clock (throttled at 280/min) | **~1.5–2 h** request time → **~3–5 h** with DB writes |
| **Bandwidth** | prices ≈ 3.6 GB + actions/profiles ≈ 0.1 GB ≈ **~4–6 GB** (≈10% of the 50 GB/mo cap) |
| On-disk | **~4 GB** (`core.daily_price` + index) |

So it's **one run, same day** — no need to spread it across months. Run it detached
so a dropped session can't kill it; watermarks make it resumable.

```bash
tmux new -s fafnir-backfill
set -a; . /etc/fafnir/fafnir.env; set +a
cd /opt/fafnir

scripts/initial_backfill.sh 1990-01-01 2>&1 | tee -a var/fafnir/log/backfill.log
# detach with Ctrl-b d
```

`initial_backfill.sh` runs, in order: `ingest securities --enrich` → `ingest prices
--from 1990-01-01` → `ingest actions` → `adjust` → `db refresh-marts` → `dq run` →
`status`.

**If it's interrupted, just re-run the same command** — per-symbol watermarks resume
where it left off; `ON CONFLICT` upserts converge; nothing duplicates.

Watch progress / bandwidth from another shell:

```bash
fafnir status
psql "$FAFNIR_DSN" -c "SELECT source,endpoint,status,rows_inserted,bytes_downloaded
  FROM ops.ingestion_run ORDER BY started_at DESC LIMIT 10;"
# bandwidth used this month vs the 50 GB cap:
psql "$FAFNIR_DSN" -c "SELECT pg_size_pretty(sum(bytes_downloaded)) FROM ops.ingestion_run
  WHERE started_at >= date_trunc('month', now());"
```

## 7. Verify

```bash
fafnir status
fafnir dq run
psql "$FAFNIR_DSN" -c "SELECT count(*) FROM core.daily_price;"
psql "$FAFNIR_DSN" -c "SELECT pg_size_pretty(pg_total_relation_size('core.daily_price'));"
duk -S db ls --sector Technology -n 10
duk -S db ph SPY --adj -f week -n 8
```

## 8. Schedule daily upkeep

```bash
crontab /opt/fafnir/etc/crontab.example     # edit paths/times first
```

`daily_update.sh` is incremental and idempotent
(`ensure-horizon` → prices → actions → adjust → refresh-marts → dq), so a missed
day is caught up automatically and the partition/calendar horizon keeps rolling
forward on its own.

---

## When you'd split the load (and how)

A single run is fine for **daily** data. You only need to chunk for:

- **Intraday** history (1-min/5-min) — GB *per symbol*; this can exceed the 50 GB/mo cap.
- Re-pulling full history repeatedly, or a much larger universe than estimated.

Both loaders are resumable and chunkable:

1. **By symbol batch** — `fafnir ingest prices --symbols <batch>` for a slice now, the
   rest next cycle. Per-symbol watermarks skip finished symbols automatically.
2. **By date window** — `--from 1990-01-01 --to 2005-12-31` now, `--from 2006-01-01`
   later. Explicit windows load just that range (upserts converge), so you can stage
   history oldest-first.

Watch the monthly bandwidth gauge (query in §6) and pause as you approach the cap.

## Notes

- **Bandwidth, not disk, is the constraint** on a deep pull — watch the 50 GB/mo gauge.
- **Resumable** — kill/re-run freely; watermarks + `ON CONFLICT` upserts converge.
- **Least privilege** — point `duk`/apps at `fafnir_app` (mart read-only); only
  `fafnir_ingest` writes.
- **Active-only** — delisted retention (survivorship-bias-free) is a fast-follow.
- **Adjusted prices cost no storage** — they're derived on read
  (`mart.v_daily_price_adjusted`).
