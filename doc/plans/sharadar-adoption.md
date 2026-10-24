# Plan: Sharadar adoption

- Status: **in progress — plan v2, revised 2026-10-18.** Plan v1 (2026-09-23, seventeen weekly
  sprints S01–S17) is superseded; its calendar and status tables are in git history
  (`git show 1d26d82:doc/plans/sharadar-adoption.md`).
- **Sharadar has been the primary nightly vendor since 2026-10-22** (#125). P0–P2 are closed, P3 is open,
  P4 and P5 have not started. The open issues were reconciled with the build on 2026-10-24 (*Issue
  dispositions (applied 2026-10-24)*).
- Tracking issue: [#41](https://github.com/rtrimble13/fafnir/issues/41) · Assessment:
  [sharadar-coa-assessment.md](sharadar-coa-assessment.md) · Vendor comparison:
  [fafnir-vendor-assessment.md](fafnir-vendor-assessment.md)
- Status legend: ⬜ planned · 🔄 in progress · ✅ done (PR #) · ⏭ carried over · ✖ dropped (reason)

## What changed on 2026-10-18

The assessment's recommendation stands: Sharadar becomes the permanent system of record and FMP is
reduced, not cancelled. What changes is how it is built, and what no longer needs deciding.

1. **The gates are decided.** Subscribe to the Sharadar **Core US Equities Bundle, max history**, on the
   annual plan from the start (the month-to-month trial existed only for a subscribe gate that no longer
   exists). Reduce the FMP subscription to a lower tier once Sharadar feeds the nightly. The licence
   letters (SA-0102) are still sent, because their answers decide *how low* FMP can go and whether FMP
   can one day be cancelled; they are no longer a gate on paying Sharadar.
2. **One warehouse of market data, not two.** Plan v1 kept a complete typed copy of every Sharadar table in
   `landing.sharadar_*`, a staging database, and a sixty-session shadow run in which Sharadar bars were
   landed but never loaded. All three are dropped. Sharadar's bulk files and nightly deltas land **on
   disk** under a small file ledger (`landing.sharadar_file`: path, sha256, rows, run) and are loaded
   straight into `core`. The warehouse's history lives in `core.daily_price` once, whichever vendor
   supplied each bar.
3. **Augment and correct, not fill-only.** Sharadar supplies what FMP never had (issuers that died before
   fafnir first ran, bars and actions FMP never served on live names) *and* corrects what FMP has wrong
   where the two disagree. Plan v1's symmetric fill-only rule becomes a **precedence rule**:
   `operator > sharadar > fmp`. A Sharadar write may replace an FMP row; every replaced row is kept in an
   audit table with the run that replaced it, so a correction is reviewable and reversible. FMP never
   overwrites a Sharadar row. Provenance (`source` on every row) stays.
4. **A bounded verification replaces the sixty-session parallel run.** One historical comparison over the
   full load (FMP bars against Sharadar's recovered raw bars, every linked security, every overlapping
   date) is the evidence for both the corrections and the cutover. During the weeks both feeds run, the
   nightly job reconciles the previous session. Cutover happens when the comparison report is clean and
   explained, not on a date set by a session counter.
5. **Less ceremony.** The seventeen sprint close-out issues, the sprint-by-sprint milestones and the
   `docs-gate` CI requirement go. Six phases replace them; the plan is brought current when a phase
   closes. The PR template and the link checker (SA-0101) stay: they cost nothing nightly.
6. **`duk` reads fafnir by default** and a vendor on request: `-S db` (default), `-S fmp`, `-S sharadar`
   (shipped with this revision, see [duk.md](../duk.md)).
7. **dorq is untouched.** The DQ engines run as [dorq.md](../dorq.md) describes; the backfilled history is
   judged by dorq's existing backfill path (history behind the window) and by the SQL checks. Phase 2
   *measures* what that produces before the production backfill; it adds no DQ policy machinery.

## Target state

| Domain | Today | After phase 2 (backfill + corrections) | After phase 3 (cutover) |
|---|---|---|---|
| Live names' 1990–2026 bars | FMP | FMP where the vendors agree; **Sharadar where they disagree** (audited); FMP's pre-1998 history untouched | unchanged |
| Bars FMP never served (gaps, delisted-while-live, dead issuers 1998+) | absent | **Sharadar**, raw recovered from `closeunadj` (`source='sharadar'`) | Sharadar |
| Nightly prices and actions | FMP | FMP; Sharadar reconciled against it nightly | **Sharadar**, observed raw (ratio exactly 1 on the capture day) |
| Minting, renames, delistings | FMP screener and sweeps | FMP, plus a permaticker link for every security | **Sharadar TICKERS/ACTIONS** via the xref |
| Identity | ticker-keyed (`security_id` surrogate) | `core.security_vendor_xref` → permaticker | permaticker authoritative ([ADR 0016](../adr/0016-sharadar-identity-and-survivorship-floor.md), #67) |
| Fundamentals | none | — | **SF1**, bitemporal, 1998+ (phase 4) |
| Mutual funds (8 tracked) | FMP, 3 of 8 | FMP 3 of 8 | **Tiingo** for all eight once FMP's tier drops them (phase 5) |
| `duk -S …` | `db` / `live` | `db` default · `fmp` · `sharadar` | same |
| `duk yc` | FMP | FMP | FMP, then FRED (phase 5) |
| Subscriptions | FMP Premium | FMP Premium + Sharadar Bundle (max history, annual) | **Sharadar annual + FMP reduced tier** |

**Survivorship floor: 1998-01-01.** Neither vendor has delistings before 1998, so cross-sectional work before
that date is survivor-only under any plan. [ADR 0016](../adr/0016-sharadar-identity-and-survivorship-floor.md) (#67) records it.

## Working agreements

1. **Provenance on every row; precedence decides writes.** `source ∈ {fmp, sharadar, tiingo, fred,
   operator}` says who supplied a row. Writes follow `operator > sharadar > tiingo > fmp`: a vendor revises its own
   rows freely, replaces a lower-precedence vendor's row only through the correction path (audited), and
   never touches a higher one. [ADR 0015](../adr/0015-multi-vendor-provenance-and-precedence.md) (SA-0103, #44) states this.
2. **Corrections are reviewable.** Every FMP row a Sharadar load replaces is written, as it stood, to
   `ops.vendor_correction` (security, date, old values, old source, new source, run, reason:
   `reconcile_divergence | gap_fill | restatement`). `fafnir vendor corrections list|revert` reads it. A
   correction that reverts an operator decision is refused: operator rows and suppressed keys win.
3. **Measure before loading** (ADR 0007's rule): `probe-sharadar` on the paid key before the first `core`
   write; the historical comparison before any correction is applied.
4. **Operator executes production mutations** (ADR 0010): sessions build and test; `owner:operator` issues
   run on the host, each starting with a logical dump.
5. **No vendor data on GitHub.** Fixtures are synthetic; reports posted to issues carry aggregates and
   cite `security_id` and dates only.
6. **Docs move with the code**, in the same PR. The PR template asks which; the link checker runs in CI.
7. **Deploy what merged.** An operator issue that depends on new code starts by deploying `main` and running
   `fafnir db migrate`.

## Phases

Phases are ordered by dependency, not by calendar week. Each closes with a short note in this document
(what shipped, what was measured, what changed) rather than a sprint close-out issue.

| Phase | Theme | Issues | Done when |
|---|---|---|---|
| **P0** | Subscribe, client, probe, safety rails | #42, #43, #44, #45, #46, #47, #49, #50, #52, #57 | `probe-sharadar` passes on the paid key; FMP loaders cannot touch a non-FMP-fed security; precedence write path merged |
| **P1** | Land, link, mint | #55, #56, #58, #59, #62, #63, #64, #67, #68, #71 | ≥ 99% of active non-fund securities linked or explained; dead issuers minted (empty) |
| **P2** | Backfill, compare, correct | #69, #73, #74, #75, #76, #77, #79, #80, #81 | Dead-issuer history loaded; comparison report posted; corrections applied and audited; DQ queue triaged; `v_security_lifecycle` live; release **v1.6.0** (shipped as v1.8.x; closed on v1.8.4) |
| **P3** | Cutover to the Sharadar nightly | #93, #98, #109, #111, #118, #120, #122, #124, #125, #126, #128, #130 | Sharadar-primary nightly green for five sessions; FMP tier reduced; release **v2.0.0** (tagged 2026-10-22) |
| **P4** | Fundamentals on SF1 | #83, #85, #86, #87, #90, #91, #92, #95, #96, #100, #101, #102, #106, #107, #108 | Panel refreshed nightly with the look-ahead invariant at zero; `duk fs/fa`; `doc/fundamentals.md`; a **minor** release (v2.1.0 went to `security relist` on 2026-10-24) |
| **P5** | Funds, FRED, close-out | #88, #97, #104, #114, #116, #117, #131 | Eight funds on one vendor; `duk yc -S db`; plan marked implemented |

P3 and P4 are independent once P2 is done; run them in whichever order the comparison report and the SF1
probe allow. P5's fund work (#88, #97, #104) is only urgent if the reduced FMP tier drops the three funds FMP
serves today — check that in #128 before scheduling it.

### The verification that replaces the parallel run

*Revised 2026-10-20: Sharadar wins on price, so the categories, the per-category policy and the nightly
reconcile are dropped, and #66 is folded into #75.*

1. **Historical comparison** (#69): a dry run of `fafnir vendor correct` (#75) over every linked security
   and every date both vendors hold. Report the bars compared and the bars that would be replaced, by asset
   type, exchange and decade.
2. **One convention to settle first: volume.** If one vendor reports consolidated volume and the other
   primary-exchange only, the difference is a definition, not an error: volume is then left alone. #69's
   dry run decides it.
3. **Corrections** (#75): `fafnir vendor correct` replaces each differing FMP bar with Sharadar's under
   precedence, each replaced row audited in `ops.vendor_correction`.
4. **Cutover criteria** (#122): the corrections applied; every unlinked active non-fund security explained
   by a reason code (4.8% unlinked on 2026-10-20, #64); the Sharadar nightly has run in dry-run mode on
   production for five sessions and proposed the same renames, delistings and listings the FMP sweeps made
   (or each difference is explained).

*Superseded 2026-10-22.* The operator cut over before #122 recorded a decision and before any dry-run night
had run. #122 is closed with where each criterion stood:
1. partly met: run 415 corrected the securities with no split on file, and the split-bearing ones were
   corrected after the cutover (#197, #198);
2. held: 198 unlinked, every one with a reason code;
3. waived: 0 of the 5 dry-run nights;
4. not recorded.

### Migrations (expected numbers — take the next free number at merge time)

| Expected | Issue | Adds |
|---|---|---|
| 0029 | #46 | `ops.vendor_correction` (the audit trail of replaced rows) — **built** |
| 0030 | #50 | `landing.sharadar_file` (file ledger only; no typed landing tables) — **built** |
| 0031 | #55 | `core.vendor_security`, `core.security_vendor_xref`, `core.vendor_security_event` — **built** |
| 0032 | #63 | `core.security_vendor_rejection` (a person's "not this key"; not in v2's first list) — **built** |
| ~~0033~~ | ~~#66~~ | ~~`ops.vendor_bar_comparison`~~: dropped on 2026-10-20 (#66 folded into #75); later migrations take the next free number |
| 0033 | #174 | `mart.v_corporate_action`, outside this plan (a mart-only reader's corporate actions) — so #79's view takes the next free number after it |
| 0034 | #79 | `mart.v_security_lifecycle` — **built** |
| 0035–0038 | #87, #95, #100, #101 | fundamentals tables, PIT functions, panel, aggregates |
| 0039 | #109 | attribute precedence; `mart.security_latest` re-defined (same columns) |
| (optional) | #114, #116 | fund distribution types; FRED economic series |

## Issue dispositions (applied 2026-10-18)

Ninety-five issues were open when v2 was drafted; the operator approved the dispositions below and they
were applied the same day: 28 closed (each with a one-line reason), 27 bodies rewritten (a *Revised
2026-10-18* note on top of the v1 text, the superseded sentences replaced, `docs-gate` and the
status-row items removed from the definition of done), and the tracking issue #41 rewritten for the
phases. 67 issues remain open. "Keep" bodies were left exactly as written, so their definition-of-done
blocks still name `docs-gate` and a status row; read those two lines as "lint, build-test, security" and
"phase note".

### Close

| Issue | Reason |
|---|---|
| [#21](https://github.com/rtrimble13/fafnir/issues/21) add symbol profile to duk | **Completed**: `duk ls SYMBOL\|NAME` shipped ([duk-company-summary.md](duk-company-summary.md)) |
| [#20](https://github.com/rtrimble13/fafnir/issues/20) treasury rates | **Duplicate** of #116 (SA-1402), which carries the design |
| [#51](https://github.com/rtrimble13/fafnir/issues/51) gate: subscribe? | Decided: subscribe (Bundle, max history, annual) |
| [#53](https://github.com/rtrimble13/fafnir/issues/53) nightly shadow capture | No shadow run; the nightly delta fetch moves into #98 |
| [#60](https://github.com/rtrimble13/fafnir/issues/60) staging database | One database; rehearsals become production dry-runs (#93, #98 `--dry-run`) |
| [#82](https://github.com/rtrimble13/fafnir/issues/82), [#103](https://github.com/rtrimble13/fafnir/issues/103), [#121](https://github.com/rtrimble13/fafnir/issues/121) parallel-run reports | Replaced by the historical comparison (#69) and the nightly reconcile summary (#66) |
| [#112](https://github.com/rtrimble13/fafnir/issues/112), [#115](https://github.com/rtrimble13/fafnir/issues/115) staging dress rehearsal and its fixes | No staging database |
| [#129](https://github.com/rtrimble13/fafnir/issues/129) move Sharadar to its long-term plan | Annual from the start; folded into #57 |
| [#48](https://github.com/rtrimble13/fafnir/issues/48), [#54](https://github.com/rtrimble13/fafnir/issues/54), [#61](https://github.com/rtrimble13/fafnir/issues/61), [#65](https://github.com/rtrimble13/fafnir/issues/65), [#72](https://github.com/rtrimble13/fafnir/issues/72), [#78](https://github.com/rtrimble13/fafnir/issues/78), [#84](https://github.com/rtrimble13/fafnir/issues/84), [#89](https://github.com/rtrimble13/fafnir/issues/89), [#94](https://github.com/rtrimble13/fafnir/issues/94), [#99](https://github.com/rtrimble13/fafnir/issues/99), [#105](https://github.com/rtrimble13/fafnir/issues/105), [#110](https://github.com/rtrimble13/fafnir/issues/110), [#113](https://github.com/rtrimble13/fafnir/issues/113), [#119](https://github.com/rtrimble13/fafnir/issues/119), [#123](https://github.com/rtrimble13/fafnir/issues/123), [#127](https://github.com/rtrimble13/fafnir/issues/127), [#132](https://github.com/rtrimble13/fafnir/issues/132) sprint close-outs | Weekly ceremony dropped; phases close in this document |

### Edit

| Issue | Change |
|---|---|
| [#41](https://github.com/rtrimble13/fafnir/issues/41) tracking | Body rewritten for v2: phases instead of sprints, decided gates, link to this revision |
| [#42](https://github.com/rtrimble13/fafnir/issues/42) docs machinery | Keep the PR template and `check_doc_links.py`; `docs-gate` becomes optional (not a required check) |
| [#43](https://github.com/rtrimble13/fafnir/issues/43) licence letters | No longer a gate on paying; eligibility self-check stays a hard stop; answers feed #128 |
| [#44](https://github.com/rtrimble13/fafnir/issues/44) ADR 0012 (written as ADR 0015) | Provenance + **precedence** (`operator > sharadar > fmp`) + audited corrections; per-vendor excision demoted from guarantee to report |
| [#46](https://github.com/rtrimble13/fafnir/issues/46) fill-only write path | Becomes the **precedence write path** + `ops.vendor_correction`; `--reclaim-from` dropped |
| [#47](https://github.com/rtrimble13/fafnir/issues/47) Sharadar client | Share endpoint knowledge with `duk.sharadar_api`; its UNVERIFIED items are probe inputs for #49 |
| [#49](https://github.com/rtrimble13/fafnir/issues/49) probe | Runs on the **paid key**, not the free sample; adds "API shape as `duk.sharadar_api` implements it" |
| [#50](https://github.com/rtrimble13/fafnir/issues/50) landing migration | File ledger only; no typed landing tables; size M → S |
| [#52](https://github.com/rtrimble13/fafnir/issues/52) bulk downloader | Drop the "load into typed landing" step; loaders read the downloaded zips (Sharadar's bulk format, #47) |
| [#57](https://github.com/rtrimble13/fafnir/issues/57) subscribe | Bundle, **max history, annual**; first smoke test `duk -S sharadar ph AAPL -n 5` |
| [#59](https://github.com/rtrimble13/fafnir/issues/59) initial production load | No shadow timer: download + master load + probe report |
| [#66](https://github.com/rtrimble13/fafnir/issues/66) reconciliation | Output is the **correction queue** (`fafnir vendor reconcile` → `fafnir vendor correct`), plus the per-session summary; one ops table, not two |
| [#69](https://github.com/rtrimble13/fafnir/issues/69) historical comparison | From "gate evidence #1" to **the** verification: categories, outside-reference spot checks, correction policy |
| [#70](https://github.com/rtrimble13/fafnir/issues/70) vendor excision | **Defer** (`optional`): a dry-run report of what each vendor supplied; `vendor:fmp` semantics dropped |
| [#73](https://github.com/rtrimble13/fafnir/issues/73) actions loader | Also fills missing actions on live names and corrects disagreeing ones under precedence |
| [#75](https://github.com/rtrimble13/fafnir/issues/75) price loader (backfill) | Three modes: dead issuers; gap-fill on live names; corrections from the queue |
| [#76](https://github.com/rtrimble13/fafnir/issues/76) DQ pre-measure | On production with a random `--sample`, not staging; dorq's backfill path judges the history, no new policy |
| [#77](https://github.com/rtrimble13/fafnir/issues/77) production backfill | Steps without staging; corrections applied after the report is reviewed |
| [#90](https://github.com/rtrimble13/fafnir/issues/90) SF1 loader | Reads the SF1 file / nightly delta file, not typed landing |
| [#98](https://github.com/rtrimble13/fafnir/issues/98) nightly loaders | Absorbs #53's delta fetch; acceptance is five production dry-runs, not a staging run |
| [#111](https://github.com/rtrimble13/fafnir/issues/111) vendor switch + rollback | Rollback = flip the config; FMP resumes from watermarks and never overwrites Sharadar rows |
| [#117](https://github.com/rtrimble13/fafnir/issues/117) reconcile.sh | Uses `duk -S fmp` / `-S sharadar`; depends on what the reduced FMP tier still serves |
| [#118](https://github.com/rtrimble13/fafnir/issues/118) ops tooling | "multi-vendor"; drop the `landing.sharadar_*` MCP read |
| [#122](https://github.com/rtrimble13/fafnir/issues/122) gate | Cutover go/no-go only, on the criteria above; the FMP tier is decided |
| [#124](https://github.com/rtrimble13/fafnir/issues/124) release v2.0.0 | README duk section already rewritten; keep the rest |
| [#125](https://github.com/rtrimble13/fafnir/issues/125) production cutover | No session-60 reference; preceded by the five dry-runs |
| [#128](https://github.com/rtrimble13/fafnir/issues/128) FMP subscription | Decided: **reduce**. Verify first what the lower tier serves: unadjusted EOD, the three funds, treasury rates, `search-*` identifiers |

### Keep as written

#45, #55, #56, #58, #62, #63, #64, #67, #68, #71, #74, #79, #80, #81, #83, #85, #86, #87, #88, #91, #92, #93,
#95, #96, #97, #100, #101, #102, #104, #106, #107, #108, #109, #114, #116, #120, #126, #130, #131.

### Verify on the host, then close or re-scope

[#24](https://github.com/rtrimble13/fafnir/issues/24) *duk ls shows 2 rows per ticker*: `mart.security_latest`
is unique per `security_id`, so two rows for one ticker are two `core.security` rows sharing it. Since the
issue was filed, migrations 0009 and 0015 constrain active identity and `fafnir security dedupe` /
`merge` fold duplicates. Check on the host:

```sql
SELECT symbol, count(*) FROM mart.security_latest GROUP BY 1 HAVING count(*) > 1;
```

Close if it no longer reproduces. If rows remain, each pair is either a reused ticker (one live, one
delisted — correct, and `duk ls` list mode shows both by design) or a `dedupe` candidate.

*Closed 2026-10-24 without the host check* (the 2026-10-24 dispositions below); the query is on the issue.

### Milestones

The seventeen `S01`–`S17` milestones map onto the phases above (S01–S02 → P0, S03–S05 → P1, S06–S07 → P2,
S08–S12 → P4, S13–S17 → P3/P5). Recommended: rename six of them to `P0`–`P5`, move each kept issue to its
phase, close the rest. The GitHub API tools available to a session cannot create or delete milestones, so
this step is the operator's.

## Issue dispositions (applied 2026-10-24)

Thirty-three issues were open. The cutover (2026-10-22) had overtaken some of them. The ADR numbers had
moved under others: plan v1 called provenance ADR 0012, identity ADR 0013 and the fundamentals ADRs 0014
and 0015, and those numbers went to instrument scope, dorq and venue transfers before the ADRs were
written. Four issues were closed and 18 bodies revised, each with a *Revised 2026-10-24* note on top. In
each revised body, `docs-gate` and the status-row items were taken out of the definition of done. 29 remain
open.

### Close

| Issue | Reason |
|---|---|
| #93 universe maintenance | **Completed**: shipped in #188, in production since the cutover. Its five dry-run nights were overtaken by the cutover; the comparison after it is #126's |
| #98 nightly prices and actions, `vendor feed` | **Completed**: shipped in #190 and #191, used at the cutover (run 426), rollback tested (#111). The SF1 delta it absorbed from #53 goes to #106 |
| #122 cutover gate | **Superseded** by the cutover (*The verification that replaces the parallel run*, above); closed with the criteria record |
| #24 duk ls shows 2 rows per ticker | **No longer relevant** as filed: duplicates are folded by `security dedupe` / `merge`, and a reused ticker lists a live and a dead row by design. Closed without the host check |

### Edit

| Issue | Change |
|---|---|
| #41 tracking | Phase log (P1 and P2 closed, P3 started), #122's outcome, the decisions since 2026-10-20, P4's release |
| #124 release v2.0.0 | The release is done (a version bump, 2026-10-22); re-scoped to the docs rewrite, since README still names FMP as the nightly feed |
| #128 FMP tier | `fmp_secondary` and `-S fmp`. The lower tier must also serve FMP's secondary night (198 unlinked securities, about 3,945 funds) and the five endpoints ADR 0017 moved into `FMPClient` |
| #109 attributes | A live gap since the cutover: the screener is not in the Sharadar night. ADR 0012 → ADR 0015 §4. Part 1, sector and industry, now; part 2, market cap and beta, with #100 |
| #130 legacy notes | ADR 0015 and 0016 and the cutover date in the status note; FMP's probes in `ingest/fmp_probe.py`; which FMP loaders do not run in Sharadar mode |
| #131 close-out | No parallel run to review; estimates by phase; #129's dependency dropped; ADR 0017's acceptance |
| #126 five sessions | Counted from the first Sharadar night; what the first nights turned up (#195, #196, #199, #201, #202) |
| #86 fundamentals ADRs | The next free numbers (0018 and 0019 today); ADR 0016 cited for the floor |
| #85 SF1 build prompt | The prompt's path in the repository; migrations 0035–0038; the delta never paged by offset |
| #83 SF1 probe | Reads the ledger's SF1 file, so it lives under `ingest/` (ADR 0017); #49 answered part of item 8 |
| #92 fundamentals backfill | No shadow step; re-download the SF1 file if it is stale |
| #106 fundamentals cadence | Owns the SF1 delta #98 deferred, and the prune's applied state for it; measured on production |
| #107 `duk fs` / `fa` | `-S fmp` and `-S sharadar` refuse; `TICKER@date` shipped with #80 |
| #88, #116 Tiingo and FRED clients | Clients only (ADR 0017 §1); the caller passes the key |
| #97 Tiingo loader | No `ingest/bars.py` and no `owner=`; the funds' feed moves in #104, not at the cutover |
| #104 Tiingo production | Step 6: `vendor feed --vendor tiingo --funds` after the first load |
| #117 reconcile.sh | No divergence categories and no `vendor reconcile`; corrected dates are expected differences; the fallback is `duk -S sharadar` |

#87, #90, #91, #95, #96, #100, #101, #102, #108, #114 and #125 were left as written. #125 stays open for its
step-6 verification comment.

## Costs

| Phase | Sharadar | FMP | Funds | ≈ annual rate |
|---|---|---|---|---|
| P0–P2 | Bundle, max history, annual: $499 | Premium $708 | FMP | ~$1,207 |
| P3 onward | $499 | reduced tier (~$264 if Starter; verify in #128) | Tiingo (free tier) | **~$763** |
| If FMP confirms retention after cancellation (#43) | $499 | $0 | Tiingo | ~$499 |

Prices are from the assessment (2026-09-23) and the vendor pages as last read; confirm at checkout.

## Risks

| Risk | Where it is handled |
|---|---|
| Sharadar's API differs from the documented shape `duk.sharadar_api` implements | #49 probe on the paid key; base URL and table are configurable; one module to correct |
| A bulk correction applies a vendor convention difference as if it were an error | Working agreement 2 (audited, revertible); categories decided from outside-reference spot checks before any bulk apply (#69) |
| SEP precision too coarse to recover exact raw prices for split-heavy dead issuers | #49, #75 (`sharadar_split_ratio_mismatch`) |
| Reused-ticker histories duplicated across vendors | #68 (`vendor_history_overlap`) before minting |
| The reduced FMP tier drops something the nightly still needs before cutover | #128 verifies the tier's contents **before** the change; P5's Tiingo work covers funds |
| FMP's licence does not permit retaining data after a downgrade | #43's letter; the agreement stays in force on a lower tier (the assessment's reading); cancellation only on written confirmation |
| Personal-licence eligibility fails (professional use) | #43 self-check before paying — the one remaining hard stop |
| Backfill floods the DQ queue | #76 measures first; dorq's backfill path and the SQL checks as they are |
| Host disk | #50 size projection on the bulk files (zips on disk, no typed copy) |

## Decision log

| Date | Decision | Where |
|---|---|---|
| 2026-09-23 | Adopt COA 2-M: Sharadar permanent; FMP downgraded, not cancelled, unless FMP confirms retention in writing | [assessment](sharadar-coa-assessment.md) |
| 2026-09-23 | Weekly sprints S01–S17 from 2026-09-28; sixty-session parallel run | plan v1 (git history) |
| 2026-10-18 | Subscribe to the Bundle, **max history**, annual; reduce FMP once Sharadar feeds the nightly. The subscribe and tier gates are closed as decided | this revision |
| 2026-10-18 | **One warehouse**: no typed Sharadar landing copy, no staging database, no shadow run. Files on disk under a ledger, loaded straight into `core` | this revision, #50/#53/#60 |
| 2026-10-18 | **Augment and correct**: Sharadar fills FMP's gaps and corrects FMP's errors under `operator > sharadar > fmp`, every replaced row audited in `ops.vendor_correction` | this revision, #44/#46/#66/#75 |
| 2026-10-18 | The sixty-session parallel run is replaced by the historical comparison plus a nightly reconcile during the overlap; cutover on evidence, not a date | this revision, #69/#122 |
| 2026-10-18 | Sprint close-outs and the `docs-gate` requirement dropped; six phases | this revision |
| 2026-10-18 | `duk`: `-S db` is the default; `-S fmp` and `-S sharadar` read a vendor directly; `live` kept as an alias | shipped, [duk.md](../duk.md) |
| 2026-10-18 | dorq integration unchanged by this plan | this revision |
| 2026-10-18 | **Subscribed**: Sharadar Bundle, max history, annual, from 2026-10-18 to 2027-10-18. Renewal reminder **2027-09-18** (30 days ahead: a lapse triggers the licence's 30-day deletion clause). Key in `/etc/fafnir/fafnir.env` on the host, never an rc file | #57 |
| 2026-10-19 | **First production population** of the Sharadar files and the vendor master, on the host (#59). This date replaces plan v1's "parallel-run session 1" | #59 |
| 2026-10-20 | **#66 folded into #75; #69 narrowed to a dry run.** Sharadar wins on price, so `fafnir vendor correct` (#75) finds and replaces differing FMP bars in one pass, audited in `ops.vendor_correction`. Not built: the divergence categories, `ops.vendor_bar_comparison`, the nightly reconcile and its report section. #69 posts the dry run's counts and settles the volume convention. Cutover criteria amended to match | #66, #69, #75 |
| 2026-10-20 | **Identity ADR is ADR 0016** (0013 was taken): permaticker is the identity authority from cutover, `security_id` stays the key; survivorship floor 1998-01-01; the inclusion rules for minting dead issuers | #67 |
| 2026-10-20 | **#64's gate recorded as it stands: 4.8% of active non-fund securities unlinked**, against the issue's < 1%. Every one carries a reason code, so P1's own criterion (≥ 99% linked *or explained*) holds. Further triage is not worth the effort: the residue is immaterial, and half of it is duplicate rows of linked issuers. The 126 identifier links without price support are kept, at their low confidence | #64 |
| 2026-10-20 | **Sharadar is the more reliable vendor for identity linking.** FMP's history disagreeing with Sharadar's is a correction to make, not a reason to withhold a link: `vendor link` links on the latest sample or a shared identifier when the history disagrees, and flags the history `vendor_history_disagrees`. The identity evidence must still not rest on FMP's history | #62 |
| 2026-10-21 | **Screens are actively trading only by default.** With thousands of dead issuers in `mart.security_latest`, `duk ls` screens and the MCP `screen_securities` tool return actively trading securities unless `--include-delisted` / `include_delisted=true` (or `is_actively_trading=false`, only delisted). The warehouse still retains every one; plain `duk ls` and single-company reads are unchanged | #79 |
| 2026-10-21 | **Volume convention: `keep`.** Corrected bars keep FMP's volume. On bars whose closes agree, FMP's volume over Sharadar's has a median of 1.0, with 88.5% within 1% and 94.9% within 5% | #69, #75, #77 |
| 2026-10-21 | **The first correction is limited to linked securities with no split on file.** That is 11,088 securities, and 589,256 bars corrected (run 415). A sample of the dry run's differing bars showed whole-split-factor and ADR-ratio differences where Sharadar is not always the right side. The 4,470 split-bearing securities wait for a bar-level rule, and their 763 `vendor_history_disagrees` flags stay open | #75, #77 |
| 2026-10-23 | **The split-bearing rule** (`vendor correct --split-bearing`). An FMP bar is replaced only when its close times the product of the stored Sharadar splits after it meets Sharadar's recovered close within 0.5%, and only in a stretch between two such splits where 90% of the compared bars are explained. Such a stretch is corrected whole: its few unexplained bars from the vendor too, and FMP's bars on dates the vendor lacks rescaled by the same factor as one revertible edit. Otherwise the first dry run (2026-10-23) would have left 501 spikes and 13,910 bars a level apart. The rule is the evidence of 2026-10-23: of 149 Sharadar splits with no raw jump in FMP's bars, 93 had Sharadar's as-traded close jump by the factor while FMP's sat exactly that factor below, and about nine more a product of later splits below. A corrected bar takes the vendor's volume, the exception to `keep`, because FMP's volume is pre-adjusted with its prices | #75, #77 |
| 2026-10-22 | **Dead-history outliers beyond §2k are accepted by an operator bulk rule.** One rule covers a move that comes back within 10 bars; the other a move that does not, but fails the §2d unreported-split test with no split within 120 days. 19,703 were accepted, and 699 stay open for split or dividend review. This is a decision for this population, recorded in the sweep policy, not a standing licence | #81 |
| 2026-10-22 | **Cutover: Sharadar is the primary nightly vendor.** The switch took effect at 17:41 UTC, on v2.0.0, ahead of its gate by the operator's decision: no dry-run night had run. `vendor feed --vendor sharadar --all-linked` (run 426) moved 15,567 securities, so all 27,494 open Sharadar links feed. FMP stays the secondary (`fmp_secondary = true`) for the funds and the 198 unlinked securities. The first Sharadar-primary night ran from the timer at 22:34 ET (2026-10-23 UTC). **The vendor seam of a fed series is 2026-10-22** (data-semantics §25); a rollback would add a second. #122 is closed with where its criteria stood | #125, #122 |
| 2026-10-24 | **Vendor access stays one library inside fafnir** ([ADR 0017](../adr/0017-vendor-access-is-one-library-not-a-distribution-per-vendor.md), proposed). A rename of `duk`, a standalone Sharadar CLI and a standalone FMP CLI were weighed; the CLIs are not built as separate distributions. `fafnir.sources` is the only vendor code and may not import the warehouse (a test enforces it); `duk -S sharadar` requests through `SharadarClient` and `duk -S fmp` / `duk yc` / `duk ls` through `FMPClient`, which took over the five endpoints only `duk` read; `duk` keeps no HTTP code. The rename is a separate decision | ADR 0017 |
| 2026-10-24 | **Issues reconciled with the build.** Four closed and 18 bodies revised (*Issue dispositions (applied 2026-10-24)*). Plan v1's ADR numbers are retired: the fundamentals ADRs (#86) take the next free numbers, and P4's release is the next minor, since v2.1.0 went to `security relist` | #41 |

## [built] notes

- **2026-10-24 · P4, #83: the SF1 probe.** `fafnir source probe-fundamentals [--vendor sharadar]
  [--symbols] [--dead N] [--foreign N] [--seed S] [--from-api] [--json]` asks the SF1 rows of the
  probe set, 20 random dead issuers and 3 issuers that report outside USD the questions the
  fundamentals design rests on. It gives each a PASS, NOTE or BLOCK verdict, as probe-sharadar does.
  The verdict table is in [backfill.md](../backfill.md#confirming-the-sf1-feed).
  - **Where it lives.** `ingest/sf1_probe.py`, not `fafnir.sources`, because it reads the ledger's
    SF1 and ACTIONS files and the vendor master (ADR 0017 §1). `--from-api` reads the same rows from
    the API, so this session could run it: there is no ledger here, and the bulk host is not
    reachable. `sharadar_files.type_row` types an API row as the file reader types the same row.
  - **First run** (2026-10-24, `--from-api`, 29 tickers, 6,439 rows, 60 requests): no BLOCK.
    - AR `date` is the SEC filing date; two public-record 10-Ks agree, and no era looks imputed.
    - MR `date` is the period end, so a restatement carries no knowledge date.
    - Q4 is its own row, and quarters are discrete.
    - **Share counts and per-share values are in today's share terms**, against the build prompt's
      §4.7 rule 1.
    - An absent item is 0 (41 indicators) and an inapplicable one NULL.
    - The reporting currency is TICKERS'; `fxusd` is foreign units per USD.
    - SF1 holds the primary share class only.
  - **Departures from the issue:**
    - item 10, *share terms*, was added, because the first measurement showed §4.7's premise does
      not hold;
    - the ADR sample became issuers that report outside USD, since a USD-reporting ADR answers
      nothing about currency;
    - EDGAR is not reachable from the session, so the five filings to compare are listed in the
      evidence, and two public-record filing dates are checked in code.
  - **API facts, now in the client's docstring.** A `ticker` list on SF1 returns only about the last
    two years, and nothing for a dead issuer. TICKERS ignores `isdelisted`. An SF1 `lastupdated`
    delta is small: 1,105 rows over four days.
  - **For #85:** what knowledge date a restatement gets; what share terms the statement tables
    store; whether a split's re-expression is a new version; whether rows before 1998 load; and how
    a secondary share class reaches its issuer's statements.
  - **Next.** On the host: `fafnir source probe-fundamentals` against the ledger's file, compare the
    five filings with EDGAR, and post the counts on #83.

- **2026-10-24 · P3, Sharadar file retention (no issue; asked for directly).** Nothing deleted a Sharadar
  file, so `bulk_dir` grew every night. The night downloads TICKERS and ACTIONS whole (up to 0.1 GB) and two
  deltas, and a killed sort can leave ~4 GB.
  - **`fafnir source sharadar-prune`** is step 7 of `ingest sharadar`, the dry-run nights included. It decides
    from the ledger:
    - always kept: each table's current full file and the deltas the nightly has not applied;
    - applied deltas for 45 days (the universe window plus a margin);
    - superseded files for `[sharadar] keep_days` (7), and the newest of each table;
    - a killed run's `.part` and sort scratch after 24 hours, and an unrecorded `<sha256>` file after 14
      days.

    A deleted row stays, marked `deleted`. The prune refuses when a current file is missing. Steady state is
    about 2.5 GB.
  - **Departure from the proposal:** no 90-day full refresh of SEP/SFP. A new listing now reads every delta
    still on disk, not only those after the current full. The 45-day window bounds the deltas, and a
    refresh no longer hides a listing's first days. Its rows are taken from its link's `valid_from`, so an old
    delta's rows for a reused ticker cannot be written to it. `monitor.sh sharadar` warns past 8 GB and on
    leftovers.
- **2026-10-22 · P3, #120: the cutover runbook.** The runbook is `doc/operations.md` › *Cutover to Sharadar
  as primary*:
  - preconditions (gate GO, release, no night in progress);
  - seven steps: say so, dump, `vendor link --new` and the unlinked-by-reason query, `vendor feed`, the
    setting, the first night, and next-morning verification;
  - six verification queries, each run against a migrated database: the night ran, fed bars are
    Sharadar's, FMP wrote only as the secondary, freshness, the night's counts, flag deltas;
  - rollback triggers and steps, and where to say what.

  The issues were edited first: #120 no longer depends on #115, and #125's step 3b no longer feeds the
  funds to Tiingo, which has no loader until #97. Skill updates:
  - **data-semantics:** §25, a series' vendor seam at the cutover and how to see it, and the volume step
    #69 measured. §22 is brought up to date (the feed rule, no reconciliation categories), and §8 notes
    Sharadar's once-only quarantines.
  - **dq-playbooks:** a vendor-check index, and *After the cutover*.
  - **schema-map:** *Which vendor, for what*.
  - **automations:** a pointer to the runbook.
  - **Still the operator's:** the dry-read the issue's acceptance asks for.
- **2026-10-22 · P3, #118: operations tooling for more than one vendor.** The issue was edited first. Shadow
  capture and reconciliation were folded away (#53, #66), there is no staging, and an install that starts
  from Sharadar would need a mint for live securities that does not exist.
  - **`monitor.sh`:**
    - **A `sharadar` section** shows the newest successful delta fetch, nightly load and file download, and the
      file ledger by table. It warns only in Sharadar mode, when the last *weeknight's* `prices:nightly`
      failed. The night runs Mon–Fri, so a fixed age would cry wolf every Monday.
    - **`bandwidth`** is split by vendor, and FMP's 50 GB budget counts FMP's bytes alone.
  - **`fafnir status`** gains a `Primary` line and `New (7d)` by minting vendor.
  - **Nightly report:**
    - **Evidence.** A `VENDOR MODE` section is read as fafnir and handed to `monitor.sh sharadar`.
    - **Prompt.** The model says whether the Sharadar night succeeded, and names the vendor DQ checks
      apart.
  - **MCP.** A new ops-only tool, `sharadar_files`, lists the ledger. `landing_payload` keeps its one-payload
    contract.
  - **`install_hetzner.md`** says the install starts from FMP, and points to backfill.md for Sharadar.
  - **Tests:**
    - `monitor.sh` against the test database: the warning by mode, and the budget by vendor;
    - a `collect_facts.sh` smoke test;
    - the MCP profile and integration tests.
- **2026-10-22 · P3, #111: the vendor switch in `daily_update.sh`.** The script reads `fafnir config get
  primary_price_vendor`, a new subcommand that prints only the settings scripts branch on.
  - **`fmp`** runs today's night, unchanged.
  - **`sharadar`** runs `ensure-horizon → ingest sharadar`, then, while `[general] fmp_secondary` is true (the
    new setting, default true), FMP's `tracked`, `prices` and `actions` with `--allow-secondary` and
    `adjust --changed`, then `refresh-marts → status`.
  - **The guard.** FMP's six loaders refuse to write in Sharadar mode without `--allow-secondary`.
  - **Rollback** is `vendor feed --vendor fmp --all-linked` and the setting flipped back. Precedence makes FMP
    fill only what Sharadar did not.
  - **Tests.** `test/scripts/test_daily_update_modes.py` runs the real script against a stub `fafnir`, in both
    modes, with `fmp_secondary` on and off, across the rollback and on failures. It also tests the CLI guard,
    and the rollback's fill-only upsert.
  - **Departures** (the issue was edited first):
    - `fmp_reconcile` became `fmp_secondary`, since `vendor reconcile` was folded into `vendor correct` (#66).
      FMP keeps feeding the funds and the unlinked residue while the reduced tier serves them.
    - `dq run` stays in its own unit, `fafnir-dq`.
    - Tiingo joins with #97.
- **2026-10-22 · P3, #98 (part 2): the Sharadar night.** `fafnir ingest sharadar [--dry-run]` runs
  six steps in order:
  1. TICKERS and ACTIONS downloaded whole (`sharadar-bulk --force`);
  2. `source sharadar-delta`;
  3. `sharadar-master`;
  4. `sharadar-universe`;
  5. `sharadar-prices --nightly`;
  6. `sharadar-actions --backfill --gap-fill`.

  A failed step stops the night. A real run waits for `primary_price_vendor = "sharadar"`.
  - **Deltas.** SEP and SFP rows by `lastupdated`, from the newest file's fetch day less 3 days. They are
    plain `<sha256>.csv` files with `kind = 'delta'`. Measured: one day of SFP was 33,022 rows on four
    pages, dated across a year. That is the probe's item 9, restamped history.
  - **The nightly.** It applies the delta files after the last nightly's `params.through`. For a fed
    security it writes new dates and revises Sharadar's own bars. A revision counts only beyond `vendor
    correct`'s test or 1% of volume, because a restamped history recovered at a new factor moves by rounding.
    Other parties' bars are left (`kept:<source>`). A security with no bars reads every delta since the
    full file.
  - **Actions.** The 7-day near-duplicate guard now applies to fed securities: one taken over from FMP keeps
    FMP's history.
  - **The acceptance tool.** `--as-if-fed` (dry run) compares the linked FMP securities per trade date:
    `fmp_agrees`, `fmp_differs`, `new` and `fmp_only`. The five nights are the host's to run.
  - **Departures.**
    - SF1 deltas (#53's list) wait for the fundamentals loader, which has nothing to apply them to yet.
    - `daily_update.sh` calls the night from #111, as the issue says; until then it is run by hand.
    - The nightly recomputes its own factors, as the backfill does, because `adjust --changed` reads FMP's
      actions runs.
- **2026-10-22 · P3, #98 (part 1): `fafnir vendor feed` and FMP's feed rule.** `vendor feed --vendor
  sharadar|tiingo|fmp (--all-linked | --funds | --security-id N…) [--dry-run] [--by] [--yes]` moves `is_feed`.
  Sharadar or Tiingo sets the vendor's open link, clearing any other first. FMP clears the links of what FMP
  minted, which is the rollback. A switch is one ingestion run (`endpoint = feed`) recording who, the scope
  and the ids changed; no price or action row moves.
  - **The rule #46 handed over:** `FMP_FED_SECURITY` (`source = 'fmp'`) became `repo.fmp_fed(alias)`, "minted
    by FMP and fed by no other vendor". It is used by the price, actions and reconciliation universes,
    `vendor-gaps` and `is_fed_by`. The duplicate-identity check and `security dedupe` keep
    `FMP_MINTED_SECURITY`: an FMP duplicate is one whoever feeds it.
  - **Departure:** "recording who and when" is the run row, not new columns on the xref. No migration.
  - **Next (part 2):** the nightly delta fetch into the ledger, `sharadar-prices` and `sharadar-actions`
    nightly modes, and `fafnir ingest sharadar` (universe, prices, actions).
- **2026-10-22 · P2, #76, #77, #79, #80, #81: the backfill closed out on the host.** On v1.8.4, migrations
  through 0034. Counts are on each issue; the skill's lessons are in
  [fafnir-dba skill enhancements, 2026-10-22](fafnir-dba-skill-enhancements-2026-10-22.md).
  - **#77, the backfill:**
    - **Sequence and timings** are now in [backfill.md](../backfill.md#the-dead-issuer-backfill-on-the-production-host-77):
      mint 17 s, prices 67 min, actions 9 min, corrections 49 min, dorq `--full` 6 min.
    - **Bars:** Sharadar now holds 19,910,449 = 3,741,428 (run 403) + 15,579,765 (run 412) + 589,256
      corrected from FMP. Actions: 4,921 splits and 95,548 dividends.
    - **Size:** the new bars are about 3.3 GB at 173 B a bar, and the database is 22 GB.
    - **Level checks:** Lehman, Bear Stearns, Enron and Sun's 1:4 reverse split match published closes, raw and
      adjusted. Washington Mutual is **not minted**: it is held back by `vendor_history_overlap`, its samples
      disagree with the FMP `WM` row, and the repair is a mint with `--allow-overlap`.
  - **#76, measured:** 33,906 outliers on 6,345 of 11,927 dead issuers; `gap` and `sparse_coverage` 0 on
    complete histories; dorq `--full` 6,849 reports on 2,674. The policy held. §2k needed the bulk rule above.
    Closed.
  - **#79, survivorship:** 30,962 Sharadar securities in scope, 27,474 held.
    - Every gap has a reason: 2,546 dead issuers held back by overlap flags, and 942 live names unlinked (826
      outside fafnir's FMP universe).
    - 894 FMP-linked histories start later by design, because gap-fill fills from the link's `valid_from`.
    - Closed.
  - **#80, acceptance:** `CC@2008-06-30` resolves to Circuit City and `CC@2020-06-30` to Chemours. Closed.
  - **#81, triage:**
    - 33,324 flags closed in eight batches, each dry-run and approved; the queue went from 51,277 conditions to
      17,953.
    - Left open with reasons: `vendor_history_overlap` 4,206, `vendor_history_disagrees` 763,
      `sharadar_split_ratio_mismatch` 226, and on dead histories 699 outliers, 115 `dividend_no_prior_close`,
      5 `dividend_exceeds_price` and 1 `split_duplicate_suspect`.
  - **Next:**
    - the 2,546 held-back dead issuers (`--allow-overlap` mints, by agreement class);
    - a bar-level rule for correcting the split-bearing securities;
    - the split review of the 226 mismatches and 699 outliers.

- **2026-10-22 · P3, #93: Sharadar-driven universe maintenance.** `fafnir ingest sharadar-universe
  [--dry-run] [--since D]` applies Sharadar's renames, delistings and new listings, resolved by the
  permaticker's open link. The window opens 30 days before the last real run. The FMP sweeps' rules carry
  over: `repository.apply_rename` is `apply_symbol_change` after resolution, and `symbol_change_conflict` and
  `security_delisting_contradicted` are flagged the same way. The FMP path and its tests are unchanged.
  - **Departures from the issue:**
    - no `--staging` (there is no staging database, plan v2). A real run waits for
      `primary_price_vendor = "sharadar"`, and the dry run applies every step and rolls it back, so it is the
      rehearsal and #122's comparison: `already` where FMP's sweep made the same change;
    - the TICKERS/ACTIONS load stays `ingest sharadar-master`, run before it, not a step inside it;
    - "link before mint" is a guard, not a `vendor link --new` call: `--new` evaluates FMP's new securities,
      and after cutover there are none. A new key whose ticker a fafnir security already trades under is
      `held`, for `vendor link`, never minted;
    - a delisting is also declined when TICKERS no longer lists the key as delisted, or a later `listed` event
      relisted it;
    - new listings keep the FMP load's product scope: ADR 0012's warrant/right/unit rule by ticker and name
      (`exclude_instruments`, declared symbols exempt). They also take TXSE, which Sharadar lists and FMP's
      screener cannot reach. The six names that moved there on 2026-10-05 are one security each in Sharadar,
      with no `delisted` event, so nothing here touches them.
  - **Next.** On the host: `sharadar-universe --dry-run` after each night's `sharadar-master`, five sessions,
    each difference from the FMP sweeps explained (acceptance).
- **2026-10-21 · P2, #81 (part 1): the playbook for Sharadar's quarantines.** Preparing the bulk triage
  turned up a gap. `sharadar-prices` quarantines through `add_dq_flag` (no dedupe), and a quarantined date is
  not held, so every run re-reads it. A bar a later run loads leaves its flag open, because nothing re-checks a
  `price_*` flag. Run 403's 146,710 rounding quarantines, which v1.8.1 now loads, are that case. dq-playbooks.md
  gains "Quarantines from the Sharadar loader": resolve by rule the flags whose bar has since loaded, then the
  older duplicates. Both queries are tested against a fixture, with batched `dq resolve` runs whose dry runs are
  summed against the prediction. The sweep policy's dead-history table points there first.
  - **Next.** The triage itself, on the host after #77 and #76's counts.
- **2026-10-21 · P2, #80: point-in-time ticker resolution.** `resolve_security_id(db, symbol, as_of=…)`,
  `duk -S db ph CC@2008-06-30` / `ls CC@…`, and MCP `resolve_symbol(as_of=…)` name the security that traded
  under a ticker on a date. Every MCP tool that takes a symbol also accepts the `TICKER@date` form. A period
  counts when it contains the date (`valid_to` is its last day). The 1900-01-01 "first seen" start counts
  only from the security's first bar. Of several, the latest start wins. Loaders never pass a date. The duk
  copy of the ladder carries the same rung on the mart seam, and a test asserts the two agree.
  - **Departures from the issue:**
    - the issue's tie-break (`is_primary`, then lowest id) would hand a reused ticker's old dates to the live
      holder, whose period starts at the 1900 fallback. The latest start goes first, and the 1900 start is
      bounded by the first bar;
    - a bare ticker that has named several securities still answers as before. `duk` lists the others on
      stderr with their dated form instead of refusing, so no script changes meaning;
    - `-S fmp` / `-S sharadar` refuse the dated form: the vendors have no ticker history to read it against.
  - **Next.** On the host, after #77 and deploy: `duk -S db ph CC@2008-06-30` and `CC@2020-06-30` (acceptance).
- **2026-10-21 · P2, #79: survivorship QA and `mart.v_security_lifecycle`.** Migration 0034 adds the view:
  per security, the trading span, `minted_by`, and for one that stopped trading, the vendor's terminal
  event and acquirer, with `survivorship_complete_from = 1998-01-01`. It uses definer rights, and
  `fafnir_app` reads it but still cannot reach `core.vendor_*` (tested). `scripts/survivorship_check.sql`
  gives, per year, the Sharadar securities in scope against those fafnir holds. Each missing one gets a
  reason (`no_link`, `no_bars`, `outside_span`); the script also counts deaths and the 20 largest gaps.
  - **Departures from the issue:**
    - the issue's `mergerfrom` is the survivor's event. Sharadar's ACTIONS, read on 2026-10-21, give the
      merged-away side `mergerto`, so the view reads `mergerto` and `acquisitionby` for the acquirer;
    - the vendor writes `N/A` for no other party, which the view reads as NULL;
    - a specific reason wins over a bare `delisted` within 30 days of the last terminal event, and the
      reason is given only for a security that has stopped trading;
    - `acquirer_security_id` and `delist_event_date` were added.
  - **Mart views on dead issuers:** `v_security_profile`, `v_security_price_coverage` and
    `v_security_action_summary` key on `security_id` and LEFT JOIN the master, so dead Sharadar rows read
    through them unchanged (tested).
  - **Reader default** (decision log): screens are actively trading only unless asked.
  - **Next.** On the host, after #77: `psql "$FAFNIR_DSN" -X -f scripts/survivorship_check.sql`; the counts
    go on #79.
- **2026-10-21 · P2, #76 (part 2): the policy for dead histories.** The policy was decided from what can
  repair a dead Sharadar history, before the counts. The counts confirm or revise it.
  - **The host had already minted the whole population** before part 1 merged: run 403 wrote dead issuers'
    bars. A sample was no longer possible, so the measurement is all of it. The SQL checks judge every
    history nightly. dorq needs one `dq run --engine dorq --full` once the backfill is complete, as its
    backfill pass skipped a mint of that size.
  - **Decisions** (fafnir-dba `sweep-policy.md`, "Dead Sharadar histories"):
    - no check changes, and none drops to `info`;
    - `gap` and `sparse_coverage` on a dead history are accepted by rule, in operator-directed bulk mode,
      because nothing re-fetches these rows and their bars are final;
    - `gap` keeps a calendar test in place of `cohort_size == 1`: a date most dead histories lack is
      reported, not accepted;
    - outliers are classified by `outlier-classification.md` §2k: collapse into failure, cash acquisition,
      liquidating distribution.
  - **Not done:** the measured counts. They go on #76 when the backfill and the dorq run are done, and the
    issue closes on them.
- **2026-10-21 · P2, #76 (part 1): the sample and the procedure.** `fafnir vendor mint-delisted --sample N
  [--seed S]` mints N dead issuers picked at random, the same N for the same seed; the run records both.
  `--limit` takes the first N by permaticker, the oldest keys, not a fair sample.
  - **No DQ code change.** The SQL checks rescan every history each night, and dorq's backfill pass judges
    the whole history of a security minted since the previous night that has bars. So a sample minted and
    loaded before the night's `fafnir-dq` is judged by that run, up to `dorq_backfill_max` (500).
  - **The procedure, on the host:** `vendor overlap`, `mint-delisted --sample 200 --seed 1`,
    `sharadar-prices --backfill`, `sharadar-actions --backfill`, all before `fafnir-dq`. The next morning,
    count the open flags on `source = 'sharadar'` securities by check and severity, and the night's shadow
    file's dorq rows on them by check. Counts only go on #76.
  - **For #77.** The full mint is over `dorq_backfill_max`, and `--gap-fill` and `vendor correct` rewrite
    history the backfill pass does not catch, so `fafnir dq run --engine dorq --full` once after them
    ([dorq.md](../dorq.md#what-a-run-does)).
  - **Next.** The measurement; then part 2: the per-check decisions in `sweep-policy.md`, and the dead-issuer
    patterns in `outlier-classification.md`.

- **2026-10-21 · #75 follow-up: `vendor correct` memory.** The first full `vendor correct --dry-run` on
  the host reached 1.6 GB in 50 minutes and pushed the 3.8 GB machine into swap. It was stopped.
  - **The cause:** every agreeing date of every linked security was kept for `_resolve_history_flags`,
    which reads only the dates an open `vendor_history_disagrees` flag sampled. Only those are kept now.
  - **Measured on 600,000 agreeing bars:** peak RSS 122 MB before and 95 MB after, the difference
    growing with every bar compared.

- **2026-10-21 · #73 follow-up: deep reverse splits.** The first `sharadar-actions --backfill --gap-fill
  --dry-run` stopped on `a split ratio must be positive, not Decimal('0.0002')`. `split_ratio` snapped every
  value to a denominator of at most 1,000:
  - a 1-for-5,000 became 0/1 and refused the whole load;
  - a 1-for-1,001 to 1-for-1,999 became 1/1000, a wrong split stored silently.

  The snap is now kept only within the value's own rounding (0.3333 -> 1/3). Otherwise the value is read as
  written, to a denominator of at most 1,000,000 (0.0002 -> 1/5000, 0.0008 -> 1/1250). No Sharadar split had
  been stored, so nothing needs repair.

- **2026-10-21 · #75 follow-up: Sharadar's files grouped by ticker; rounding no
  longer quarantines.** Run 403 (`sharadar-prices --backfill --gap-fill`) was killed
  after 13 hours. It had written 3.74M bars, about a quarter of SEP, and no funds.
  - **The cause:** SEP is largely date-major, and the loader batched consecutive
    rows of one ticker, so nearly every row was a batch with its own lookups and
    commit. `sharadar_files.select_by_ticker` now groups a file's wanted rows by
    ticker in 256 bucket files under `bulk_dir` (about 4 GB for SEP). Both
    `sharadar-prices` and `vendor correct` read through it, one batch per security.
  - **Measured:** the same 600k-bar date-major fixture loaded at about 6,200 bars/s,
    against about 510 for the old code. The bucket pass ran at about 69k rows/s.
  - **Progress:** both scans write it to the open run every minute
    (`RunLog.checkpoint`, `params.progress`). Run 403 left only `started` and zeros.
  - **Rounding:** 146,710 bars were quarantined as `cross_field_violation`. On
    PFFBQ (factor 2.1), 20 7/8 recovered as 20.874 against an exact close of
    20.875: 55 of its 210 bars on the wrong side, 45 just on the right one. A high
    below the close, or a low above it, within 0.0005 × (factor + 1) is now set to
    the close. A larger violation is still quarantined.
  - **Next.** On the host: deploy, `sharadar-prices --backfill --gap-fill --dry-run`
    (the `rounding_clamped` and `quarantined:*` counts say how much of the 146,710
    was rounding), then the real run, kept alive with `tmux` or `systemd-run`.

- **2026-10-20 · P2, #75 (part 2): `fafnir vendor correct`.** For every FMP security linked to Sharadar,
  compares its bars with Sharadar's (recovered raw, from the link's `valid_from`) and replaces the ones whose
  close differs by more than max($0.01, 1 bp), or whose open/high/low differ by more than that and the
  recovery's rounding. Each replacement goes through the precedence correction path into
  `ops.vendor_correction`, revertible; operator-edited bars are never touched. `vendor_history_disagrees`
  flags resolve once their sampled dates agree.
  - **`--dry-run` is #69's report:** compared and differing bars by asset type, exchange and decade, and the
    FMP/Sharadar volume ratio on agreeing bars (exact shares within 1% and 5%; median and deciles from a
    seeded 100,000-ratio sample, so memory stays bounded on the full warehouse).
  - **Volume stays FMP's** (`--volume keep`) until the dry run decides the convention; `--volume sharadar`
    is there for when it does. The decision goes in the decision log.
  - **Next.** On the host: `vendor correct --dry-run`, post its counts on #75, decide volume, then the real
    run.
- **2026-10-20 · P2, #75 (part 1) and #74: Sharadar's bars.** `fafnir ingest sharadar-prices [--backfill]
  [--gap-fill] [--symbols] [--dry-run] [--no-adjust]` writes SEP/SFP bars as `source = 'sharadar'`, raw
  recovered: the whole history of the securities Sharadar feeds, and fill-only the dates a linked FMP security
  lacks, from the link's `valid_from`. ADR 0004 gains the amendment #74 asked for.
  - **The factor:** the ACTIONS split product where SEP's `closeunadj / close` agrees within the two closes'
    rounding; the vendor's ratio otherwise, with `sharadar_split_ratio_mismatch` once per security.
  - **Departures from the issue:**
    - the issue's move of the FMP validators into `ingest/bars.py` is not done: the loader imports
      `daily_price`'s gates as they are, so FMP's code and tests are untouched;
    - a date the security already holds is never re-read, in both modes, so a re-run of a 20k-security
      backfill is a no-op; Sharadar's later revisions come with the nightly (#98);
    - the files are streamed once each and written per run of a ticker's rows, not loaded whole;
    - the mismatch tolerance is the two closes' 3-dp rounding, not the issue's 1e-6, which every old
      low-priced bar would fail (#49).
  - **Part 2** is `fafnir vendor correct`, with its dry-run report and the volume summary.
  - **Next.** On the host, after `vendor mint-delisted`: `sharadar-prices --backfill --gap-fill --dry-run`,
    the real run, then `sharadar-actions --backfill --gap-fill`.
- **2026-10-20 · P2, #73: Sharadar's splits and dividends.** `fafnir ingest sharadar-actions [--backfill]
  [--gap-fill] [--symbols] [--dry-run] [--no-adjust]` writes ACTIONS' splits and dividends as `source =
  'sharadar'`: every one for the securities Sharadar feeds (`--backfill`), and fill-only for the linked FMP
  securities (`--gap-fill`), inside the link's span and never within 7 days of another source's action of the
  same type. No future ex-date; a regular and a special on one date are summed.
  - **Departures from the issue:**
    - no `correct=True` path: the issue tied it to the reconcile queue, which is gone (#66 folded into #75). A
      disagreeing FMP action is counted `near_existing`, for #75's `vendor correct`;
    - the command recomputes the factors of what it changed itself, for securities with bars, because
      `adjust --changed` reads FMP's actions runs only;
    - `--gap-fill` added a guard the issue did not name: the 7-day window, so a date the vendors disagree on
      cannot store one dividend twice (data-semantics §19 is the same failure within FMP).
  - **Next.** On the host, after #75 loads the dead issuers' prices: `--backfill --gap-fill --dry-run`, then the
    real run.
- **2026-10-20 · #70 (optional): the excision report.** `scripts/reset_data.sh --scope vendor:sharadar [--bulk-dir P]`
  is plan v2's report, not the issue's v1 deletion. It counts the rows Sharadar supplied (`source =
  'sharadar'`) and the rows that hold its data (master, events, links and `vendor_*` flags, whose evidence
  holds sampled closes, corrections, watermarks, the file ledger). It also counts the operator edits on minted
  securities, the files under `bulk_dir`, and the dumps. It refuses `--yes`. `vendor:fmp` is refused, as
  v2 dropped it. The 30-day checklist that uses it is in
  [operations.md](../operations.md#ending-a-vendor-subscription). The deletion is still not built: it is
  written and reviewed if a licence event comes.
- **2026-10-20 · P1, #71: dead issuers minted.** `fafnir vendor mint-delisted [--dry-run] [--permaticker K
  [--allow-overlap]] [--limit N]` mints every dead Sharadar issuer ADR 0016 §3 admits
  (`vendor_overlap.mintable`, no open link, no open `vendor_history_overlap` flag). Each row is
  `source = 'sharadar'`, delisted, with closed ticker periods cut at its ticker-change events and a `minted`,
  feeding link. A commit per 500; `RunLog(endpoint='mint-delisted')`.
  - **Guard.** It refuses to run until `vendor overlap` (#68) has run since the master was last loaded.
  - **Departures from the issue:**
    - an unmapped sector or industry is left NULL and counted in the run, not flagged: the
      `security_missing_classification` check and its recheck cover listed securities only, so a flag on a dead
      row would be resolved on the next recheck;
    - `cusip` is left NULL: Sharadar lists every CUSIP an issuer had, and none is "the" one;
    - `--limit N` added, for a first run on the host (and `--sample N`, for #76);
    - #70 (excision) is not a prerequisite: it is deferred, and a minted row is found by `source = 'sharadar'`.
  - **Next.** On the host: `vendor overlap`, `mint-delisted --dry-run` (counts by category, venue and death
    year for #71), then the real run. `duk -S db ph <dead ticker>` resolves; prices come with #75.
- **2026-10-20 · P1, #68: overlap detection before minting.** `fafnir vendor overlap [--dry-run]` flags
  `vendor_history_overlap` on the FMP security, in two kinds:
  - **`dead_issuer`:** a dead issuer #71 would mint (`vendor_overlap.mintable`, ADR 0016 §3) shares a ticker
    with an FMP security, linked or not, that holds bars inside its price span. The flag has the range, the
    bar count and five sampled closes, so an agreeing row (holds the dead issuer's bars) is told from a
    disagreeing one (another issuer's). #71 must not mint a permaticker with an open flag.
  - **`earlier_history`:** a linked security whose bars start over 30 days before its permaticker's first
    price, where that is after Sharadar's December 1997 start.
  - **Departures from the issue:**
    - the check runs on every FMP security, not only the live ones, since a delisted row carries the same
      glue;
    - a full run also resolves the flags whose overlap is gone, so the queue follows the repairs;
    - a dead issuer whose FMP row *is* that issuer (delisted, unlinked, agreeing) is a `vendor link accept`,
      not a mint; the playbook says so.
  - **Not done:** the issue's "playbook dry-run on a staging copy". There is no staging database (plan v2),
    and the playbook's first step is #71's. The flags' counts come from the first host run.
- **2026-10-20 · P1, #67: the identity ADR.** [ADR 0016](../adr/0016-sharadar-identity-and-survivorship-floor.md)
  records three decisions:
  - the permaticker is the identity authority from cutover, and `security_id` stays fafnir's key;
  - cross-sections before 1998-01-01 are survivor-only under any vendor;
  - #71 mints a dead Sharadar issuer when it has SEP/SFP prices, died on or after 1998-01-01, maps to an
    asset type (common and preferred stock, ETF/ETN/ETD/ETMF, CEF), last listed on NYSE, NASDAQ, AMEX
    (NYSEMKT, NYSEARCA) or BATS, holds no open link, and is not flagged by #68.

  It cites #56's mappings, and #64's 4.8% unlinked residue as the price of not triaging further. ADR 0005
  gains a status note pointing to it. The counts by category, exchange and death year come from #71's dry
  run, not from the ADR.
- **2026-10-20 · P1, #64: the hit rate.** After run 399 and 13 merges:
  - **Active, non-fund FMP securities:** 12,888; linked 12,265 (95.2%); unlinked 623 (4.8%).
  - **The unlinked, by reason:**

    | Reason | Securities | What it is |
    |---|---|---|
    | `duplicate_of_linked` | 302 | 315 before the merges: a second active row of a linked issuer. `security merge` refused 306 of 319 (an identifier or the overlapping bars differ) |
    | `price_disagrees` | 92 | No basis decided |
    | `no_candidate` | 75 | No Sharadar row shares a ticker over the span |
    | `key_claimed_twice` | 64 | Rows of one issuer, none live on the vendor's ticker |
    | `no_bars` | 48 | Shells with no history (`security dedupe`) |
    | `insufficient_overlap` | 24 | Too short a shared span to sample |
    | `several_agree` | 18 | Share classes or a vendor duplicate |

  - **By asset type and exchange** (before the 13 merges), the linked share:

    | | NASDAQ | AMEX | NYSE | CBOE | TXSE |
    |---|---|---|---|---|---|
    | equity | 92.4% of 3,922 | 89.8% of 294 | 96.7% of 2,123 | 1 of 1 | 7 of 7 |
    | ETF | 97.2% of 1,437 | 96.2% of 3,244 | 98.8% of 80 | 95.8% of 1,792 | 1 of 1 |

  - **Structural misses.** Mutual funds: 4,588 `not_covered_asset_type`, which Sharadar does not price.
    CBOE ETFs are covered (95.8%): the BATS/CBOE gap the issue anticipated is not there. Delisted rows: 2,798
    linked; the rest are mostly duplicates (667) and claimed keys (264).
  - **The nightly `vendor link --new`** ran on 2026-10-20 (run 394).
  - **Runbook correction.** `duplicate_of_linked` is folded with `security merge <id> <linked_security>`,
    not `merge-rename`, which needs the old ticker live and keeps the old row.
- **2026-10-20 · P1, #62: link when FMP's history is wrong.** Two bases after `price_history` (≥ 4 of 5):
  - `latest_price`: one candidate with four samples agrees on its latest. That is run 398's back-adjusted
    candidates and about 324 of the 807 non-monotonic securities.
  - `identifier`: no sample decides, and exactly one candidate shares a CIK or CUSIP (`match_method =
    identifier`).
  - **Confidence** keeps one formula, so a link on one agreeing sample scores about 0.2.
  - **Every link with a disagreeing sample** gets `vendor_history_disagrees`, listing those samples: the
    worklist for the correction from Sharadar (#66, #75). Links made before this change are not flagged;
    the historical comparison (#69) covers every date of every link.
  - **Next.** `fafnir vendor link --dry-run` on the host gives the counts per basis before a real run.
- **2026-10-20 · P1, #62: run 398 lost the duplicates.** All 782 `duplicate_of_linked` flags of run 397 were
  replaced: 511 by `no_candidate` and 259 by `price_disagrees`.
  - **The cause.** Run 397 linked each key's live claimant, so on run 398 that key was held, and a held key
    was no candidate for anything. The duplicate row lost the only key that agreed with it.
  - **The fix.** A held key is now price-tested like any other but never won. When it is the one that
    agrees, the security is `duplicate_of_linked`, naming the holder, which is the verdict run 397 reached.
  - **Next.** Re-run `fafnir vendor link`: the 782 should come back as `duplicate_of_linked`.
  - **The adjust check on run 398's samples** (aggregates):
    - 544 candidates are FMP history back-adjusted against Sharadar's raw closes, on the right issuer.
    - Of the 807 securities whose every candidate is non-monotonic:
      - one candidate whose latest sample agrees: 269;
      - several candidates, exactly one agreeing on the latest sample: 55;
      - nothing agreeing on the latest sample: 480.
    - Sharadar is the more reliable vendor (decision of 2026-10-20), so these are mostly FMP history
      errors, not other issuers. The next change links on the latest sample or an identifier and puts the
      disagreeing history on a correction worklist.
- **2026-10-20 · P1, #62: run 397, and refreshed evidence.** The follow-up engine linked 746 more (14,683 in
  all, 67%), with 4,590 mutual funds `not_covered_asset_type`, 782 `duplicate_of_linked`, 328
  `key_claimed_twice`, 1,088 `price_disagrees`, 268 `no_candidate` and 108 `no_bars`.
  - **The gap.** The split check on the samples returned nothing. The first run's `price_disagrees` flags
    had no samples, and a run that reaches the same reason left the open flag untouched.
  - **The fix.** It now refreshes that flag's `detail` in place: same id, same `detected_at`, this run's
    evidence.
  - **Next.** Re-run `fafnir vendor link`, then the split check.
- **2026-10-19 · P1, #62 follow-up: what the first production run taught the engine.**
  - **The dry run** (21,903 securities): linked 13,937, `no_candidate` 4,826, `key_claimed_twice`
    1,852, `price_disagrees` 1,213, `insufficient_overlap` 47, `several_agree` 28.
  - **The operator's queries showed:**
    - `no_candidate`: 4,577 were mutual funds, which FMP files on NASDAQ as `equity` + `is_fund`. Sharadar
      prices none.
    - `key_claimed_twice`: every claimed key's rows carried different tickers, so these were missed
      renames, not venue transfers. In 742 of 905 keys exactly one claimant was live on the vendor's
      current ticker.
    - `price_disagrees`: 928 candidates agreed on 1–3 of 5 samples. That suggests a split on one side, but
      the flags did not keep the prices to prove it.
  - **The engine now:**
    - flags an `is_fund` non-ETF with no candidate `not_covered_asset_type` (`info`), and a security with no
      bars `no_bars`;
    - links the live claimant on the current ticker and flags the rest `duplicate_of_linked`, naming it:
      a merge worklist for `security merge-rename`;
    - keeps the sampled closes in every flag's candidates, so a SQL query can test the split hypothesis
      before the price test changes;
    - resolves an open flag whose reason changed within the same check, so the first run's flags are
      replaced, not left behind.
  - **Expected effect on a re-run:** about 14,680 linked, about 4,580 not covered, about 250
    `no_candidate`.
- **2026-10-19 · P1, #63: manual link review.** `fafnir vendor link show|accept|reject`, with `--dry-run`,
  `--by` (default the OS user) and commit-before-output. The runbook is in
  [operations.md › Resolving vendor links](../operations.md#resolving-vendor-links).
  - **`accept`** opens a `manual` link and resolves the security's `vendor_xref_*` flags with the note.
    It refuses a key another security holds, a feed, and a start date on which a closed link to the same
    key starts.
  - **`reject`** records the pair so the engine never proposes it again, and closes an open link to that
    key.
  - **`vendor link` is now a group.** Without a subcommand it is still the engine, so `fafnir vendor link
    --dry-run` and the nightly `--new` are unchanged.
  - **Departure: a table for rejections.** The issue said to record a rejection "in `evidence`". It is
    migration 0032 instead: `core.security_vendor_rejection`, ON DELETE CASCADE with its security. A
    link row would make a rejection read as a span of identity, and a link is the only thing with
    `evidence`. The LP test, `reset_data.sh` and the test fixture's truncation list include the table.
    #55's rollback test is made version-robust: it rolls back everything from 0031 on, as the 0028 test
    already did.
  - **Agent rule 12** (fafnir-dba `SKILL.md`): the agent may run `show --evaluate` and propose an
    `accept` or `reject` with its evidence. It never runs either without `--dry-run`.
- **2026-10-19 · P1, #62: the linking engine.** `fafnir vendor link --vendor sharadar [--dry-run]
  [--symbols …] [--new] [--prices auto|bulk|api]` links FMP-minted securities to their permaticker. It
  finds candidates by ticker over overlapping spans, decides on 5 sampled raw closes against `closeunadj`
  (≥ 4 must agree), breaks ties with a shared CIK/CUSIP, and flags the rest `vendor_xref_ambiguous` or
  `vendor_xref_unmatched` with a reason. The rules are in
  [ingestion.md](../ingestion.md#linking-to-a-vendors-identity).
  - **Additions to the issue:**
    - an `insufficient_overlap` reason (no candidate had 4 bars to compare), kept apart from
      `price_disagrees`;
    - `key_claimed_twice`: one permaticker won by two FMP rows, usually a venue-transfer or re-mint
      duplicate for `security merge`;
    - a flag of the other kind is resolved when the outcome changes.
    - tickers compared with `.` read as `-`: Sharadar writes 505 share classes `BRK.B`, FMP `BRK-B`.
  - **Measured.** 3,304 of 3,331 suffixed dead-issuer tickers (`CLA1`) list their root in `related_tickers`,
    so no suffix-stripping rule is needed. Reading closes for 15,000 tickers × 5 dates from SEP takes 84 s;
    rows are filtered on the raw text and parsed only when wanted, against 246 s for a parse-everything read.
  - **Nightly placement.** `--new` runs in `daily_update.sh` after `ingest prices`, not after `ingest
    securities` as the issue had it. A security minted tonight has no bars to compare until the price step.
  - **Operator step.** After deploying: `fafnir vendor link --dry-run` for the counts, then `fafnir vendor
    link`. Post the counts by outcome and reason on #62. Linking rates are not measured here: the cloud
    session has no FMP data.
- **2026-10-19 · P1, #59: first production population, on the host.**
  - **`source sharadar-bulk`:** 5 tables, 1,966,247,912 bytes downloaded, 2 min 45 s (runs 381–385, files
    1–5).
    - Rows match the cloud measurements exactly: tickers 74,420; actions 718,464; stocks 45,451,818; funds
      15,760,389; fundamentals 3,218,845.
    - Most of the time was the row count. The `stocks` zip downloaded in 11 s and took 67 s to count.
    - The ledger reports 1,875 MB on disk.
  - **`ingest sharadar-master`:** 11.0 s (runs 386, 387).
    - TICKERS: 61,146 rows, with 13,274 investors skipped.
    - ACTIONS: 142,852 events, with 571,824 splits and dividends, 654 duplicates and 3,134 unresolved rows
      skipped.
    - Every count matches the cloud run on the same files.
    - `fafnir status` shows the Sharadar block.
  - **Warehouse beside it:** 21,959 securities and 65.5M bars (`core.daily_price` 11 GB). `landing.fmp_raw`
    is 6.1 GB, three times the Sharadar files.
- **2026-10-19 · P1, #59: the production sequence, written down.**
  [backfill.md](../backfill.md#on-the-production-host) gives the host's order:
  dump, deploy and migrate (0030, 0031), `sharadar-bulk`, the ledger sizes, the probe, `ingest
  sharadar-master`, `status`. It also lists what to record on #59.
  - **Runbook correction.** The issue's `--tables TICKERS,ACTIONS,SEP,SFP,SF1` failed: only the lowercase
    `tickers` and `actions` were known names. Table names are now case-insensitive. The API takes every
    spelling and redirects `TICKERS` and `tickers` to the same `tickers.csv.zip` (checked 2026-10-19).
  - **No parallel-run date.** The issue's decision-log item, "parallel-run session 1 date", has no
    counterpart in plan v2. The log records the date of the first production population instead, once
    the operator has run it.
- **2026-10-19 · P1, #58: the Sharadar master and events.** `fafnir ingest sharadar-master [--from-file
  FILE_ID]` loads the ledger's current TICKERS into `core.vendor_security`, then its ACTIONS into
  `core.vendor_security_event`, resolving each row's ticker to a permaticker through the master. It reads
  the files #52 recorded and downloads nothing. One run per file (`master:<table>`). Rows are upserted
  through a staging COPY, and a row equal to the stored one is not written, so a re-run is a no-op.
  `fafnir status` gains a Sharadar block, printed only once the tables hold rows.
  - **Measured on the 2026-10-19 files** (cloud session, local database):
    - TICKERS: 74,420 rows → 61,146. The counts by table and delisted flag match the file.
    - ACTIONS: 718,464 rows → 142,852 events.
    - Time: 12.5 s for both; the re-run took 7.6 s and wrote nothing.
  - **Where the file differs from the issue:**
    - `SF3B` rows are institutional investors, so they are skipped (13,274).
    - ACTIONS uses about seventy action names, not the sixteen the issue lists. Every action but `split`
      and `dividend` is kept, so that none is silently dropped.
    - 3,134 rows name a ticker TICKERS does not hold, mostly acquirers outside the dataset. They are
      skipped and counted by action.
    - The event grain repeats for 375 acquisitions that differ only in `contraname`. The first name in
      sort order is kept (654 rows collapsed).
  - **Not built here:**
    - a TICKERS/ACTIONS delta, since the downloader fetches full files only;
    - the issue's "last shadow run" line, since plan v2 has no shadow run. The status block shows the
      newest master load instead.
- **2026-10-19 · P0, #57: subscribed, key installed.** The Bundle (max history, annual) runs
  2026-10-18 → 2027-10-18; the renewal reminder is 2027-09-18. The key went into the host's
  `/etc/fafnir/fafnir.env`.
  - **Cloud session check:** `fafnir source ping --require-paid` reports the paid tier, one request in
    under a second (3.8 s with startup); `duk -S sharadar ph AAPL -n 5` returns the five sessions
    2026-10-12..16.
  - **Runbook:** [install_hetzner.md](../install_hetzner.md#adding-the-sharadar-key-to-a-running-host)
    now has the step for a host installed before the subscription. The issue said `chmod 600`; the
    file stays `640`, group `fafnir`, because the timers run as `fafnir` and source it.
- **2026-10-19 · P1, #55: vendor identity.** Migration 0031 adds `core.vendor_security` (a vendor's own
  master), `core.security_vendor_xref` (security ↔ vendor key, dated, with evidence and `is_feed`) and
  `core.vendor_security_event` (lifecycle events). All three arrive empty. What #49 measured shaped them:
  - **Keys and names.** `vendor_table` takes modern names, permaticker is the key, and events carry only a
    ticker until #58 resolves them through TICKERS.
  - **Additions to the issue's DDL:**
    - a unique index allowing **one feeding link per security**, with a CHECK that a feed is an open link
      (ADR 0015 §2);
    - CHECKs on vendor, method, range, confidence, `created_by`, and the price-date span.
  - **No explicit GRANT.** Read access comes from the default privileges, as for every migration since
    0021: `fafnir_read` and `fafnir_ops` read the tables, `fafnir_app` has no USAGE on core. A new
    least-privilege test reads them as a real `fafnir_read` member.
  - **`is_fed_by(db, id, "sharadar"|"tiingo")`** now reads the open `is_feed` link instead of raising.
  - **Code that deletes a security now handles links:**
    - `security merge` and the stub fold move links to the survivor, and refuse two open links to one
      vendor, or two feeds (`force` cannot lift it; the fold declines rather than fail the sweep);
    - `security split-history --undo` keeps a destination that holds links;
    - `reset_data.sh --scope all` lists the three tables.
  - **Handed to #98:** FMP's rule is still `source = 'fmp'` alone. When `fafnir vendor feed` first gives
    a security a Sharadar feed, it must also take it out of FMP's universes so one vendor feeds it. The
    `s.{FMP_FED_SECURITY}` callers assume a bare column, so #98 changes the predicate and its callers
    together.
- **2026-10-19 · P1, #56: boundary mappings.** `fafnir.ingest.sharadar_map` holds the pure functions every
  loader calls: `map_exchange`, `map_category`, `map_classification`, `split_ratio`,
  `split_product_after`, `as_declared_dividend`, `recover_raw`. Tests cover every branch, plus the round
  trip `recover_raw(adjust(raw)) == raw` at 6 dp for factors 1, 2, 7, 112 and 1/20, by ratio and by
  factor. The table and its reasons are in
  [ingestion.md](../ingestion.md#sharadar-field-mapping). The decisions:
  - **Exchanges.** `NYSEARCA` and `NYSEMKT` map to `AMEX`, as FMP files them; the operator confirms with
    the SQL in the module before the first load.
  - **Asset types.** CEFs are `equity` + `is_fund`. ETN, ETD and ETMF are `etf`. Preferreds are `equity`.
    `UNIT` and `IDX` map to none.
  - **Where it is recorded.** The issue named ADR 0013 for this, but 0013 is taken, so the decisions live
    here and in ingestion.md. The identity ADR (#67) cites them.
  - **Departures, forced by #49.**
    - Dividends convert by the ACTIONS split product, because the value is split-adjusted.
    - `recover_raw` takes an optional exact factor beside the documented ratio; #75 chooses which to pass.
  - **For #58.** Bulk TICKERS writes `table` in legacy codes (`SEP`, `SFP`, `SF1`, `SF2`, `SF3B`), so its
    loader maps them to the modern names.
- **2026-10-19 · P0, #52: the bulk downloader.** `fafnir source sharadar-bulk [--tables …] [--years 5|10|full]
  [--force]`. For each table it:
  - downloads the zip (via the 302, without the key);
  - checks the header against the table's schema: a missing column fails the table, naming it; an unknown
    column is a warning kept in `params.unmapped_columns`;
  - counts rows, records the file, and supersedes older full files.

  One `ops.ingestion_run` per table (`bulk:<table>`); a table on record is skipped unless `--force`, so
  runs resume; the same content twice is one file. The shared reader is `sharadar_files.rows(db, file_id,
  bulk_dir)`, which refuses a deleted, missing or resized file. Download errors drop the pre-signed query
  string, which is credentials plus noise.
  - **Measured: the first full download** (2026-10-19, cloud session, once
    `static-sharadar.nyc3.digitaloceanspaces.com` was allowed). Five tables, 1.9 GB of zips, under 4 minutes,
    most of it counting rows:

    | Table | Rows | Zip |
    |---|---|---|
    | tickers | 74,420 | 4.8 MB |
    | actions | 718,464 | 10 MB |
    | stocks | 45,451,818 | 907 MB |
    | funds | 15,760,389 | 322 MB |
    | fundamentals | 3,218,845 | 632 MB |

    Each zip holds one `<modern name>.csv`. Every header matched its schema with no unknown column. #50's
    projection held for the price files; SF1 came in under it (632 MB, not ~1.1 GB).
    Every row of all five files then read through `sharadar_files.rows` with no parse error: stocks in
    260 s, funds in 95 s, fundamentals in 180 s.
- **2026-10-19 · P0, #50: the file ledger.** Migration 0030 adds `landing.sharadar_file`, one row per
  downloaded file: table, `full`/`delta`, filters, path, `member`, sha256 (unique), bytes, rows, header,
  run and status. `fafnir.sources.sharadar_files` reads a file as typed rows, streamed. Differences from
  the issue:
  - **Zip, not gzip.** Bulk files are zips (#49), so the ledger has a `member` column, and the reader
    takes zip, gzip and plain CSV alike.
  - **Relative paths.** `path` is relative to `bulk_dir`, so the directory can move to a Volume.
  - **What `status` tracks.** It is the file's lifecycle (`downloaded`, `superseded`, `deleted`), not a
    loader's progress, because several loaders read one file.
  - **Types follow Sharadar's own schemas** (`/v1.0/schema`): price fields come back as `Decimal` from
    the text, never float; volume is a `Decimal` because it is not always whole; an empty field is
    `None`; a value that does not parse fails, naming its line and column.
  - **Size, projected from the paid feed:** one full set of stocks, funds, fundamentals, actions and
    tickers is ~2.4 GB of zips (~5 GB during a refresh). `core` gains at most ~4.6 GB of delisted
    issuers' bars (151 B a bar, measured). `scripts/landing_sizes.sql` reports the actual figures.
  - **Backups.** The bulk files are not backed up, by decision: they are re-downloadable while
    subscribed, and the licence decides off-site copies. The dump holds the ledger and everything
    loaded from the files.
  - **`reset_data.sh --scope all`.** It now lists the ledger, which references `ops.ingestion_run`. It
    also lists `core.symbol_change`, whose absence already made that scope fail on `main` with a
    foreign-key error.
- **2026-10-19 · P0, #49: the Sharadar probe.** `fafnir source probe-sharadar` lives in its own module,
  `fafnir.sources.sharadar_probe`, not `probe.py`, which holds FMP's probes. Run on the paid key over 200
  securities (seed 0, 427 requests, 108 MB), it found **no blocking verdict**. The table is in
  [backfill.md](../backfill.md#confirming-the-sharadar-feed); what follows is what the loaders must
  accommodate:
  - **Prices: 3 decimal places.** Raw close (`closeunadj`) is exact to that. A recovered open/high/low is
    off by up to 0.0005 × the cumulative split factor. This is the risk row "SEP precision too coarse",
    now measured: #75 decides whether to flag it.
  - **Splits and dividends.** A split's `value` is new shares per old, the direction core already reads.
    Dividends are **split-adjusted**, so the loader multiplies by the later splits' product. Raw recovery
    matched ACTIONS on every split-affected bar within rounding.
  - **Identity.** History is keyed by the **current** ticker, and dead reused tickers carry a numeric
    suffix. SEP and SF1 rows have no `permaticker`; TICKERS does, and it is consistent across tables. So
    #55 keys on permaticker through TICKERS.
  - **Updates.** `lastupdated` re-stamps the whole history on every split or dividend, so the nightly
    delta (#58) cannot use it to find changed raw bars without comparing.
  - **API.** An unknown parameter is ignored, not refused, so every loader asserts its filters. The JSON
    row cap is 10,000. The rate limit is 5,000 requests (25,000 weighted) per window. Bulk redirects to
    `static-sharadar.nyc3.digitaloceanspaces.com` as `<table>.csv.zip`, so the host's egress allows that
    host too.
  - **Unmeasured:** the zip's members, because the build session could not reach the download host. #52
    measures them.
- **2026-10-19 · P0, #47: the Sharadar client.** `fafnir.sources.sharadar.SharadarClient` on
  `BaseSource`: `ping`, `tables`, `query` and `bulk_export`. `fafnir source ping` makes one request and
  writes nothing.
  - **Built from Sharadar's agent guide, not yet measured.** The build host's network could not reach the
    vendor, so the module docstring labels each mechanic `DOCUMENTED` or `UNVERIFIED`. It is the direct
    API at `api.sharadar.com/v1.0/data/{table}`, not Nasdaq Data Link: no `qopts.*` and no `datatable`
    cursor.
    - **Documented:** key as `api_key` or the `x-api-key` header (the client sends the header); tables by
      modern name or legacy code; slices by `ticker`, `from`, `to` and `fields`; `limit` (default
      10,000) / `offset` paging (`query` pages by offset); bulk as the same route with
      `years=5|10|full`, answering a 302 to a pre-signed **zip**.
    - **Unverified, for #49:** the JSON payload shape, a list in `ticker`, which date `from`/`to` filter
      where a table has two, a `lastupdated` filter for the nightly delta (#58), the largest `limit` and
      the default sort under offset paging, the rate limit, and the zip's contents.
    - **Measured 2026-10-19, free tier.** The cloud session reached the API, but not with the paid key.
      - The payload is `{"count": n, "data": [...]}`; an error is `{"error", "description"}`.
      - `SEP` answers newest date first, and `sort=date` reverses that.
      - The free tier serves AAPL's prices from five years back and refuses `TWO` with 403 `Exceeds free
        tier`.
      - **A missing or invalid key is not refused; it is served as free tier.** So `fafnir source ping`
        now also reports the tier the key is served as (`SharadarClient.tier()`, one request for a
        non-sample ticker), and `--require-paid` fails on anything but `paid`.
    - **Operator step:** `fafnir source ping --require-paid` with the subscription's key.
  - **Bulk.** `bulk_export(table, dir, years=...)` follows the redirect by hand, so the key header never
    reaches the link's host. It streams to `<sha256><suffix>` under `[sharadar] bulk_dir` via a temporary
    name. The files are zips, not the gzip CSVs #50 and #52 assumed: the ledger and the shared reader
    read a zip member.
  - **duk now shares this module's endpoint knowledge.** `duk.sharadar_api` imports the default base URL,
    the price tables and the payload parser from it, so #49 corrects one module, not two. duk's date
    filter was the Nasdaq Data Link spelling (`date.gte` / `date.lte`) and is now the documented `from` /
    `to`; its test asserted the old spelling and was corrected with it.
  - **The key.** `SHARADAR_API_KEY` is read from the environment and nowhere else. `[api].sharadar_key`
    stays duk's, and fafnir ignores it, but names it when the env var is missing.
  - **Redaction.** It now masks a client's key by value, as well as by the `api_key=` pattern, and also
    masks a pre-signed link's `X-Amz-*` credentials.
  - **Config placement.** `primary_price_vendor` lives in `[general]` (validated `fmp|sharadar`; nothing
    reads it before P3). `rate_per_min` (default 60) and `bulk_dir` live in `[sharadar]`. `base_url`
    stays `[api].sharadar_base_url`, shared with duk.
- **2026-10-18 · P0, #46: the precedence write path.** Migration 0029 (not 0031: it merged before #50
  and #55) adds `ops.vendor_correction`. Both upserts keep their existing `source` parameter as the
  writer instead of gaining an `owner`, so no FMP call site changed. `upsert_daily_prices` now returns
  `UpsertResult`, an `int` (rows written, as before) carrying `inserted`, `updated`, `corrected`,
  `skipped_foreign` and `skipped_suppressed`. `upsert_corporate_action` keeps its `bool` return, which
  the tests compare with `is True`, and reports the same counts through an optional `counts` dict. A
  correction replaces an action only when its values differ. Two things beyond the issue:
  - The restatement guard compared FMP's re-read with any stored bar. A bar Sharadar holds would have
    raised `price_restatement_conflict` every night, or been reported as restated by an explicit
    re-fetch that wrote nothing. The price loader now routes another party's dates around the guard,
    and the upsert skips and counts them.
  - `security merge` retargets corrections as it does overrides. `security split-history` does not move
    them yet: no correction exists until #66 makes one, so that is left until a correction can meet a split.
  `--reclaim-from` stays dropped: reverting corrections by `new_source` covers vendor removal (ADR 0015
  §5) once a bulk form is needed.
- **2026-10-18 · P0, #45: FMP feeds what FMP minted.** `VENDOR_FED_SECURITY` (`source <> 'operator'`)
  became `FMP_FED_SECURITY` (`source = 'fmp'`), with `repo.is_fed_by(db, id, "fmp")` as the per-symbol
  guard. Beyond the call sites #45 listed, the same scope now applies to the `source vendor-gaps` report
  and `security venue-transfer`, both of which read FMP's feed for a security; the `ingest prices`
  universe moved into `repo.fmp_price_universe` so it is tested. `is_fed_by` refuses any vendor but FMP
  until `core.security_vendor_xref` exists (#55). The write-side operator guard in
  `upsert_corporate_action` is left for #46, which replaces it with precedence.
- **2026-10-18 · P0, #44: the provenance ADR is ADR 0015.** ADRs 0012–0014 were taken by the time it
  was written (instrument scope, dorq, venue transfers), so it is
  [ADR 0015](../adr/0015-multi-vendor-provenance-and-precedence.md); the identity ADR (#67) and the
  fundamentals ADRs (#86) take the next free numbers when they merge. It ranks `tiingo` between Sharadar
  and FMP: Sharadar has no mutual funds, so the two never hold the same key, and Tiingo replaces FMP for
  the funds. It also records that `ops.vendor_correction` is what makes removing Sharadar reversible:
  the FMP rows it replaced are restored from there.
- **2026-10-18 · duk source model.** `duk` gained the three-source model ahead of any warehouse-side
  Sharadar code: `duk.sharadar_api` (SEP → SFP fallback, raw recovery from `closeunadj`, redaction) and
  `duk.datasource.sharadar`. The endpoint shape is documented from Sharadar's pages and **not yet measured
  against a paid key**; `[api].sharadar_base_url` exists so the probe's findings can be applied without a
  release. Before this change a bare `duk ph` with no DSN fell back to FMP; it now stops and names the
  vendor flags. `-S sharadar` serves `ph` only; identifiers (`cik:` …), `ls` and `yc` are refused with the
  source to use instead.
