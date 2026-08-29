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
`ensure-horizon → ingest symbol-changes → ingest securities → ingest tracked →
ingest delisted → ingest prices → ingest actions → adjust → refresh-marts → dq run`.

The first four steps are **universe maintenance**, and their order is load-bearing:

1. **`ingest symbol-changes`** applies ticker renames to the security that already
   holds the history (FB → META keeps one `security_id`). It runs first because to
   the screener a renamed ticker is indistinguishable from a new listing — run the
   security master first and it mints a *second* security for a company fafnir
   already has, stranding its prices, corporate actions and price watermark on a
   ticker nothing will ever close.
2. **`ingest securities`** re-reads the screener, so a security that listed today —
   an IPO, a spin-off, a new ETF — enters scope. It reports how many were new. A
   new security has no price watermark, so the price step below pulls its full
   available history on the same run; nothing else is needed.
3. **`ingest tracked`** refreshes the *declared* universe — `ref.tracked_symbol`,
   the symbols no screener returns because they have no listing venue (mutual funds,
   principally). It runs *after* the security master, not before: both write through
   the same upsert on the same key, and going second is what makes the declared
   `asset_type` and venue the ones that stand for a symbol that appears in both.
4. **`ingest delisted`** marks what stopped trading, before prices asks it for bars.
   Funds are recorded on the `MUTF` pseudo-venue, which is deliberately outside
   `SCREENER_EXCHANGES`, so this sweep cannot reach them — retiring a fund is
   `fafnir track rm <SYMBOL> --closed <date>`.

Bandwidth: the security-master refresh is ~5 screener requests per venue-page pass
(a few MB), once a night. The declared universe costs one `profile` request per
tracked symbol, plus the usual price/splits/dividends calls — about four requests a
night per fund, which is noise against the budget.

### Tracking a mutual fund

```bash
fafnir source probe-fund VFIAX        # FIRST -- confirm the NAV series is raw
fafnir track add VFIAX --note "core US equity sleeve"
fafnir ingest tracked                 # mints it (the nightly job also does this)
fafnir ingest prices --symbols VFIAX  # no watermark yet -> full history
fafnir ingest actions --symbols VFIAX && fafnir adjust
fafnir track list                     # what is declared, and whether it is loaded
```

`probe-fund` is not optional ceremony. `core.daily_price` is defined as raw and
fafnir's own factors are the only adjustment in the system; that is verified for
equities and not for funds. The probe measures NAV across the largest distribution
on record — a raw series drops by about the distributed amount, an
already-reinvested one does not. If it does not drop, loading fund distributions as
corporate actions would adjust every one of them twice.

To stop tracking one, say which kind of stopping it is. `fafnir track rm VFIAX`
halts the pulls and leaves the security active — so `dq run` will flag it stale
every night. `fafnir track rm VFIAX --closed 2027-03-31` retires it the ordinary
way: `delisted_date` stamped, ticker period closed, every bar kept.

> **First run after upgrading.** A deployment whose universe was built with
> `--limit` (the README quick start uses `--limit 500`) gets the *rest* of the
> universe on its first nightly security-master load. None of those securities has
> a price watermark, so the next `ingest prices` pulls full history for every one —
> a multi-hour run and a large download against the 50 GB/month budget. The loader
> warns when a run brings in 100+ new listings. If you see that on an intentionally
> limited universe, run `scripts/initial_backfill.sh` deliberately (it is resumable
> and chunked) rather than letting the nightly discover it.

The three universe steps are run through a `upkeep` wrapper in `daily_update.sh`: if
one fails, the job warns and carries on to prices rather than costing the night's
data for every symbol. Nothing is swallowed — the failure is still an
`ops.ingestion_run` row with `status = 'failed'`, which is why that query is on the
monitoring list below.

### When a rename cannot be applied automatically

`fafnir status` ends with a **Renames** section when a rename is waiting on a
human. That happens when the new ticker already belongs to a *different* listed
security that carries its own price history — two live claims on one ticker, which
usually means fafnir was not running when the rename happened and the security
master minted the new ticker as its own company in the meantime. The loader will
not merge two price histories silently, so it changes nothing and raises a
`symbol_change_conflict` DQ flag.

```sql
-- the review queue
SELECT old_symbol, new_symbol, change_date, detail FROM core.symbol_change
 WHERE status <> 'applied' ORDER BY change_date DESC;
```

Resolve it by deciding which `security_id` is the company: move the bars off the
duplicate (or retire the stale row with a `delisted_date`), then re-run
`fafnir ingest symbol-changes`, which retries every non-applied row. The next sweep
is what clears the queue — it reaches a terminal outcome (`applied` if the new
ticker is now the company's, `ignored` if the old ticker now belongs to a retired
row) and the entry disappears from `fafnir status`. A duplicate that holds *no*
prices, actions or factors needs no decision at all — the sweep folds it into the
surviving security by itself and says so in the log.

The `symbol_change_conflict` DQ flag is raised on the night a conflict is first
seen, not on each nightly retry, so one unresolved rename does not inflate the
open-flag count night after night. `core.symbol_change` is the durable queue; the
flag is only the notification.

## Monitoring

```bash
fafnir status                 # securities, price rows, latest date, open DQ flags
fafnir dq list                # the open queue, by check and severity
scripts/run_dq_checks.sh      # gaps/outliers/freshness + open-flag summary
```

Things to watch:
- **Open DQ is a count of problems, not of runs** — a standing condition is flagged
  once and stays one row until someone sets `resolved_at`, however many nights it
  goes unfixed, so a rising count means new problems. True for data already in the
  table as well: migration 0016 collapsed the duplicates a warehouse running before
  0014 had accumulated, so the number did not just stop growing, it dropped to the
  problems it always should have been counting. The exception is `price_*`,
  which repeats by design (see
  [data_dictionary.md](data_dictionary.md#opsdata_quality_flag--quarantineanomaly-queue-grain-dq_flag_id));
  filter it out when you want the count of distinct issues (`fafnir dq list` says
  so on the summary when `price_*` is in the table). Nothing sets `resolved_at`
  automatically — a flag you have worked stays open until you close it, with
  [`fafnir dq resolve`](#working-the-dq-queue).
- **Company-name drift** — `security_company_name_drift` flags in
  `ops.data_quality_flag`. The security master keys a listed security on
  `(source, symbol)` (0012), which assumes one issuer per ticker. This check is the
  safety net under that assumption: if two listed companies ever shared a symbol,
  the second would silently *update* the first instead of inserting, and the tell
  is the company name changing into something unrelated while the ticker stays put.
  ```bash
  fafnir dq list --detail --check security_company_name_drift
  ```
  The `detail` column carries the old name, the incoming one and the similarity
  score that tripped it; `--json` gives them unabridged.
  It is **advisory**: a genuine same-ticker rebrand (Google → Alphabet) trips it
  too, as does a vendor switching to an abbreviation (International Business
  Machines → IBM). Confirm the `security_id` still describes one company — the
  price history either continues sensibly across the name change or it does not —
  then close it with a note saying which (`fafnir dq resolve <id> --note "..."`). Escalate only if two different issuers really are sharing
  the ticker, which would mean the identity assumption is wrong for your universe.
  The flag is raised once, on the night the name changes, not nightly.
- **Unapplied renames** — the `Renames` line in `fafnir status`. Each one is a
  company whose identity is currently split across two `security_id`s.
- **New listings** — the `New (7d)` line. A week of zero on a working FMP key means
  the security-master step is not running, and the universe is quietly going stale.
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

## Working the DQ queue

`fafnir dq run` writes flags; `fafnir dq list` and `fafnir dq resolve` are how they
get worked. The two take the **same** selection options, so the workflow is to
narrow with `list` until the page is the problem you mean, then re-run the same
options under `resolve`.

```bash
fafnir dq list                                    # what is open, by check and severity
fafnir dq list --detail --check gap --limit 20    # the individual gaps
fafnir dq list -d --symbol AAPL --state all       # one security, open and resolved
fafnir dq list --check 'price_*' --since 2024-08-01
fafnir dq list --json                             # same rows, for a script
```

`--detail` switches from the per-check summary to the flags themselves, with the
`record_key` and `detail` JSONB rendered inline. Trailing `*` globs a check family
(`price_*`, `security_*`). `--state` picks the open queue (default), the resolved
record, or both.

Closing a flag records **who** and **why** — the judgement is the part `resolved_at`
alone does not keep:

```bash
fafnir dq resolve 12841 12842 --note "exchange holiday, no bar expected"
fafnir dq resolve --check gap --symbol AAPL --note "backfilled" --yes
fafnir dq resolve --check outlier --since 2024-08-01 --dry-run
fafnir dq reopen 12841                            # undo; the note goes with it
```

`--by` overrides the recorded resolver (it defaults to the OS user). `--dry-run`
prints what would close and changes nothing. A resolve by filter asks for
confirmation unless you pass `--yes`; a resolve by id does not.

Guard rails, because this closes rows in bulk:
- **Ids or filters, never both, never neither.** An unfiltered resolve would close
  the entire queue — including problems nobody has looked at — and `reopen` cannot
  put that back, because it cannot know which rows were open a moment earlier.
- **An unknown `--symbol` is fatal.** A mistyped ticker that fell through as "no
  security filter" would widen the command to the whole universe.
- **An already-resolved flag is never rewritten**, so re-running a resolve is a
  no-op rather than a silent re-attribution of someone else's decision to you. Ids
  that did not close are reported, one line each, saying which.

**Resolving is a judgement, not a repair.** Closing a flag frees that condition's
slot in `ux_dq_flag_open_condition`, so if the problem is still in the data the next
`fafnir dq run` flags it again — which is the check telling you it never went away,
not a bug. Fix the data (backfill the gap, load the missing corporate action) if you
want it to stay closed.

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
- **Adjustment factors failed** — `fafnir adjust` commits per security and flags one
  it cannot compute (`adjustment_failed`) instead of ending the run; those securities
  keep their previous factors (none on a first backfill, stale afterwards) until
  fixed. Past 1% of the universe failing it exits non-zero — that is systemic — and
  it stops early if the first 50 fail without a success. See
  [backfill.md](backfill.md#when-step-5-adjustment-factors-stops).
- **Schema rollback** — `fafnir db rollback --steps N` (uses the `.down.sql`).
  Never roll back in a way that drops `core.daily_price` history without a backup.

## Backups

The whole warehouse is rebuildable from `landing` + the sources, but back up at
least `core` and `landing`:

```bash
pg_dump -Fc -n core -n landing -n ref fafnir > fafnir_$(date +%F).dump
```
