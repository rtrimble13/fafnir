# Plan: Sharadar adoption

- Status: **in progress — plan v2, revised 2026-10-18.** Plan v1 (2026-09-23, seventeen weekly
  sprints S01–S17) is superseded; its calendar and status tables are in git history
  (`git show 1d26d82:doc/plans/sharadar-adoption.md`).
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
| Identity | ticker-keyed (`security_id` surrogate) | `core.security_vendor_xref` → permaticker | permaticker authoritative (the identity ADR, #67) |
| Fundamentals | none | — | **SF1**, bitemporal, 1998+ (phase 4) |
| Mutual funds (8 tracked) | FMP, 3 of 8 | FMP 3 of 8 | **Tiingo** for all eight once FMP's tier drops them (phase 5) |
| `duk -S …` | `db` / `live` | `db` default · `fmp` · `sharadar` | same |
| `duk yc` | FMP | FMP | FMP, then FRED (phase 5) |
| Subscriptions | FMP Premium | FMP Premium + Sharadar Bundle (max history, annual) | **Sharadar annual + FMP reduced tier** |

**Survivorship floor: 1998-01-01.** Neither vendor has delistings before 1998, so cross-sectional work before
that date is survivor-only under any plan. The identity ADR (#67) records it.

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
| **P2** | Backfill, compare, correct | #66, #69, #73, #74, #75, #76, #77, #79, #80, #81 | Dead-issuer history loaded; comparison report posted; corrections applied and audited; DQ queue triaged; `v_security_lifecycle` live; release **v1.6.0** |
| **P3** | Cutover to the Sharadar nightly | #93, #98, #109, #111, #118, #120, #122, #124, #125, #126, #128, #130 | Sharadar-primary nightly green for five sessions; FMP tier reduced; release **v2.0.0** |
| **P4** | Fundamentals on SF1 | #83, #85, #86, #87, #90, #91, #92, #95, #96, #100, #101, #102, #106, #107, #108 | Panel refreshed nightly with the look-ahead invariant at zero; `duk fs/fa`; `doc/fundamentals.md`; release **v2.1.0** |
| **P5** | Funds, FRED, close-out | #88, #97, #104, #114, #116, #117, #131 | Eight funds on one vendor; `duk yc -S db`; plan marked implemented |

P3 and P4 are independent once P2 is done; run them in whichever order the comparison report and the SF1
probe allow. P5's fund work (#88, #97, #104) is only urgent if the reduced FMP tier drops the three funds FMP
serves today — check that in #128 before scheduling it.

### The verification that replaces the parallel run

1. **Historical comparison** (#69, using #66): for every linked security and every date both vendors hold,
   compare FMP's raw bar with Sharadar's recovered raw bar and FMP's actions with Sharadar's. Report
   agreement by asset type, exchange, decade and price bucket; categorise every divergence (`scale`,
   `date_shift`, `missing_bar`, `split_missing`, `dividend_value`, `volume_basis`, `other`).
2. **Decide the correction policy from the categories**, not from a threshold set in advance: a category
   where Sharadar is right by inspection (checked against an outside reference on a sample, the ADR 0004
   level-check discipline) is corrected in bulk; a category that is a convention difference (consolidated
   vs primary-exchange volume) is documented and left; anything unexplained stays flagged.
3. **Nightly reconcile** during the overlap (the previous session, FMP vs Sharadar) reports the same
   categories, so a feed-behaviour difference (first prints, late corrections, split-day handling) shows
   up before cutover.
4. **Cutover criteria** (#122): the comparison report's unexplained category is empty; the nightly
   reconcile has been clean for ten sessions; < 1% of active non-fund securities unlinked; the Sharadar
   nightly has run in dry-run mode on production for five sessions and proposed the same renames,
   delistings and listings the FMP sweeps made (or each difference is explained).

### Migrations (expected numbers — take the next free number at merge time)

| Expected | Issue | Adds |
|---|---|---|
| 0029 | #46 | `ops.vendor_correction` (the audit trail of replaced rows) — **built** |
| 0030 | #50 | `landing.sharadar_file` (file ledger only; no typed landing tables) |
| 0031 | #55 | `core.vendor_security`, `core.security_vendor_xref`, `core.vendor_security_event` |
| 0032 | #66 | `ops.vendor_bar_comparison` (per-session summary; divergences are rows in the correction queue, not a second table) |
| 0033 | #79 | `mart.v_security_lifecycle` |
| 0034–0037 | #87, #95, #100, #101 | fundamentals tables, PIT functions, panel, aggregates |
| 0038 | #109 | attribute precedence; `mart.security_latest` re-defined (same columns) |
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
| [#76](https://github.com/rtrimble13/fafnir/issues/76) DQ pre-measure | On production with `--limit`, not staging; dorq's backfill path judges the history, no new policy |
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

### Milestones

The seventeen `S01`–`S17` milestones map onto the phases above (S01–S02 → P0, S03–S05 → P1, S06–S07 → P2,
S08–S12 → P4, S13–S17 → P3/P5). Recommended: rename six of them to `P0`–`P5`, move each kept issue to its
phase, close the rest. The GitHub API tools available to a session cannot create or delete milestones, so
this step is the operator's.

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

## [built] notes

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
