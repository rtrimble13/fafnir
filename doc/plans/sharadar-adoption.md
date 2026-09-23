# Plan: Sharadar adoption (COA 2-M)

- Status: **in progress** — updated at every sprint close-out (`SA-xx99` issues).
- Tracking issue: [#41](https://github.com/rtrimble13/fafnir/issues/41) · Milestones: S01–S17 · Assessment: [sharadar-coa-assessment.md](sharadar-coa-assessment.md)
- Status legend: ⬜ planned · 🔄 in progress · ✅ done (PR #) · ⏭ carried over · ✖ dropped (reason)

## Plan overview

**Decision being implemented:** COA 2-M from the assessment — adopt Sharadar as the permanent system of
record going forward (identity, dead-issuer backfill from 1998, SF1 fundamentals, then nightly prices and
corporate actions after a 60-session parallel run), and **downgrade — do not cancel — FMP**, which remains the
licence under which the existing 1990–2026 history lives, unless FMP confirms in writing that downloaded data
may be retained after cancellation.

**Outcome at the end of S17 (2027-01-22):** 100% of existing price history retained; every schema change
additive; survivorship-free research from 1998-01-01; bitemporal SF1 fundamentals with a point-in-time factor
panel; Sharadar primary for new data (release v2.0.0); each vendor's data separately removable.

### Working agreements

1. **Fill-only, tagged, removable** (ADR 0012, SA-0103): a vendor never overwrites or relabels another
   vendor's row; every row says who supplied it; `reset_data.sh --scope vendor:<v>` can remove one vendor.
   The two safety rails (SA-0104 vendor-scoped loaders, SA-0105 fill-only writes) merge **before** any
   Sharadar row reaches `core`.
2. **Docs move with the code.** Every PR updates the docs its issue lists, in the same PR. Enforced by the
   PR template and the `docs-gate` CI check (SA-0101). The living plan
   (`doc/plans/sharadar-adoption.md`), the tracking issue and the milestone descriptions are brought current
   at every sprint close-out (`SA-xx99`).
3. **Measure, don't assume** (ADR 0007's rule): probes before loaders — `probe-sharadar` (SA-0201),
   `probe-fundamentals --vendor sharadar` (SA-0705), `probe-fund --vendor tiingo` (SA-0804).
4. **Operator executes production mutations** (ADR 0010): Claude Code sessions build and test; issues labelled
   `owner:operator` run on the host, each starting with a logical dump.
5. **Gates are decisions, recorded:** subscribe (SA-0205), fundamentals sign-off (SA-0801), cutover +
   FMP tier (SA-1503). Each writes a decision-log entry.
6. **No vendor data on GitHub.** The repository is public and every vendor licence forbids redistribution:
   tests use synthetic fixtures shaped like the feeds, and reports posted to issues or PRs carry aggregates
   only (examples cite `security_id` and date, never vendor values). Raw evidence stays on the host.
7. **Deploy what merged.** An operator issue that depends on new code starts by deploying `main` and
   running `fafnir db migrate`; every sprint close-out confirms the host runs the latest `main`.

### Calendar

Weekly sprints, Monday–Friday. Capacity: ~20 operator hours/week (reviewing and merging Claude Code PRs,
production runs, decisions). Sizes: S ≈ 1 h, M ≈ 2–3 h, L ≈ 4–6 h of *operator* time; planned load is kept
at or under 18 h against ~20 h of capacity.

| Sprint | Dates | Theme | Issues | Critical-path | Planned h |
|---|---|---|---|---|---|
| S01 | Sep 28–Oct 02 | Governance & safety rails | 7 | 5 | 17 |
| S02 | Oct 05–Oct 09 | Probe & vendor schema | 6 | 4 | 18 |
| S03 | Oct 12–Oct 16 | Subscribe, land, start the shadow clock | 7 | 3 | 15 |
| S04 | Oct 19–Oct 23 | Link the existing universe to permaticker | 4 | 2 | 14 |
| S05 | Oct 26–Oct 30 | Mint dead issuers safely | 7 | 4 | 17 |
| S06 | Nov 02–Nov 06 | Backfill dead-issuer prices & actions | 6 | 3 | 16 |
| S07 | Nov 09–Nov 13 | Survivorship QA, read path, SF1 probe | 6 | 1 | 16 |
| S08 | Nov 16–Nov 20 | Fundamentals design & schema | 5 | 2 | 15 |
| S09 | Nov 23–Nov 27 | Fundamentals loader | 5 | 2 | 15 |
| S10 | Nov 30–Dec 04 | Point-in-time marts, TTM, funds loader | 5 | 2 | 17 |
| S11 | Dec 07–Dec 11 | Factor panel & fundamentals DQ | 6 | 1 | 16 |
| S12 | Dec 14–Dec 18 | Fundamentals ops & docs; Sharadar nightly loaders | 5 | 1 | 15 |
| S13 | Dec 21–Dec 25 | Cutover preparation (holiday week) | 3 | 1 | 7 |
| S14 | Dec 28–Jan 01 | Buffer, rehearsal fixes, FMP dependency cleanup (holiday week) | 6 | 1 | 14 |
| S15 | Jan 04–Jan 08 | Gate: 60 sessions | 4 | 3 | 6 |
| S16 | Jan 11–Jan 15 | Cutover | 4 | 2 | 10 |
| S17 | Jan 18–Jan 22 | Stabilise & settle subscriptions | 5 | 0 | 9 |

**Key dates:** shadow capture (parallel-run session 1) **Tue 2026-10-13** · interim report #1 (session 20)
**Mon 2026-11-09** · interim report #2 (session 40) **Tue 2026-12-08** · session 60 **Thu 2027-01-07** ·
go/no-go **Fri 2027-01-08** · cutover **Mon 2027-01-11**. The 60-session parallel run, not the build work, sets
the end date; the fundamentals build and the holiday buffer (S13–S14) run inside it. The capture code is built
in S02 so the clock can start on the first paid day.

**If the clock slips:** the gate is 60 sessions from the first shadow capture. A late session 1 moves SA-1503,
S16 and S17 by the same number of sessions (re-date the milestones and say so in the tracking issue). Build
slips are absorbed by S13–S14; calendar slips are not.

### Refinements since the assessment

- The raw-recovery DQ check is `sharadar_split_ratio_mismatch`: recovery reproduces `closeunadj` by
  construction, so what can be wrong is the ratio against landed splits.
- Fill-only applies to FMP too; the only exception is an explicit, logged reclaim used by rollback.
- Reader-facing changes are additive (new mart views, `duk fs/fa`, `SYMBOL@DATE`); the one behavioural change
  considered — the screener's default treatment of dead issuers — is an explicit decision in SA-0701.
- `mart.security_latest` is re-defined once (SA-1205) so screener attributes keep updating after cutover; its
  columns and duk contract stay identical.
- Landing is loaded with client-side `COPY FROM STDIN` into typed tables (the ingest role is not a superuser).

### Workstreams

| Stream | Sprints | Issues |
|---|---|---|
| Governance, licensing, gates | S01–S02, S11, S15, S17 | SA-0102, SA-0205, SA-1105, SA-1503, SA-1701, SA-1702 |
| Safety rails & provenance | S01, S05 | SA-0103, SA-0104, SA-0105, SA-0505 |
| Sharadar access, landing, staging | S01–S03 | SA-0106, SA-0201, SA-0203, SA-0206, SA-0207, SA-0301, SA-0305, SA-0308 |
| Identity (permaticker) & universe | S03–S05, S07, S09, S12 | SA-0306, SA-0307, SA-0304, SA-0401, SA-0402, SA-0404, SA-0501, SA-0702, SA-0904, SA-1205 |
| Survivorship backfill | S05–S07 | SA-0502, SA-0503, SA-0601, SA-0602, SA-0603, SA-0604, SA-0605, SA-0701, SA-0703 |
| Parallel run & reconciliation | S05–S15 | SA-0506, SA-0504, SA-0704, SA-1104, SA-1502 |
| Fundamentals (SF1, full build) | S07–S12 | SA-0705, SA-0801, SA-0802, SA-0803, SA-0901, SA-0902, SA-0903, SA-1001, SA-1002, SA-1101, SA-1102, SA-1103, SA-1201, SA-1202, SA-1203 |
| Funds & FMP-dependent tooling | S08–S14 | SA-0804, SA-1003, SA-1105, SA-1405, SA-1402, SA-1403, SA-1404 |
| Cutover | S10–S17 | SA-1005, SA-1301, SA-1302, SA-1401, SA-1501, SA-1601, SA-1602, SA-1603, SA-1703 |
| Documentation & close-out | every sprint | SA-0101, SA-xx99, SA-1704 |

### Migrations (expected numbers — take the next free number at merge time)

| Expected | Issue | Adds |
|---|---|---|
| 0027 | SA-0203 | `landing.sharadar_file` + typed Sharadar landing tables |
| 0028 | SA-0306 | `core.vendor_security`, `core.security_vendor_xref`, `core.vendor_security_event` |
| 0029 | SA-0506 | `ops.vendor_bar_comparison`, `ops.vendor_bar_divergence` |
| 0030 | SA-0701 | `mart.v_security_lifecycle` |
| 0031 | SA-0803 | fundamentals statement tables, `ref.statement_line_item` (+ seed 0002) |
| 0032 | SA-1001 | point-in-time functions/views, TTM |
| 0033 | SA-1101 | `mart.fundamental_panel`, `fn_fundamental_snapshot` |
| 0034 | SA-1102 | `mart.v_industry_fundamentals`, `mart.security_fundamentals_latest` |
| 0035 | SA-1205 | attribute precedence view; `mart.security_latest` re-defined (same columns) |
| (optional) | SA-1405, SA-1402 | fund distribution types; FRED economic series |

### Releases

v1.1.0 at S07 (survivorship backfill, after the screener-default decision and DQ triage) · v1.2.0 at S12
(fundamentals) · **v2.0.0 at S16** (Sharadar primary).

### Costs by phase

| Phase | Sharadar | FMP | Funds | ≈ annual rate |
|---|---|---|---|---|
| S01–S02 | $0 (free sample) | Premium $708 | FMP | $708 |
| S03–S15 | $69/mo | Premium $708 | FMP + Tiingo (free) | ~$1,536 run-rate (~$1,207 once Sharadar is annual) |
| After S17, FMP → Starter | $499 | ~$264 | Tiingo | ~$763 |
| After S17, FMP stays Premium | $499 | $708 | Tiingo | ~$1,207 |

### Top risks

| Risk | Where it is handled |
|---|---|
| FMP's §5 retention clause does not cover the warehouse | SA-0102 → SA-1503 (tier decision) |
| Personal-licence eligibility fails (RIA/professional use) | SA-0102 — stops the plan before any payment |
| SEP precision too coarse to recover exact raw prices for split-heavy dead issuers | SA-0201, SA-0601 (`sharadar_split_ratio_mismatch`) |
| A vendor seam between FMP and Sharadar bars in live series | SA-0506, SA-0704, SA-1104, SA-1502; NO-GO keeps FMP Premium as the price feed |
| Reused-ticker histories duplicated across vendors | SA-0503 (`vendor_history_overlap`) |
| Host disk | SA-0203 projection, SA-0205 gate (attach a Hetzner Volume if needed) |
| Backfill floods the DQ queue | SA-0604 (measure first), SA-0703 (bulk triage) |

## Status by sprint

### S01 · Governance & safety rails (Sep 28–Oct 02)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0101` | [#42](https://github.com/rtrimble13/fafnir/issues/42) | Stand up the documentation machinery: living plan, PR template, docs-gate CI | Claude Code | M | ⬜ |  |
| `SA-0102` | [#43](https://github.com/rtrimble13/fafnir/issues/43) | Send the licence questions to FMP and Sharadar; confirm personal-licence eligibility | Operator | S | ⬜ |  |
| `SA-0103` | [#44](https://github.com/rtrimble13/fafnir/issues/44) | ADR 0012 — multi-vendor provenance, fill-only writes and per-vendor separability | Claude Code | M | ⬜ |  |
| `SA-0104` | [#45](https://github.com/rtrimble13/fafnir/issues/45) | Scope every FMP loader, guard and ticker check to FMP-fed securities | Claude Code | M | ⬜ |  |
| `SA-0105` | [#46](https://github.com/rtrimble13/fafnir/issues/46) | Fill-only write path for secondary vendors (prices and corporate actions) | Claude Code | M | ⬜ |  |
| `SA-0106` | [#47](https://github.com/rtrimble13/fafnir/issues/47) | Sharadar configuration, secret handling and API client skeleton | Claude Code | M | ⬜ |  |
| `SA-0199` | [#48](https://github.com/rtrimble13/fafnir/issues/48) | Sprint 01 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S02 · Probe & vendor schema (Oct 05–Oct 09)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0201` | [#49](https://github.com/rtrimble13/fafnir/issues/49) | `fafnir source probe-sharadar` — answer the open questions from the free sample | Claude Code | L | ⬜ |  |
| `SA-0203` | [#50](https://github.com/rtrimble13/fafnir/issues/50) | Migration 0027 — Sharadar landing: file ledger on disk + typed landing tables | Claude Code | M | ⬜ |  |
| `SA-0205` | [#51](https://github.com/rtrimble13/fafnir/issues/51) | Gate — subscribe to Sharadar? | Operator | S | ⬜ |  |
| `SA-0206` | [#52](https://github.com/rtrimble13/fafnir/issues/52) | Bulk downloader: `fafnir source sharadar-bulk` → content-addressed files + ledger | Claude Code | L | ⬜ |  |
| `SA-0207` | [#53](https://github.com/rtrimble13/fafnir/issues/53) | Nightly shadow capture: `fafnir ingest sharadar --shadow` (landing only) | Claude Code | M | ⬜ |  |
| `SA-0299` | [#54](https://github.com/rtrimble13/fafnir/issues/54) | Sprint 02 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S03 · Subscribe, land, start the shadow clock (Oct 12–Oct 16)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0306` | [#55](https://github.com/rtrimble13/fafnir/issues/55) | Migration 0028 — vendor identity: `core.vendor_security`, `core.security_vendor_xref`, `core.vendor_security_event` | Claude Code | M | ⬜ |  |
| `SA-0307` | [#56](https://github.com/rtrimble13/fafnir/issues/56) | Boundary mappings: exchange, category → asset type, split ratios, dividend conversion | Claude Code | S | ⬜ |  |
| `SA-0301` | [#57](https://github.com/rtrimble13/fafnir/issues/57) | Subscribe to Sharadar (Bundle, full history, monthly) and install the key | Operator | S | ⬜ |  |
| `SA-0304` | [#58](https://github.com/rtrimble13/fafnir/issues/58) | Load Sharadar's security master and lifecycle events into vendor tables | Claude Code | M | ⬜ |  |
| `SA-0305` | [#59](https://github.com/rtrimble13/fafnir/issues/59) | Production: initial bulk load, vendor master load, start the shadow timer | Operator | M | ⬜ |  |
| `SA-0308` | [#60](https://github.com/rtrimble13/fafnir/issues/60) | Staging database: provision it and script its refresh from the latest dump | Operator | M | ⬜ |  |
| `SA-0399` | [#61](https://github.com/rtrimble13/fafnir/issues/61) | Sprint 03 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S04 · Link the existing universe to permaticker (Oct 19–Oct 23)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0401` | [#62](https://github.com/rtrimble13/fafnir/issues/62) | Linking engine: `fafnir vendor link` — existing securities → permaticker | Claude Code | L | ⬜ |  |
| `SA-0402` | [#63](https://github.com/rtrimble13/fafnir/issues/63) | Manual link review: `fafnir vendor link show|accept|reject` | Claude Code | M | ⬜ |  |
| `SA-0404` | [#64](https://github.com/rtrimble13/fafnir/issues/64) | Production: run linking, triage the queue, record the hit rate | Operator | L | ⬜ |  |
| `SA-0499` | [#65](https://github.com/rtrimble13/fafnir/issues/65) | Sprint 04 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S05 · Mint dead issuers safely (Oct 26–Oct 30)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0506` | [#66](https://github.com/rtrimble13/fafnir/issues/66) | Migration 0029 + cross-vendor reconciliation: `fafnir vendor reconcile` | Claude Code | L | ⬜ |  |
| `SA-0501` | [#67](https://github.com/rtrimble13/fafnir/issues/67) | ADR 0013 — Sharadar as identity authority; survivorship floor 1998; dead-issuer inclusion rules | Claude Code | S | ⬜ |  |
| `SA-0503` | [#68](https://github.com/rtrimble13/fafnir/issues/68) | Detect reused-ticker overlaps before loading: `vendor_history_overlap` | Claude Code | M | ⬜ |  |
| `SA-0504` | [#69](https://github.com/rtrimble13/fafnir/issues/69) | Historical cross-vendor comparison (FMP vs SEP/SFP, last 12 months) — gate evidence #1 | Claude Code | S | ⬜ |  |
| `SA-0505` | [#70](https://github.com/rtrimble13/fafnir/issues/70) | Vendor excision: `scripts/reset_data.sh --scope vendor:sharadar` | Claude Code | M | ⬜ |  |
| `SA-0502` | [#71](https://github.com/rtrimble13/fafnir/issues/71) | Mint dead issuers: `fafnir vendor mint-delisted` | Claude Code | M | ⬜ |  |
| `SA-0599` | [#72](https://github.com/rtrimble13/fafnir/issues/72) | Sprint 05 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S06 · Backfill dead-issuer prices & actions (Nov 02–Nov 06)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0602` | [#73](https://github.com/rtrimble13/fafnir/issues/73) | Sharadar corporate-actions loader: splits and dividends → `core.corporate_action` | Claude Code | M | ⬜ |  |
| `SA-0603` | [#74](https://github.com/rtrimble13/fafnir/issues/74) | Amend ADR 0001/0004: vendor-derived raw prices for Sharadar history; observed raw going forward | Claude Code | S | ⬜ |  |
| `SA-0601` | [#75](https://github.com/rtrimble13/fafnir/issues/75) | Sharadar price loader (backfill mode): recovered raw OHLCV into `core.daily_price` | Claude Code | L | ⬜ |  |
| `SA-0604` | [#76](https://github.com/rtrimble13/fafnir/issues/76) | Pre-measure and scope DQ on the backfilled population | Claude Code | M | ⬜ |  |
| `SA-0605` | [#77](https://github.com/rtrimble13/fafnir/issues/77) | Production: mint + backfill dead issuers (prices → actions → adjust → marts → DQ) | Operator | M | ⬜ |  |
| `SA-0699` | [#78](https://github.com/rtrimble13/fafnir/issues/78) | Sprint 06 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S07 · Survivorship QA, read path, SF1 probe (Nov 09–Nov 13)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0701` | [#79](https://github.com/rtrimble13/fafnir/issues/79) | Survivorship QA and `mart.v_security_lifecycle` | Claude Code | M | ⬜ |  |
| `SA-0702` | [#80](https://github.com/rtrimble13/fafnir/issues/80) | Point-in-time ticker resolution (`CC@2008-06-30`) in repository, duk and MCP | Claude Code | M | ⬜ |  |
| `SA-0703` | [#81](https://github.com/rtrimble13/fafnir/issues/81) | Triage the backfilled DQ queue (bulk mode, fafnir-dba agent assisted) | Operator | M | ⬜ |  |
| `SA-0704` | [#82](https://github.com/rtrimble13/fafnir/issues/82) | Parallel-run interim report #1 (20 sessions) | Operator | S | ⬜ |  |
| `SA-0705` | [#83](https://github.com/rtrimble13/fafnir/issues/83) | Probe SF1: `fafnir source probe-fundamentals --vendor sharadar` | Claude Code | L | ⬜ |  |
| `SA-0799` | [#84](https://github.com/rtrimble13/fafnir/issues/84) | Sprint 07 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S08 · Fundamentals design & schema (Nov 16–Nov 20)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0801` | [#85](https://github.com/rtrimble13/fafnir/issues/85) | Retarget the fundamentals build prompt to SF1 — sign-off gate | Claude Code | M | ⬜ |  |
| `SA-0802` | [#86](https://github.com/rtrimble13/fafnir/issues/86) | ADR 0014 (bitemporal fundamentals on SF1) and ADR 0015 (derived metrics, not vendor metrics) | Claude Code | M | ⬜ |  |
| `SA-0803` | [#87](https://github.com/rtrimble13/fafnir/issues/87) | Migration — fundamentals statement tables, `ref.statement_line_item`, seed | Claude Code | L | ⬜ |  |
| `SA-0804` | [#88](https://github.com/rtrimble13/fafnir/issues/88) | Tiingo client and `probe-fund --vendor tiingo` for all eight funds | Claude Code | M | ⬜ |  |
| `SA-0899` | [#89](https://github.com/rtrimble13/fafnir/issues/89) | Sprint 08 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S09 · Fundamentals loader (Nov 23–Nov 27)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-0901` | [#90](https://github.com/rtrimble13/fafnir/issues/90) | SF1 loader: bitemporal statements (bulk backfill + nightly delta) | Claude Code | L | ⬜ |  |
| `SA-0902` | [#91](https://github.com/rtrimble13/fafnir/issues/91) | Fundamentals integration tests: idempotency, restatement, golden values, upgrade path | Claude Code | M | ⬜ |  |
| `SA-0903` | [#92](https://github.com/rtrimble13/fafnir/issues/92) | Production: fundamentals backfill; publish `knowledge_source` by decade | Operator | S | ⬜ |  |
| `SA-0904` | [#93](https://github.com/rtrimble13/fafnir/issues/93) | Sharadar-driven universe maintenance: renames, delistings, new listings via the xref | Claude Code | L | ⬜ |  |
| `SA-0999` | [#94](https://github.com/rtrimble13/fafnir/issues/94) | Sprint 09 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S10 · Point-in-time marts, TTM, funds loader (Nov 30–Dec 04)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1001` | [#95](https://github.com/rtrimble13/fafnir/issues/95) | Migration — point-in-time access and TTM: `fn_statements_as_of`, `v_statements_current`, `v_fundamentals_ttm` | Claude Code | L | ⬜ |  |
| `SA-1002` | [#96](https://github.com/rtrimble13/fafnir/issues/96) | `refresh_marts`: iterate a matview list and stop swallowing real failures | Claude Code | M | ⬜ |  |
| `SA-1003` | [#97](https://github.com/rtrimble13/fafnir/issues/97) | Tiingo fund loader: NAV + distributions, fill-only against FMP NAV history | Claude Code | M | ⬜ |  |
| `SA-1005` | [#98](https://github.com/rtrimble13/fafnir/issues/98) | Sharadar nightly prices and actions + `fafnir vendor feed` | Claude Code | L | ⬜ |  |
| `SA-1099` | [#99](https://github.com/rtrimble13/fafnir/issues/99) | Sprint 10 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S11 · Factor panel & fundamentals DQ (Dec 07–Dec 11)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1101` | [#100](https://github.com/rtrimble13/fafnir/issues/100) | Migration — `mart.fundamental_panel` + `fn_fundamental_snapshot`; look-ahead invariant; split-era test | Claude Code | L | ⬜ |  |
| `SA-1102` | [#101](https://github.com/rtrimble13/fafnir/issues/101) | Migration — `mart.v_industry_fundamentals` and `mart.security_fundamentals_latest` | Claude Code | M | ⬜ |  |
| `SA-1103` | [#102](https://github.com/rtrimble13/fafnir/issues/102) | Fundamentals DQ checks and `fafnir status` lines | Claude Code | M | ⬜ |  |
| `SA-1104` | [#103](https://github.com/rtrimble13/fafnir/issues/103) | Parallel-run interim report #2 (40 sessions) | Operator | S | ⬜ |  |
| `SA-1105` | [#104](https://github.com/rtrimble13/fafnir/issues/104) | Production: Tiingo terms, key, fund declarations and first load | Operator | M | ⬜ |  |
| `SA-1199` | [#105](https://github.com/rtrimble13/fafnir/issues/105) | Sprint 11 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S12 · Fundamentals ops & docs; Sharadar nightly loaders (Dec 14–Dec 18)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1201` | [#106](https://github.com/rtrimble13/fafnir/issues/106) | Fundamentals cadence: nightly SF1 delta + mart refresh in the nightly job | Claude Code | M | ⬜ |  |
| `SA-1202` | [#107](https://github.com/rtrimble13/fafnir/issues/107) | `duk fs` / `duk fa` and MCP read tools for fundamentals | Claude Code | M | ⬜ |  |
| `SA-1203` | [#108](https://github.com/rtrimble13/fafnir/issues/108) | `doc/fundamentals.md` researcher guide; roadmap and README updates | Claude Code | M | ⬜ |  |
| `SA-1205` | [#109](https://github.com/rtrimble13/fafnir/issues/109) | Security attributes after cutover: sector, industry, market cap, beta for Sharadar-fed securities | Claude Code | L | ⬜ |  |
| `SA-1299` | [#110](https://github.com/rtrimble13/fafnir/issues/110) | Sprint 12 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S13 · Cutover preparation (holiday week) (Dec 21–Dec 25)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1301` | [#111](https://github.com/rtrimble13/fafnir/issues/111) | `daily_update.sh` vendor switch, FMP loaders off by config, tested rollback | Claude Code | M | ⬜ |  |
| `SA-1302` | [#112](https://github.com/rtrimble13/fafnir/issues/112) | Staging dress rehearsal: Sharadar-primary nightly on a restored copy for ≥ 5 sessions | Operator | M | ⬜ |  |
| `SA-1399` | [#113](https://github.com/rtrimble13/fafnir/issues/113) | Sprint 13 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S14 · Buffer, rehearsal fixes, FMP dependency cleanup (holiday week) (Dec 28–Jan 01)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1405` | [#114](https://github.com/rtrimble13/fafnir/issues/114) | Distribution action types for funds (income vs short/long capital gains) — optional | Claude Code | S | ⬜ |  |
| `SA-1401` | [#115](https://github.com/rtrimble13/fafnir/issues/115) | Fix dress-rehearsal findings | Claude Code | M | ⬜ |  |
| `SA-1402` | [#116](https://github.com/rtrimble13/fafnir/issues/116) | Treasury curve from FRED for `duk yc -S db` (minimal economic-series tables) | Claude Code | L | ⬜ |  |
| `SA-1403` | [#117](https://github.com/rtrimble13/fafnir/issues/117) | `scripts/reconcile.sh` becomes a cross-vendor check | Claude Code | S | ⬜ |  |
| `SA-1404` | [#118](https://github.com/rtrimble13/fafnir/issues/118) | Operations tooling for a two-vendor warehouse | Claude Code | M | ⬜ |  |
| `SA-1499` | [#119](https://github.com/rtrimble13/fafnir/issues/119) | Sprint 14 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S15 · Gate: 60 sessions (Jan 04–Jan 08)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1501` | [#120](https://github.com/rtrimble13/fafnir/issues/120) | Cutover runbook and fafnir-dba skill updates | Claude Code | M | ⬜ |  |
| `SA-1502` | [#121](https://github.com/rtrimble13/fafnir/issues/121) | Parallel-run final report (60 sessions) | Claude Code | S | ⬜ |  |
| `SA-1503` | [#122](https://github.com/rtrimble13/fafnir/issues/122) | Gate — go/no-go for cutover; FMP tier decision | Operator | S | ⬜ |  |
| `SA-1599` | [#123](https://github.com/rtrimble13/fafnir/issues/123) | Sprint 15 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S16 · Cutover (Jan 11–Jan 15)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1602` | [#124](https://github.com/rtrimble13/fafnir/issues/124) | Release v2.0.0 — Sharadar primary; README/architecture/ingestion rewritten for it | Claude Code | M | ⬜ |  |
| `SA-1601` | [#125](https://github.com/rtrimble13/fafnir/issues/125) | Production cutover: Sharadar becomes the primary nightly vendor | Operator | M | ⬜ |  |
| `SA-1603` | [#126](https://github.com/rtrimble13/fafnir/issues/126) | Post-cutover monitoring: five sessions | Operator | M | ⬜ |  |
| `SA-1699` | [#127](https://github.com/rtrimble13/fafnir/issues/127) | Sprint 16 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

### S17 · Stabilise & settle subscriptions (Jan 18–Jan 22)

| Key | Issue | Title | Owner | Size | State | PR |
|---|---|---|---|---|---|---|
| `SA-1701` | [#128](https://github.com/rtrimble13/fafnir/issues/128) | Change the FMP subscription per the licence answer | Operator | S | ⬜ |  |
| `SA-1702` | [#129](https://github.com/rtrimble13/fafnir/issues/129) | Move Sharadar to its long-term plan | Operator | S | ⬜ |  |
| `SA-1703` | [#130](https://github.com/rtrimble13/fafnir/issues/130) | Mark FMP-only machinery as legacy/secondary; ADR status notes | Claude Code | M | ⬜ |  |
| `SA-1704` | [#131](https://github.com/rtrimble13/fafnir/issues/131) | Close the plan: `[built]` notes, final docs audit, retrospective | Claude Code | M | ⬜ |  |
| `SA-1799` | [#132](https://github.com/rtrimble13/fafnir/issues/132) | Sprint 17 close-out: docs audit, tracking update, carry-over | Operator | S | ⬜ |  |

## Parallel-run evidence

| Report | Sessions | Date | Result |
|---|---|---|---|
| Historical comparison (SA-0504) | 12 months of history | S05 |  |
| Interim #1 (SA-0704) | 1–20 | 2026-11-09 |  |
| Interim #2 (SA-1104) | 1–40 | 2026-12-08 |  |
| Final (SA-1502) | 1–60 | 2027-01-07 |  |

## Decision log

| Date | Decision | Where |
|---|---|---|
| 2026-09-23 | Adopt COA 2-M: Sharadar permanent; FMP downgraded, not cancelled, unless FMP confirms retention in writing | [assessment](sharadar-coa-assessment.md) |
| 2026-09-23 | Weekly sprints S01–S17 from 2026-09-28; ~20 operator h/week; full SF1 fundamentals in scope; 60-session parallel run | this plan |

## [built] notes

_None yet — add one wherever the implementation departs from this plan, as `doc/plans/db-operations-agent.md` does._
