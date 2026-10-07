# dorq: the Bayesian data-quality engine

[dorq](https://github.com/rtrimble13/dorq) is a linter for price series. Where the SQL
checks ask whether a move is larger than 50%, dorq asks whether a bar is a **data
error**. It weighs a bad print, a price at the wrong scale, an unreported split, a
history at the wrong date, a feed outage and a real move against each other, and
reports the probability of an error, the evidence, and the repair it suggests.
[ADR 0013](adr/0013-dorq-dq-engine.md) records why it runs beside the SQL checks
rather than instead of them.

## Install

dorq is a single static binary. Unpack a release archive and link it:

```bash
version=0.7.0
curl -fsSLO "https://github.com/rtrimble13/dorq/releases/download/v${version}/dorq-${version}-linux-x86_64.tar.gz"
sudo tar -xzf "dorq-${version}-linux-x86_64.tar.gz" -C /opt
sudo ln -sfn "/opt/dorq-${version}" /opt/dorq
/opt/dorq/bin/dorq --version
```

Or build it from source with the `release` preset (dorq's README). Either way the
binary is `/opt/dorq/bin/dorq`, which is `[dq] dorq_path`'s default.

## Configure

In `~/.fafnirrc` (all optional):

```toml
[dq]
engine = "both"            # sql (default) | dorq | both -- what `fafnir dq run` runs
dorq_path = "/opt/dorq/bin/dorq"          # or FAFNIR_DORQ_PATH
dorq_config = "/etc/fafnir/dorq.toml"     # dorq's settings; empty: dorq's defaults
dorq_shadow = true         # dorq's flags to var/dorq-shadow/, not the queue
dorq_shadow_dir = "var/dorq-shadow"
dorq_lookback_sessions = 260              # bars per security on a nightly run
dorq_threads = 0           # 0: one per core; the output does not depend on it
```

`dorq_config` is dorq's own TOML. dorq ships fafnir's starting configuration at
`/opt/dorq/share/dorq/priors/fafnir.toml`. Point at it from a file of your own,
which the calibration fitted on this warehouse's labels joins later (see
[Calibrate](#calibrate)):

```toml
# /etc/fafnir/dorq.toml
include = ["/opt/dorq/share/dorq/priors/fafnir.toml"]
``` Without it dorq runs `--isolated` on its
defaults, never on a `dorq.toml` that happens to sit in the working directory.

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
   - `core.corporate_action`, splits and dividends as stored;
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
3. **Run.** `dorq check --format fafnir --exit-zero`. Each line is an
   `ops.data_quality_flag` row: `check_name` is `dorq_<check>` (`dorq_bad_print`,
   `dorq_scale_shift` …), `record_key` is `{"trade_date": …}`, and `detail` is
   dorq's whole report (`p_error`, `hypotheses`, `evidence`, `suggested_action`,
   `dorq.version`, `dorq.config_hash`).
4. **Write.** In shadow mode the rows go to `var/dorq-shadow/<as-of>.jsonl`.
   Otherwise one set-based insert writes them, with the same two guards every SQL
   check carries: a condition already open, or accepted, is not written again. The
   run is recorded in `ops.ingestion_run` (`source = 'dorq'`, endpoint `dq-run` or
   `dq-shadow`), and its as-of date in `ops.load_watermark`.

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
fafnir dq compare --labels labels.jsonl              # with precision
fafnir dq compare var/dorq-shadow/2026-10-06.jsonl --json
```

- **Overlap.** Of the `outlier`, `gap` and `stale` flags keyed in the night's
  window, how many dorq also reports, by its successor codes:

  | SQL check | dorq codes |
  |---|---|
  | `outlier` | DQ2xx, DQ7xx |
  | `gap` | DQ301, DQ303 |
  | `stale` | DQ304, DQ501 |

  Also: how many of dorq's reports no SQL check made.
- **Precision.** Against the labels, the share of dorq's reports that were real
  data errors, at warn and at error. Matching is dorq-eval's: a fault label within
  three days, with the code it expects, and a market fact on its date.

dorq plan §8 sets the cutover bar:
- precision of 0.85 or better at warn, and 0.95 or better at error;
- recall no lower than the SQL checks';
- at least 70% fewer new flags a night;
- an expected calibration error of 0.05 or less.

It must hold over at least 20 sessions. Then set `dorq_shadow = false`.

## Labels

```bash
fafnir dq export-labels --out labels.jsonl --restore before.csv
```

These are the warehouse's own decisions, in dorq's label format (dorq's
doc/labels.md):

| From | Label |
|---|---|
| a bar deleted (`prices delete`) | `data_error`, any code |
| a history shifted (`prices shift`) | `data_error`, DQ206 |
| an era rescaled (`prices rescale`) | `data_error`, DQ202 |
| a history moved to another security (`security split`) | `data_error`, DQ205 |
| a split added (`actions add --split`) | `context_gap`, DQ203 |
| an `outlier` accepted, or resolved with the bar unchanged and no repair near it | `market_fact` |
| an `outlier` closed by `dq recheck` once its split was loaded | `context_gap`, DQ203 |
| a `dorq_*` flag accepted | `data_error`, its code |
| a `dorq_*` flag resolved with no repair near it | `market_fact`, its code |

The overrides keep each repaired bar as it stood (`detail.row`), and `--restore`
writes those rows out so dorq sees the evidence the repair removed. Left out:
- flags the vendor's own correction closed, since nothing kept the bad bar;
- corporate-action deletes and re-dates;
- gaps.

The export is a starting set. The labelling pass (dorq plan DR-0701) reviews it
and adds what the tables cannot say on their own.

## Calibrate

dorq fits its priors to the labels:

```bash
psql -c "\copy (SELECT security_id, trade_date, open, high, low, close, volume
               FROM core.daily_price ORDER BY security_id, trade_date)
         TO 'bars.csv' CSV HEADER"
dorq calibrate bars.csv --labels labels.jsonl --restore before.csv \
     --config /etc/fafnir/dorq.toml --holdout 0.3 --out /etc/fafnir/dorq-calibration.toml
# then, in /etc/fafnir/dorq.toml:
#   include = ["/opt/dorq/share/dorq/priors/fafnir.toml", "dorq-calibration.toml"]
```

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
