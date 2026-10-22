# Automations: the nightly job, its budgets, and what may be changed

## What runs, and why the order is load-bearing

Two scheduled jobs, one after the other, after US settlement. Both are
incremental and idempotent — a missed day is caught up on the next run.

1. **The daily load**, `fafnir-daily` (`scripts/daily_update.sh`, `daily.log`).
   It runs one of two nights, by `[general] primary_price_vendor` (#111); the log's
   first line names it (`primary vendor: fmp`). FMP mode:

   ```
   ensure-horizon → ingest symbol-changes → ingest securities → ingest tracked
     → ingest delisted → ingest prices → vendor link --new → ingest actions
     → adjust --changed → db refresh-marts → status
   ```

   Sharadar mode, from the cutover:

   ```
   ensure-horizon → ingest sharadar
     → [fmp_secondary] ingest tracked, prices, actions --allow-secondary → adjust --changed
     → db refresh-marts → status
   ```

2. **DQ**, `fafnir-dq` (`scripts/run_dq_checks.sh`, `dq.log`): `dq run` (the SQL
   checks, then dorq per `[dq] engine`), then `dq list`. Its timer is 23:00 ET, but
   the unit is `After=fafnir-daily.service`, so it starts when the load ends. A
   DQ failure (dorq exiting) does not mark the load failed.

   It is the night's **only** `dq run`. Up to v1.4.0 the daily ran one too, so
   there were two `dq-shadow` runs a night and the second, narrower one replaced
   the first's shadow file. One `dq-shadow` row in `ops.ingestion_run` a night,
   started after `fafnir-daily` ended, is the healthy shape.

The first four are **universe maintenance** and their order is not arbitrary:

1. **`symbol-changes` first.** To the screener a renamed ticker is
   indistinguishable from a new listing. Run the security master first and it
   mints a *second* security for a company fafnir already holds, stranding its
   prices, actions and watermark on a ticker nothing will ever close.
2. **`securities`** re-reads the screener, so a security that listed today enters
   scope today. A new security has no watermark, so the price step pulls its full
   history on the same run.
3. **`tracked`** refreshes the declared universe (funds — no listing venue to be
   screened on). It goes *after* the security master because both write through
   the same upsert on the same key, and going second is what makes the declared
   `asset_type` and venue the ones that stand.
4. **`delisted`** marks what stopped trading *before* prices asks it for bars.

The three universe steps run through an `upkeep` wrapper: if one fails the job
warns and carries on to prices, rather than costing the night's data for every
symbol. Nothing is swallowed — the failure is still an `ops.ingestion_run` row
with `status = 'failed'`, which is why that query is on the monitoring list.

### The Sharadar night (`fafnir ingest sharadar`, #98)

From the cutover (`[general] primary_price_vendor = "sharadar"`, #125), one command
does universe, prices and actions for the securities Sharadar feeds, and
`daily_update.sh` runs it (#111). Before the cutover only `--dry-run` is allowed.

```
source sharadar-bulk --tables tickers,actions --force → source sharadar-delta
  → ingest sharadar-master → ingest sharadar-universe
  → ingest sharadar-prices --nightly → ingest sharadar-actions --backfill --gap-fill
```

A failed step stops the night non-zero, and every step is safe to re-run. Its runs
are all `source = 'sharadar'`:

| Endpoint | Step | Read in `params` |
|---|---|---|
| `bulk:tickers`, `bulk:actions` | the whole-file downloads | `file_id`, `rows`, `superseded` |
| `delta:stocks`, `delta:funds` | SEP/SFP rows by `lastupdated` | `lastupdated.gte`, `rows`, `file_id`, `dates` (a year back is normal: the vendor restamps history it adjusts) |
| `master:tickers`, `master:actions` | the vendor tables | |
| `universe` | renames, delistings, listings | the counts |
| `prices:nightly` | the bars | `through` (last delta file applied per table), `counts` (`new`, `revised`, `unchanged`, `kept:<source>`), `by_date` |
| `actions` | splits and dividends | `skipped.near_existing` |

Diagnosing it:
- **No bars tonight.** Look for a `delta:*` run that failed, or one with `rows = 0`,
  which records no file. Then read `prices:nightly`'s `through`: the nightly reads
  the delta files after it, so a failed night is re-read the next.
- **`kept:fmp` is expected.** It means a security taken over from FMP keeps FMP's
  bars. To replace them, use `vendor correct`, never the nightly.
- **`revised` counts only material changes.** That is `vendor correct`'s price test,
  or volume beyond 1%. A history restamped after a split recovers to within rounding
  and stays `unchanged`.
- **FMP as secondary.** While `[general] fmp_secondary` is true (`fafnir config get
  fmp_secondary`), the night runs FMP's `tracked`, `prices` and `actions` with
  `--allow-secondary`, upkeep-wrapped. They load only what FMP still feeds: funds
  and unlinked securities (`repo.fmp_fed`). A failure there warns and does not
  stop the marts.
- **"`fafnir ingest prices` is FMP's, and ... primary_price_vendor is sharadar"**
  is the guard working: in Sharadar mode FMP's six loaders refuse without
  `--allow-secondary`. A cron line or runbook step that hits it is FMP's night
  trying to resume. Do not add the flag to make it pass; find out why it ran.
- **Rollback** is `vendor feed --vendor fmp --all-linked`, then the setting back to
  `"fmp"` (operations.md, "Primary vendor switch and rollback"). FMP resumes from
  its watermarks and never overwrites a Sharadar row.
- **The five dry runs before the cutover** are `fafnir ingest sharadar --dry-run`
  after FMP's night. Their price step reports per trade date `fmp_agrees`,
  `fmp_differs`, `new` and `fmp_only`. Read the night's own date; the differences
  are what #98 asks to have explained.

## Diagnosing a night

```sql
-- which step dominated the window
SELECT endpoint, status, started_at, finished_at - started_at AS duration,
       rows_inserted, rows_quarantined, bytes_downloaded
  FROM ops.ingestion_run
 WHERE started_at > current_date - 1 ORDER BY started_at;

-- what failed, recently
SELECT * FROM ops.ingestion_run WHERE status IN ('failed','partial')
 ORDER BY started_at DESC LIMIT 20;
```

Or via the tools: `ingestion_runs(since=…)` and `ingestion_runs(status="failed")`.

Host side — all read-only, safe to run freely:

```bash
scripts/monitor.sh                     # all ten sections; non-zero if any tripped
scripts/monitor.sh disk timers backups # or narrow it
scripts/monitor.sh --quiet             # only what tripped
systemctl list-timers 'fafnir-*'
journalctl -u fafnir-daily -n 200 --no-pager
fafnir status
```

`monitor.sh` sections: `status dq disk timers journal backups runs bandwidth sharadar slow`.
`bandwidth` is by vendor, and FMP's budget counts FMP's bytes alone. `sharadar`
shows the newest delta fetch, nightly load and file download, and the file ledger.
In Sharadar mode it warns when the last weeknight's `prices:nightly` did not
succeed.
It changes nothing, by design — it exists because these checks are spread across
the fafnir CLI, the OS and psql, which makes them easy to half-do at 8am.

### Vendor gaps in the nightly report

`scripts/collect_facts.sh` runs `fafnir source vendor-gaps` (reads only) into a
`VENDOR GAPS` section, and the morning email lists them with what is new since the
previous night. Each is a live security FMP stopped serving: in the nightly
universe, its last bar two or more sessions behind, its newest payload an
open-ended request that returned nothing newer, 95% of its venue ahead of it, and
not on the delisted feed. They are for an FMP ticket (an operator task), not for the
queue: their `stale` flags stay open (`references/dq-playbooks.md`, `stale`).

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir source vendor-gaps --json
```

### What the price load holds back

From v1.5.0 the nightly price load never overwrites a stored bar by more than
`[prices] restatement_max_ratio` (1.25, close) or `restatement_max_volume_ratio` (3):
it keeps the bar and flags `price_restatement_conflict`. A night with many is the
vendor restating, not the load failing; `ops.ingestion_run` stays `success`. An
explicit `ingest prices --from` re-fetch overwrites, records `params.restated` and
`restated_count`, and resolves the conflicts on its dates. A volume difference with
both sides at or below `restatement_min_volume` (100) is FMP rounding an odd lot to
a round lot: never a conflict; the stored volume is kept and counted in
`params.restatement_odd_lot_kept` (G7).

## The two budgets

**Bandwidth** — 50 GB/month on the FMP Professional plan, counted in FMP's bytes
alone (`source = 'fmp'`); Sharadar's downloads are their own:

```sql
SELECT date_trunc('month', started_at) AS m, source, sum(bytes_downloaded)
  FROM ops.ingestion_run GROUP BY 1, 2 ORDER BY 1, 2;
```

**Requests** — the actual constraint on the nightly window, and dominated by one
step. `ingest actions` *ships* in `symbol` mode: a full split and dividend history
for every active security, every night — ~16,000 requests and about an hour to
capture a few hundred changed rows. The alternative (`actions_mode = "auto"`,
ADR 0007) costs ~2 requests plus a first-load for anything newly minted and a 1/30
reconciliation slice.

**Never assume which mode is running.** The shipped default is not the deployed
setting: this warehouse has been on `auto` since 2026-09-01, and reading the mode
from this file instead of from the data nearly cost the most important finding of
the 2026-09-10 session. Read it from the run:

```sql
SELECT started_at, params->>'mode' AS mode
  FROM ops.ingestion_run
 WHERE job_name = 'corporate-actions'
 ORDER BY started_at DESC LIMIT 1;
```

Under `auto`, `corporate_action_drift` is the 30-night evaluation of that mode, and
its drift rate — about 2% of each nightly reconciliation slice — is an
operator-facing number, not queue noise. Report it as evidence, not as flags.

**You may run the probe. You may not flip the switch.**

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir source probe-actions   # 2 + 2N requests, writes nothing
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir source probe-fund <SYM>  # only if funds are held
```

Report the verdict. Only `calendar_complete` justifies the change, and the change
itself is an operator edit to `~/.fafnirrc` — on any other verdict the sweep
silently drops dividends, which is worse than an hour of requests. Then watch
`corporate_action_drift` for a full 30-night cycle; an empty queue after that is
the evidence the switch was right, and until then it is only a reasonable bet.

## Other maintenance

| Task | Command | Notes |
|---|---|---|
| Partitions & calendar horizon | `fafnir db ensure-horizon` | in the nightly; rolls automatically |
| Mart refresh | `fafnir db refresh-marts` | after any repair that changes core |
| Re-adjust | `fafnir adjust [--symbol X]` | **also after a price reload** — dividend factors are valued against the prior raw close, so new prices change them |
| Reconciliation | `scripts/reconcile.sh AAPL,MSFT,SPY` | weekly, rotating sample; reports, never overwrites |
| Backups | `scripts/backup_dump.sh`, `backup_offsite.sh` | dump **every** schema, not a subset |
| Sharadar bulk files | `fafnir source sharadar-bulk [--tables …] [--force]` | operator-run for SEP, SFP and SF1; the Sharadar night re-downloads TICKERS and ACTIONS. Downloads whole tables to `[sharadar] bulk_dir` (`<sha256>.csv.zip`) and records each in `landing.sharadar_file`; loads nothing into core. A table already on record is skipped unless `--force`. Runs are `source = sharadar`, `endpoint = bulk:<table>`. The files are **not** backed up (re-downloadable while subscribed) |
| Sharadar deltas | `fafnir source sharadar-delta [--tables SEP,SFP] [--since D]` | step 2 of the Sharadar night. Rows stamped `lastupdated` from the newest file's fetch day less 3 days, as `<sha256>.csv` with `kind = 'delta'`. A fresh full SEP/SFP (`sharadar-bulk --force`) shortens what a newly minted security reads: every delta since the full file |

On backups, the trap worth knowing: skipping `mart` because it is derived and
`meta` because it is "only bookkeeping" gives a restore that is not a working
warehouse. `meta.schema_migration` is what stops `fafnir db migrate` re-applying
everything, and without the `mart` views there is nothing for `refresh-marts` to
refresh.

## What you may and may not do

**May, freely** — every read: `monitor.sh`, `systemctl status`,
`list-timers`, `fafnir status`, `fafnir dq list`, `fafnir db status`,
`duk -S db …`, and all the MCP read tools.

**Not `journalctl`, despite the example above.** Reading another unit's journal
needs the `adm` or `systemd-journal` group and `claude` is in neither, so
`journalctl -u fafnir-daily` is a dead end rather than a permission to be asked
for. Use `ops.ingestion_run` for what ran, how long it took and how it ended;
propose the `journalctl` line for the operator when only the journal will do.

**May, with the dry run shown first** — repairs in scope of the flag being worked:
`ingest prices --symbols`, `ingest actions --symbols`, `ingest delisted`,
`ingest symbol-changes`, `adjust --symbol`, `db refresh-marts`, `dq resolve`,
`dq accept`, `dq recheck`, `dq reopen`, `track rm --closed`, `security merge` /
`merge-rename` / `dismiss-rename` / `dedupe`.

**A first load has no `--from` default of its own on a warehouse predating the
2026-09-10 DQ fixes**: `ingest prices` asked FMP with no start date, and the vendor
answered with its own ~5-year window. Always pass an explicit `--from` when
re-backfilling a truncated history, whichever version is deployed.

**Propose, never run** — `scripts/reset_data.sh`, `fafnir db rollback`,
`fafnir db migrate`, `fafnir security descope` (ADR 0012: irreversible, and its dry
run too, since the settings deny the command by prefix), edits to `~/.fafnirrc`,
timer and unit changes,
`systemctl restart/stop`, anything touching `/etc`.

**Never** — edit the deployed checkout at `/opt/fafnir` (rule 7: it is deployed,
not developed; a fix there is invisible to the repo and destroyed by the next
`git pull`). Code changes — a new DQ check, a third OHLC alias, a loader guard —
go to the repository as a branch and a PR, and reach the host only after merging.

Two things deliberately **not** automated, and the reasons, since they will be
suggested:

- **A scheduled agent triage.** Before the queue is understood, a robot closing
  flags on a timer is the same mistake as a robot opening them. Revisit once the
  resolution record has been read by a human for a month.
- **Agent-driven service restarts.** systemd state is an operator decision. Report
  `systemctl status`; do not act on it.

## Upgrading a deployment onto migration 0021

`fafnir db migrate` **fails** on a least-privilege install until the role exists,
because `CREATE ROLE` needs `CREATEROLE` which the migrator does not have. The
error is actionable and this is the same behaviour migration 0001 has for the other
three roles.

Roles are cluster-global, so if another database on the same cluster already has
`fafnir_ops` the migration just works and the statement below is a no-op — "role
already exists" is not a problem to report. One statement, as a superuser, then
migrate normally (both are operator commands: propose, do not run):

```bash
sudo -u postgres psql -d fafnir -c 'CREATE ROLE fafnir_ops NOLOGIN;'
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir db migrate
```
