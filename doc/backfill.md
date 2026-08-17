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
fafnir source probe-prices          # is the price feed actually unadjusted?
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

**Check a deep-history price level against a known value.** Counts and DQ flags
cannot catch a wrongly-adjusted feed — a doubly-adjusted series is internally
consistent and passes every structural check. Only comparing a level to an outside
reference will reveal it:

```bash
psql "$FAFNIR_DSN" -c "
  SELECT p.trade_date, p.close AS raw_close, v.close AS adj_close, v.price_factor
  FROM core.daily_price p
  JOIN mart.v_daily_price_adjusted v USING (security_id, trade_date)
  JOIN core.security s USING (security_id)
  WHERE s.primary_symbol = 'AAPL' AND p.trade_date = '1990-01-02';"
```

Expect `raw_close` around **\$39** (the price AAPL actually traded at in 1990) and
`adj_close` around **\$0.35** (that divided by the 112:1 cumulative split since).
A `raw_close` near \$0.35 means the split-adjusted endpoint is being loaded and
`adj_close` will be ~\$0.003 — stop and re-read
[adr/0004](adr/0004-unadjusted-price-feed.md).

## 8. Schedule daily upkeep

```bash
crontab /opt/fafnir/etc/crontab.example     # edit paths/times first
```

`daily_update.sh` is incremental and idempotent
(`ensure-horizon` → prices → actions → adjust → refresh-marts → dq), so a missed
day is caught up automatically and the partition/calendar horizon keeps rolling
forward on its own.

---

## Switching to the unadjusted price feed

**Who this is for:** anyone whose warehouse was loaded before
[ADR 0004](adr/0004-unadjusted-price-feed.md), i.e. while `ingest prices` still read
`historical-price-eod/full`. A fresh install can skip this section.

Those bars were already split-adjusted, so `fafnir adjust` applied the split factor
a second time and deep history collapsed toward zero. The rows cannot be repaired
in place by topping up — they have to be replaced.

Check whether you are affected:

```bash
psql "$FAFNIR_DSN" -c "
  SELECT endpoint, count(*) AS symbols, max(last_loaded_date) AS through
  FROM ops.load_watermark WHERE source='fmp'
    AND endpoint LIKE 'historical-price-eod%' GROUP BY endpoint;"
```

Rows under `historical-price-eod/full` mean the old feed. `fafnir ingest prices` will
refuse to run incrementally in that state rather than half-refill the table.

Re-backfill (a few hours, same cost as the original run):

```bash
# 1. Confirm the unadjusted feed is really unadjusted before spending the run.
#    3 requests, writes nothing. See "Confirming the price feed" below.
fafnir source probe-prices --symbol AAPL --date 1990-01-02

# 2. Drop the split-adjusted prices, their derived factors, and the stale
#    watermarks. Corporate actions, the security master and landing payloads are
#    all kept -- only the price fact is wrong. Preview first (the default), then
#    commit with --yes.
scripts/reset_data.sh --scope prices
scripts/reset_data.sh --scope prices --yes

# 3. Reload from the unadjusted endpoint. The explicit --from is REQUIRED:
#    without it each request is capped at 5000 bars (~19.8y) and you would
#    silently lose everything older.
fafnir ingest prices --include-inactive --from 1990-01-01

# 4. Recompute factors against the new raw closes, then refresh and check.
fafnir adjust
fafnir db refresh-marts
fafnir dq run
```

Step 4's `fafnir adjust` is not optional: dividend factors are valued against the
prior raw close, so every factor derived from the old prices is stale. (Step 2
clears `core.adjustment_factor` for exactly that reason — a stale factor is more
dangerous than a missing one, because the view silently applies it.)

Then run the deep-history level check in [§7 Verify](#7-verify) — that is what
actually confirms the switch worked.

Expect `dq run` to report more `outlier` flags than before. Raw prices contain real
split-sized jumps; the check excludes moves landing on a known split ex-date, so
what remains is usually a **missing** corporate action — worth investigating, and a
detection capability the pre-adjusted feed did not have.

---

## Confirming the price feed

```bash
fafnir source probe-prices                              # AAPL @ 1990-01-02
fafnir source probe-prices --symbol MSFT --date 1995-01-03
```

Field names alone cannot tell you whether a feed is adjusted — a payload can be
named `close` and still be split-adjusted, which is precisely how ADR 0004's bug
went unnoticed. So the probe checks arithmetic instead. It pulls the same old bar
from both the unadjusted and split-adjusted endpoints, pulls the split history, and
verifies:

```
unadjusted close  ==  split-adjusted close  ×  cumulative split ratio
```

For AAPL at 1990-01-02 that is `$39.20 == $0.35 × 112`. It costs 3 requests, writes
nothing, and reports:

| Verdict | Meaning |
|---|---|
| `unadjusted_confirmed` | The feeds differ by exactly the split ratio. Safe to backfill. |
| `feeds_agree` | Both returned the same price despite a split — the "unadjusted" endpoint is **not** unadjusted. Stop; fafnir would double-adjust. |
| `ratio_mismatch` | They differ, but not by the split ratio. Splits payload incomplete, or a feed changed meaning. |
| `inconclusive` | No splits after that date (use an earlier `--date` or a symbol that has split), or a feed returned no bar. |

It also prints the payload's raw field names and whether the ingestion boundary would
accept the bar — worth a glance, since FMP labels OHLC as
`adjOpen`/`adjHigh`/`adjLow`/`adjClose` on this endpoint and a *third* spelling
appearing would quarantine every bar.

### Volume is checked separately

Volume back-adjusts the **opposite way** to price: a 4:1 split multiplies pre-split
share counts by 4 rather than dividing them. So a volume that arrives already
split-adjusted is **inflated by the split ratio squared** (12,544× for AAPL's 112:1),
not collapsed toward zero. There is no vanish-to-zero tell, and no DQ check covers
volume — it is the quieter of the two failures, which is why it gets its own verdict:

| Verdict | Meaning |
|---|---|
| `volume_raw_confirmed` | Volume being ingested is raw — either the feeds differ by the split ratio, or the payload carries an explicit `unadjustedVolume`, or both feeds agree *and* match `unadjustedVolume`. |
| `volume_adjusted` | Both feeds report a volume larger than `unadjustedVolume` — what is being ingested is split-adjusted, and fafnir would inflate it again. |
| `volume_ambiguous` | Both feeds report the same volume and neither carries `unadjustedVolume`. See below. |
| `volume_ratio_mismatch` | The feeds differ by something that is neither 1 nor the split ratio. |

**`volume_ambiguous` is a real limit, not a bug.** "FMP never split-adjusts volume"
(fine) and "FMP split-adjusts volume on both endpoints" (double-counted) produce an
*identical* signature from these two feeds — equal volumes on each. Only
`unadjustedVolume` breaks the tie, and the stable endpoints may not return it. If you
land here, check one deep-history date against an outside source (the exchange, or
another vendor) before trusting old volume. The probe reports this rather than
guessing, and does **not** exit non-zero on it, since it is a prompt to verify rather
than evidence of breakage.

The loader hedges the same way: where a payload offers `unadjustedVolume` it is
preferred over `volume`, because `core.daily_price` is defined as raw and
`unadjustedVolume` is raw by definition.

The command exits non-zero on `feeds_agree`, `ratio_mismatch`, `volume_adjusted` and
`volume_ratio_mismatch`, so it can gate a scripted backfill.

---

## Clearing data for a reload

`scripts/reset_data.sh` truncates the tables a reload has to **replace** rather than
top up. It never touches structure: migrations, partitions, and the seeded reference
data (`ref.exchange`, `ref.sector`, `ref.industry`, `ref.trading_calendar`) all
survive, so you go straight back to ingesting.

**Dry run is the default** — it reports the row counts it would delete and stops.
Pass `--yes` to execute.

```bash
scripts/reset_data.sh --scope prices            # preview
scripts/reset_data.sh --scope prices --yes      # execute
scripts/reset_data.sh --scope all --yes --vacuum
```

| Scope | Clears | Keeps |
|---|---|---|
| `prices` | `daily_price`, `adjustment_factor`, price watermarks (current **and** retired endpoint) | security master, actions, landing |
| `actions` | `corporate_action`, `adjustment_factor` | prices, security master |
| `market-data` | prices + actions + factors + price watermarks | security master (skips re-ingesting ~8k securities) |
| `landing` | `landing.fmp_raw` | everything else — reclaims disk, loses the payload audit trail |
| `dq-flags` | `ops.data_quality_flag` | everything else |
| `all` | every core/ops/landing table, identities restarted | reference data, partitions, migrations |

Each scope prints the exact reload sequence to run next. Three design notes worth
knowing:

- **Factors go with prices.** `adjustment_factor` is derived partly from raw closes
  (dividends are valued against the prior close), so factors left behind after a
  price reload are stale — and the adjusted view applies them silently.
- **The truncate is one transaction, without `CASCADE`.** Each scope's table list is
  closed under foreign keys. Omitting `CASCADE` means that if a future migration adds
  a referencing table, this fails loudly instead of quietly wiping it.
- **Marts are not refreshed.** `mart.security_latest` keeps its pre-reset snapshot
  until you run `fafnir db refresh-marts` after reloading. An empty mart is honest;
  a stale one pretending to be current is not.

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
