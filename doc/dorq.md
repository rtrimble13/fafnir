# dorq: the Bayesian data-quality engine

[dorq](https://github.com/rtrimble13/dorq) is a linter for price series. Where the SQL
checks ask whether a move is larger than 50%, dorq asks whether a bar is a **data
error**. It weighs a bad print, a price at the wrong scale, an unreported split, a
history at the wrong date, a feed outage and a real move against each other, and
reports the probability of an error, the evidence, and the repair it suggests.
[ADR 0013](adr/0013-dorq-dq-engine.md) records why it runs beside the SQL checks
rather than instead of them.

## Install

dorq is a single static binary, and fafnir needs version 0.8.0 or later. Version
0.7.0 refuses its whole actions file over a dividend of zero, which
`core.corporate_action` holds (see [What a run does](#what-a-run-does)). Check a
release archive, unpack it and link it:

```bash
version=0.8.0
base="https://github.com/rtrimble13/dorq/releases/download/v${version}"
curl -fsSLO "${base}/dorq-${version}-linux-x86_64.tar.gz"
curl -fsSLO "${base}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
sudo tar --no-same-owner -xzf "dorq-${version}-linux-x86_64.tar.gz" -C /opt
sudo ln -sfn "/opt/dorq-${version}-linux-x86_64" /opt/dorq
/opt/dorq/bin/dorq --version
```

The archive unpacks to `/opt/dorq-<version>-linux-x86_64`, which is what the link
names. Keep `--no-same-owner`. Run as root, tar otherwise keeps the owner recorded
in the archive. From 0.8.0 that is root, but earlier archives recorded a uid from
the build machine. On the host that uid belongs to whichever local account has it,
and that account could then replace a binary the service user runs with the
warehouse's credentials. To upgrade, unpack the next version beside this one and
move the link. To roll back, move it back.

Or build it from source with the `release` preset (dorq's README). Either way the
binary is `/opt/dorq/bin/dorq`, which is `[dq] dorq_path`'s default.

## Configure

In `~/.fafnirrc` (all optional):

```toml
[dq]
engine = "both"            # sql (default) | dorq | both -- what `fafnir dq run` runs
dorq_path = "/opt/dorq/bin/dorq"          # or FAFNIR_DORQ_PATH
dorq_config = "/etc/fafnir/dorq.toml"     # dorq's settings; empty: dorq's defaults
dorq_shadow = true         # dorq's flags to dorq_shadow_dir, not the queue
dorq_shadow_dir = "/var/lib/fafnir/dorq-shadow"   # absolute on a host: see below
dorq_lookback_sessions = 260              # bars per security on a nightly run
dorq_threads = 0           # 0: one per core; the output does not depend on it
dorq_backfill_max = 500    # most securities a night's backfill judges (see Window)
```

`dorq_shadow_dir` defaults to `var/dorq-shadow`. Like `log_dir`'s default, it is
relative to the working directory, which suits a checkout and not a host. The
systemd units run from `/opt/fafnir`, so the nightly would write inside the
deployed git checkout. A `fafnir dq compare` run from any other directory would
then look somewhere else and find nothing. On a host, set an absolute path the
service user can write, such as one under its home (`/var/lib/fafnir` in
[install_hetzner.md](install_hetzner.md)). Not one under `/home`: the units run
with `ProtectHome=read-only`.

`dorq_config` is dorq's own TOML. dorq ships fafnir's starting configuration at
`/opt/dorq/share/dorq/priors/fafnir.toml`. Point at it from a file of your own,
which the calibration fitted on this warehouse's labels joins later (see
[Calibrate](#calibrate)):

```toml
# /etc/fafnir/dorq.toml
include = ["/opt/dorq/share/dorq/priors/fafnir.toml"]
```

With `dorq_config` empty, dorq runs `--isolated` on its defaults. It never reads
a `dorq.toml` that happens to sit in the working directory.

`dorq config show --config /etc/fafnir/dorq.toml` prints the settings in effect.
It also prints a hash, but that is not the hash a run stamps on its reports.
Every flag carries `detail.dorq.config_hash`, and that hash covers the config file
**and** the options fafnir passes to `dorq check` **and** the calendar's content.
So it changes when `ref.trading_calendar` changes, even with the config file
untouched. `config show` takes only `--config` or `--isolated`, so it prints the
config file's own hash, a different number, and cannot reproduce a run's. On the
host, `config show` printed `c0195518ff64845f` while every row of the first shadow
night carried `119a126869e5c4f0`.

Use the run's hash to compare runs with each other. Two runs with the same hash
were judged under the same settings and calendar. Each run keeps it in
`ops.ingestion_run`, as `params.dorq_config_hash`. To see which config file a run
used, read the command line it logged:

```
dorq: /opt/dorq/bin/dorq check --config /etc/fafnir/dorq.toml - --format fafnir ...
```

`fafnir status` prints a `dorq` line, with its version and mode, once the engine
includes dorq or a binary is installed.

## What a run does

```bash
fafnir dq run --engine both            # the nightly: SQL checks, then dorq
fafnir dq run --engine dorq --full     # dorq over every bar of every security
fafnir dq run --engine dorq --no-shadow   # into the queue (after cutover)
```

1. **Export.** The bars stream from `COPY core.daily_price ... ORDER BY
   security_id, trade_date` straight into dorq's stdin. They are raw bars, as
   ADR 0001 stores them. Beside them go three files:
   - `ref.trading_calendar` for the venue (`--exchange`, NASDAQ by default);
   - `core.corporate_action`, splits and dividends as stored. That includes
     dividends of zero, which the feed sends for funds and stocks alike and the
     schema allows. dorq reports each as `dorq_dividend_implausible` (DQ705) at
     warn: the feed lost the amount, or the event paid no cash;
   - the security master's `asset_type`, exchange and `nav_priced`, by the same
     rule `stale` uses.

   dorq expects raw bars with the actions beside them. That is what lets it tell a
   split that is right from one that is misdated, inverted, missing from the bars
   or applied twice.
2. **Window.** A nightly run reads the last `dorq_lookback_sessions` sessions of
   bars, reports from five sessions before the previous run's as-of date, and
   judges staleness against the market's latest open session (`--as-of`). `--full`
   reads and reports everything. The first run has no previous date and reports
   the whole lookback.

   A second run for an as-of date already judged reports over the same span as
   that night's run, not from five sessions before the night itself. Step 5,
   **Write**, says why.

   **Backfill.** A bar is judged on the nights whose window it falls in. History
   that arrives, or changes, behind the window falls in none. CELG-RI was minted
   on 2026-10-13 with bars back to 2022. The SQL checks have no window: they
   rescan every security's whole history each night, and wrote ten outliers on
   its history that night. dorq never saw those bars. So after the window, a
   nightly run judges the
   **whole history** of each security whose history before the window is new or
   was rewritten since the previous night's run started:
   - minted since (`core.security.first_seen_at`), with bars before the window;
   - an operator override made or revoked since, on a date before the window:
     `prices delete|shift|rescale`, `actions add|delete|redate`, and
     `security split-history` (both sides of it).

   It is the call `dq recheck` makes: those securities' bars, no `--since`. Of its
   reports it keeps those dated before the window (the window's own dates are the
   nightly run's), less cross-sectional rows (a cohort of a few new names means
   nothing) and stale-feed rows (the window judged staleness), and less any the
   night already holds. A re-run of the night measures "since" from the same
   previous night, so it takes the same securities.

   Above `dorq_backfill_max` securities (500 by default; the busiest night since
   2026-09-15 minted 112) the pass is skipped with a warning: that many is a
   universe rebuild, and wants `--full`. A failure in the pass fails the run, so
   the watermark stays where it was and the next night takes the same securities.

   Not caught: `security merge` (and `merge-rename`, `dedupe`), which moves one
   security's bars onto another and leaves no dated record; and a history
   re-pulled for a security that already had one. Run `--full` after either if it
   matters.
3. **Run.** `dorq check --format fafnir --exit-zero`. Each line is an
   `ops.data_quality_flag` row: `check_name` is `dorq_<check>` (`dorq_bad_print`,
   `dorq_scale_shift` …), `record_key` is `{"trade_date": …}`, and `detail` is
   dorq's whole report (`p_error`, `hypotheses`, `evidence`, `suggested_action`,
   `dorq.version`, `dorq.config_hash`).
4. **Filter.** Stale-feed reports (`dorq_stale_feed`, DQ304) on securities that
   are not actively trading are left out. This is the SQL `stale` check's own rule
   (`is_actively_trading`), read when the run is made. dorq cannot tell a delisting
   from a feed outage: on the first shadow night, 1,127 of its 1,195 stale-feed
   reports were on securities fafnir already knew had stopped trading. Their other
   reports are kept. A delisted security's bars still go to dorq, so its history
   is still judged for bad prints, splits and the rest. The run logs how many it
   left out. `dq recheck` applies the same filter, so it closes an open
   `dorq_stale_feed` flag once its security stops trading.
5. **Write.** In shadow mode the rows go to `<dorq_shadow_dir>/<as-of>.jsonl`.
   Otherwise one set-based insert writes them, with the same two guards every SQL
   check carries: a condition already open, or accepted, is not written again. The
   as-of date goes to `ops.load_watermark`.

   A shadow file is named by its as-of date, and a second run for the same date
   replaces it. `dq compare` reads one file a night, so the second run judges the
   same night: it takes `--since` from that night's run (the newest successful
   nightly run of the same endpoint for that date) and the same backfill
   securities. On unchanged data its file is byte for byte the first's; after a
   repair, it is a fair re-judgement of the same span. Up to v1.4.0 the second
   run reported only from five sessions before the night's own as-of date, and the
   nightly ran DQ twice (the daily, then `fafnir-dq`), so every night's file was
   the narrower one. DQ now runs once a night, from `fafnir-dq`
   ([operations.md](operations.md)).

**The run's record.** Each run has a row in `ops.ingestion_run` (`source =
'dorq'`, endpoint `dq-run` or `dq-shadow`), which the MCP `ingestion_runs` tool
shows. It is written before dorq starts, so a run that fails still leaves one:
`status = 'failed'`, with dorq's message in `error_message`, and the watermark
where it was. `started_at` and `finished_at` are the real times. `params` holds:

| Key | What |
|---|---|
| `dorq_version` | the binary's version (null on a failed run; dorq's exit message names it) |
| `dorq_config_hash` | the hash on the run's reports (null when dorq reported nothing) |
| `dropped` | the reports the filter left out, per check: `{"dorq_stale_feed": 1127}` |
| `since`, `shadow`, `full` | the window's `--since`, whether the run went to the shadow, and whether it was `--full` |
| `backfill` | a nightly run's backfill: `securities` (how many), `security_ids`, `before` (the window's first date), `reports` (added). When skipped, `skipped` says why and `security_ids` is empty |
| `holding` | the SQL flags' state as the run found them, for `dq compare`: `since` (how far before the window it reaches) and `states` (`{dq_flag_id: holding\|traded_since\|inactive}` for every `outlier`/`gap`/`stale` flag open at the run's start that compare can fetch). Absent above 20,000 flags |

**Runtime.** dorq checks while the bars stream in, so a run takes about as long as
the export. On a test cluster, 2.3M bars took these times:

| Step | Time |
|---|---|
| Postgres `COPY` of the bars, sorted, on its own | 3.1 s |
| The same `COPY` through `fafnir dq run` into dorq | 4.6 s |
| dorq alone, from a file, four threads | 1.4 s |

At that rate:
- a nightly window of ~8M bars (30,000 securities × 260 sessions) takes about
  16 s;
- a `--full` run over 150M bars takes about 5 minutes, nearly all of it Postgres
  reading and sorting the history.

## The shadow period

Until dorq's flags have earned the queue, they go to the shadow directory. Each
morning:

```bash
fafnir dq compare                                    # the newest shadow night
fafnir dq compare --labels-from-db                   # with precision
fafnir dq compare /var/lib/fafnir/dorq-shadow/2026-10-1{2,3,4}.jsonl --labels-from-db
fafnir dq compare /var/lib/fafnir/dorq-shadow/2026-10-06.jsonl --json
```

`--labels-from-db` is the normal way to give it labels: the warehouse's own, as
`dq export-labels` would write them, read in process. On the host the agent and the
fafnir user share no writable directory, so a file is a detour; `dq export-labels
--out - | fafnir dq compare --labels -` is the pipe if one is wanted. Several files
are several nights, with a summary over their distinct conditions at the end; with
`--json` they come as `{"nights": [...], "all": {...}}`, and one file as before
(every v1.4.2 key, and v1.5.0's beside them).

- **Overlap.** Of the `outlier`, `gap` and `stale` flags keyed in the night's
  window, how many dorq also reports, by its successor codes:

  | SQL check | dorq codes |
  |---|---|
  | `outlier` | DQ2xx, DQ601, DQ7xx |
  | `gap` | DQ301, DQ303 |
  | `stale` | DQ304, DQ501 |

  Also: how many of dorq's reports no SQL check made.

  A flag is "also reported" when a successor report is about its security within
  the slack (`--slack`, three calendar days) of its date. So the flags fetched are
  those keyed from the window's first date less the slack to its last date plus
  the slack: a `stale` flag is keyed on the last bar's date, and DQ304 on the
  first missing session after it, so the two straddle the window's start. Every
  flag on a security the night backfilled is fetched too, at any date. A report
  is about:
  - the dates it spans, and the move a misdated split explains (DQ701 is keyed
    at the split's ex-date, the move at `detail.detail.move_date`);
  - its security, or for a cross-sectional row (no security), each security in
    `detail.detail.members`. dorq 0.9 writes it as a comma-separated string of
    series ids (`"9161,10640,11237"`), because every value in a dorq report's
    `detail.detail` is a scalar; a list is read too. `compare` before v1.4.2 read
    only a list, so v1.4.1's figures under-credit DQ601. dorq up to 0.8.1 does
    not write `members`, so its cohort rows cover no one. A cohort row counts
    once among dorq's reports.

  A flag written after the run is not one the night could have matched, and is
  left out.
- **Holding.** Recall is measured on the flags whose condition still **held**
  when dorq ran: open at the run's start (not resolved or accepted by then), and
  for `stale`, on a security with no bar between its `last_date` and the night,
  and not delisted by then. A `stale` flag stays open until `dq recheck` closes
  it, so a security that has traded since still carries one. On 2026-10-12, 12 of
  the 21 stale flags dorq did not report were like that, and 5 more were closed.
  The flags dorq did not report are counted by why: still holding (a miss),
  closed, traded since, inactive.

  The state is the one the run recorded (`params.holding`, the **holding
  snapshot**), not the one `compare` could compute later. `core.daily_price` as it
  stands later holds bars the night could not have seen: FMP delivered fifteen
  securities' missing bars in the 10-14 load, and the 10-12 night's stale recall
  moved from 23/25 to 8/10 after the fact. The 5-session re-read overwrites
  `loaded_at` and `ingestion_run_id`, so the bars cannot say what was there. A
  run from before v1.5.0 has no snapshot; its states are recomputed as of its
  start, and the output says so.
- **Before the window.** A `stale` flag keyed before the window is a condition
  that may still hold: a feed that stopped on 10-02 is still stopped on 10-14.
  Every `stale` flag open and holding at the run's start is fetched, whatever its
  date, and counted on its own line, against any DQ304/DQ501 report on that
  security dated from the flag's on. dorq up to 0.9 keys DQ304 on the first
  missing session and stops reporting once that date leaves `--since` (XJUL, XAPR,
  XFEB and FPAS on 10-14; VAI never), so the line shows misses until dorq 0.10
  keeps reporting a stale feed while it holds. `gap` flags are not fetched this
  way: a missing session in the middle of a history is a fact about one past
  date, which no nightly window reports again by design, so every open historical
  gap would count as a miss.
- **New flags per night.** The conditions in this night's file absent from the
  previous night's (the newest shadow file dated before it), of which the
  backfill pass's (`params.backfill`: its securities, dated before the window),
  against the `outlier`/`gap`/`stale` flags written between the two runs'
  starts. The cutover bar is 70% fewer. On the host: 10-13, dorq 91 (57 the
  history pass) against SQL 26; 10-14, 33 against 25. A night with no previous
  file says n/a.
- **Conditions.** dorq up to 0.9 keys a cross-sectional row on its date alone, so
  two cohorts on one date (a 2:1 group and a 3:2 group, 2026-10-06) shared a key
  and merged in every count. One rule, `fafnir.dq.dorq.condition_key`, keys them
  apart by the cohort's split `ratio` -- dorq 0.10's own key -- in compare's sets,
  the backfill's dedupe and the queue's `record_key`. Not by `members`: a cohort's
  membership changes as it is re-judged (six series on 10-12, five on 10-13), and
  dorq 0.8.1 writes none.

  A report on a flag the queue *accepted* as a market fact is counted at the
  foot. With `--labels` it counts as false when it is of a DQ2xx code on that
  security and date. A cohort row or misdated split there is unlabelled: labels
  match as dorq-eval's do, not as the overlap does.
- **Precision.** Against the labels, per code, at warn and above, and at error:
  **strict** precision (right / judged) and **fault** precision ((right + other
  fault) / judged) -- DQ701 that found a real duplicate split row under the
  phantom-row code DQ702 is a fault, not a right. A coverage line says how many
  reports any label judged: 3, 12 and 2 of 89/145/116 with v1.4.2's export.
  Matching is dorq-eval's: a fault label within three days, with the code it
  expects, and a market fact on its date. A cohort fact (`series "*"` with
  `members`) covers only the cohort rows naming one of its members.

  A label judges a night only when the night's run started before its
  `applies_until`: a repair label stops applying when the repair was made. The
  WORX rescale of 2026-10-13 18:52 covered 2016-2026, and the 10-13 night ran at
  10-14 03:48, on the corrected bars: v1.4.2 scored nine reports against a repair
  dorq never saw. `--labels-ignore-time` turns this off (calibration-style use);
  `--verified-only` keeps the labels marked verified.

  Several files add a summary over their distinct conditions, each judged by the
  first night that reported it: the report the queue would have written. With the
  77 labels verified that week (hand-20261015.jsonl), the three nights 10-12..14
  were 239 conditions, 45 judged: 0.378 strict / 0.667 fault at warn and above,
  0.636 / 0.727 at error. (238 before the cohort key: two cohorts on 10-06.)

  With `--dorq-eval PATH` (by default `dorq-eval` beside `dorq_path`, when built
  there), dorq's own tool scores the same night against the same labels, and
  compare says where the two disagree, code by code and at error. dorq-eval is
  built with dorq's tests and not installed by default; it reads `dorq check
  --format csv`, so compare writes the night in that shape. Its "warn" row is
  warn only (compare's is warn and above), and it reads no `members`, so a
  cohort fact can differ.

dorq plan §8 sets the cutover bar:
- precision of 0.85 or better at warn, and 0.95 or better at error;
- recall no lower than the SQL checks', on the conditions still holding;
- at least 70% fewer new flags a night;
- an expected calibration error of 0.05 or less.

It must hold over at least 20 sessions. Then set `dorq_shadow = false`.

### Known false positives in dorq ≤ 0.8.1

Two kinds of report are false positives until dorq's next release fixes them.
Count them against dorq, but do not repair anything for them:
- **A cash dividend on file** is ignored by the price model. The ex-date's drop
  is reported as DQ203 (`dorq_unreported_split`) or DQ201 (`dorq_bad_print`):
  about 140 a year, mostly December fund distributions. The tell is a dividend
  on file at that date with `prev_close − dividend ≈ close`.
- **A split on file whose ratio matches the move** is reported anyway when share
  volume does not shift with it, as DQ202 (`dorq_scale_shift`): about 100 a year.
  The tell is `detail.hypotheses.explained_split`.

The cutover waits for the release that fixes both, and its 20 shadow sessions
start after it. The fafnir-dba skill's playbooks say how to work each one.

## Labels

```bash
fafnir dq export-labels --out labels.jsonl --restore before.csv \
    --restore-actions actions-before.csv
fafnir dq export-labels --out - | fafnir dq compare --labels -
```

Write them anywhere but the shadow directory. `dq compare` takes the last
`*.jsonl` there as the newest night, and `labels.jsonl` sorts after the dated
files. `dq compare --labels-from-db` needs no file at all.

These are the warehouse's own decisions, in dorq's label format (dorq's
doc/labels.md). **A label is derived automatically only where the disposition is
unambiguous** -- a repair, an outlier or dorq flag accepted or resolved. An
accepted `gap` can mean "the session had no trades" (a market fact) or "the vendor
has no bar and never will" (a data error), and acceptance alone cannot say which,
so it is not guessed: a person states it (below).

| From | Label |
|---|---|
| a bar deleted (`prices delete`) | `data_error`, any code (`bar_deleted`) |
| a history shifted (`prices shift`) | `data_error`, DQ206 (`date_shift`) |
| an era rescaled (`prices rescale`) | `data_error`, DQ202 (`scale_era`) |
| a history moved to another security (`security split-history`) | `data_error`, DQ205 (`history_split`) |
| a split deleted (`actions delete`) | `data_error`, DQ702\|DQ704 (`split_deleted`): the bars do not move by it -- a phantom, or a duplicate of a real one |
| a split re-dated (`actions redate`) | `data_error`, DQ701 on the **original** ex-date (`split_redated`; `redated_to` names the corrected one) |
| a split added (`actions add --split`) | `context_gap`, DQ203 (`split_added`) |
| a dividend deleted (`actions delete`) | `data_error`, DQ705 (`dividend_deleted`) |
| a dividend added (`actions add --dividend`) | `context_gap`, DQ201\|DQ203 (`dividend_added`): a drop the missing dividend explains |
| an `outlier` accepted, or resolved with the bar unchanged and no repair near it | `market_fact` |
| an `outlier` closed by `dq recheck` once its split was loaded | `context_gap`, DQ203 (`split_loaded`) |
| a `dorq_*` flag accepted | `data_error`, its code |
| a `dorq_*` flag resolved with no repair near it | `market_fact`, its code |
| a row of `ops.dq_label` | as stated |

Only an override whose `reason` is `data_error` is a label. CELG-RI's 770 bars were
deleted on 2026-10-14 because a contingent value right does not belong, not because
a bar was wrong: `override set-reason --reason out_of_scope` says so, and they leave
the export. Every edit command takes `--reason data_error|out_of_scope|cleanup`.

The overrides keep each repaired row as it stood (`detail.row`): `--restore` writes
the bars out so dorq sees the evidence the repair removed, and `--restore-actions`
the deleted and re-dated corporate actions, in the shape of the `--actions` file
`dq run` sends. A re-dated split's corrected row is still on file at `redated_to`;
whoever restores the misdated one drops that one. Left out:
- flags the vendor's own correction closed, since nothing kept the bad bar;
- a re-dated dividend, which no check names;
- the actions a history split or a shift `--with-actions` moved with the bars
  (they are that repair's label);
- gaps, stale feeds and dividends nobody stated a label for.

### When a label applies, and how it was judged

Every label carries:

| Field | What |
|---|---|
| `decided_at` | when the decision was made: the override's `created_at`, the flag's acceptance or resolution, or the stated label's `decided_at` |
| `applies_until` | for a repair (anything from `ops.operator_override`; `split_loaded`, whose split the vendor has since put on file), when the data changed -- after it, dorq reads the corrected data. Null for a judgement that changed no data: it always applies |
| `verified` | `true` for a flag closed `--per-flag` and a stated label marked verified; `false` for one closed `--by-rule` (the 23,903 bulk acceptances of 2026-09-10/11, marked by migration 0028) or a stated label that is not; absent when nobody said |

`dq compare` applies a label to a night only when the night's run started before
`applies_until`. `--verified-only` keeps the verified labels, on `export-labels`
and `compare` alike.

### Labels a person states: `ops.dq_label`

```bash
fafnir dq label add --security-id 601133 --first 2026-10-05 --last 2026-10-13 \
    --kind feed_gap --verified -m "trades daily on stockanalysis; FMP has no bar"
fafnir dq label add --cohort --members 3560,5286,19195 --first 2026-10-06 \
    --kind cohort_fact --verified -m "each checked: three real moves"
fafnir dq label list --since 2026-10-01
fafnir dq label revoke 17 -m "it was the vendor after all"
fafnir dq label import hand-20261015.jsonl --verified --dry-run
fafnir dq accept 893223 --per-flag --label data_error --kind feed_gap -m "..."
```

| Kind | Class | Codes |
|---|---|---|
| `feed_gap` | data_error | expect DQ304 (DQ301 for a run of missing sessions) |
| `odd_lots_only` | market_fact | DQ304 |
| `distribution` | market_fact | DQ705 and DQ2: two labels |
| `dividend_units` | data_error | expect DQ705 |
| `wrong_instrument` | data_error | expect DQ202\|DQ205 |
| `bad_print` | data_error | expect DQ201 |
| `cohort_fact` | market_fact | DQ601, on `*` with `members` |

Another kind is free text and needs `--class`. `dq accept|resolve --label CLASS
[--kind K] [--expect X] [--codes X]` states one per closed flag, on its security and
date, in the same transaction as the closure; a dorq flag's own code is the default.
`dq reopen` revokes it. `import` reads dorq's JSONL; its dry run counts by class and
kind, and a series that is not a security refuses the file. In the export a stated
label wins over a derived one on the same security, overlapping dates and class. An
agent may add, revoke or import after showing the dry run and getting a yes, as for
`dq accept`.

What makes a good label: checked on the bars or an outside source, and the note
says which; one decision, never a bulk class; a repair's label expires at the
repair; a scope removal is not an error.

## Calibrate

dorq fits its priors to the labels. It scores the moves it fits on exactly as
`dorq check` would (dorq's doc/calibration.md), so give it the context `dq run`
gives `dorq check`: the venue calendar, the corporate actions and the security
metadata. Without `--actions`, the splits on file cannot explain the jumps they
cause, and the price model would be fitted on different evidence from what
production judges with. The second step below writes the three files with
`dq run`'s own code, so they match what the nightly sends:

```bash
psql -c "\copy (SELECT security_id, trade_date, open, high, low, close, volume
               FROM core.daily_price ORDER BY security_id, trade_date)
         TO 'bars.csv' CSV HEADER"
/opt/fafnir/.venv/bin/python - <<'EOF'   # calendar.csv, actions.csv, meta.csv
from pathlib import Path
from fafnir.config import FafnirConfig
from fafnir.db.connection import Database
from fafnir.dq.dorq import export_context

with Database(FafnirConfig().dsn) as db:
    export_context(db, Path.cwd(), "NASDAQ")
EOF
dorq calibrate bars.csv --labels labels.jsonl --restore before.csv \
     --config /etc/fafnir/dorq.toml \
     --calendar-file calendar.csv --calendar-exchange NASDAQ \
     --actions actions.csv --meta meta.csv \
     --holdout 0.3 --out /etc/fafnir/dorq-calibration.toml
# then, in /etc/fafnir/dorq.toml:
#   include = ["/opt/dorq/share/dorq/priors/fafnir.toml", "dorq-calibration.toml"]
```

Do not pass `--clean-unlabelled`. It counts every scored move that no label
matches as a market fact, and dorq's doc keeps it for data where every fault is
labelled, as with dorq-synth. The warehouse's labels are not that complete.

The `--holdout` figures are the ones to quote in the go/no-go. The fit changes
`detail.dorq.config_hash`, so every flag records which calibration wrote it.

## Working dorq flags

The `dorq_*` entries in the fafnir-dba skill's `dq-playbooks.md` say how to read a
flag's evidence and which repair each code maps to. `sweep-policy.md` places them
in tiers. Three are never auto-resolved:
- `dorq_scale_shift`;
- `dorq_split_without_jump`;
- `dorq_split_double_applied`.

`fafnir dq recheck` re-runs dorq over the full history of the securities with open
dorq flags, and closes each one dorq no longer emits. It skips the never-resolve
three and the cross-sectional `dorq_cohort_gap` and `dorq_cohort_move`. When the
binary has changed since a flag was written, the resolution note says so.
