# Plan: fafnir-dba skill enhancements from the 2026-09-15 outlier session

- Status: **applied** — S1–S7 are in `.claude/skills/fafnir-dba/` in the same PR as
  this document. The "Code and tool enhancements" section is not done; two of its
  items are the companion PRs `claude/security-split-history` and
  `claude/prices-shift-rescale`.
- Scope: what the on-host DBA agent got wrong, nearly got wrong, or had to derive
  while working the `outlier` queue from 300 open flags to 166, and the skill
  changes that follow.
- Touches: `SKILL.md`, `references/dq-playbooks.md`, `references/sweep-policy.md`,
  `references/data-semantics.md`, a new `references/outlier-classification.md`, and
  one consistency test.
- Related: [the 2026-09-10 plan](fafnir-dba-skill-enhancements.md), which this
  follows in form.

## How this document was reviewed

It is the agent's own account of its session. Before writing any claim into the
skill it re-read the code the claim depends on:

- The outlier check skips a split only when `ca.ex_date = m.trade_date`
  (`dq/checks.py`), and the recheck's `_OUTLIER_SQL` uses the same equality — so a
  split between two bars is flagged by both, and a resolve is re-written.
- The recheck closes an outlier whose bar **or previous bar** no longer exists
  (`m.close IS NULL OR m.prev_close IS NULL`), which is what closed every delete
  repair.
- The gap check only considers sessions between a security's first and last bar
  (`c.trade_date BETWEEN b.dmin AND b.dmax`), so deleting bars before the first real
  bar creates no `gap` flags.
- `delete_operator_bars` skips a date with no stored bar, and `revoke_operator_override`
  does not write a deleted row back.
- `source probe-prices` prints `bar compared:` and `unadjusted close` for the date it
  matched.

---

## What the session did

- **Result:** 300 open outliers → 166; 134 closed or accepted, in 12 batches of one
  cause each, every one dry-run, approved, and checked in SQL.
- **Repairs:** 2,382 bar deletes, 7 split re-dates, 11 added splits and one re-fetch,
  all recorded as operator overrides.
- **Left open on purpose:** 166, each with a named reason — mostly shapes no command
  can repair yet (date-shifted and rescaled histories, two issuers on one row).

What worked, and is now written down so it is not re-derived:

- A first-pass feature table for every flag, partitioned into named groups that are
  counted back to the total before anything is proposed.
- `dq recheck` as the closing step after every repair.
- Probing the vendor before a delete: GEVX's bad block had been corrected upstream
  and was re-fetched instead.
- Scanning one security's whole history for bars far from their neighbours, so one
  delete batch takes all of them and the note can say there are no others.
- Splitting a history at gaps of more than 60 days to see how many instruments it holds.

---

## What cost time or caused mistakes

| # | What happened | Cost | Root cause in the skill |
|---|---|---|---|
| 1 | A batch of 28 "a split explains this move" outliers held AKR, HUN, JKL, NVGS and NGHT, where the split row matched a fabricated price scale (AKR closes up to 149,613,176) | Caught by a spot check before the dry run was offered | The playbook's decisive test (raw jumps, adjusted smooth) has no plausibility gate |
| 2 | CAPA's 2003–08 bars were deleted as "pre-launch history" after checking only that the 2020–21 part lived elsewhere (QSI). The earlier issuer is held nowhere else | The only copy now lives in `ops.operator_override.detail` | No survivorship check before deleting a segment; `data-semantics.md` said delisted companies are kept but not that a ticker's history can hold several |
| 3 | A batch was predicted to close "11"; the dry run said 14. The listed ids were right, the sum was wrong | A stop, an extra enumeration query, an extra round with the operator | Predictions were added by hand; no rule to compute them |
| 4 | 5 of 12 "misdated splits" were not misdates (two jumps of the split size; a bad bar reverting; thin-trading noise) | Per-case bar reads that should have been the first step | The near-miss rule ("a matching ratio 1–5 days off") is too broad |
| 5 | The playbook said to **resolve** an outlier a real split explains | Would have churned every one of the 23 accepted that day | Written before the exact-ex-date skip was understood as permanent |
| 6 | EQC's ×20 jump passed every unreported-split metric; the bars before it were flat 0.9475 on millions of shares | Caught by reading the bars | The unreported-split test has no "flat pre-bars" exclusion |
| 7 | Most "mis-scaled blocks" were the unpaired halves of spike-and-revert pairs bulk-accepted on 09-10/11, and most windows alternated two or three scales | A classification pass spent rediscovering a class already decided | No guidance to look for the partner flag in any state |
| 8 | Result sets and date lists (up to 2,234 dates, 70 KB) came back inline or had to be forced into files; one near-retype of dates by hand | Context and time | No mechanics section; `sql_read`'s inline/persisted behaviour undocumented |
| 9 | A probe's output was cut with `tail -8`, hiding the closes; all ten probes were re-run | 30 extra vendor requests | Nothing says what the probe prints or which lines matter |
| 10 | A cross-security "same close on the same date" query timed out | One retry, then a name search | The partition guidance covers per-security probes, not cross-security matches |
| 11 | The operator said "run the recheck" while `refresh-marts` and its dry run were still running in the background | Nearly ran a real recheck whose dry run nobody had seen | Rule 2 assumes the dry run finishes in the same turn |

---

## Changes applied

### S1. A new reference: `outlier-classification.md`

The order to sort outlier flags in, with the query for each cause:
- recheck first;
- splits between bars, behind a plausibility gate (#1, #5);
- misdates, with the double-jump exclusion (#4);
- the unreported-split table, with the flat-pre-bars and exchange-ratio cases (#6);
- partner flags in any state (#7);
- the neighbour-median scan;
- one-tick moves;
- new listings carrying earlier issuers, with the survivorship check (#2);
- the shapes to leave open.

Also covered: probe before a delete, and what each batch leaves behind for the next nightly.

### S2. The outlier playbook in `dq-playbooks.md`

- "Resolve when" becomes "accept when" (#5).
- The exact-ex-date skip is stated as the reason.
- The near-miss and unreported-split shapes gain their exclusions.
- Added: one-tick moves, unpaired pair halves, probe-then-re-fetch, the weekday check before `--non-session`, the MLK calendar bug, new listings on reused tickers, and provisional newest bars.
- The "Close with" line names what to leave open.
- `sparse_coverage` gains the survivorship caution and the pre-launch case that recheck closes.

### S3. `sweep-policy.md`

- The outlier precondition gains the plausibility line and the accept disposition.
- Three new batch rules:
  - compute predictions, and enumerate a mismatch with an independent query (#3);
  - the dry run carries the same `--by` and `-m`;
  - destructive repairs require a probe, a whole-history scan and the survivorship check.
- Operator mode gains:
  - a yes that precedes the dry run's output is not a yes to it (#11);
  - bulk classes are accepted whole (#7);
  - notes state what was not checked.

### S4. `SKILL.md`

- A "spot-check a class before accepting it" paragraph (#1).
- A "Mechanics that cost time" section: big results to files, background waits, cross-security timeouts, `ingest prices` has no dry run, counting a delete's dry-run rows (#8–#10).
- The new reference in the list.
- Reporting now names the gap and sparse-coverage flags a delete creates, and says to own a mistake in the report it is found in (#2).

### S5. `data-semantics.md`

Four traps:
- one ticker's history spanning several issuers;
- an "unadjusted" feed that is adjusted, and invented split rows;
- the MLK 1990–1997 calendar bug;
- provisional newest bars.

### S6. A consistency test

`test_every_reference_is_linked_from_the_skill`: a reference file `SKILL.md` never
names is one the agent never opens.

### S7. This document, and its entry in `doc/index.md`.

---

## Code and tool enhancements (for the server and CLI owners, not the skill)

In rough order of how much they would have saved:

1. **Skip a split between the previous bar and the flagged one** in `check_outliers`
   and the recheck's `_OUTLIER_SQL` (`ca.ex_date > prev_bar_date AND ca.ex_date <=
   trade_date`). 23 flags were accepted by hand for exactly this, and new ones arrive
   with every weekend-dated split.
2. **`security split-history`** — move an earlier issuer's bars and actions to
   their own security (companion PR). Unblocks the 17 two-issuer boundaries accepted
   on 2026-09-15 and CAPA's 2003–08 issuer.
3. **`prices shift` and `prices rescale`** (companion PR). Unblocks FVI/WLL (51 flags)
   and the off-scale histories (AKR, HUN, JKL, EQC, probably WZRD and SMUP).
4. **`dq recheck --dry-run --list`**, printing the ids it would close. A total alone
   forced an independent enumeration query on the one mismatch.
5. **`prices delete --from/--to` for session ranges, and `--dates-file`.** The
   pre-launch deletes needed 2,234 `--date` arguments built from a saved query.
6. **`dq_triage` for outliers could return** the adjusted move, a split between the
   bars, the coverage `min_close`/`max_close`, and the partner flag's state — the
   four facts every classification pass had to join in by hand.
7. **A `--brief` mode for `source probe-prices`** printing the vendor's close and
   volume beside the stored bar for a date range.
8. **Place `sparse_coverage` in a sweep tier.** It is still report-only, and a
   delete repair can create one (DRAL).
