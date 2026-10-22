# Plan: fafnir-dba skill enhancements from the 2026-10-22 backfill close-out

- **Status: applied.** S1–S6 are in the same PR as this document. The "Code and
  tool enhancements" section is not done.
- **Scope:** what the on-host DBA agent got wrong, nearly got wrong, or had to
  derive while closing out the Sharadar backfill on the host:
  - #76's measurement of 11,927 dead issuers;
  - #77's level checks and the dorq `--full` run;
  - #79's survivorship QA;
  - #80's acceptance;
  - #81's triage of 33,906 outliers on dead histories.
- **Touches:**
  - the skill: `SKILL.md`, `references/sweep-policy.md`,
    `references/outlier-classification.md`, `references/dq-playbooks.md`;
  - the docs: `doc/dorq.md`, `doc/backfill.md`, `doc/ingestion.md`, and the header
    of `scripts/survivorship_check.sql`.
- **Related:** [the 2026-09-15 plan](fafnir-dba-skill-enhancements-2026-09-15.md),
  which this follows in form.

## How this document was reviewed

It is the agent's own account of its session. Before writing any claim into the
skill, it re-read the code the claim depends on:

- **`dq recheck`'s outlier predicate.** It closes a flag when the bar or the
  previous bar is gone, when a split sits on the date, or when the move is no longer
  over the threshold. The by-id query in the sweep policy reproduces it exactly: it
  selected 7,228 dead-history flags + 117 others + 31 held FMP flags, which is the
  recheck's own dry-run total of 7,376.
- **`dq recheck` has no scope.** It takes `--check`, but no security, source or id
  filter.
- **`check_gaps` covers delisted securities.** It judges every security that has
  bars, between its first and last, so 0 gap flags on dead histories is a
  measurement, not a gap in coverage.
- **A full run's file is the night's file.** `full_window` takes the market's
  latest session as its as-of date, and `shadow_path` names the file by that date.
- **`previous_night_start` does not pass over full runs**, while
  `night_run(nightly_only=True)` does.
- **`mint-delisted --allow-overlap` works only with `--permaticker`.**

---

## What the session did

- **Result:** the queue went from 51,277 open conditions to 17,953.
  - 33,324 flags were closed in eight batches: two resolves and six accepts.
  - Each dry run matched a prediction counted from its id file. Each batch was
    approved by the operator and verified in SQL afterwards.
- **Measured and posted:**
  - #76's counts, from the SQL checks and from dorq `--full`;
  - #77's level checks, sizes and timings;
  - #79's survivorship table, with every gap explained;
  - #80's acceptance.
- **Left open, each with a reason:**
  - 699 dead-history outliers that a split or dividend fix may change;
  - the vendor-link families: `vendor_history_overlap` 4,206 and
    `vendor_history_disagrees` 763;
  - 226 `sharadar_split_ratio_mismatch`.

What worked, and is now written down so it is not re-derived:

- **One feature pass over every flag**, chunked by `security_id`, partitioned into
  named classes that were counted back to the total before anything was proposed.
- **Reconciling a by-id "already gone" set** against the recheck's own dry-run
  total.
- **Spot-checking each accept class on the bars.** Every sampled collapse,
  acquisition and distribution matched the company's real event, and two tests
  were tightened.

---

## What cost time or caused mistakes

| # | What happened | Cost | Root cause |
|---|---|---|---|
| 1 | Two `sql_read` results of about 85,000 characters came back inline despite the 4,000-character pad the skill prescribes | Context, and two re-runs | `SKILL.md`'s pad size |
| 2 | The survivorship script timed out through the read role. Its header says `fafnir_ops` can run it | Eleven chunked queries and an off-database join | The script's header |
| 3 | A bare `dq recheck --check outlier` would also have closed 31 FMP wrong-instrument flags held open on purpose, and it cannot be scoped | A by-id query, and a reconciliation against the recheck's dry run | No guidance for a scoped "already gone" set |
| 4 | dorq `--full` would replace the night's shadow file and spoil the next `dq compare`. Neither dorq.md's #77 instruction nor the playbooks said so | An operator decision mid-session; the night's file copied aside | The instruction omitted the side effect |
| 5 | The first liquidating-distribution and cash-acquisition tests let through a drop the dividend explained only half of, and a "flat tail" of zero-volume carry bars | Caught by spot checks; 17 + 8 flags moved out | §2k gave no thresholds |
| 6 | The clean-split-ratio guard pulled 2,879 of 4,961 one-tick moves (1¢ → 2¢ is exactly ×2) | One classification redo | No note that the guard means nothing on ticks |
| 7 | After §2g and §2k, 19,703 outliers had no class the policy could accept, so the operator set a bulk rule | One decision round | The policy expected §2k to cover most outliers. Measured, it covered 1,315 of 33,906, and ticks another 4,961 |
| 8 | 21% of the outliers were already gone: the 10-21 nightly wrote them on a load that had stopped part-way | None (the recheck predicate caught them) | Nothing said to work the flags only after the last load |
| 9 | The agent reported migrations 0033/0034 as unapplied. The operator applied them at 13:47:53, moments after the agent's read | A correction in the next report | Not in the skill: a race with the operator's deploy. Re-read deploy state before reporting it as missing |

---

## Changes applied

### S1. `sweep-policy.md`, "Dead Sharadar histories"

- The measured counts.
- Work the flags only after the last load (#8).
- A new subsection, "Outliers on dead histories: the order that cleared them":
  - the seven dead-history batches;
  - the by-id recheck predicate and its reconciliation (#3);
  - the operator bulk rule, recorded as a decision for this population, not a
    standing licence (#7);
  - what stays open, and why.

### S2. `outlier-classification.md` §2k

- The tests as run, with their thresholds and guards (#5).
- Ticks are exempt from the clean-ratio guard (#6).
- A clean ratio elsewhere is a reason to run §2d, not a split.

### S3. `dq-playbooks.md`

- The `dorq_*` family gains the `--full` caveat (#4).
- The Sharadar quarantine section records its outcome.

### S4. `SKILL.md`

The pad is now 90,000 characters, with the reason (#1).

### S5. The runbook docs

- `doc/dorq.md` (#4):
  - a `--full` run replaces the night's file;
  - it becomes the next night's "previous night";
  - the measured runtime on the host.
- `doc/backfill.md`: #77's sequence, timings and sizes, what differed from the
  issue's plan, and the measured bytes per bar.
- `doc/ingestion.md`: #76's measurement.
- `scripts/survivorship_check.sql`: the header says how to run it (#2).

### S6. This document, and its entry in `doc/index.md`.

---

## Code and tool enhancements (for the server and CLI owners, not the skill)

In rough order of how much they would have saved:

1. **Scope `dq recheck`** with `--security-id`, `--source`, or an ids file. Today
   the by-id query is a re-implementation of `_OUTLIER_SQL`, and it can drift.
2. **`dq recheck --dry-run --list`**, repeated from the 2026-09-15 plan. The 148
   non-dead flags in the recheck's total needed their own query to identify.
3. **`dq run --engine dorq --full --out PATH`**, so a one-off full run is written
   outside the shadow directory. Also have `previous_night_start` pass over full
   runs, as `night_run(nightly_only=True)` already does.
4. **A bulk overlap mint.** 2,581 `vendor_history_overlap` flags disagree with the
   FMP row on every sample, so the repair is a mint with `--allow-overlap`. That
   option takes one `--permaticker` at a time. A `--permatickers-file`, or a
   "disagreeing overlaps only" mode, would make it one reviewed batch.
5. **A sub-dollar floor or a symmetric threshold in `check_outliers`.**
   `|close − prev| / prev > 0.5` flags a one-cent rise from 1¢ but not the fall
   back. 4,961 one- or two-tick flags on dead histories were that, and rises
   outnumbered falls three to one.
6. **`scripts/survivorship_check.sql` could `SET statement_timeout = 0` itself,**
   or be wrapped by a CLI command, so it runs the same way for every role.
