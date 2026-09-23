> Imported from the Claude project (`claude/sharadar-coa-assessment.md`) on 2026-09-23. It builds on the
> vendor assessment (`claude/fafnir-vendor-assessment.md`), which remains in the Claude project.

# Sharadar Courses of Action — Assessment for `fafnir`

**Question asked:** The warehouse has survivorship issues because of FMP's limited history. Two courses of action (COAs) use Sharadar:

- **COA 1:** Subscribe to Sharadar, backfill historical prices and populate fundamentals once, cancel Sharadar, then continue on FMP.
- **COA 2:** Keep Sharadar going forward and cancel FMP.

Assess each and recommend one, minimizing disruption: keep most existing price history and the database structure.

**Date:** 2026-09-23
**Builds on:** `claude/fafnir-vendor-assessment.md` (rev. 4) and `claude/fafnir-fundamentals-build-prompt.md`
**Repo reviewed:** `rtrimble13/fafnir` @ `main` `7830505` (post-v1.0.0; 26 migrations; ~53k lines of Python)
**Caveat:** §3 reads vendor licence terms. It is a careful reading, not legal advice.

---

## 1. Bottom line

**Neither COA works as written, and for the same reason: each ends by cancelling a vendor whose licence says you must then delete that vendor's data.**

| | COA 1: backfill with Sharadar, cancel it, stay on FMP | COA 2: move to Sharadar, cancel FMP |
|---|---|---|
| What cancellation obliges you to delete | Everything Sharadar supplied: the dead-company backfill and the fundamentals. The deadline is **30 days**, and Sharadar can ask for an **affidavit** of deletion. | Under FMP ToS §6.2–6.3, **everything FMP supplied**: 1990–2026 prices, corporate actions, security-master attributes and `landing.fmp_raw`. A narrower retention clause in §5 may save it; the text is ambiguous. |
| Effect on the survivorship problem | Fixed for 30 days, then back where you started | Fixed from 1998, but by replacing the warehouse rather than extending it |
| Existing price history kept? | Yes, untouched | **Not on the cautious reading of FMP's terms** |
| Verdict | **Not viable.** The clause is explicit. | **Right destination, wrong exit** |

### Recommendation: modified COA 2 ("COA 2-M"): adopt Sharadar, downgrade FMP instead of cancelling it

1. **Sharadar Core US Equities Bundle (full history) becomes the permanent system of record going forward.** It supplies:
   - identity (`permaticker`);
   - the dead-company backfill from 1998;
   - point-in-time fundamentals (SF1);
   - nightly prices and corporate actions, once a parallel run passes.
2. **FMP stays subscribed, at a lower tier.** It is the licence under which your existing 1990–2026 history lives. Cancel it only if FMP confirms in writing that you may keep data you have already downloaded.
3. **Separate the vendors' data.** Every row stays tagged by vendor, and Sharadar never overwrites a row FMP supplied. If either vendor is dropped later, removing its data is a `DELETE`, not a rebuild. Given §3, this is a requirement.

**Outcome:**

- All existing price history stays.
- Every schema change is additive.
- Research is survivorship-free from 1998. Before 1998 it is not: neither vendor has delistings earlier than that, under any COA.

| | Annual cost |
|---|---|
| Today (FMP Premium) | $708 |
| COA 2-M during the transition (Sharadar $499 + FMP Premium $708) | ~$1,207, pro-rated over ~3–6 months |
| **COA 2-M steady state** (Sharadar $499 + FMP Starter ~$264 + Tiingo free tier for funds) | **~$763** |
| COA 2-M keeping FMP Premium permanently | ~$1,207 |

All of these are within the $1,000–3,000 budget.

---

## 2. What exactly is broken, according to the code

The survivorship gap is not really about how far back FMP's history goes. It comes from how the universe is built.

- **The universe comes from today's screener.** `ingest/security_master.py` re-reads FMP's `company-screener` for `SCREENER_EXCHANGES`. So the warehouse holds what was listed when fafnir first ran (Aug 2026), plus everything that has listed since.
- **The delisting sweep only marks names fafnir already holds.** `ingest/delisted.py` skips any feed row it does not recognize ("A name fafnir never tracked ... skip it"). Its own docstring states the limit (`delisted.py:14–19`):
  > *"FMP serves no EOD history for long-dead tickers (LEH, WCOM, ENRNQ and friends all return zero bars), and its delisted list is shallow. ... It cannot reconstruct issuers that died before fafnir first saw them -- that needs a vendor with real delisted history (Sharadar, Norgate, CRSP)."*
- **The backfill says so too.** `doc/backfill.md` notes that "this backfill covers active securities" and lists delisted retention as "a fast-follow".

So fafnir is survivorship-free **from its first nightly run onward** and survivor-only before that. The gap is every issuer that died between 1990 and Aug 2026. Sharadar's datasheet counts more than **5,000 active and 9,000 delisted** companies. That figure is older; the current docs say "nearly 18,000" in total. Either way, roughly **two out of three companies that traded between 1998 and 2026 are missing** from the warehouse.

**A quieter defect has the same cause.** FMP keys history by ticker, so a dead company's bars can sit *inside* a live security's history:

- `BID` holds Sotheby's 2003–2019.
- `CAPA` held a 2003–08 stock and a 2020–21 SPAC.

These are documented in the DBA skill's `data-semantics.md` §14. `fafnir security split-history` (PR #40) moves such bars by hand. Sharadar's `permaticker` marks those boundaries for you.

**One limit no COA removes: survivorship-free research starts in 1998.**

- An independent analysis of Sharadar's TICKERS data found *"no de-listings of any companies between 1986 and 1998"*.
- SEP (Sharadar's stock prices) starts in December 1997.
- SF1 (Sharadar's fundamentals) starts in January 1998.

1990–1997 stays survivor-only whichever vendor you use. That makes **1998-01-01 the floor for cross-sectional work**, and it should be recorded in an ADR rather than rediscovered.

---

## 3. The licence terms: this is what decides it

### 3.1 Sharadar Personal Use License, §10 (Termination)

> *"Upon termination, you will immediately stop using the Services and the Services Data."*

> *"Within thirty (30) days of termination, delete from all computer systems you own or operate all copies of the Services Data (including downloads, bulk files, caches, and extracts), all data sets that contain, substantially copy, or could reproduce the Services Data or Sharadar tables, and all software provided as part of the Services. If requested, you will promptly provide an affidavit certifying deletion."*

> *"You may keep research outputs, backtest results, models, summary statistics, trade logs, and similar derived works that do not contain and cannot reproduce the Services Data or Sharadar tables."*

QuantRocket, a Sharadar reseller, states the same rule: *"...all copies of the Services Data, and all data sets derived from the Services Data."*

**Applied to COA 1**, each thing it would put into fafnir falls inside that clause:

| What COA 1 puts into fafnir | Contains or can reproduce Sharadar data? |
|---|---|
| Unadjusted OHLCV for dead companies, recovered from SEP | **Yes.** Sharadar's own formula (`OpenUnadj = Open × CloseUnadj / Close`) reverses it exactly. |
| `core.corporate_action` rows from ACTIONS | **Yes.** |
| Dead securities: names, delisting dates, `permaticker` mappings | **Yes.** |
| Fundamentals statement rows from SF1 | **Yes.** |
| A backtest result computed while subscribed | No. You may keep it. |

The clause names bulk files and extracts explicitly, and the affidavit provision shows Sharadar expects to be able to check. **COA 1 is not a grey area.**

### 3.2 FMP Terms of Service (last updated 2023-08-01)

- **§6.2 (effect of termination):** licence rights end. The customer must stop using the Services and destroy, or return if FMP asks, *"all copies or other embodiments of … any and all data or information contained in or derived from The Services."* Note that this covers derived data as well as copies.
- **§6.3 (data deletion):** *"Customer must delete all Data it has received from FMP under all applicable Order Forms, including data cached"*. The customer also signs a Data Deletion Agreement, and FMP may audit compliance.
- **§5 (confidentiality):** has a narrower exception. *"If the Agreement is not terminated for cause, the Customer may retain copies of the reports or information printed or obtained through The Services,"* subject to the licence restrictions.
- I found no carve-out for personal plans. The terms apply to every tier.

**What this means for COA 2.** On the cautious reading, cancelling FMP obliges deleting:

- every `core.daily_price` row FMP supplied (1990–2026);
- FMP's corporate actions;
- FMP-sourced security attributes and profiles;
- all of `landing.fmp_raw`.

That is the opposite of what you want. The §5 exception *might* protect the warehouse, but "reports or information printed or obtained" is an odd way to describe a 36-year warehouse built from bulk API pulls, and §6.3 is the more specific clause. **Do not stake 36 years of history on "might".** Ask FMP in writing (Phase 0, §8.2).

Downgrading is not terminating: the Agreement stays in force. That is why COA 2-M downgrades rather than cancels. Neither vendor's terms mentions downgrades at all, so confirm that point in writing too.

### 3.3 Corrections to the 2026-09-02 vendor assessment

Three statements in rev. 4 did not take the termination clauses into account. The first two corrections apply wherever retention depends on cancelling FMP.

| Rev. 4 said | Correction |
|---|---|
| §6.4: "`landing.fmp_raw` holds the original payloads, so … cancelling FMP does not destroy provenance" | `landing.fmp_raw` is FMP Data under §6.3 and goes with everything else |
| §6.4 decision table: "Sharadar alone, ~$299–$499" | Viable only if FMP confirms retention in writing, or if you accept rebuilding the warehouse from 1998 |
| §7.3: take a full Sharadar bulk snapshot as insurance against Sharadar disappearing | A local copy is licensed only while you are subscribed; it is not insurance against leaving |

---

## 4. What Sharadar supplies, measured against what fafnir needs

**Coverage**

| Item | Fact | What it means for fafnir |
|---|---|---|
| **Plans (personal licence)** | Bundle with full history: **$499/yr** or **$69/mo**. Prices only: $299/yr. Fundamentals only: $399/yr. 5-year and 10-year tiers cost less. | Bundle is cheaper than Prices + Fundamentals ($698). Month-to-month suits a trial. |
| **SEP (stock prices)** | Active and delisted US stocks, from December 1997. More than 16,000 tickers per an older datasheet. | Closes the survivorship gap for equities from 1998 |
| **SFP (fund prices)** | ETFs, CEFs, ETNs and ETDs; about 10,000 tickers; from December 1997. **No mutual funds.** | Covers fafnir's ETF half. Does nothing for the 8 tracked mutual funds. |
| **SF1 (fundamentals)** | "Nearly 18,000 active and delisted" companies; primary share class; from January 1998 | Point-in-time fundamentals with genuine filing dates (below) |
| **SP500** | S&P 500 constituents from 1957 | Point-in-time index membership, a survivorship-safe benchmark universe |
| **Updates** | Daily at 17:30 and 23:30 ET | Compatible with the nightly job. Schedule it after 17:30 ET. |

**Schema**

| Item | Fact | What it means for fafnir |
|---|---|---|
| **SEP/SFP price fields** | `open`, `high`, `low`, `close`, `volume` are *split-adjusted*; `closeadj` is fully adjusted; `closeunadj` is unadjusted; plus `lastupdated` | Unadjusted values must be recovered (below) |
| **Recovering unadjusted values** | Sharadar documents `OpenUnadj = Open × CloseUnadj / Close` and `VolumeUnadj = Volume × Close / CloseUnadj` | Every unadjusted field, volume included, is recoverable |
| **TICKERS** | `permaticker` ("unique, unchanging identifier for a security"), `ticker`, `exchange`, `isdelisted`, `category`, `cusips`, `figi`, SIC and Fama codes, `sector`/`industry`, `firstpricedate`/`lastpricedate`, `secfilings` (contains the CIK), `relatedtickers` | The vendor-independent key that ADR 0005 calls "the right long-term answer" |
| **ACTIONS** | 18 action types, including `split`, `dividend`, `spinoff`, `delisted`, `tickerchangefrom`/`to`, `acquisitionby`, `mergerfrom`, `bankruptcyliquidation`, `regulatorydelisting`, `voluntarydelisting`; from 1998 | Delistings **with a reason and the acquirer**, which gives you a basis for approximating delisting returns |
| **ACTIONS dividend `value`** | Split-adjusted, according to third-party code (stockparfait). **Medium confidence.** | fafnir divides the as-declared dividend by the unadjusted prior close, so this needs converting (below) |
| **SF1 dimensions** | ARQ/ARY/ART are as-reported, indexed to the SEC filing date, and exclude restatements. MRQ/MRY/MRT include restatements. | Gives the build prompt's §4.5–4.6 real knowledge dates and real restatement history |
| **Decimal precision of SEP fields** | **Not documented** | Probe with the free sample (Phase 0) |

### 4.1 The precision objection mostly disappears for nightly loads

Vendor assessment §5.2b objected that recovering unadjusted OHLCV from SEP loses precision in deep history. That is true, but it only applies to **history**.

- A split-adjusted field is adjusted only for splits *after* its date. On the evening a bar is captured, no such split exists yet. So `close == closeunadj`, the ratio is exactly 1, and **a nightly Sharadar load is observed unadjusted OHLCV, volume included.**
- The same holds for dividends: a split-adjusted dividend captured on its ex-date equals the as-declared amount.
- Recovery is needed in only two places: backfilled history, and old bars that Sharadar revises later (visible through `lastupdated`).
- For a dead company, the ratio only includes splits between the bar and the delisting. For many dead names that is none, so the ratio is 1 and there is no error at all.

**The consequence:** switching the nightly feed to Sharadar does not break ADR 0001/0004's rule that unadjusted prices are observed rather than derived. Only the backfilled dead-company history is derived, and it should be labelled as such.

---

## 5. COA 1: backfill, then cancel Sharadar

**What it involves:**

1. Subscribe for a month or two (~$69–$138).
2. Bulk-download SEP, SFP, ACTIONS, TICKERS and SF1.
3. Mint the dead securities and load their bars and actions.
4. Build the fundamentals milestone on SF1.
5. Cancel Sharadar and return to FMP for everything.

| Dimension | Assessment |
|---|---|
| **Licence** | ❌ **Fails §10.** Everything loaded must be deleted within 30 days of cancelling (§3.1). |
| Existing price history | ✅ Untouched. The backfill only adds rows. |
| Schema | ✅ Additive |
| Survivorship | ⚠️ Fixed for 30 days, then deleted. Going forward, FMP plus fafnir's retention works again, but the historical gap returns. |
| Fundamentals | ❌ **A seam in every company that is still alive**, even setting the licence aside (details below). |
| Keeping data current | ❌ Sharadar revises history (`lastupdated`). After you cancel, no corrections reach the frozen copy. |
| Identity | ⚠️ Dead names get permatickers. New listings go back to FMP's ticker-keyed identity, with all the rename and ticker-reuse handling that implies. |
| Cost | ~$69–$138 once, plus FMP Premium $708/yr |

**The fundamentals seam.** At the cancellation date, every live company switches vendors in every fundamentals series:

- SF1's standardized indicators give way to FMP's line items, which have different definitions.
- Trailing-twelve-month windows and year-over-year growth that straddle the switch mix the two definitions.
- FMP's habit of reporting zero where it should report null (vendor assessment §3.4) comes back.
- Restatement history stops at the switch, because FMP returns only the current version of each period.

You would also build the fundamentals loader twice, once for each vendor's taxonomy, and one of the two would be dead code the day you cancelled.

**Verdict: not viable.** It is not close. Even without the licence clause, the fundamentals seam would argue against it.

> **A variant worth asking about (not a rescue):** backfill on the full-history plan, then *downgrade* Sharadar to a 5-year plan ($299 instead of $499) rather than cancelling. Downgrading is not termination. The terms do not say whether a 5-year plan covers continued use of full-history data downloaded earlier. **Ask Sharadar.** If the answer is yes, this saves $200/yr on the recommended path. It does not rescue COA 1, because you would still be a subscriber.

---

## 6. COA 2: Sharadar going forward, cancel FMP

**What it involves:**

1. Map every existing security to a `permaticker`.
2. Backfill the dead securities.
3. Move the nightly security master, prices and corporate actions to Sharadar.
4. Build fundamentals on SF1.
5. Move the mutual funds to another vendor.
6. Rework `duk -S live`, `duk yc` and `scripts/reconcile.sh`, which all call FMP.
7. Cancel FMP.

| Dimension | Assessment |
|---|---|
| **Licence** | ❌ **FMP §6.2–6.3 on the cautious reading** (§3.2). If FMP confirms in writing that you may keep the data, this becomes ✅ and is the cheapest option. |
| Existing price history | ❌ At risk under the licence. If it may be kept, ✅ it is untouched. |
| Schema | ✅ Additive (§8.3) |
| Survivorship | ✅ Fixed from 1998 |
| Identity | ✅✅ **The biggest structural win** (details below) |
| Fundamentals | ✅ Built once, on SF1, with genuine point-in-time data from 1998 |
| Nightly prices | ✅ Observed unadjusted prices (§4.1) |
| Backfilled prices | ⚠️ Derived from split-adjusted fields for dead names. Label them as derived in a new ADR. |
| **Vendor seam** | ⚠️ Every live series switches vendor at cutover. FMP and Sharadar may differ in which exchange's close they report, how they count volume, and how they correct errors. Unmeasured, so a parallel run is required. |
| **Dividends** | ⚠️ ACTIONS values appear to be split-adjusted. Convert to as-declared with Sharadar's own ratio, `D_declared = D × closeunadj ÷ close` taken on the ex-date. For dividends captured on their ex-date the ratio is 1. |
| **Mutual funds** | ❌ SFP has none. All 8 tracked funds need another vendor (Tiingo's free tier per the vendor assessment §5.3). Today FMP covers 3 of them. |
| **Tooling tied to FMP** | ❌ `duk -S live`, `duk yc` (treasury rates) and `scripts/reconcile.sh` all call FMP. `probe-prices`, the volume verdicts and `_guard_split_adjusted_changeover` encode FMP-specific knowledge that does not carry over. |
| Cost | ~$499/yr, plus $0–300/yr for funds |

**Why identity is the biggest win.** Much of fafnir's recent engineering exists because FMP keys everything on the ticker:

- migrations 0009, 0011, 0012, 0015 and 0018;
- `security merge`, `dedupe` and `split-history`;
- the delisting-contradiction guard (the CMDT case).

ADR 0005 already says a vendor-independent key *"remains the right long-term answer"*. `permaticker` is that key, supplied by the vendor.

**Verdict: the right destination.** Ending FMP is the step that breaks it.

---

## 7. Side by side

| Criterion | COA 1 | COA 2 as written | **COA 2-M (recommended)** |
|---|---|---|---|
| Compliant with both vendors' licences | ❌ | ❌ unless FMP confirms retention | ✅ |
| Existing 1990–2026 price history kept | ✅, but the backfill is lost | ❌ at risk | ✅ **100%** |
| Database structure kept | ✅ | ✅ | ✅ additive only |
| Survivorship-free from 1998 | ❌ after 30 days | ✅ | ✅ |
| Point-in-time fundamentals | ❌ seam at cancellation | ✅ SF1 | ✅ SF1 |
| Vendor-independent identity | ❌ frozen | ✅ | ✅ |
| Mutual funds | ✅ FMP covers 3 of 8 | ❌ none from FMP | ✅ Tiingo; FMP-sourced NAV history kept |
| Ongoing cost per year | $708 | ~$499–$799 | ~$763 steady state (~$1,207 with FMP Premium) |
| Build effort | Medium, and thrown away | Large | Large, in phases, each reversible |
| Reversibility | n/a | Low | **High.** Each vendor's data can be removed on its own. |

---

## 8. Recommendation: COA 2-M, adopt Sharadar and downgrade FMP

### 8.1 Target state

| Domain | Now | After Phase 1 | After cutover (Phase 3) |
|---|---|---|---|
| Existing 1990–2026 bars (live names, and names FMP has marked delisted) | FMP | FMP, **unchanged** (`source='fmp'`) | FMP, **unchanged** |
| Dead issuers, 1998–2026 | *absent* | **Sharadar** SEP/SFP, unadjusted values recovered from split-adjusted fields (`source='sharadar'`) | Sharadar |
| Nightly prices and corporate actions | FMP | FMP. Sharadar runs alongside, landed but not loaded into `core`. | **Sharadar**, observed unadjusted (§4.1) |
| Minting new securities | FMP screener | FMP screener, plus a `permaticker` link for every security | **Sharadar TICKERS.** FMP tickers are linked to it, not the other way round. |
| Renames and delistings | FMP sweeps | FMP sweeps | Sharadar ACTIONS, which include the delisting reason |
| Fundamentals | *not built* | **SF1**, built once | SF1 |
| Mutual fund NAVs | FMP (3 of 8) | FMP (3) + Tiingo (5) | Tiingo for all 8 if FMP drops below Premium. FMP NAV history is kept. |
| `duk -S live`, `duk yc` | FMP | FMP | FMP Starter while subscribed. FRED for `yc` (already on the roadmap). |

### 8.2 Phases and gates

**Phase 0: before paying anything (free, this week)**

1. **Ask FMP in writing:**
   - After *cancellation*, may data already downloaded be kept for personal, non-commercial use? (§5 against §6.3.)
   - Does *downgrading* to Starter keep that right?

   The answer decides Phase 3.
2. **Ask Sharadar in writing:** may full-history data downloaded under a full-history plan be kept after downgrading to a 5-year plan? (§5, the variant box.)
3. **Confirm you qualify for the personal licence.** Sharadar requires a natural person, not "a company, partnership, trust, fund…", and no professional use. This is the RIA question from vendor assessment §7.2, and it is still open.
4. **Build `fafnir source probe-sharadar` against the free Dow-30 sample.** It should report:
   - the decimal precision of SEP fields;
   - whether recovered unadjusted close reproduces `closeunadj` exactly;
   - whether ACTIONS dividends are split-adjusted (compare against a known pre-split dividend);
   - how SF1's field inventory compares with the build prompt's §4.3 column list.

   Like the existing probes, it should write nothing and exit non-zero on failure.
5. **Land the two code fixes in §8.3 items 1–2** before any Sharadar row reaches `core`.

**Phase 1: subscribe (Bundle, full history)**

Pay month-to-month ($69) until the Phase 2 gate passes, then switch to annual ($499). Keep FMP Premium throughout.

1. **Load TICKERS** and link every existing `security_id` to a `permaticker` (§8.4).
2. **Mint the dead securities** (`source='sharadar'`) and load their SEP/SFP bars and ACTIONS. Loading is **fill-only**: Sharadar never writes a (security, date) key that already holds a row.
3. **Detect overlaps.** Before loading a dead `permaticker`, check whether an existing security already holds bars under that ticker within the permaticker's date span. If one does, **do not load it automatically**. Raise a `vendor_history_overlap` flag and repair it with `security split-history`. This is exactly the BID/CAPA case.
4. **Run `fafnir adjust`** on the new securities. The existing securities' adjustment factors are not touched.
5. **Build the fundamentals milestone on SF1.** The build prompt carries over largely unchanged (vendor assessment §8.2 step 9). Load ARQ as version 1 with `knowledge_date = datekey` (the SEC filing date). Treat differences in MRQ as restatement versions. **Probe first** what knowledge date MRQ rows can honestly carry.

**Phase 2: parallel run, at least 60 sessions**

Pull Sharadar SEP/SFP nightly into `landing` only, and compare it bar by bar with FMP. Suggested gate for cutover:

- at least **99.5%** of matched bars close within max($0.01, 1 basis point) of each other;
- the distribution of volume differences has been reviewed and explained (for example, consolidated versus primary-exchange volume);
- **fewer than 1%** of active FMP-universe securities lack a `permaticker` link after review;
- **zero** Sharadar writes to keys FMP holds, enforced by a test.

**Phase 3: cutover**

1. Sharadar takes over the nightly security master, prices and actions. FMP loaders are disabled in configuration but kept in the code.
2. Choose the FMP tier based on FMP's written answer:
   - **Downgrade to Starter** (~$264/yr) if FMP confirms a downgrade keeps your right to retain the data;
   - otherwise **stay on Premium**. This is also the choice if the parallel run shows a seam you would rather not have.
3. Move funds to Tiingo.
4. Point `duk yc` at FRED.

**If FMP confirms that you may keep the data after cancelling**, COA 2 as you wrote it becomes licence-compliant, and you can cancel FMP after Phase 3.

### 8.3 Code and schema changes (all additive)

**Nothing changes** to the grains of `core.daily_price`, `core.security`, `core.symbol_xref`, `core.corporate_action` or `core.adjustment_factor`, to any existing row, or to the `mart` views, `duk`, the MCP server or the DQ queue.

**1. Every FMP loader must select only FMP-fed securities. This has to land first.**

- Today the FMP loaders exclude only securities with `source = 'operator'`:
  - `VENDOR_FED_SECURITY` (`repository.py:2925`) defines that rule for the shared selectors;
  - `ingest prices` repeats it inline (`cli.py:373`);
  - `load_symbol_prices` has only an `is_operator_security` guard.
- So a dead security minted from Sharadar would be fed from FMP in two places:
  - `ingest prices --include-inactive`, the re-backfill path `doc/backfill.md` documents;
  - the corporate-actions first-load (`securities_without_actions_watermark`).
- That loader looks the security up by its ticker. FMP serves a ticker's whole history whoever used it (`data-semantics.md` §14), and the upsert is `DO UPDATE`. The dead issuer's Sharadar bars would be overwritten and mixed with other issuers' FMP bars. That is the same contamination `split-history` exists to repair.
- **Fix:** restrict every FMP universe and guard to FMP-fed securities (for example `source = 'fmp'`), and restrict the Sharadar loaders to their own securities.

**2. The Sharadar loader must be fill-only** (`upsert_daily_prices`, `repository.py:1699`).

- That function upserts with `ON CONFLICT … DO UPDATE SET …, source = EXCLUDED.source`.
- A Sharadar loader routed through it would silently overwrite FMP bars *and relabel them as Sharadar's*.
- Use `DO NOTHING` for any key held by another vendor, and add a test that fails if a Sharadar load changes an FMP row.

**3. New table `core.security_vendor_xref`:** `(security_id, vendor, vendor_key, valid_from, valid_to, match_method, confidence)`.

- The `permaticker` lives here rather than as a column on `core.security`.
- Removing Sharadar later is then `DELETE … WHERE vendor = 'sharadar'`.
- Keep Sharadar's reference attributes (sector, SIC, FIGI, delisting reason) here or in a sibling table. **Do not overwrite FMP-sourced columns on `core.security`.**

**4. Resolve Sharadar events through the xref, never by (source, ticker).**

- Today's rename and delisting resolvers are scoped to one source, for example `active_security_for_symbol(db, symbol, source)`.
- With `source='sharadar'` they would not find a single security FMP minted.
- Look up events by `permaticker` → `security_id` instead.

**5. A landing table for Sharadar.**

- `landing.fmp_raw` is named for FMP.
- Bulk files are large, so record per-file metadata and a hash, and land the nightly delta rows.

**6. Boundary mappings.**

- Exchange codes: `NYSEMKT` → `AMEX`, plus `NYSEARCA` and others; `OTC` is already seeded.
- `corporate_action.action_type` allows only `split` and `dividend` (0006). Widen that CHECK only if you decide to store spinoffs.
- Delisting reason goes in the vendor attribute table.

**7. New DQ checks:**

- `sharadar_raw_recovery_mismatch`: the recovered unadjusted close differs from `closeunadj`;
- `vendor_xref_unmatched` and `vendor_xref_ambiguous`;
- `vendor_history_overlap`;
- `cross_vendor_bar_divergence`, used during the parallel run.

**8. A script to remove one vendor's data**, e.g. `scripts/reset_data.sh --scope vendor:sharadar`, preview by default like the existing scopes. It deletes that vendor's rows, the securities it minted and their dependents, and its xref rows, then recomputes adjustment factors. You hope never to run it. It is what makes either subscription safe to end.

**9. ADRs.** The repo's ADRs run to 0011, so the build prompt's references to "ADR 0005/0006" for fundamentals are now taken.

- **0012: multi-vendor provenance and removal.** Every row is tagged by vendor, loaders are fill-only, one vendor can be removed without the other, and why (§3).
- **0013: Sharadar as the identity authority, and a survivorship floor of 1998.**
- **An amendment to 0001/0004:** unadjusted prices may be *vendor-derived* for Sharadar-backfilled history, recorded in `source`; nightly captures are observed.
- **A fundamentals ADR** retargeted at SF1.

### 8.4 Linking the existing ~21k securities to `permaticker`

The screener does not carry CUSIP or CIK (ADR 0005), so most rows will link on ticker plus dates. Price agreement is the tie-breaker that settles it.

1. **Candidates:** Sharadar tickers whose `firstpricedate`–`lastpricedate` span overlaps the ticker's `symbol_xref` period and the security's own first and last bar.
2. **Corroboration:** match CUSIP, CIK or FIGI where fafnir has them (profile-enriched rows, the `duk` identifier indexes from migration 0023). Otherwise compare names with the existing `company_name_similarity()`.
3. **Decisive test:** compare FMP's unadjusted close with SEP `closeunadj` on 3–5 sampled overlapping dates. Agreement confirms the link.
4. **Write the link** with its `match_method` and `confidence`. Anything ambiguous or unmatched goes to the DQ queue; nothing is guessed.

I cannot estimate the hit rate from here, so measure it in Phase 1. Expect most leftovers to be ETFs listed on BATS or CBOE and names already affected by ticker reuse.

### 8.5 Cost by phase

| Phase | Sharadar | FMP | Funds | Approximate annual rate |
|---|---|---|---|---|
| 0 | $0 (free sample) | Premium $708 | FMP | $708 (today) |
| 1–2 (~3–6 months) | $69/mo, then $499/yr | Premium $708 | FMP + Tiingo (free) | ~$1,207 |
| 3, keeping Premium | $499 | $708 | FMP + Tiingo | ~$1,207 |
| **3, downgrading to Starter** | $499 | ~$264 | Tiingo | **~$763** |
| 3, Starter plus a Sharadar 5-year plan (if Sharadar confirms retention) | $299 | ~$264 | Tiingo | ~$563 |
| COA 2 as written, only if FMP confirms retention | $499 | $0 | Tiingo ($0–300) | ~$499–$799 |

The Starter price and the list of what Starter includes come from FMP's pricing page. That page suggests unadjusted prices and mutual funds are Premium-only, so **on Starter, FMP is the licence anchor, not a data feed**. Verify this before relying on it.

---

## 9. Risks and open questions

| Risk or question | Severity | How to settle it |
|---|---|---|
| FMP's §5 retention exception does not cover the warehouse | **High:** it decides between COA 2 and COA 2-M | Written answer from FMP (Phase 0) |
| A downgrade is treated as termination | High | Same letter |
| You fall outside Sharadar's personal licence (RIA or other professional use) | **High:** a professional licence has no public price | Settle before subscribing (vendor assessment §7.2) |
| SEP decimals too coarse to recover exact unadjusted prices for split-heavy dead names | Medium | `probe-sharadar`; the `sharadar_raw_recovery_mismatch` DQ check |
| A seam between FMP and Sharadar bars in live series at cutover | Medium | 60-session parallel run and gate; if it fails, keep FMP Premium for nightly prices |
| ACTIONS dividends turn out *not* to be split-adjusted | Low. The conversion just becomes a no-op. | Probe against a known pre-split dividend |
| Two sources of new listings during Phases 1–2 | Medium | FMP keeps minting new listings until cutover. In Phases 1–2 Sharadar mints only *dead* securities and links live ones; it mints new listings only after cutover. |
| Sharadar revises old bars after they are loaded | Low–Medium | Re-pull an overlap window keyed on `lastupdated`. Revisions to Sharadar rows are allowed; FMP rows are never changed. |
| Losing Sharadar later wipes 1998–2026 dead-name history and every bar since cutover | Medium | The removal path (§8.3 item 8) keeps this tidy. Price this into any future vendor decision. |
| 1990–1997 stays survivor-only | Accepted | ADR 0013; flag pre-1998 cross-sectional queries |
| Delisting returns (CRSP-style) are available from neither vendor | Low (a future item) | Approximate from ACTIONS delisting reasons and the acquirer's terms in a `mart` view |

---

## 10. Direct answers

> **COA 1?**

Not viable. Sharadar's §10 requires you to delete the backfill and the fundamentals within 30 days of cancelling, and Sharadar can ask for an affidavit. What you would be left with is the backtests you ran in the meantime. Even without that clause, COA 1 would put a vendor seam into every live company's fundamentals and freeze the identity improvements.

> **COA 2?**

Right destination, wrong exit. Sharadar fixes the survivorship gap from 1998, supplies genuine point-in-time fundamentals, and replaces ticker-keyed identity with a permanent key. It also delivers observed unadjusted prices on nightly loads, which removes most of the precision objection. But cancelling FMP triggers FMP's deletion clause on the cautious reading, and that would take the 1990–2026 history you want to keep.

> **Recommendation?**

COA 2-M: adopt Sharadar permanently and downgrade FMP rather than cancel it, unless FMP confirms retention in writing.

- All existing price history stays.
- Schema changes are additive.
- Research is survivorship-free from 1998.
- Steady-state cost is ~$763/yr.

Build it so each vendor's data stays separate and can be removed on its own. The licence terms make that a requirement.

---

## Sources

**Licence terms**
- [Sharadar — Terms of Use / Personal Use License (§2–4, §10)](https://sharadar.com/terms)
- [QuantRocket — Sharadar data pricing, professional vs non-professional, deletion on cancellation](https://www.quantrocket.com/pricing/data/sharadar/)
- [FMP — Terms of Service (last updated 2023-08-01; §2.2, §5, §6.2–6.3)](https://site.financialmodelingprep.com/terms-of-service)

**Sharadar product facts**
- [Sharadar — Subscribe / personal plan pricing](https://sharadar.com/subscribe)
- [Sharadar — Stock prices (SEP fields, history, update times)](https://sharadar.com/prices)
- [Sharadar — Stock Prices, Fund Prices and Adjustments (2026-07-29; unadjusted-value recovery formulas)](https://sharadar.com/blog/posts/sharadar-stock-prices-fund-prices-and-adjustments)
- [Sharadar — Fundamentals docs (SF1 dimensions, coverage, 1998 start)](https://sharadar.com/docs/fundamentals)
- [Sharadar — TICKERS docs (permaticker)](https://sharadar.com/docs/tickers)
- [Sharadar — ACTIONS docs (action types, 1998 start)](https://sharadar.com/docs/actions)
- [Sharadar — Fund prices docs (SFP: ETFs, CEFs, ETNs, ETDs)](https://sharadar.com/docs/funds)
- [Sharadar datasheet via Nasdaq/Quandl (coverage counts)](https://resources.quandl.com/a/res-hub/Sharadar_Datasheet_final.pdf)
- [stockparfait `ndl/sharadar` Go package (ACTIONS dividend values split-adjusted)](https://pkg.go.dev/github.com/stockparfait/stockparfait/ndl/sharadar)
- [R-bloggers — Exploring Stock Market Listing Mortality since 1986 (no Sharadar delistings 1986–1998)](https://www.r-bloggers.com/2021/08/exploring-stock-market-listing-mortality-since-1986/)

**FMP product facts**
- [FMP — Pricing plans (Starter/Premium/Ultimate)](https://site.financialmodelingprep.com/pricing-plans)

**Repository** (`rtrimble13/fafnir` @ `7830505`)
- `src/fafnir/ingest/delisted.py` (docstring, lines 14–19), `src/fafnir/ingest/security_master.py`, `src/fafnir/ingest/daily_price.py`, `src/fafnir/db/repository.py` (`upsert_daily_prices` line 1699, `VENDOR_FED_SECURITY` line 2925, universe selectors from line 4075)
- `sql/migrations/0003`, `0005`, `0006`, `0009`, `0012`, `0023`, `0025`, `0026`
- `doc/adr/0001`, `0002`, `0004`, `0005`, `0006`; `doc/backfill.md`; `doc/ingestion.md`; `doc/operations.md` ("Separating two issuers held on one security")
- `.claude/skills/fafnir-dba/references/data-semantics.md` §14–15, `schema-map.md`

**Project docs**
- `claude/fafnir-vendor-assessment.md` (rev. 4), `claude/fafnir-fundamentals-build-prompt.md`, `claude/fmp-ultimate-upgrade-impact.md`

### Confidence

- **High:**
  - the text of Sharadar §10 (confirmed by QuantRocket) and of FMP §6.3;
  - Sharadar plan prices;
  - SEP field semantics and the unadjusted-value recovery formulas;
  - SFP having no mutual funds;
  - the 1998 start of SEP, SF1 and ACTIONS;
  - both code hazards (`VENDOR_FED_SECURITY`, and the `DO UPDATE` that overwrites `source`);
  - nightly captures being unadjusted (this follows from what "split-adjusted" means).
- **Medium:**
  - how FMP §5 and §6.3 interact;
  - ACTIONS dividends being split-adjusted (third-party code);
  - Starter lacking unadjusted prices and funds (pricing-page summary);
  - no Sharadar delistings before 1998 (a 2021 independent analysis);
  - the Starter price.
- **Low, to be probed:**
  - SEP decimal precision;
  - how closely FMP and Sharadar bars agree;
  - the `permaticker` link rate;
  - whether downgrading either vendor preserves the right to keep the data.
