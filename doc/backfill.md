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

`initial_backfill.sh` runs, in order: `ingest symbol-changes` → `ingest securities`
→ `ingest delisted --full` → `ingest prices --from 1990-01-01 --include-inactive` →
`ingest actions` → `adjust` → `db refresh-marts` → `dq run` → `status`.

The rename step comes first for the same reason it does in the nightly job: on a
re-run the master already holds the old tickers, and a security-master load that
goes first mints a *second* security for any company renamed since the last run.
The sweep can fold that duplicate away while it is empty — but the very next step
fills it with bars, and from then on the rename is a permanent `conflict` that only
`fafnir security merge-rename` can clear. On a genuinely empty database the step
does nothing and costs one small feed read.

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
(`ensure-horizon` → prices → actions → adjust → refresh-marts, then DQ from
`run_dq_checks.sh`), so a missed
day is caught up automatically and the partition/calendar horizon keeps rolling
forward on its own.

---

## When step 5 (adjustment factors) stops

`fafnir adjust` recomputes `core.adjustment_factor` from `core.corporate_action`.
The 2026-08-27 backfill died there with

```
psycopg.errors.NumericValueOutOfRange: numeric field overflow
DETAIL:  A field with precision 20, scale 10 must round to an absolute value less than 10^10.
```

A cumulative factor is the **product** of every split and dividend factor after a
date, over a whole history — so it spans far more orders of magnitude than any one
action. Reverse splits drive the price factor up (and volume down), forward splits
the other way, and one long-lived penny stock with six reverse splits passes 1e10.
`NUMERIC(20, 10)` could hold only `[1e-10, 1e10)`, so the first such security in the
universe overflowed it. **Migration 0013** stores both factors as unconstrained
`NUMERIC`, and `mart.v_daily_price_adjusted` no longer rounds adjusted prices to a
fixed scale (a price back-adjusted below 5e-11 used to round to `0.0000000000`).

Recovery — nothing before step 5 is lost, prices and actions are already committed:

```bash
fafnir db migrate          # applies 0013
fafnir adjust              # step 5, resumable and no longer all-or-nothing
fafnir db refresh-marts    # step 6
fafnir dq run
```

`fafnir adjust` now commits per security and steps over one it cannot compute, so a
single bad security costs that security instead of the whole run. If more than 1% of
the universe fails it exits non-zero instead — at that scale it is the schema, the
grants or a lock, not the data, and a run that wrote no factors must not report
success. If the first 50 securities fail without a single success it stops there,
rather than grinding through the universe writing an error flag per security.
Anything it skipped or found implausible is flagged rather than silently dropped:

```sql
-- securities whose recompute failed. They keep the factors from their last
-- successful run -- NONE on a first backfill (so they read unadjusted), STALE
-- afterwards (so their newest actions are missing). Either way, resolve before
-- trusting the series.
SELECT security_id, detail FROM ops.data_quality_flag
 WHERE check_name = 'adjustment_failed' AND resolved_at IS NULL;

-- factors past a plausible band (> 1e12 or < 1e-12): almost always a mis-scaled
-- or duplicated vendor split, so check the action history before trusting the series
SELECT security_id, detail FROM ops.data_quality_flag
 WHERE check_name = 'adjustment_factor_extreme' AND resolved_at IS NULL;

-- the action history behind one of them
SELECT action_type, ex_date, split_numerator, split_denominator, dividend_amount
  FROM core.corporate_action WHERE security_id = :id ORDER BY ex_date;
```

A confirmed-bad action is corrected in `core.corporate_action` (the raw payload it
came from is in `landing.fmp_raw`); re-run `fafnir adjust --symbol <SYM>` afterwards.

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

The counts `dq run` prints are *new* flags. A condition already sitting unresolved in
`ops.data_quality_flag` is not flagged again, so a second run over unchanged data
reports zeros — that is the check working, not the check finding nothing. Use
`fafnir dq list` (or `fafnir status`) for the standing totals, and
`fafnir dq list --detail --check outlier` to read the ones this switch turned up.

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
| `inconclusive` | No splits after that date (use an earlier `--date` or a symbol that has split), a feed returned no bar, or the two feeds have no bar for the *same* trading day. |

Both feeds are always compared **on the same trading day**. The probe anchors on the
unadjusted feed's first bar on/after `--date` and then demands that exact date from
the other feed; if it is missing it reports `inconclusive` rather than comparing
adjacent days, since the price move between them would otherwise be charged to the
split ratio. The bar actually used is printed under the header.

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
| `volume_unusable` | `unadjustedVolume` is present but not a number. The loader prefers that field, so every bar would quarantine as `nonnumeric_volume`. |
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

The command exits non-zero on `feeds_agree`, `ratio_mismatch`, `volume_adjusted`,
`volume_unusable` and `volume_ratio_mismatch`, so it can gate a scripted backfill.

---

## Confirming the Sharadar feed

```bash
fafnir source probe-sharadar                      # 200 securities, seed 0
fafnir source probe-sharadar --sample 500 --seed 7
fafnir source probe-sharadar --json > /var/lib/fafnir/probe-sharadar-$(date +%F).json
```

Run it before any Sharadar load, and again after a change to the client or the
loaders. It draws a deterministic sample from TICKERS: listed and delisted
securities, stratified by the decade of their first price, a tenth of them funds.
It asks the paid feed every question the loaders' design rests on, then gives each
answer a verdict:

- **PASS:** the fact is as the design assumes.
- **NOTE:** a fact the design must accommodate. The detail says how.
- **BLOCK:** the fact contradicts the design. The command exits non-zero, and nothing
  loads until the plan's decision log records a mitigation.

It writes nothing, and costs about two requests per sampled security plus 30: 427
requests and 108 MB for the default 200 securities.

**The `--json` report holds vendor rows** (the worst cases, as evidence). Keep it on
the host. Post only the verdicts and the aggregates the terminal report prints: the
repo and its issues are public (ADR 0012 §8).

What the first run on the paid key found (2026-10-19, 200 securities, seed 0):

| # | Item | Verdict | What it means for the loaders |
|---|---|---|---|
| 1 | API mechanics | NOTE `measured` | 16 of 16 behaviours as documented or assumed. **An unknown parameter is ignored, not refused**, so a misspelt filter returns everything; loaders assert their filters in the rows they get back. TICKERS' `table` filter takes `stocks` / `fundamentals` / `funds`, not `SEP`. |
| 2 | Precision | NOTE `precision_3dp` | Every price field carries at most 3 decimal places. The raw close (`closeunadj`) is exact to that; a raw open/high/low recovered from the split-adjusted fields carries up to 0.0005 × the cumulative split factor of error. Volume is split-adjusted too, and whole on 94.5% of bars. |
| 3 | Raw recovery | PASS `ratio_matches_within_rounding` | All 127,726 split-affected bars agree with the ACTIONS split product: 73% exactly, the rest within the rounding of the stored decimals; none beyond. 71% of recovered raw volumes are whole, so the loader rounds. |
| 4 | Split direction | PASS `split_value_new_per_old` | AAPL's 4-for-1 on 2020-08-31 is `value = 4`, and a 1-for-4 is 0.25. All 131 sampled splits step the unadjusted/adjusted ratio by their value. |
| 5 | Dividend value | NOTE `dividend_split_adjusted` | AAPL's $0.77 of 2019-11-07 is `0.1925` = 0.77 / 4. The loader multiplies by the split product after the ex-date to store the amount as declared. |
| 6 | Ticker keys | NOTE `history_under_current_ticker` | META's bars start 2012-05-18, under META, and `FB` returns nothing. A dead issuer's ticker carries a numeric suffix: 2,880 of 14,692 delisted, 911 on a root a live security trades under today. Loaders key on `permaticker`. |
| 7 | permaticker | NOTE `permaticker_shared` | All 17,842 tickers listed for both stocks and fundamentals carry one permaticker. SEP and SF1 rows carry no `permaticker`; they reach it through TICKERS. |
| 8 | Same-day values | PASS `same_day_raw_confirmed` | On 2026-10-16, `close == closeunadj` for all 100 listed securities sampled. |
| 9 | Update behaviour | NOTE `history_restamped_on_adjustment` | Every split or dividend re-stamps `lastupdated` on the whole history (`closeadj` moves). A `lastupdated` delta re-reads whole histories. |
| 10 | Row shapes | PASS `recorded` | No null in the price rows; the zeros (22,042, all volume) are values. SF1 has nulls, kept as NULL. |
| 11 | API as implemented | PASS `as_implemented` | Every behaviour the client and duk rely on holds. |

No blocking verdict. The download host for bulk files is
`static-sharadar.nyc3.digitaloceanspaces.com` (a 302 from `api.sharadar.com`), so
the host's egress must allow both hosts. The probe did not measure the zips; #52
did, and its sizes and timings are under
[Sharadar initial population](#sharadar-initial-population).

---

## Sharadar initial population

```bash
fafnir source ping --require-paid                        # the key is the subscription's
fafnir source sharadar-bulk                              # tickers, actions, SEP, SFP, SF1
fafnir source sharadar-bulk --tables SEP --force         # re-download one table
psql "${FAFNIR_DSN}" -X -f scripts/landing_sizes.sql     # what the ledger now holds
fafnir ingest sharadar-master                            # TICKERS + ACTIONS -> the vendor tables
```

`sharadar-bulk` downloads each table's whole-history file and records it in
`landing.sharadar_file`. It loads nothing into `core`; the loaders do that, reading
the files (#55, #58, #75). For each table:

- **Download.** It asks `api.sharadar.com` for the table with `years=full`
  (`--years 5|10` for less). The answer is a 302 to a pre-signed zip on
  `static-sharadar.nyc3.digitaloceanspaces.com`, which it fetches **without** the key.
  It streams the zip to `[sharadar] bulk_dir/<sha256>.csv.zip` under a temporary name,
  so a broken download leaves nothing behind.
- **Check the header.** The header is checked against the table's schema:
  - **A missing column** fails the table, naming the column and what the file has
    instead. The file is kept for inspection and not recorded.
  - **An unknown column** is a warning, kept in the run's `params.unmapped_columns`
    and read as text.
- **Record it.** It counts the rows and records the file, then marks the table's
  older full files `superseded`.

Its behaviour across runs:
- **One run row per table.** `ops.ingestion_run` gets `source = sharadar`,
  `endpoint = bulk:<table>`, with the bytes downloaded and, in `params`, the rows,
  file and header findings.
- **Resumable.** A table the ledger already holds is skipped (no request), so a run
  that stopped part-way picks up at the first table it had not finished. One
  table's failure does not undo the others; the command exits non-zero naming the
  failures.
- **Idempotent.** With `--force`, content identical to a file on record is that
  file again: no second row, no second copy on disk.

Before the first run:
- **Egress.** The host's egress must allow **both** `api.sharadar.com` and the
  download host. A blocked download host fails at the download with `Tunnel
  connection failed` or a connection error.
- **`bulk_dir`.** Set `[sharadar] bulk_dir` to an absolute path (a Volume if the
  local disk is tight).

**What to expect**, measured on 2026-10-19 (full history, the paid key, from a cloud
session):

| Table | Rows | Zip | CSV inside | Run (download + row count) |
|---|---|---|---|---|
| `tickers` | 74,420 | 4.8 MB | 25 MB `tickers.csv` | 2 s |
| `actions` | 718,464 | 10 MB | 50 MB `actions.csv` | 3 s |
| `stocks` (SEP) | 45,451,818 | 907 MB | 3.2 GB `stocks.csv` | 117 s |
| `funds` (SFP) | 15,760,389 | 322 MB | 1.1 GB `funds.csv` | 38 s |
| `fundamentals` (SF1) | 3,218,845 | 632 MB | 2.4 GB `fundamentals.csv` | 62 s |
| **All five** | | **1.9 GB** | 6.8 GB | **under 4 minutes** |

- **Zip contents:** each zip holds one CSV named for the table's modern name
  (`stocks.csv`, even when `SEP` was asked for).
- **Headers:** every header matched its schema, with no unknown column (SF1 has 112
  columns).
- **Time:** most of each run is counting rows, not the download. A host on a slower
  link will differ.
- **Reading:** every row of all five files reads back through
  `sharadar_files.rows` with no parse error. A full `stocks` read takes 260 s
  (about 175,000 rows a second); `funds` takes 95 s and `fundamentals` 180 s.

### On the production host

The first population of a host (#59), in order. Each command runs as `fafnir` with the
environment file sourced:

```bash
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir
  scripts/backup_dump.sh                          # 1. a restorable copy first
  git pull && fafnir --version && fafnir db migrate'  # 2. deploy main; 0030, 0031
```

Step 2 is [releasing.md › Deploying it](releasing.md#deploying-it); add its `pip
install` if `pyproject.toml` changed. Before step 3:
- set an absolute `[sharadar] bulk_dir` in `~fafnir/.fafnirrc` (the relative default
  follows the working directory);
- note the free space on its filesystem (`df -h /var/lib/fafnir` for the path used
  above). The full set needs about 2 GB, and a later refresh about 4 GB at peak
  ([disk budget](#disk-budget-with-sharadar)).

Then:

```bash
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir
  time fafnir source sharadar-bulk                # 3. tickers, actions, SEP, SFP, SF1
  psql "$FAFNIR_DSN" -X -f scripts/landing_sizes.sql
  fafnir source probe-sharadar                    # 4. verdicts; --json stays on the host
  time fafnir ingest sharadar-master              # 5. TICKERS + ACTIONS -> vendor tables
  fafnir status'
```

- **Table names.** `--tables` takes any case: `TICKERS` and `tickers` are the same
  table.
- **What to record on #59** (aggregates only, never vendor values):
  - the dump's path;
  - each table's rows, bytes and time, from step 3's output;
  - the disk free before and after step 3;
  - the probe's verdict table;
  - the master load's counts by table and delisted flag, and its time;
  - anything that did not work as written here.
- **A table that fails** in step 3 does not undo the others. Re-run step 3: the
  tables already on record are skipped.

## Disk budget with Sharadar

Plan v2 keeps Sharadar's files on disk, under `[sharadar] bulk_dir`, recorded in
`landing.sharadar_file`. Nothing is copied into a landing table. So Sharadar adds
three things to the host's disk:

1. **The files.**
2. **The rows it adds to `core`.** Bars of issuers FMP never held; a correction
   replaces a bar, it does not add one.
3. **The dumps,** which grow with `core`.

Projected from the paid feed on 2026-10-19, and since confirmed by the first full
download (the measured table above). The measured files total **1.9 GB**: the
price files came in as projected, and SF1 came in smaller (632 MB, at 3.2M rows and
3.65× compression):
- **Row counts** come from TICKERS' first and last price dates.
- **Bytes per row and compression** come from CSV slices of each table.
- **Bytes per bar in `core.daily_price`** come from 260,800 synthetic bars on
  the test database: 151 bytes, heap plus primary key.

| | Rows | CSV | On disk (zip, ~3.3×) |
|---|---|---|---|
| `stocks` (SEP) | ~45.5M, 55% from delisted issuers | ~70 B/row → ~3.2 GB | **~1.0 GB** |
| `funds` (SFP) | ~15.8M, 32% delisted | ~70 B/row → ~1.1 GB | **~0.35 GB** |
| `fundamentals` (SF1) | ~3.5M (17,866 issuers × ~4.5 rows a quarter) | ~1 KB/row → ~3.5 GB | **~1.1 GB** |
| `actions`, `tickers` | ~1M (estimated from the sample's events per issuer), ~50k | | **< 0.1 GB** |
| **One full set** | | | **~2.4 GB** projected; **1.9 GB** measured |

- **Files.** A refresh downloads a new full set before the old one is
  superseded, so allow **~4 GB** at peak (two 1.9 GB sets). `daily` and `metrics` are not in that
  figure; they are not needed before the fundamentals phase (P4), and each is of
  the order of `stocks`.
- **`core`.** At most ~30M bars of delisted issuers (stocks and funds) are new to
  the warehouse. At 151 bytes a bar that is **~4.6 GB**, an upper bound: some of
  those issuers FMP already holds.
- **Dumps.** Each nightly dump grows in proportion to `core.daily_price`, and
  `backup_dump.sh` keeps 14. Measure the bytes per bar of the current dump (its
  size over `core.daily_price`'s row count), multiply by the new bars, and check
  `FAFNIR_BACKUP_RETAIN_DAYS` still fits.

Together that is about 10 GB plus the dumps' growth, on top of the 1990 FMP
figures in [install_hetzner.md §1.1](install_hetzner.md#11-sizing), which has the
row. On the recommended 80 GB host that leaves the disk under half full. Read the actual figures any time with:

```bash
psql "${FAFNIR_DSN}" -X -f scripts/landing_sizes.sql
du -sh /var/lib/fafnir/landing/sharadar     # your [sharadar] bulk_dir
```

---

## Clearing data for a reload

`scripts/reset_data.sh` truncates the tables a reload has to **replace** rather than
top up. It never touches structure: migrations, partitions, and the seeded reference
data (`ref.exchange`, `ref.sector`, `ref.industry`, `ref.trading_calendar`) all
survive, so you go straight back to ingesting.

**Dry run is the default** — it reports the row counts it would delete and stops.
Pass `--yes` to execute. It names the target as `db=<dbname> host=<host>`, parsed
from either DSN form; the DSN itself is never echoed, so a URL-form password cannot
reach the terminal or a redirected log.

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
| `all` | every core/ops/landing table (applied renames and the Sharadar file ledger included), identities restarted | reference data, partitions, migrations, the Sharadar files on disk |

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
