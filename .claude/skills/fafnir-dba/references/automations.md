# Automations: the nightly job, its budgets, and what may be changed

## What runs, and why the order is load-bearing

One scheduled job: `scripts/daily_update.sh`, after US settlement. Incremental and
idempotent — a missed day is caught up on the next run.

```
ensure-horizon → ingest symbol-changes → ingest securities → ingest tracked
  → ingest delisted → ingest prices → ingest actions → adjust --changed
  → db refresh-marts → dq run → status
```

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
scripts/monitor.sh                     # all nine sections; non-zero if any tripped
scripts/monitor.sh disk timers backups # or narrow it
scripts/monitor.sh --quiet             # only what tripped
systemctl list-timers 'fafnir-*'
journalctl -u fafnir-daily -n 200 --no-pager
fafnir status
```

`monitor.sh` sections: `status dq disk timers journal backups runs bandwidth slow`.
It changes nothing, by design — it exists because these checks are spread across
the fafnir CLI, the OS and psql, which makes them easy to half-do at 8am.

## The two budgets

**Bandwidth** — 50 GB/month on the FMP Professional plan:

```sql
SELECT date_trunc('month', started_at) AS m, sum(bytes_downloaded)
  FROM ops.ingestion_run GROUP BY 1 ORDER BY 1;
```

**Requests** — the actual constraint on the nightly window, and dominated by one
step. `ingest actions` ships in `symbol` mode: a full split and dividend history
for every active security, every night. That is ~16,000 requests and about an hour
to capture a few hundred changed rows.

The alternative (`actions_mode = "auto"`, ADR 0007) costs ~2 requests plus a
first-load for anything newly minted and a 1/30 reconciliation slice.

**You may run the probe. You may not flip the switch.**

```bash
sudo -u fafnir fafnir source probe-actions   # 2 + 2N requests, writes nothing
sudo -u fafnir fafnir source probe-fund <SYM>  # only if funds are held
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

On backups, the trap worth knowing: skipping `mart` because it is derived and
`meta` because it is "only bookkeeping" gives a restore that is not a working
warehouse. `meta.schema_migration` is what stops `fafnir db migrate` re-applying
everything, and without the `mart` views there is nothing for `refresh-marts` to
refresh.

## What you may and may not do

**May, freely** — every read: `monitor.sh`, `systemctl status`,
`list-timers`, `journalctl`, `fafnir status`, `fafnir dq list`, `fafnir db status`,
`duk -S db …`, and all the MCP read tools.

**May, with the dry run shown first** — repairs in scope of the flag being worked:
`ingest prices --symbols`, `ingest actions --symbols`, `ingest delisted`,
`ingest symbol-changes`, `adjust --symbol`, `db refresh-marts`, `dq resolve`,
`track rm --closed`, `security merge-rename` / `dismiss-rename`.

**Propose, never run** — `scripts/reset_data.sh`, `fafnir db rollback`,
`fafnir db migrate`, edits to `~/.fafnirrc`, timer and unit changes,
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
sudo -u fafnir fafnir db migrate
```
