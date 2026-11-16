# dorq: the Bayesian data-quality engine

[dorq](https://github.com/rtrimble13/dorq) is a linter for price series. Where the SQL
checks ask whether a move is larger than 50%, dorq asks whether a bar is a **data
error**. It weighs a bad print, a price at the wrong scale, an unreported split, a
history at the wrong date, a feed outage and a real move against each other, and
reports the probability of an error, the evidence, and the repair it suggests.
[ADR 0013](adr/0013-dorq-dq-engine.md) records why it runs beside the SQL checks
rather than instead of them. From dorq 0.13 it judges the financial statements too,
in a run of their own ([Statements](#statements)).

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

The statements run ([Statements](#statements)) needs dorq 0.13.0 or later: the
first release with every check that succeeds a SQL fundamentals check.

## Configure

In `~/.fafnirrc` (all optional):

```toml
[dq]
engine = "both"            # sql (default) | dorq | both -- what `fafnir dq run` runs
dorq_path = "/opt/dorq/bin/dorq"          # or FAFNIR_DORQ_PATH
dorq_config = "/etc/fafnir/dorq.toml"     # dorq's settings; empty: dorq's defaults
dorq_shadow = true         # dorq's flags to dorq_shadow_dir, not the queue
dorq_shadow_dir = "/var/lib/fafnir/dorq-shadow"   # absolute on a host: see below
dorq_work_dir = ""         # a run's working files; empty: dorq-work beside the shadow dir
dorq_lookback_sessions = 260              # bars per security on a nightly run
dorq_threads = 0           # 0: one per core; the output does not depend on it
dorq_backfill_max = 500    # most securities a night's backfill judges (see Window)
dorq_statements = false    # dorq on the statements too: see Statements
```

`dorq_shadow_dir` defaults to `var/dorq-shadow`. Like `log_dir`'s default, it is
relative to the working directory, which suits a checkout and not a host. The
systemd units run from `/opt/fafnir`, so the nightly would write inside the
deployed git checkout. A `fafnir dq compare` run from any other directory would
then look somewhere else and find nothing. On a host, set an absolute path the
service user can write, such as one under its home (`/var/lib/fafnir` in
[install_hetzner.md](install_hetzner.md)). Not one under `/home`: the units run
with `ProtectHome=read-only`.

`dorq_work_dir` is where a run keeps its working files: the calendar, actions,
meta and items files beside dorq's input, and dorq's output before fafnir reads it.
Each run has a directory of its own there, removed when the run ends. The next run
prunes a directory that a killed run left, once it is 6 hours old. Empty, the
default, is `dorq-work` beside `dorq_shadow_dir`, so `/var/lib/fafnir/dorq-work` on
the host. It is on disk on purpose. Under a unit's `PrivateTmp` on a host whose
`/tmp` is a tmpfs, a file in the system temporary directory is memory. That was how
the first statements night on the host ran out of it (see
[Memory](#memory-on-the-host)).

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

   **A night is judged once.** When the market's latest session is a date a run
   already judged (its watermark), a nightly run judges nothing and says which run
   did: no session has loaded since. That is a failed load (2026-10-31: Sharadar's
   SFP answered 503, the night stopped before its prices, and `fafnir-dq` re-judged
   10-29), a market holiday, or `dq run` run a second time. A `--full` run's night
   counts as judged too. The SQL checks still run. `--rerun` judges the night again,
   over the span that night's run used (step 5, **Write**): the deliberate re-run
   after a repair. Skipping costs the next night nothing, since its backfill pass
   starts from the last run that judged a night.

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

   The Sharadar backfill (#77) is both cases at once. `vendor mint-delisted` mints
   thousands of dead issuers in one night, over the limit; `ingest sharadar-prices
   --gap-fill` and `vendor correct` write bars behind the window of securities that
   already had a history. Run `fafnir dq run --engine dorq --full` once after them.

   Two things to know before a `--full` run in shadow mode (both met on the host on
   2026-10-22, #77):
   - **It replaces that night's shadow file.** Its as-of date is the market's latest
     session, the date the night's run already wrote. The next night's `dq compare`
     then counts "new per night" against a whole-history file, so that night's figure
     is meaningless. Copy the night's file aside first. If the figure matters, put
     it back (as `fafnir`) before the next morning's `dq compare`.
   - **It becomes the "previous night" of the next nightly run.**
     `previous_night_start` does not pass over full runs. The next night's backfill
     pass therefore takes what was minted or overridden since the full run started.
     Anything minted or overridden between the night's run and the full run is
     judged only in the full file. Run it before that day's repairs.
3. **Run.** `dorq check --format fafnir --exit-zero`. Each line is an
   `ops.data_quality_flag` row: `check_name` is `dorq_<check>` (`dorq_bad_print`,
   `dorq_scale_shift` …), `record_key` is `{"trade_date": …}`, and `detail` is
   dorq's whole report (`p_error`, `hypotheses`, `evidence`, `suggested_action`,
   `dorq.version`, `dorq.config_hash`).
4. **Filter.** Stale-feed reports (`dorq_stale_feed`, DQ304) on securities that
   are not actively trading, or that nothing feeds (`feed_frozen_on`, ADR 0020),
   are left out. This is the SQL `stale` check's own rule (`is_actively_trading
   AND feed_frozen_on IS NULL`), read when the run is made. dorq cannot tell a delisting
   from a feed outage: on the first shadow night, 1,127 of its 1,195 stale-feed
   reports were on securities fafnir already knew had stopped trading. Their other
   reports are kept. A delisted security's bars still go to dorq, so its history
   is still judged for bad prints, splits and the rest. The run logs how many it
   left out. `dq recheck` applies the same filter, so it closes an open
   `dorq_stale_feed` flag once its security stops trading.

   **Cohort gaps without the series that end.** DQ303 (`dorq_cohort_gap`) counts
   a series that stops before the as-of date as missing on the session after its
   last bar, and dorq's `--meta` has no field that says a series ends. When 3,946
   funds nothing feeds were frozen at their 2026-11-06 bar, every night with 11-09
   in its window reported "4,008 of 15,837 series ... have none on this one ... a
   failed load". A cohort also downgrades each member's DQ304 to info, below what
   the run reports, and names no members. So the false cohort hid real stale
   feeds: 37 thinly traded SFP ETFs that Sharadar stopped sending after 11-06,
   which the SQL `stale` check flagged on 11-11, had no dorq report at warn and
   above. So each cohort gap of a run over the whole universe is judged
   again without the securities fafnir knows end the session before its date: not
   actively trading or frozen (the rule above), with that session as their last
   bar and a bar on at least 19 of the 20 sessions ending there (DQ303 counts only
   the series it expected that day). They come out of the session's expected and
   missing counts, the expected misses shrink in proportion, and DQ303's own
   Poisson test runs at the config's `cohort_min_series` and `cohort_max_tail`
   (from `dorq config show`).
   - A cohort that no longer passes is left out, and counted in `dropped`. The
     11-02 cohort (66 of 15,838) held 11 securities delisted on 10-30; without
     them, 55 against 32.0 expected is not one.
   - One that still passes is kept, its figures and message restated on the rest,
     with dorq's own under `detail.fafnir`. It names its members, as
     `detail.detail.members` (where dorq writes a cohort's, so `dq compare` and the
     queue read them), with `detail.fafnir.members_estimated`. They are fafnir's
     estimate: the near-daily securities, not ended, with no bar on the date and a
     bar on the session before it or after it. Without the 3,954 that end on 11-06,
     11-09 is 54 of 11,883 against 24.1 expected: still a cohort, because the SFP
     ETFs that stopped together are one. It is now reported at error as what it
     is, naming 48 securities (30 of them stopped on 11-06).

   The ended securities' bars still go to dorq, so their history is still judged.
   Not undone: the downgrade itself. The members' stale tails are not in the run's
   output to restore, so a cohort left out leaves them unreported by dorq. The
   fix for that is in dorq: a series-end field in `--meta` that DQ303 honours.
5. **Write.** In shadow mode the rows go to `<dorq_shadow_dir>/<as-of>.jsonl`.
   Otherwise one set-based insert writes them, with the same two guards every SQL
   check carries: a condition already open, or accepted, is not written again. The
   as-of date goes to `ops.load_watermark`.

   A shadow file is named by its as-of date, and a `--rerun` for the same date
   replaces it. `dq compare` reads one file a night, so the re-run judges the
   same night: it takes `--since` from that night's run (the newest successful
   nightly run of the same endpoint for that date) and the same backfill
   securities. On unchanged data its file is byte for byte the first's; after a
   repair, it is a fair re-judgement of the same span. Up to v1.4.0 the second
   run reported only from five sessions before the night's own as-of date, and the
   nightly ran DQ twice (the daily, then `fafnir-dq`), so every night's file was
   the narrower one. DQ now runs once a night, from `fafnir-dq`
   ([operations.md](operations.md)). A second run without `--rerun` writes nothing
   (step 2), so the scheduled run on a night with no new session leaves the judged
   night's file as it stands.

**The run's record.** Each run has a row in `ops.ingestion_run` (`source =
'dorq'`, endpoint `dq-run` or `dq-shadow`), which the MCP `ingestion_runs` tool
shows. It is written before dorq starts, so a run that fails still leaves one:
`status = 'failed'`, with dorq's message in `error_message`, and the watermark
where it was. `started_at` and `finished_at` are the real times. `params` holds:

| Key | What |
|---|---|
| `dorq_version` | the binary's version (null on a failed run; dorq's exit message names it) |
| `dorq_config_hash` | the hash on the run's reports (null when dorq reported nothing) |
| `dropped` | the reports the filter left out, per check: `{"dorq_stale_feed": 1127, "dorq_cohort_gap": 1}` |
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

Measured on the production host (3.8 GB RAM) on 2026-10-22: a `--full` run over all
84.9M bars (5.2 GB through dorq) took **6 min 3 s** at a peak of 668 MB RSS.

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
  dorq 0.8.1 writes none. A cohort gap (DQ303) is keyed on its date alone: there
  is one per session, and the members fafnir estimates for it change as series end.

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
- **Recall on confirmed errors.** With labels, compare measures both engines
  against the faults that were confirmed, not only dorq against the SQL flags.
  The confirmed errors are the `data_error` labels on one security that are about
  the night: their dates overlap the window within the slack, or they are on a
  security the night backfilled. They are scoped exactly as precision is: those
  that applied when the run started, and with `--verified-only`, those verified.
  On each, compare asks:
  - **dorq:** is a report at warn and above on it, matched as precision matches?
  - **The SQL checks:** is an `outlier`, `gap` or `stale` flag on its security,
    detected by the run's start and still open then, dated within the label's
    dates and the slack? A `stale` flag is dated at the feed's last bar, before
    the sessions it is about, so it counts when it spans them: from that bar to
    the day it was closed, or on while it is open.

  Both engines are asked what they were saying when the run started: dorq by the
  night's file, the SQL checks by their open flags. A flag closed before the run
  does not count. A bar fault is often closed by the load that made it. On
  2026-11-10 the vendor filled 28 thin ETFs' missing sessions with zero-volume
  carries of the prior close, and the night's re-check closed their `gap` and
  `stale` flags because a bar now existed. Counting those closed flags gave the
  SQL checks 28 of 28 on the wrong bars, against dorq's 0, three nights after
  neither engine could see them. The statements' recall still counts closed
  flags: a statement flag is closed by a person's judgement, not by a load.

  `found` needs a code the label expects. For an SQL flag, that is one of its
  check's successor codes, as in the overlap table. `any` takes any code. A table
  by the label's kind shows where the confirmations came from. The labels pool
  both engines' confirmations (repairs made on SQL flags, verdicts stated on
  dorq's reports), so neither engine's recall is 1 by construction. This
  mirrors the statements' recall. In `--json` it is `recall` (`labels`, `dorq`
  and `sql`, each with `found`, `any`, `recall` and `recall_any`, and
  `by_kind`). A summary over several nights does not carry it.

  **Why it exists.** The holding recall above is measured on the SQL flags, and
  most SQL `outlier` flags are real moves. Of the 247 in the 2026-11-11 night's
  window, 201 were closed, most accepted as market facts. dorq staying silent on
  those is right, and the holding recall counts it as a miss (14-22% on the
  November nights). This line asks the question the bar's recall clause is after:
  of the faults that were real, how many each engine reported. The bar's text
  below is unchanged; read its recall clause against both lines.

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
| `unit_error` | data_error | expect DQ912 (statements) |
| `sign_error` | data_error | expect DQ913 (statements) |
| `misdated_filing` | data_error | expect DQ931 (statements) |
| `structural_gap` | market_fact | DQ903 (statements): an identity's gap that is the company's structure |
| `fiscal_year_change` | market_fact | DQ941 (statements) |
| `real_zero` | market_fact | DQ915 (statements): the filing has the zero |

A label about statements names its period by `--fiscal-date` (or `--first` and
`--last`, fiscal dates), and `--statement`, `--period-type`, `--version-no` and
`--item` as far as the fault has them. Each one left out covers every value, as in
dorq's label format:

```bash
fafnir dq label add --security-id 752016 --fiscal-date 2024-06-30 \
    --statement income --period-type quarter --item revenue \
    --kind unit_error --verified -m "10-Q p.4: 412.3M; SF1 had 412,300M"
```

A statement kind takes them, and a bars kind refuses them. `dq label list` adds a
`SCOPE` column when a label has one, and `dq label import` reads the four fields.
`dq accept|resolve --label` on a statement flag states its label on the flag's
statement, period, version and item.

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

### Statements

The warehouse never edits a statement value, so its statement labels come from what
does change one, and from how a statement flag was closed (DR-0913):

| From | Label |
|---|---|
| SF1 corrected a value by a power of ten | `data_error`, DQ912 (`corrected_unit`) |
| by a sign | `data_error`, DQ913 (`corrected_sign`) |
| from a zero | `data_error`, DQ915 (`corrected_zero`) |
| beyond tolerance otherwise | `data_error`, any of DQ902, DQ903, DQ904, DQ911 to DQ916, DQ921, DQ922, DQ932 (`corrected_value`) |
| SF1 corrected a filing date | `data_error`, DQ931 (`corrected_filing_date`) |
| a statement flag accepted, SQL or `dorq_*` | `market_fact` on its successor's code (`<check>_accepted`): the filing shows it |
| a statement flag resolved after SF1 corrected its period | `data_error` on its code: added to the correction's label, or `<check>_vendor_fixed` where the correction moved nothing beyond tolerance |
| a row of `ops.dq_label` with a statement scope | as stated |

- **What a correction is.** SF1 corrects a first-reported row, and the loader
  supersedes it and loads the corrected one, with the same version
  (`fundamentals_vendor_correction`). Per version, the row as first loaded is
  compared with the row now current. A move beyond the SQL checks' tolerance
  (0.5% of the larger value, and 1M of the currency) is a label. So is a move by a
  power of ten, a sign or from a zero, at any size. Each kind of move is one label:
  on its item when it moved alone, and on the row when several items moved alike (a
  row stored in thousands). A cent's correction is no label.
- **The scope.** Every statement label names its statement, period type, version
  and item where it has one, with `first` and `last` its fiscal dates (dorq's
  doc/labels.md, *Labels about statements*). An SQL flag's label takes its
  successor's code (the table in [The statements shadow
  period](#the-statements-shadow-period)) and the period, fiscal year or span its
  key names.
- **When it applies.** A correction's label applies until the correction loaded:
  from then on dorq reads the corrected row. An acceptance always applies.
  `verified` follows the closure (`--per-flag` or `--by-rule`), and a correction is
  not marked: nobody checked it, though the vendor stated it. `--verified-only` and
  `dq compare`'s time scoping read them as they read bar labels.
- **Rows as they stood.** `--restore-statements FILE` writes the superseded rows the
  labels describe, in the shape of the statements export (its columns, `close`
  included, in its order). Each replaces the current row of its version. dorq 0.13's
  `--restore` restores bars only; a statements restore is dorq's DR-0911.
- **Kept apart from bars.** A label is about statements when it names a statement
  scope or a DQ9xx code. It judges statement reports only (`dq compare
  --statements`), and a stated statements label never replaces a derived bars label
  on the same dates.

```bash
fafnir dq export-labels --out labels.jsonl --restore-statements statements-before.csv
dorq-eval --labels labels.jsonl --results night.jsonl   # a statements night, as jsonl
```

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
binary has changed since a flag was written, the resolution note says so. A flag on
a statement table is re-run on the statements ([Recheck](#recheck)).

## Statements

dorq 0.13 reads financial statements as a third kind of input, beside bars. Its
DQ9xx checks judge identities, values over time, share counts, the knowledge
timeline, coverage and freshness (dorq's `doc/statements.md`). fafnir runs it on
`core.income_statement`, `core.balance_sheet` and `core.cash_flow_statement` in a
run of its own, after the bars. **The bars run is the same with it or without it:**
the same export, window, history pass, shadow file and run record.

It succeeds thirteen of the fourteen SQL fundamentals checks (the dorq fundamentals
plan, §3). Until the statements' own shadow period has met its cutover criteria (the
plan's §6; DR-0914), its rows go to a shadow directory of their own, and the SQL
checks keep the queue.

### Configure

```toml
[dq]
engine = "both"
dorq_statements = true            # off by default
dorq_statements_config = "/etc/fafnir/dorq-statements.toml"
dorq_statements_shadow = true     # <dorq_shadow_dir>/statements/, not the queue
```

```toml
# /etc/fafnir/dorq-statements.toml
include = ["/opt/dorq/share/dorq/priors/fafnir-statements.toml"]
```

`fafnir-statements.toml` ships with dorq. It holds SF1's identities, the SQL checks'
tolerances (an identity within 0.5% or 1M, the change in cash within 2%, or 10% from
2018), the items that are never negative or never absent, and `[fafnir] table_name`.
With `dorq_statements_config` empty, dorq runs `--isolated`, on its defaults: no
identities, so DQ903 judges none.

### What a run does

```bash
fafnir dq run --engine both               # SQL, dorq on the bars, then the statements
fafnir dq run --statements-only           # dorq on the statements alone
fafnir dq run --statements-only --rerun   # judge the night again, after a repair
fafnir dq run --no-statements             # this run without them
```

1. **Export.** One `COPY` streams every current version (`superseded_at IS NULL`) of
   the three tables into dorq's stdin. It is ordered by security, statement, period
   type, fiscal date and version. Each row carries every line item, empty for another
   statement's, so the three tables share one header. Each row also carries `close`,
   the raw close on or before its `known_from`, for the share-terms checks (DQ921).
   That close is looked up once per security and `known_from`, since the three
   statements of a version share one. It reaches back 92 days at most: a security
   that has not traded for a quarter has no price to sharpen DQ921 with, and the
   reach keeps each lookup to one or two of `core.daily_price`'s yearly partitions,
   not every one before the date. Beside the stream go three files:
   - `ref.statement_line_item` (`--items`), as it is;
   - `core.corporate_action` (`--actions`), as the bars run sends it;
   - the security master (`--meta`):

     | Field | From |
     |---|---|
     | `currency` | `core.security.currency`: DQ944 compares the reporting currency with it |
     | `first_trade_date`, `last_trade_date` | the first and last bar in `core.daily_price` |
     | `active`, `delisted_date` | `is_actively_trading`, `delisted_date` |
     | `sector`, `industry` | `core.v_security_classification`, as readers see it; dorq's profiles can match on `sector` |
     | `statements_expected` | the open Sharadar link names a fundamentals master row with a `first_quarter`, the rule `fundamentals_coverage` uses |
2. **Window.** The whole history, every night, as of the market's latest session
   (`--as-of`). DQ942 judges staleness against that date. There is no `--since`: the
   night's file is dorq's standing view of the statements, as the SQL checks' open
   flags are theirs. **A night is judged once**, as for bars. A second run for a
   session a statements run already judged says which run judged it and does
   nothing; `--rerun` judges it again and replaces its file.
3. **Run.** `dorq check --kind statements --format fafnir --exit-zero --show-info`.
   Info is shown because some of the SQL checks this run succeeds write info flags:
   a gap, a currency, a coverage hole.
4. **Write.** The rows stream from dorq's output file, one at a time, to where
   they go. The night is never held in memory (see [Memory](#memory-on-the-host)).
   In shadow mode the rows go to `<dorq_shadow_dir>/statements/<as-of>.jsonl`.
   That is a directory of its own, because `dq compare` takes the last `*.jsonl` in
   the shadow directory's top level as the newest night of bars. Otherwise the bars'
   ingest writes them, with the same open and accepted guards. Each row's
   `table_name` is its statement's table.

   **An accepted SQL flag carries to its successor.** The guards know only dorq's
   own `check_name` and `record_key`, so a condition the queue accepted under one
   of the thirteen SQL checks' names would be written again under dorq's. About
   164k such flags were accepted by rule (SF1 as published: the ties, the
   identity, the TTM and negative checks, the pre-SF1 gaps). On the 2026-11-11
   night 175,748 reports were on them. So before the guards, the ingest withholds
   a report that a flag of a check it succeeds, accepted at any time, covers. The
   rule is the overlap's (the table under [the statements shadow
   period](#the-statements-shadow-period)), with one more condition for the two
   checks whose flag names several items of its period, `fundamentals_suspicious_zero`
   and `fundamentals_negative_impossible`: it covers a report on one of those items,
   or any report when it names none. A report on another item, or on the whole row,
   is written. A carry withholds a write, so it is held to the SQL check's own
   condition where the overlap's rule is looser: a flag keyed on a `version_no`
   carries only that version (a restated version that still fails is a new
   condition), a `fundamentals_stale` flag only a report on its
   `latest_fiscal_date` (a later filing is a new occurrence), and a
   `fundamentals_ttm_incomplete` flag only an income statement report. A shadow
   night withholds nothing: its file is dorq's whole view, and
   `dq compare --statements` counts what the queue would take. A finding on no one statement goes under
   `core.income_statement`, as the SQL checks file theirs: DQ943 (statements missing)
   and DQ951's cohorts. A date can hold two DQ951 cohorts: the series revised that
   day, and those carrying one finding in rows loaded that day. dorq keys both on the
   date, so fafnir's condition key adds which it is (`cohort`: `revisions` or
   `findings`).
5. **Record.** `ops.ingestion_run`, `source = 'dorq'`, endpoint `dq-statements-shadow`
   (or `dq-statements` into the queue), with its own watermark. The bars run's
   window, history pass and `dq compare` never read these rows. `params` holds:
   - `kind: statements`, `full: true`, `dorq_version`, `dorq_config_hash`;
   - `reports`;
   - `withheld` (into the queue only): the reports an accepted SQL flag carries,
     not written;
   - `bytes` and `bytes_out`: the statements sent and dorq's output;
   - `dorq_max_rss_mb`: dorq's peak resident memory, as the kernel accounts for
     the child;
   - `fafnir_max_rss_mb`: this process's peak so far.

   A failed run leaves `status = 'failed'` and the watermark where it was.
   A run the kernel killed leaves `status = 'started'` and no `finished_at`. Every
   reader of these rows takes only a `success`, so it is harmless.

`fafnir status` prints a `statements` line under the `dorq` line once the run is on
or has run: on or off, shadow or queue, and the newest run's as-of date, reports and
time.

**Runtime.** The plan's target is the whole statements history in 2 minutes or less
on the host, end to end (F4). On a 4-core test machine, with dorq's 6M-row
benchmark (20,000 securities, twenty years) loaded into the three tables:

| Step | Time |
|---|---|
| The `COPY` alone, 2.3 GB of CSV | 50 s |
| dorq alone, from a file, four threads | 44 s at 1.4 GB |
| `fafnir dq run --statements-only`, end to end | 70 s |

Postgres's sort of the wide rows dominates the `COPY`. dorq checks while the rows
stream in, so the two overlap. The benchmark's line items are drawn independently,
so its identities and cash ties fail on nearly every row. dorq's figure is from a
run on its defaults, and the end-to-end one ignores DQ904. Measure it on the host with
`fafnir dq run --statements-only --rerun`. It prints the time and dorq's peak memory:
`(as of <date>, <seconds> s, dorq peak <MB> MB)`. Or read `finished_at - started_at`
and `params.dorq_max_rss_mb` on the run's row. If it is over 2 minutes, the plan's
fallback is to judge only the securities with a row loaded since the previous run.
It is not built: DQ942 has to judge every trading security each night, because a
filing becomes overdue with time, not with a row.

#### Memory on the host

The first statements night on the host (2026-11-07, run 787) was killed by the
kernel's OOM killer 12 minutes in (#219). The `fafnir-dq` unit peaked at 3.1 GB on
a 3.8 GB host where Postgres holds 2 GB of shared buffers. The run then held
dorq's reports three times over:
- in dorq, which holds its results until every series is in while DQ951 is on (as
  a bars run does for DQ303): about 1.7 KB a report, and with `--show-info` the info
  reports too;
- in dorq's output file, in the unit's private `/tmp`, a tmpfs, so in memory;
- in fafnir, which read that file whole into a list: about 6.5 times its size.

Since #219's fix, only dorq's copy remains:
- the output file is on disk, in [`dorq_work_dir`](#configure);
- the night streams from it to the shadow file or the queue's `COPY`, at a few
  hundred KB whatever the night's size;
- the run row and the CLI line record dorq's own peak, which is the figure to
  watch.

The readers of the shadow file stream it too. `dq compare --statements` read the
night whole until 2026-11-10, when the first night that reached a file (run 809:
505,793 reports, 693 MB) killed the compare itself on the host. It now makes one
pass over the file a report at a time, and over the previous night's for the new
conditions; what it holds is the condition keys as hashed integers, the open SQL
flags a report covers by id, the counters, and the severe reports on the
securities the confirmed errors name. `dq sample` reads the file the same way and
holds one record per distinct condition at warn and above. On a night of 20,000
reports the compare peaks at 0.41 times its file's size in Python memory, where
the whole-file read peaked at 9.2 times it; a test holds it under the file's size.

dorq's figure grows with the reports a night holds. What makes it small is the
reports themselves: a statements history that judges clean.

**Cap the unit,** so a run that outgrows the host is killed inside its own cgroup,
never Postgres. A drop-in, with a limit that leaves Postgres its shared buffers
and the night's load its share:

```bash
sudo systemctl edit fafnir-dq.service
# [Service]
# MemoryMax=1200M
```

`systemctl show fafnir-dq.service -p MemoryPeak -p Result` says what the last night
used, and whether it was killed (`Result=oom-kill`). A night killed at the cap fails
the `dq run`, not the load. It leaves the statements' watermark where it was, so
the next night judges again. Turn `[dq] dorq_statements` off until dorq's peak
fits.

### The statements shadow period

```bash
fafnir dq compare --statements                    # the newest statements night
fafnir dq compare --statements --labels-from-db   # with precision
fafnir dq compare --statements /var/lib/fafnir/dorq-shadow/statements/2026-11-0{2,3}.jsonl
fafnir dq compare --statements --json
```

- **Overlap.** These are the SQL flags open when the night's run started, for each
  of the thirteen checks dorq succeeds. For each, the comparison counts how many a
  successor report also covers. A report covers a flag when it is on the same
  security and of a successor code, and agrees with whatever the flag's key names,
  as below:

  | SQL check | dorq | Agrees on |
  |---|---|---|
  | `fundamentals_version_chain` | DQ901 | statement, period |
  | `fundamentals_negative_impossible` | DQ902 | statement, period |
  | `balance_sheet_identity` | DQ903 | the balance sheet, period, identity `balance_sheet` |
  | `cash_flow_tie` | DQ903 | the cash flow, period, identity `cash_flow` |
  | `cash_balance_tie` | DQ904 | period, tie `cash` |
  | `fundamentals_suspicious_zero` | DQ915 | statement, period |
  | `fundamentals_ytd_suspect` | DQ916 | statement, fiscal year |
  | `fundamentals_implausible_filing_lag` | DQ931 | period |
  | `fundamentals_gap` | DQ941 | a fiscal date in the gap's span |
  | `fundamentals_ttm_incomplete` | DQ941 | that fiscal year or the one before |
  | `fundamentals_stale` | DQ942 | the security |
  | `fundamentals_coverage` | DQ943 | the security |
  | `fundamentals_currency_mismatch` | DQ944 | the reporting currency |

  A period is the period type and fiscal date. Per dorq code: its reports, those at
  warn and above, those an open SQL flag covers, and those no SQL check made. Then
  the reports on an SQL flag accepted before the run, and how many of them the
  queue's ingest would withhold (an itemized flag carries only its items; see
  **Write** under [What a run does](#what-a-run-does-1)).
- **Standing.** dorq's conditions at warn and above against the SQL checks' open
  flags at warn and above. The bar is 30% or less.
- **New per night.** The conditions in this night's file absent from the previous
  statements night's, against the SQL flags of the thirteen written between the two
  runs' starts. The bar is 70% fewer.
- **As written to the queue.** Standing and new per night again, on the conditions
  the ingest would write: less those an SQL flag accepted before the run carries.
  The file's figures count conditions the queue has judged already, under the SQL
  check's name. The queue's figures are what the cutover would put in front of a
  person. Both are printed, and in `--json` the queue's are `standing.written`
  and `new_per_night.written`. A night whose conditions are open in the queue
  under dorq's own names (none while the run is in shadow) would be smaller again,
  by the open guard.
- **Precision.** With labels, it is reported at warn and above, at error, and per
  code. Matching is dorq-eval's for a statements label: the same security, the
  statement, period type, version and item agreeing where both name one, and the
  fiscal dates overlapping within the slack (`--slack`, three days). Only labels about
  statements judge statement reports, and they judge no bars report. A label is
  about statements when it names a statement scope or a DQ9xx code.
  `--labels-from-db` reads the warehouse's own (DR-0913).
- **Recall, both engines'.** With labels, the confirmed errors are the `data_error`
  statements labels on one security that apply at the night: the EDGAR sample's
  confirmations of dorq's reports, and the corrections and closures of the SQL
  checks' flags. They pool both engines' confirmations, so neither one's recall is
  1 by construction. On each, the comparison asks:
  - **dorq:** is a report at warn and above on it, matched as precision matches?
  - **The SQL checks:** is a flag of the thirteen at warn and above on it, open or
    closed, detected by the night's start? A flag is placed as `dq export-labels`
    places it: its successor's code, and the period, fiscal year or span its key
    names.

  `found` needs a code the label expects (an SQL flag's is its successor's); `any`
  takes any code. The bar is dorq's recall at least the SQL checks'. A table by the
  label's kind shows where the confirmations came from. A correction's label stops
  applying once the correction loaded, so read recall on an early statements night
  against the labels since: its confirmed errors are the conditions it read.

### The statements cutover

dorq's statement checks take over from the thirteen SQL checks only on evidence, as
ADR 0013 requires for bars (DR-0914, fafnir#221). SF1's nightly delta is about 1,100
rows, too few to judge by, so the judgement is made on a full-history run: every
statements night is one. The operator's steps:

1. **Shadow 20 nights** with `[dq] dorq_statements = true`. A dorq release restarts
   the count only if it changes a statement check's output.
2. **Draw the sample** from one night, and verify each row on EDGAR:

   ```bash
   fafnir dq sample --out sample.csv                 # the newest statements night
   fafnir dq sample <dorq_shadow_dir>/statements/2026-11-20.jsonl --seed 7 --out s.csv
   ```

   The sample is the night's distinct conditions at warn and above, one security
   each: at least `--per-code` of every code (10, or all a code has), then the rest
   of `--size` (200) in an order the seed fixes. The same night and seed give the
   same sample, and a larger `--size` keeps a smaller one's rows. A DQ951 cohort row
   is left out: a cohort is verified as a load, not on a filing. Each row has the
   report's scope and message, the company's 10-K and 10-Q list on EDGAR (from
   `core.security.cik`), and two commands: `confirm` states the report right (a
   `data_error` expecting its code) and `refute` states it wrong (a `market_fact`
   on its code). Both carry `--verified`; finish the note with what the filing
   shows, and add `--yes`.
3. **Judge** against dorq plan §6 and post the aggregates on fafnir#221:
   - `dq compare --statements --labels-from-db --verified-only`: precision on the
     sample, the standing count, new flags per night;
   - `dq compare --statements --labels-from-db` on the first shadow night: precision
     on every correction label, and both engines' recall on the confirmed errors;
   - dorq-eval on the exported labels: the calibration error;
   - `fafnir status`: the night's run time.
4. **Go:** set `[dq] dorq_statements_shadow = false`.
5. **The cutover release** retires the thirteen from `FUNDAMENTALS_CHECKS`, keeping
   `run_all`'s keys at 0 for a release, and closes their open flags by `dq recheck`:
   every one of the thirteen has a recheck rule (`fundamentals_coverage` its own, the
   rest their check's query), so a flag closes only when its condition is gone.

### Recheck

`fafnir dq recheck` re-runs each open `dorq_*` flag by the run that wrote it. A flag
on a statement table is re-run on the whole statements history of its securities,
as of the market's latest session; any other flag is re-run on the bars. A bars
re-run never closes a statement flag, and a statements re-run never closes a bars
flag. A statements re-run sends the metadata of those securities alone, so it does
not take every other security for one with no statements (DQ943). It skips
`dorq_statement_key` (DQ901), which is never auto-resolved: as with
`fundamentals_version_chain`, the repair is made on the host. It also skips
`dorq_revision_cohort` (DQ951), a cohort that a subset of securities cannot
reproduce.
