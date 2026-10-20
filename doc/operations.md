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
ingest delisted → ingest prices → vendor link --new → ingest actions → adjust →
refresh-marts`. `vendor link --new` links tonight's new listings to Sharadar
([ingestion.md](ingestion.md#linking-to-a-vendors-identity)). Like the universe
steps, it warns and carries on if it fails.

Data quality is a second job, `fafnir-dq` (`scripts/run_dq_checks.sh`), which runs
once the daily has finished; the cron example chains it in the same entry. It is
the night's only `dq run`: a DQ failure does not mark the load failed, and its
queue summary goes to its own log. Run DQ once per load: dorq's shadow file is one
per night.

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
night per fund, which is noise against the budget. Requests, not bytes, are the
constraint on the nightly window; see *Switching corporate actions to the incremental
path* below for the step that dominates it.

### Tracking a mutual fund

```bash
fafnir source probe-fund VFIAX        # FIRST -- confirm the NAV series is raw
fafnir track add VFIAX --note "core US equity sleeve"
fafnir ingest tracked                 # mints it (the nightly job also does this)
fafnir ingest prices --symbols VFIAX  # no watermark yet -> full history
fafnir ingest actions --symbols VFIAX && fafnir adjust --symbol VFIAX
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

### Switching corporate actions to the incremental path

`ingest actions` ships in `symbol` mode: a full split and dividend history for every
active security, every night. That is ~16,000 requests and about an hour of the nightly
window to capture a few hundred changed rows, and the only reason it is still the
default is that the alternative has to be verified against your own API key first.

```bash
fafnir source probe-actions                  # 2 + 2N requests, writes nothing
fafnir source probe-fund <YOUR FUND>         # only if you hold funds
```

On `calendar_complete`, set `actions_mode = "auto"` in `[general]`. The nightly job then
costs ~2 requests plus a first-load for anything newly minted and a 1/30 reconciliation
slice, and `daily_update.sh` needs no edit. On any other verdict, leave it alone — the
verdict names what is missing, and a sweep that silently drops a dividend is worse than
an hour of requests.

For the first month, watch the reconciliation:

```sql
SELECT security_id, record_key, detail FROM ops.data_quality_flag
WHERE check_name = 'corporate_action_drift' AND resolved_at IS NULL;
```

An empty queue after a full 30-night cycle means every security has been checked against
the per-symbol feed and the sweep agreed. That is the evidence the switch was right;
until then it is only a reasonable bet. Rows in it name the securities where the calendar
missed something — the data is already repaired, the flag is telling you the sweep cannot
be trusted for that asset type.

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
-- the review queue: 'conflict' only, so a dismissed feed row drops out of it
SELECT old_symbol, new_symbol, change_date, detail FROM core.symbol_change
 WHERE status = 'conflict' ORDER BY change_date DESC;
```

A duplicate that holds *no* prices, actions or factors needs no decision at all —
the sweep folds it into the surviving security by itself and says so in the log.
Everything below is for the conflicts it cannot.

**First, work out which kind you have.** The two look identical in `fafnir status`
and need opposite treatment:

```sql
SELECT f.record_key->>'old_symbol'      AS old_sym,
       f.record_key->>'new_symbol'      AS new_sym,
       (f.detail->>'change_date')::date AS change_date,
       o.cusip AS old_cusip, n.cusip AS new_cusip,
       o.cik   AS old_cik,   n.cik   AS new_cik,
       (SELECT max(trade_date) FROM core.daily_price WHERE security_id = o.security_id)
                                        AS old_last_bar
  FROM ops.data_quality_flag f
  LEFT JOIN core.security o ON o.primary_symbol = f.record_key->>'old_symbol'
  LEFT JOIN core.security n ON n.primary_symbol = f.record_key->>'new_symbol'
 WHERE f.check_name = 'symbol_change_conflict' AND f.resolved_at IS NULL;
```

**Matching CUSIP/ISIN → one company, two rows.** The rename is real; a
security-master load that ran before the rename sweep minted the new ticker as its
own security and the price loader then filled it. Merge the duplicate into the row
that holds the history:

```bash
fafnir security merge-rename GREE VIP --dry-run     # always first
fafnir security merge-rename GREE VIP -m "backfill minted VIP as a duplicate"
fafnir db refresh-marts
```

The old ticker's row survives and keeps its `security_id`; the duplicate is
absorbed and deleted. On the overlap the survivor's bars win. It **refuses** unless
the vendor's identifiers agree and the overlapping sessions agree on OHLC — read
the blockers rather than reaching for `--force`. Adjustment factors are recomputed
for you; the mart refresh is not.

**Differing CUSIP/ISIN, or both tickers still trading → not a rename.** The feed
does emit these: a pre-launch ticker shuffle among securities that all went on to
trade concurrently, or the same change emitted in both directions. Neither can ever
clear itself, because both securities stay live and neither gives up the ticker:

```bash
fafnir security dismiss-rename QAU SCAU \
    -m "pre-launch ticker shuffle; both listed 2026-08-04 and still trading"
```

This changes no price data and merges nothing. It writes the terminal `dismissed`
status (0018), which is what actually stops the nightly retry — resolving the DQ
flag alone would not, because the sweep re-detects the conflict and re-flags it the
same night. A dismissal requires `-m`: it is a judgement about the *feed*, and the
next person needs to know why.

Both commands close the `symbol_change_conflict` flag for you.

If neither fits — you fixed the underlying state some other way — re-run
`fafnir ingest symbol-changes`, which retries every non-terminal row and reaches
`applied` or `ignored` on its own.

The `symbol_change_conflict` DQ flag is raised on the night a conflict is first
seen, not on each nightly retry, so one unresolved rename does not inflate the
open-flag count night after night. `core.symbol_change` is the durable queue; the
flag is only the notification.

## Monitoring

```bash
fafnir status                 # securities, price rows, latest date, open DQ flags, Sharadar
fafnir dq list                # the open queue, by check and severity
scripts/run_dq_checks.sh      # gaps/outliers/freshness + open-flag summary
scripts/monitor.sh            # all of the below in one pass; non-zero if any tripped
```

`scripts/monitor.sh` exists because the checks in this section are spread across
the fafnir CLI, the OS and psql, which makes them easy to half-do at 8am. It runs
the lot — status, the DQ queue, disk, timers, journal, backup age, failed runs,
FMP bandwidth, `pg_stat_statements` — and reports; it changes nothing. Narrow it
with section names (`scripts/monitor.sh disk timers backups`) or `--quiet` to see
only what tripped.

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
- **Sharadar** — once `fafnir ingest sharadar-master` has run, `fafnir status` ends
  with a block for the vendor's own tables. A host that has never loaded them prints
  the lines above unchanged.
  ```
  Sharadar   : stocks 6319 listed, 14692 delisted
               funds 6394 listed, 3612 delisted
               fundamentals 5519 listed, 12347 delisted
               insiders 5593 listed, 6670 delisted
               events 142852; last load 2026-10-19 17:08 (master:actions, run 5, success)
  ```
  - **The counts** are `core.vendor_security` rows by table and delisted flag, and
    the `core.vendor_security_event` row count.
  - **The last load** is the newest `master:*` run. A status other than `success`
    there is a failed load: its `error_message` in `ops.ingestion_run` says why.
- **New listings** — the `New (7d)` line. A week of zero on a working FMP key means
  the security-master step is not running, and the universe is quietly going stale.
- **Freshness** — `fafnir status` latest date should track the last trading day.
  Stale securities raise `stale` DQ flags.
- **Quarantine spikes** — a jump in `ops.data_quality_flag` (warn/error) on a load
  signals a source change or bad data. Investigate the `landing.fmp_raw` payload.
  A load where *every* bar quarantines as `price_missing_or_nonnumeric_ohlc` points
  at FMP renaming the OHLC fields; the loader accepts `open…close` and
  `adjOpen…adjClose`, so a third spelling would need adding to `_OHLC_ALIASES`.
- **Flattened bars** — `price_scale_collapse` counts bars whose OHLC the money
  column's scale crushed to one value. `core.daily_price` money is `NUMERIC(20, 6)`
  and `ROUND_HALF_UP` puts the quarantine cliff at **5e-7**, so a security quoted
  between roughly 5e-7 and 1.5e-6 is not rejected — it is stored with
  `open = high = low = close`, and downstream that is indistinguishable from a
  genuine no-trade day. Returns and volatility computed over a run of them are
  fictional rather than merely wrong. The flag records the source high/low it lost,
  because `core.daily_price` no longer has them:
  ```bash
  fafnir dq list --detail --check price_scale_collapse
  ```
  A security with a long run of these cannot be represented at this scale. Widening
  the column is a full rewrite of every `core.daily_price` partition; excluding the
  security is cheaper and keeps the warehouse honest about what it holds. Resolving
  the flag does neither — it is the measurement, not the decision.
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

## Adding a reader (a person, or an agent)

Per-person and per-agent roles are deployment facts, not schema — migration `0001`
owns the three functional roles and `0021` the fourth, but who is a *member* of
them belongs here ([ADR 0008](adr/0008-remote-duk-access-and-mcp.md),
[ADR 0010](adr/0010-on-host-operations-agent.md)).

Three tiers, and the tier is the whole of the authorization:

| Tier | Group role | Reads | For |
|---|---|---|---|
| app | `fafnir_app` | `mart`, `ref` | `duk` from a laptop, apps, an MCP read profile |
| research | `fafnir_read` | + `core` | notebooks, ad-hoc analysis |
| ops | `fafnir_ops` | + `ops`, `landing`, `meta` | an operations agent on the host |

Adding a person:

```bash
sudo adduser --disabled-password --gecos '' rob
sudo install -d -m 700 -o rob -g rob /home/rob/.ssh
sudo -u rob tee /home/rob/.ssh/authorized_keys <<'KEY'
restrict,port-forwarding,command="/usr/sbin/nologin" ssh-ed25519 AAAA... rob@laptop
KEY
sudo chmod 600 /home/rob/.ssh/authorized_keys
```

```sql
CREATE ROLE rob LOGIN IN ROLE fafnir_app;
ALTER ROLE rob SET default_transaction_read_only = on;
ALTER ROLE rob SET statement_timeout = '60s';
ALTER ROLE rob SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE rob CONNECTION LIMIT 8;

-- The same person's agents (Claude Code on their laptop, an MCP client) get a role
-- of their own: a smaller budget, and NOLOGIN stops them without stopping the
-- person (ADR 0008 §2).
CREATE ROLE rob_mcp LOGIN IN ROLE fafnir_app;
ALTER ROLE rob_mcp SET default_transaction_read_only = on;
ALTER ROLE rob_mcp SET statement_timeout = '20s';
ALTER ROLE rob_mcp SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE rob_mcp CONNECTION LIMIT 4;
```

```
# /etc/postgresql/16/main/pg_ident.conf
fafnirmap  rob  rob
fafnirmap  rob  rob_mcp
```

```
# /etc/postgresql/16/main/pg_hba.conf -- once per installation, not per person,
# and ABOVE "local all all peer": first match wins. Without it that generic line
# admits only a role named exactly like the OS user, so rob connects and rob_mcp
# fails with 'Peer authentication failed'. agent.md adds the same line.
local   all   +fafnir_app   peer map=fafnirmap
```

Then `sudo systemctl reload postgresql@16-main`. Using these roles from Claude Code
on the laptop is [duk.md](duk.md#from-claude-code-on-a-laptop).

No database password is issued at any tier — the SSH key is the credential and
peer authentication over the socket is the mechanism. Revocation has two
independent switches, either sufficient: remove the key, or
`ALTER ROLE rob NOLOGIN` / `REVOKE fafnir_app FROM rob`.

Adding an **agent** is the same shape plus a sudoers rule and an MCP
registration — the whole procedure, with the audit and revocation queries, is
[agent.md](agent.md).

> **Upgrading onto migration `0021`.** It creates `fafnir_ops`, which needs
> `CREATEROLE` the migrator does not have on a least-privilege install, so
> `fafnir db migrate` fails with an actionable error until the role exists. Same
> behaviour `0001` has for the other three. One statement, once:
>
> ```bash
> sudo -u postgres psql -d fafnir -c 'CREATE ROLE fafnir_ops NOLOGIN;'
> sudo -u fafnir /opt/fafnir/.venv/bin/fafnir db migrate
> ```

## Working the DQ queue

`fafnir dq run` writes flags; `fafnir dq list` and `fafnir dq resolve` are how they
get worked. With `[dq] engine = "both"`, dorq runs after the SQL checks and, until
the shadow period ends, writes to `[dq] dorq_shadow_dir` rather than the queue; read
each night with `fafnir dq compare` ([dorq.md](dorq.md)). The two take the **same** selection options, so the workflow is to
narrow with `list` until the page is the problem you mean, then re-run the same
options under `resolve`.

> Running the operations agent ([agent.md](agent.md))? Its playbooks —
> per-`check_name`, with the diagnosis and the repair for each — are
> `.claude/skills/fafnir-dba/references/dq-playbooks.md`, and they are worth
> reading as a runbook whether or not an agent is doing the triage. Its
> resolutions are attributed `resolved_by = 'claude'`, so
> `fafnir dq list --state resolved` shows what it decided and why.

> Starting from a **symbol** rather than from the queue? `duk -S db ls <TICKER>`
> shows that security's open flags alongside its price coverage and corporate
> actions, which is usually enough to tell a real data problem from a real market
> event. It reads `mart` only, so it works from a laptop; it shows counts and the
> bars flagged, but not `detail` or resolution notes — for those, and to resolve
> anything, come back to `fafnir dq list --detail --symbol <TICKER>` on the
> warehouse host ([ADR 0009](adr/0009-mart-is-the-read-seam.md)).

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

### Correcting what the vendor has wrong

When the feed itself is wrong — a duplicate or misdated split, a split it never
reported, a bar that belongs to another instrument — a re-fetch returns the same
thing, and a hand-written `DELETE` comes back on the next load. These commands make
the correction and record it in `ops.operator_override`, which the loaders consult:

```bash
fafnir actions list --symbol KEEX --type split                # ids, dates, source
fafnir actions delete 820263 -m "pre-announced duplicate of the 09-09 split" --dry-run
fafnir actions add --symbol OSCX --ex-date 2026-09-03 --split 1:3 -m "..." --dry-run
fafnir actions redate 3637912 --ex-date 2026-09-03 -m "..." --dry-run
fafnir prices delete --symbol MMSRX --non-session -m "weekend NAV echoes" --dry-run
fafnir prices delete --symbol OSCX --date 2026-09-08 -m "stale pre-split print" --dry-run
fafnir prices shift --symbol WLL --from 2004-01-01 --to 2022-07-01 --days 1 -m "..." --dry-run
fafnir prices rescale --symbol EQC --from 1990-01-02 --to 1997-10-16 --factor 20 \
    --volume-factor 1/20 -m "..." --dry-run
fafnir override list                                          # the record
fafnir override revoke 14 -m "FMP corrected it"               # undo one
```

- **Durable.** A deleted key is set aside by the calendar sweep, the per-symbol pull,
  the reconciliation and the price loader for as long as the override is active. An
  added or re-dated action is stored with `source = operator`; the loaders never
  overwrite it, and the reconciliation does not report it as withdrawn.
- **Recorded.** `--note` is required. A delete keeps the row as it stood in
  `detail`, so nothing is lost that `override list --json` cannot show.
- **Dry run is the real thing, rolled back.** It performs the edit — every refusal
  and constraint included — prints the factor change, and rolls back.
- **Factors are recomputed** in the same transaction. Follow with
  `fafnir dq recheck --check outlier` for the flags the correction settles, then
  `fafnir db refresh-marts`.
- **Deleting a bar on a real session** leaves a hole that `fafnir dq run` reports as a
  gap, and the loader will not refill it while the override stands. Do it only when
  no bar is better than the one stored; non-session bars (`--non-session`) have no
  such cost.
- **Revoking a delete does not write the row back** — run
  `fafnir ingest actions --symbols SYM` or `fafnir ingest prices --symbols SYM --from
  D --to D` to fetch the vendor's current version. A re-date is two overrides;
  revoke both.

#### Bars at the wrong date or the wrong scale

Deleting is the wrong repair for a history whose bars are real prices filed under the
wrong key: FVI and WLL carry every bar one calendar day early (Sunday bars that are
Monday's session, no Fridays), and EQC's pre-1997-10-17 decade is stored at 1/20 of the
traded price. The vendor's payload has the same defect, so a re-fetch changes nothing.

- **`prices shift --days N`** moves every bar in `--from..--to` by N calendar days,
  prices untouched. The dry run prints bars per weekday before and after — a correct
  one-day shift turns `Sun Mon Tue Wed Thu` into five weekdays. A history that is only
  partly misdated shows Fridays in the *before* row; narrow the range. Refused, naming
  the dates: a bar landing on a stored bar outside the range, on a day the venue
  calendar has no session (`--allow-non-session` overrides it; the calendar marks MLK
  Day 1990-1997 closed although NYSE traded), in the future, or outside the venue
  calendar's span altogether — which `--allow-non-session` does *not* override, since
  a date the calendar has never heard of is a mistyped `--days`, not a holiday. Corporate actions stay
  where they are unless `--with-actions`; the dry run lists the ones in the range with
  their weekdays, so you can see whether the vendor misdated them too.
- **`prices rescale --factor F [--volume-factor V]`** multiplies every price (and
  vwap) in the range by F and volume by V, rounded to a share. The dry run prints the
  closes either side of both ends of the range, before and after — a correct factor
  turns both joins into ordinary sessions. Every rescaled bar goes through the price
  loader's own validation; one the loader would quarantine (sub-resolution, out of
  range) or flag (a range the rounding flattens) refuses the whole edit.
- **The fabricated split.** Where the vendor scaled a pre-split era by the ratio and
  also carries a split row to match (HUN 2014: bars 10x with a 10:1 split), the repair
  is both commands: `prices rescale` the era back to as-traded, then
  `actions delete` the split (or keep the split and leave the bars, if the split was
  real and only the bars are wrong — the raw jump on the ex-date tells you which).
  Doing one without the other applies the scale twice in the adjusted series.
- **Durable and undoable.** Each bar becomes a `delete` of the vendor's row (kept
  whole) and an operator `add` in its place (`source = operator`), all sharing one
  `edit` id. The price loader sets aside vendor bars on both dates. `fafnir override
  revoke <any id of the edit>` undoes the whole edit and writes the vendor bars back
  exactly as they stood — unlike a plain delete, whose undo leaves the key empty. A
  `--with-actions` shift is undone with them: the corporate actions it re-dated go
  back to their own ex-dates too, so the adjusted series cannot be left a session out.
- **An operator bar is not a target for other edits.** `prices delete`, a second
  shift or rescale over it, and a `security split-history` range that covers it are
  refused until the edit is revoked; plan one edit from the vendor's bars.
- **Follow with** `fafnir dq recheck --check outlier --check gap` (a shift can open or
  close gaps), then `fafnir db refresh-marts`. Adjustment factors are recomputed in
  the same transaction when the security has corporate actions.
- **History only.** A security still trading whose vendor keeps misdating or
  mis-scaling *new* bars is not fixed by an edit of the past; those arrive on dates
  the edit does not cover.

### Separating two issuers held on one security

A reused ticker arrives from the vendor as one continuous history — Sotheby's to
2019 and a SPAC listed in 2026, both under `BID` — and the price loader stores all
of it on the security that holds the ticker today. The outlier check then flags the
jump where one issuer ends and the next begins, and `sparse_coverage` flags the
years in between. Deleting the old issuer's bars would lose the only copy of a
delisted company's history. Move them instead:

```bash
fafnir security split-history --security-id 6598 --to 2019-10-02 \
    --new-symbol BID --new-name "Sotheby's" -m "..." --dry-run        # read the plan
fafnir security split-history --security-id 423773 --from 2020-11-13 --to 2021-06-10 \
    --into-security-id 7027 --restore-deleted -m "..." --dry-run      # already held
fafnir security split-history --security-id 6598 --into-security-id 900123 \
    --undo -m "wrong boundary" --dry-run                              # take it back
```

- **What moves.** Bars and corporate actions dated `--from`..`--to` (from the first
  bar by default), and every DQ flag about one of those dates. Security-level flags
  (`sparse_coverage`) stay, for `dq recheck` to judge.
- **Where to.** `--new-symbol`/`--new-name` mints a delisted security with
  `source = operator` and a closed ticker period; the live ticker still resolves to
  the source. `--into-security-id` moves into a security that already exists; a
  session it already holds must match on every field — prices, volume and vwap — for
  the split to treat it as the same bar and only remove it from the source. Any
  difference refuses the split, because the source's row is the one that would be
  deleted.
- **Durable.** Each moved key stays suppressed on the source by a `delete` override,
  so a load of the ticker's full history does not put it back. A minted destination
  is never fed: every loader universe excludes `source = operator`, and an explicit
  `ingest prices --symbols` that resolves to one loads nothing. Moved actions are
  stored on the destination as operator rows.
- **`--restore-deleted`** also moves bars in the range that `prices delete` had
  already removed (their rows are in the overrides). Those deletes stay in force.
- **Refused:** a range holding every bar the source has (that is `security merge`
  or a rename), a range that has not finished, one already split off, one holding a
  bar or action an operator re-dated or re-scaled (revoke that edit first — see
  above), and a blank note.
- **Undo** reverses exactly what the split recorded and deletes a minted destination
  left empty. It takes back **one split**: where two disjoint ranges were moved to the
  same destination, it undoes the most recent, so run it again for the one before. It
  is the only way back — `override revoke` on one of the split's own overrides is
  refused, because lifting one key's suppression while the destination keeps its copy
  would leave that session on both securities after the next load. Then run `fafnir dq recheck --check outlier --check gap --check
  sparse_coverage` and `fafnir db refresh-marts`, as after any split.

**The vendor-assisted path (#68).** When Sharadar knows the dead issuer, mint it from
Sharadar rather than as an operator row, so it carries its permaticker and Sharadar
can fill the dates FMP never had. `fafnir vendor overlap` finds these cases before
anything is minted: it flags the FMP security `vendor_history_overlap` (`kind =
dead_issuer`) with the dead issuer's permaticker, the overlapping range, its bar
count and five sampled closes against Sharadar's.

```bash
fafnir vendor overlap --dry-run                       # counts; writes nothing
fafnir vendor overlap                                 # flag, refresh, resolve
fafnir dq list --check vendor_history_overlap --detail
```

Then, for a flag whose samples agree (the FMP row holds the dead issuer's bars):
1. `fafnir vendor mint-delisted --permaticker K --allow-overlap` (#71) mints the dead
   issuer's row, empty, with `source = sharadar` and its link.
2. `fafnir security split-history --security-id <fmp> --from … --to …
   --into-security-id <minted row> -m "..." --dry-run`, then without `--dry-run`, moves
   FMP's bars for that era onto it. The destination is empty, so nothing conflicts,
   and FMP's bars keep `source = fmp`.
3. The Sharadar backfill (#75) then fills only the dates FMP never had.

Do **not** use `--new-symbol` here: it mints an operator row, which no vendor feeds.

Two other cases:
- **The FMP row is the dead issuer** (it is delisted, unlinked, and the samples agree):
  link it with `vendor link accept` instead of minting a second row.
- **The samples disagree:** the bars are another issuer's. Mint the dead issuer with
  `--allow-overlap` and move nothing.

The next `vendor overlap` resolves a flag once its overlap is gone.

### Venue transfers

A company that moves its listing to another exchange and keeps its ticker can
look delisted to the vendor. On 2026-10-05 ET, SUN, SUNC, USAC, DDS and DDT moved
from the NYSE to the Texas Stock Exchange (**TXSE**; never TSX, which is Toronto).
FMP's delisted feed reported each move as a delisting, its screener and profile
called each security inactive on the NYSE, and both froze them. FMP's price feed
went on serving their bars. [ADR 0014](adr/0014-venue-transfer-is-not-a-delisting.md)
records the design.

**What the nightly does now.**

- **The delisting sweep** asks the price feed before stamping a recent delisting.
  If the vendor has bars *after* the date, the security is left trading and
  flagged `security_delisting_contradicted` (warn). Its `detail` holds the feed's
  venue, the bars after the date (count, first, last), and the median volume
  before and after.
- **The price universe** keeps a listed security with a bar in the last 30 days,
  whatever the vendor's `isActivelyTrading` says. When the universe load turns a
  row inactive while its last bar is within 5 sessions, it raises
  `security_inactive_but_trading` (info): often the first sign of a move.

**Deciding a `security_delisting_contradicted` flag.** The bars cannot tell a move
between listed venues from a move to OTC, so the flag waits for you. Until you
decide, the sweep declines that record every night (`pending_decision`), and the
run is marked `partial`. Volume does not decide it either: USAC traded 18% of its
median on its first TXSE day.

1. Look for a public notice of a move: the issuer's investor relations or press
   releases, or the new exchange's listing alerts.
2. **It moved to another listed venue** → `security venue-transfer`.
3. **It left for OTC, or stopped trading** → `security delist`.
4. Neither is clear → leave the flag open. The security keeps loading.

```bash
fafnir security venue-transfer 12441 --venue TXSE --date 2026-10-05 \
    -m "NYSE->TXSE 2026-10-05 (Energy Transfer press release, 2026-09-10)" --dry-run
fafnir security venue-transfer 12441 --venue TXSE --date 2026-10-05 \
    -m "NYSE->TXSE 2026-10-05 (Energy Transfer press release, 2026-09-10)" --yes
fafnir security delist 4321 --date 2025-12-31 \
    -m "Left the NYSE for OTCQX (issuer notice)" --dry-run
fafnir db refresh-marts
```

**What `venue-transfer` does**, whether or not the sweep has stamped the security:

- **The dry run** prints:
  - the row before and after, and the ticker period it re-opens;
  - the vendor's bars after `--date` (one request);
  - the price watermark the next nightly resumes from;
  - the feed records it settles, and the flags it accepts or resolves.
- **The real run:**
  - reverses a stamped delisting, re-opening the ticker period it closed;
  - records the venue as an override the nightly load keeps, with the security
    active, until FMP names a venue other than the one it left (migration 0027);
  - accepts the contradiction flag for each feed record the move settles (the
    stamped delisting, and one dated `--date`), so the sweep declines them
    without asking, and resolves any `security_inactive_but_trading` flag;
  - records an audit row in `ops.ingestion_run` (source `operator`, endpoint
    `security-venue-transfer`).
- **It refuses** a ticker another listed security holds (fold that one in with
  `security merge` first), and one the vendor has no bar for *after* `--date`.
  A move to **OTC** is a delisting here: use `security delist`.
- **Prices load on their own.** The next nightly `ingest prices` resumes from the
  watermark, less the overlap, so the bars since the move arrive before `dq run`
  and raise no `gap` flags.

**An announced move** (OBK, 2026-10-13) needs nothing in advance: the sweep and the
price universe keep it loading. Record it with `venue-transfer --date 2026-10-13`
once the vendor serves a bar after that date, whether or not it has been flagged,
stamped or turned inactive by then.

**A feed record dated otherwise than the move** raises one more flag the night the
sweep sees it. Run `venue-transfer` again, which accepts it, or `dq accept` it with
the same note.

**Checking a night's sweep.** The run records what it declined and why:

```sql
SELECT started_at, status, rows_inserted AS marked, rows_quarantined,
       params->'declined' AS declined, params->'prices_requested' AS requests
  FROM ops.ingestion_run WHERE endpoint = 'delisted-companies'
 ORDER BY started_at DESC LIMIT 5;
```

The `declined` reasons are:

- `known_transfer`: an accepted flag names the record (`venue-transfer` settled it);
- `pending_decision`: an open flag names it, and waits for you;
- `still_trading`: the vendor had bars after the date tonight, and a flag was raised;
- `fetch_failed`: the bars could not be read, so the sweep asks again tomorrow;
- `precedes_first_bar` and `traded_after`: the security's own bars contradict the
  delisting.

## Resolving vendor links

`fafnir vendor link` (the engine, [ingestion.md](ingestion.md#linking-to-a-vendors-identity))
links what it can prove from prices, and flags the rest `vendor_xref_unmatched` or
`vendor_xref_ambiguous`. A person decides those, and the decision is recorded with who made it and
why (#63). Every command takes `--symbol` or `--security-id`. `accept` and `reject` take `--by`
(default: the OS user) and `--dry-run`, and they commit before they print.

```bash
fafnir vendor link show META                    # links, rejections, open flags, candidates
fafnir vendor link show META --evaluate         # + what the engine decides today, with samples
fafnir vendor link accept --security-id 1234 --vendor-key 199059 \
    -m "B shares: CUSIP on the 10-K" --dry-run
fafnir vendor link reject --security-id 1234 --vendor-key 199058 \
    -m "the A shares" --dry-run
```

**`show`** writes nothing. Each candidate's status is one of:
- `linked`: the security's own open link;
- `candidate`;
- `rejected`;
- `no_overlap`: the ticker matches, but the dates never meet;
- `taken`: another security's open link holds the key. The engine still tests it: if it
  agrees, the security is `duplicate_of_linked`, naming the holder.

`--evaluate` prints the sampled closes. They are vendor data: keep them on the host.

**`accept`** opens a `match_method = manual` link:
- **When it starts:** `--from`, defaulting to where the two price histories meet.
- **What it resolves:** the security's open `vendor_xref_*` flags, with `--by` and the note.
- **A rejection of the same pair** is withdrawn.
- **What it refuses:**
  - a key another security's open link holds (a key is one security at a time: reject or close
    that link first);
  - replacing the security's own link when that link is its feed (moving a feed is #98's);
  - a start date on which a closed link to the same key already starts. Pass `--from` after it.
- **Any other open link of the security** is closed the day before the new one starts.

**`reject`** records the pair in `core.security_vendor_rejection`:
- **The engine** never proposes the pair again.
- **An open link to that key** is closed today, unless it is the security's feed.
- **The security's flags stay open:** it is still undecided, and the next `fafnir vendor link`
  looks again without the rejected key. Rejecting one of two `several_agree` candidates is
  usually enough for it to link the other.

Both decisions survive the engine:
- A linked security is outside the engine's scope.
- A rejected pair is never a candidate.
- The engine never moves a link, manual or not.

**Which to use for each flag:**

| Flag reason | Usual decision |
|---|---|
| `several_agree` | Reject the share class or duplicate that is not this security; the engine links the other. Or `accept` the right one when the evidence is outside the prices (CUSIP on a filing). |
| `duplicate_of_linked` | Another row of the linked security's issuer, usually a missed rename: fold it into `linked_security` with `security merge <security_id> <linked_security>` (dry run first). It moves the Sharadar link and any history the linked row lacks, and refuses when an identifier differs (a rename's new CUSIP) or the overlapping bars disagree: in #64 it merged 13 of 319 active duplicates and refused 306, which stay flagged. |
| `key_claimed_twice` | The same, with no live claimant on the vendor's current ticker: fold the rows; the next run links the survivor. |
| `price_disagrees` | Compare `duk -S db ph <SYM>` with `duk -S sharadar ph <SYM>`. A constant ratio is a split on one side only: fix that, then rerun. Noise means a different issuer: `reject`. |
| `no_candidate`, `insufficient_overlap` | Nothing to accept unless you know the key from elsewhere. Then `accept` with the evidence in `-m`. |
| `no_bars` | A shell with no history: `security dedupe`. |
| `vendor_history_disagrees` | Not a link question: the security is linked, and its samples listed in `detail.samples` disagree with Sharadar. The bars are corrected from Sharadar under ADR 0015 (#75), each replaced bar kept in `ops.vendor_correction`. If the link itself looks wrong (`basis` `latest_price` or `identifier`, the confidence low, and the name differs), `reject` the key. |
| `not_covered_asset_type` | A mutual fund (about 4,600 in production); Sharadar does not price them. `dq accept` them, with a note. |

## Reconciliation

`scripts/reconcile.sh AAPL,MSFT,SPY` re-pulls a sample live and diffs against the
warehouse to catch silent drift (re-adjustments, late corrections). Run weekly over
a rotating sample. It reports differences; it does not auto-overwrite.

Corporate actions have their own automatic version of this, and it is not optional
housekeeping: in `calendar`/`auto` mode it is the only thing that can detect a gap in
the market-wide feed. `ingest actions` reconciles 1/`actions_reconcile_buckets` of the
universe against the per-symbol endpoints on every run, repairs what it finds and
raises `corporate_action_drift`. Setting the bucket count to 0 turns that off and
leaves the sweep unverified — do that only while running in `symbol` mode, where there
is nothing to verify.

It compares **raw** closes, and both sides are unadjusted — `core.daily_price` on one
side, FMP's `historical-price-eod/non-split-adjusted` on the other — so a split in
the compared window is not by itself a reason for the two to disagree.

Once a second vendor loads, a vendor that outranks another replaces its rows only in
an explicit correction (ADR 0015), and each one is kept. `fafnir vendor corrections
list` shows them; `fafnir vendor corrections revert ID -m "why"` puts the replaced row
back exactly and recomputes the security's factors. See
[ingestion.md](ingestion.md#who-may-write-a-row-precedence-adr-0015).

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
- **Upgrade the deployed code** — `git -C /opt/fafnir pull`, then reinstall only if
  dependencies changed and `fafnir db migrate` only if `sql/migrations/` gained a
  file. Full procedure, including which of `fafnir --version`, `git describe` and
  `pip show` to trust: [releasing.md](releasing.md). To ask what is deployed right
  now:

  ```bash
  sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir --version   # the code on disk
  sudo -u fafnir -H git -C /opt/fafnir describe --tags       # the commit, e.g. v0.2.0
  ```

  A `v0.2.0-3-gabc1234` from `describe` means three commits past the tag — the host
  is on something between releases, which is worth knowing before you conclude a
  defect is unfixed.

### Descoping an instrument kind

[ADR 0012](adr/0012-warrants-rights-units-out-of-scope.md) takes warrants, rights
and units out of scope. Deploying the release stops new ones being minted.
Removing the ones already held is a one-time operator step, and it is
**irreversible**:

```bash
F="sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir"
scripts/backup_dump.sh                                # 1. a restorable copy first
K="--keep GRP-UN"   # Granite REIT's units: delisted from NYSE 2025-12-31, history kept
$F security descope $K --dry-run > descope-dry-run.txt    # 2. read every line
$F security descope $K -m "ADR 0012" --yes > descope.txt  # 3. one transaction
$F db refresh-marts                                   # 4. drop them from the marts
```

- **Keeping one.** A ticker that still lists is declared once with
  `fafnir track add`, and every descope skips it. A security that no longer lists
  is named with `--keep SYMBOL` on each run instead, because declaring a delisted
  ticker makes `ingest tracked` mint it again as a new security. Both show at the
  top of the dry run (`Kept, declared …` / `Kept, --keep …`). A `--keep` that names
  nothing the run would remove stops the run before anything changes.
- **If `GRP-UN` was declared** under the earlier version of this runbook, undo that
  before the descope: `$F track rm GRP-UN`. If `ingest tracked` has run since, check
  `SELECT security_id, delisted_date FROM core.security WHERE primary_symbol =
  'GRP-UN'`. A second, listed row is the duplicate it minted. Fold it into the
  delisted original with `$F security merge <duplicate_id> <original_id>
  --dry-run`, then without `--dry-run`, before the descope. `--keep GRP-UN` keeps
  every row under the ticker, the duplicate included.
- **What goes with each security:** bars, corporate actions, factors, profiles,
  ticker periods, watermarks, operator overrides and DQ flags. Rename records are
  kept, with `security_id` set to NULL.
- **Renames still in conflict that name one of them are dismissed**, and their
  `symbol_change_conflict` flags resolved. The dry run lists each one (`Unresolved
  rename, dismissed with them: …`). Left open, they could never resolve, or, with
  their blocker gone, would be left to the sweep to decide alone.
- **The audit record** is the `ops.ingestion_run` row with source `operator` and
  endpoint `security-descope`. It names every symbol and id removed, every rename
  dismissed, and what was kept (`kept_declared`, `kept_by_option`).
- **Order matters.** Run this only after the release that stops the minting is
  deployed. Before that, the next nightly load mints the removed securities back,
  with full history. For the same reason, a `--kind` that
  `[general] exclude_instruments` does not exclude is refused.
- **Not while the nightly job runs.** It still loads prices for these securities
  until they are gone, and its inserts collide with the deletes. Either the
  descope fails and rolls back whole, or the night's whole `ingest prices` run
  aborts on the first bar for a security the descope removed. Check
  `systemctl list-timers 'fafnir-*'` and run it in the day.

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

## Ending a vendor subscription

Sharadar's licence (§10) gives 30 days from termination to delete every copy of
its data, and an affidavit if asked ([ADR 0015](adr/0015-multi-vendor-provenance-and-precedence.md) quotes it). The subscription runs
to 2027-10-18; the renewal reminder is 2027-09-18. What ending it would take is
known in advance from one report:

```bash
scripts/reset_data.sh --scope vendor:sharadar --bulk-dir /var/lib/fafnir/landing/sharadar
```

It writes nothing and refuses `--yes`. It prints counts only, so its output can be
kept or posted. It lists:
- the rows Sharadar supplied (`source = 'sharadar'`: bars, actions, the securities it
  minted, their ticker periods);
- the rows that hold its data: its master and events, the links (their `evidence`
  holds sampled closes), the `vendor_*` DQ flags (their `detail` does too), the
  corrections it made, its watermarks, and the file ledger;
- the operator edits on securities it minted, which a person has to decide about;
- the files under `[sharadar] bulk_dir`, and the dumps under `$FAFNIR_BACKUP_DIR`.

**The 30-day checklist,** if the day comes:
1. **Run the report** and keep its output.
2. **Restore FMP's rows.** List the active corrections with `fafnir vendor corrections
   list` and revert them with `fafnir vendor corrections revert ID... -m "..."`. Each
   revert puts back the FMP row Sharadar replaced (ADR 0015 §5).
3. **Delete Sharadar's rows.** There is no command for this yet: #70 built the report
   only, because plan v2 made deletion a licence-event job. Write and review it then,
   in the report's order: the rows it supplied (minted securities last, after the
   operator edits on them are decided), then its master, events, links, rejections,
   flags and watermarks.
4. **Delete the files** under `[sharadar] bulk_dir`. The ledger's rows can go with
   them.
5. **Let the dumps expire, or delete them.** `backup_dump.sh` keeps 14 days by
   default, inside the 30. The off-site copy follows only when
   `FAFNIR_BACKUP_MIRROR=yes` (the default); otherwise delete it there by hand.
6. **Refresh the marts** (`fafnir db refresh-marts`), and delete anything exported
   by hand: CSVs, notebooks, saved `duk` output.
7. **Run the report again.** Every count should read 0.

## Backups

The whole warehouse is rebuildable from `landing` + the sources, but rebuilding
costs hours and FMP bandwidth. Dumps are cheaper:

```bash
scripts/backup_dump.sh              # whole database, custom format, 14-day retention
scripts/backup_dump.sh --globals    # + role definitions (needs a superuser connection)
scripts/backup_offsite.sh           # push the dump directory off the server
```

Dump **every** schema, not a hand-picked subset. Skipping `mart` because it is
derived and `meta` because it is only bookkeeping gives you a restore that is not
a working warehouse: `meta.schema_migration` is what stops `fafnir db migrate`
re-applying every migration on the restored copy, and without the `mart` views
there is nothing for `fafnir db refresh-marts` to refresh.

Scheduling both, and practising the restore, are in
[install_hetzner.md §9](install_hetzner.md#9-backups).

**Sharadar's files are not backed up, by decision.** The dump holds the file ledger,
`landing.sharadar_file`, but not the files it names under `[sharadar] bulk_dir`,
and `backup_offsite.sh` copies only the dump directory. There are two reasons:
- The files are re-downloadable while the subscription lasts, and a fresh
  download is the current one anyway.
- Off-site copies of a vendor's whole tables are the licence's question, not a
  backup's.

What a restore brings back is everything loaded from them: the Sharadar rows are
in `core`, and they are in the dump. After a restore onto a new host, ledger rows
whose files are gone describe nothing; the next bulk download records the files
again. As Sharadar rows reach `core`, the dump grows with `core.daily_price`
(see [backfill.md](backfill.md#disk-budget-with-sharadar)).
