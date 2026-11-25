# Plan: dorq efficacy

- Status: **in progress** (approved 2026-11-23). Phase 0's code is merged; its
  baseline audit (DE-05) is open. Tracking:
  [#267](https://github.com/rtrimble13/fafnir/issues/267).
- dorq's side of the plan, with the code-level findings, is in the dorq repository at
  `doc/plans/dorq-efficacy-plan.md` (tracked in
  [rtrimble13/dorq#40](https://github.com/rtrimble13/dorq/issues/40)). This file is fafnir's side: the success bar, the
  decisions, the integration and measurement work, and the gate.
- It comes from a review of dorq v0.14.0 (commit e013694) against fafnir v3.2.0 and
  the shadow nights 2026-11-06 to 11-20.

## Goal

dorq replaces fafnir's SQL data-quality checks in a domain (bars, statements) only
when it is **significantly better in both precision and recall**. The measure is an
audit that neither engine selected.

**dorq stays a general-purpose data-quality tool.** Nothing in it is customised for
fafnir. Anything that is fafnir's lives here, in fafnir's code, or in `/etc/fafnir`
(configuration, profiles, priors files). An agent working on a dorq item builds the
general capability; an agent working on a fafnir item uses it.

## Success bar (decided 2026-11-23)

Per domain, on the audit sample (DE-03, DE-04):

| Measure | Bar |
|---|---|
| Precision at warn and above | ≥ 0.85 (the plan's §8) |
| Precision at error | ≥ 0.95 |
| Recall on confirmed errors | ≥ SQL's, one-sided 95% paired test, on at least about 100 confirmed errors |
| Queue volume, as written, new per night and standing | ≤ 30% of SQL's, both after the queue's guards and withholding |
| Calibration (expected calibration error of `p_error`) | ≤ 0.05 |

Until a domain meets the bar on two consecutive audit cycles, dorq stays in shadow
there. **The statements cutover (#221, PR #241) waits for the gate (#276).**

## Decisions (operator, 2026-11-23)

1. **The bar.** As above.
2. **A vendor value that faithfully reproduces the filing is not a data error**, even
   when it breaks an accounting identity or a tie. This matches the by-rule accepts of
   10-30 and 11-11. dorq's identity and tie checks report at warn only with positive
   evidence of a stored value that differs from its source (dorq DE-11).
3. **Isolated missed sessions on ultra-thin series** (a vendor skipping a day on which
   an ETF traded 9 shares) need not be caught by dorq alone. The same miss across many
   series of one feed must be caught (dorq DE-21). A second-source check (DQ8xx) is
   deferred.
4. **A newest-bar verdict may wait 1 to 3 nights at info.** The exception is evidence
   that does not depend on the bars after (dorq DE-13). fafnir re-judges the last 5
   sessions each night (`SINCE_OVERLAP_SESSIONS`).
5. **dorq's input may grow, and its record keys may change.** The new inputs:
   - per-bar `source` and load date;
   - `--meta` group keys, `peer_group` and `leverage`.

   Each change ships behind a minor version, with fafnir's change in lockstep.
6. **Claude verifies every label; there is no human spot check.** The safeguards are
   in DE-03:
   - every label cites its outside source and the value read;
   - no label is judged from either engine's output;
   - a blind second verification by an independent agent on a random 10% of each
     cycle, with the cycle re-verified when agreement falls below 90%;
   - no personal data in any outside request.
7. **Access to `rtrimble13/dorq`.** The fine-grained token was extended to the dorq
   repository (contents, issues, pull requests) on 2026-11-23.
8. **Order.** Measurement, then precision, then recall, then depth. Re-measure on the
   audit after each phase.

## What the review found

### Measured, 2026-11-23

| | dorq | SQL |
|---|---|---|
| Bars, precision on verified labels (5 nights, 106 of 225 judged) | warn+ 0.67, error 0.98. DQ301 1.00 and DQ304 0.98; every judged DQ201, DQ203, DQ107 and DQ705 false | n/a |
| Bars, recall on confirmed errors | 8/37 (11-13); 0/9 (11-19, 11-20) | 37/37; 6-7/9 |
| Bars, new flags against SQL | −70% (11-20) | |
| Statements, standing warn+ | 225,995 (104,365 as written) | 213 (after ~164k accepts) |
| Statements, EDGAR-verified precision (~200 conditions) | ~0.40 (DQ941 0.00, DQ904 0.21) | n/a |
| Statements, recall on confirmed errors excluding ytd | 83/95, partly sampled from dorq's reports | 31/95 |
| Statements, ytd labels | 17/821 | 821/821, by construction |

**Too small and too circular to decide anything.** The judged sets are tiny: last
night 0 of 91 bar reports could be judged. Most labels derive from SQL dispositions,
which makes SQL's recall circular. And 35% of dorq's bar warn+ (DQ403, DQ705, DQ107)
has never been judged at all.

### Why dorq is not yet better: four general mechanisms

Detail and file:line evidence are in the dorq plan.

1. **It reports, at warn, hypotheses the data cannot yet decide.**
   - On the newest bars, a bad print cannot be ruled out until 5 bars later. Example:
     the 13 ProShares ETFs that split ex 11-19 with no split row were reported as bad
     prints, a repair that would delete correct bars.
   - A statement identity break with no evidence either way is an error at p 0.912.
   - Cash ties and period gaps use a fixed p 0.85. These two codes are 61% of the
     statement volume.
2. **Cross-sectional evidence flows one way.**
   - The cohort test sees only near-daily series and only downgrades its members. So
     a vendor's delivery gap across thin ETFs is missed: most of the bar recall gap.
   - Peer splits skip the newest bars.
3. **Persistence is judged too locally.** Statement breaks are judged against ±2
   periods. Price moves share one prior set across leveraged ETFs, micro caps,
   closed-end funds and large caps.
4. **Fixed heuristics that calibration cannot reach:** DQ403, DQ107, DQ705, DQ902,
   DQ904, DQ941. Calibration also ignores `verified`.

### Integration defects on fafnir's side

- **The history pass misses rewritten histories.** `backfill_securities` selects by
  operator overrides, so `vendor correct`, merges and re-pulls are never re-judged. On
  11-20, 351 rewritten securities were not, and dorq missed the seams they left
  (#273).
- **`--meta` carries four fields.** dorq's fund-family evidence (`peer_group`),
  `tick_size` and `--market` are supported but unused (#274).
- **The statements nightly streams ~283k info reports** (`--show-info`, 87k of them
  DQ919) that the queue never keeps (#271).
- **The statements comparison is uneven.** dorq's figure counts before withholding;
  SQL's counts after ~164k accepts (#269).

## The plan

| Phase | Key | Repo | Item | Issue |
|---|---|---|---|---|
| 0 Measurement | DE-01 | dorq | Survey-weighted evaluation and a two-engine comparison (dorq-eval, calibrate) | [dorq#41](https://github.com/rtrimble13/dorq/issues/41) |
| | DE-02 | dorq | Calibration honours `verified`; one map per code | [dorq#42](https://github.com/rtrimble13/dorq/issues/42) |
| | DE-03 | fafnir | An engine-independent audit sample, verified by Claude, with weights | [#268](https://github.com/rtrimble13/fafnir/issues/268) |
| | DE-04 | fafnir | `dq compare --audit`: the gate as decided | [#269](https://github.com/rtrimble13/fafnir/issues/269) |
| | DE-05 | fafnir | Baseline audit before any model change | [#270](https://github.com/rtrimble13/fafnir/issues/270) |
| 1 Precision | DE-11 | dorq | A series-level model for identity and tie breaks; "unexplained" is not an error by default | [dorq#43](https://github.com/rtrimble13/dorq/issues/43) |
| | DE-12 | dorq | One condition, one report | [dorq#44](https://github.com/rtrimble13/dorq/issues/44) |
| | DE-13 | dorq | Provisional findings are pending | [dorq#45](https://github.com/rtrimble13/dorq/issues/45) |
| | DE-14 | dorq | The heuristic bar checks (DQ403, DQ107, DQ705) become models or info | [dorq#46](https://github.com/rtrimble13/dorq/issues/46) |
| | DE-15 | fafnir | Statements nightly input | [#271](https://github.com/rtrimble13/fafnir/issues/271) |
| | DE-16 | fafnir | Adopt dorq's per-condition record keys | [#272](https://github.com/rtrimble13/fafnir/issues/272) |
| 2 Recall | DE-21 | dorq | Cross-sectional evidence in both directions, with configurable group keys | [dorq#47](https://github.com/rtrimble13/dorq/issues/47) |
| | DE-22 | dorq | Optional per-bar `source` and load-date columns: seams and load cohorts | [dorq#48](https://github.com/rtrimble13/dorq/issues/48) |
| | DE-23 | dorq | DQ916's recall on quarters stored year to date (17/821) | [dorq#49](https://github.com/rtrimble13/dorq/issues/49) |
| | DE-24 | fafnir | The history pass selects by `loaded_at` | [#273](https://github.com/rtrimble13/fafnir/issues/273) |
| | DE-25 | fafnir | `peer_group`, `tick_size` and `--market` | [#274](https://github.com/rtrimble13/fafnir/issues/274) |
| | DE-26 | fafnir | Per-bar source and `loaded_at` in the export | [#275](https://github.com/rtrimble13/fafnir/issues/275) |
| 3 Depth | DE-31 | dorq | Compare like vintages (DQ932, DQ904, DQ903) | [dorq#50](https://github.com/rtrimble13/dorq/issues/50) |
| | DE-32 | dorq | A class-conditional price model; `leverage` in `--meta` | [dorq#51](https://github.com/rtrimble13/dorq/issues/51) |
| | DE-33 | dorq | Heavier real-change tails in share counts; DQ922 once per period; the DQ931 lag floor | [dorq#52](https://github.com/rtrimble13/dorq/issues/52) |
| | DE-34 | dorq | DQ941 learns the issuer's cadence | [dorq#53](https://github.com/rtrimble13/dorq/issues/53) |
| | DE-35 | dorq | Every constant configurable and calibratable; statement settings per profile | [dorq#54](https://github.com/rtrimble13/dorq/issues/54) |
| Gate | DE-40 | fafnir | Go/no-go per domain | [#276](https://github.com/rtrimble13/fafnir/issues/276) |

"dorq#N" in the issue column is an issue in `rtrimble13/dorq`.

## Status

| Date | Event |
|---|---|
| 2026-11-23 | Review of dorq 0.14.0. Plan approved; fafnir issues #267-#276 opened |
| 2026-11-23 | Token extended to `rtrimble13/dorq`; dorq issues #40-#54 and dorq PR #55 (its plan doc) opened |
| 2026-11-23 | DE-01 (dorq#56) and DE-02 (dorq#57) merged into dorq 0.15.0, untagged; DE-03 and DE-04 merged (#278). DE-05, the baseline, waits for a run on the host |
| 2026-11-24 | DE-13 (dorq#45) implemented on dorq's `claude/wizardly-einstein-pgcegr`, in 0.15.0: a provisional bar finding the bar itself does not settle waits at info (`detail.pending`). The baseline is unaffected: the host runs 0.14.0, and a build of dorq's main reports `0.15.0-dev`. fafnir now states the oldest dorq it runs, `DORQ_MIN_VERSION` 0.13.0 ([releasing.md](../releasing.md#which-dorq)). fafnir needs no change for it: the bars run passes no `--show-info`, so a pending finding is not written until a later night, re-judging the last 5 sessions (`SINCE_OVERLAP_SESSIONS`), settles it (decision 4) |
| 2026-11-25 | DE-11 (dorq#64) released in dorq 0.17.0. DE-12 (dorq#65) merged and released in dorq 0.18.0: one condition, one report, four statement checks re-keyed. DE-16 (#272) implemented on `claude/wizardly-einstein-pgcegr`: fafnir judges those checks' conditions by atoms (`condition_atoms`, each keyed as dorq before 0.18.0 keyed one), so `dq compare --statements` and the audit compare a night on 0.18.0 with one before it like with like (on dorq's synthetic universes, 20-145 spurious new and 35-278 spurious closed conditions by exact keys, none by atoms); an accepted SQL flag carries a folded report only where it covers all of it; the statements ingest withholds a report the queue holds under its earlier keys; `dq recheck` keeps an earlier flag open while its condition is reported under a key the queue lacks, and closes it as re-keyed once the queue has it. fafnir reads both releases, so `DORQ_MIN_VERSION` stays 0.13.0 |
| 2026-11-25 | DE-16 merged (#289), fafnir 3.5.0. DE-15 (#271) implemented on `claude/wizardly-einstein-pgcegr`, the last of Phase 1: the statements nightly runs at warn and above (no `--show-info`: about 283k info reports a night, 87k of them DQ919's, that nothing at warn and above reads), `[dq] dorq_statements_info` or `dq run --statements-info` adding them on demand and `dq recheck` always; its `--actions` are the splits SF1's share counts were stored by, the ledger's current Sharadar ACTIONS file, not `core.corporate_action` and its spin-as-split rows (#283); and `dq export-labels --drop-unverified` keeps the by-rule labels out of a calibration with a dorq before 0.16.0, which does not read `verified`. Phase 1's items are all in: the next step is the Phase 1 audit cycle on the host (decision 4), after DE-05's baseline |

## For agents working an item

- **Read before starting:** this file, the dorq plan, and the item's issue (its
  acceptance criteria are the definition of done).
- **Bars and statements are separate domains.** Each meets the bar, and cuts over, on
  its own.
- **A dorq item must make sense for any user of dorq.** If it only makes sense for
  fafnir, it is a fafnir item, a configuration, or a profile.
- **Never judge a label from either engine's output.** Labels follow DE-03's protocol,
  and the audit is the only evidence the gate (#276) accepts.
- **After each phase,** run an audit cycle and record the result in the status table
  above and on #267.
