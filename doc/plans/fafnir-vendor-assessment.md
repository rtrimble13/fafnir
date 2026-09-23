> Imported from the Claude project (`claude/fafnir-vendor-assessment.md`, rev. 4, 2026-09-02). Superseded
> in part by [sharadar-coa-assessment.md](sharadar-coa-assessment.md) §3.3 (termination clauses).

# Market Data Vendor Assessment for `fafnir`

**Question asked:** Am I using the right vendor? If so, why? If not, who instead?
**Date:** 2026-09-02 *(rev. 2 — adds mutual fund coverage, §3.6 and §5.3)*
**Constraints given:** $1,000–$3,000/yr · personal research use only · no preference on switching cost · **must track a handful of mutual funds**
**Method:** documentation review of 20+ vendors, independent reputation research, and **live empirical testing against your own FMP Premium key** — the last of which is where the decisive findings came from.

---

## 1. Executive summary

**FMP is the right spine and the wrong sole vendor.**

Keep it. Do not migrate away. But FMP cannot deliver the one thing `fafnir` is *architecturally organized around* — point-in-time fundamentals — and no amount of careful loader design fixes that, because the defect is upstream in the feed.

The four findings that decide this, ranked:

| # | Finding | Consequence |
|---|---|---|
| 1 | **FMP fabricates filing dates for pre-1994 fundamentals.** `filingDate == fiscal_date` and `acceptedDate == fiscal_date 00:00:00` for AAPL FY1985–FY1993 — a zero-day filing lag, which is impossible. | **Critical.** `fafnir`'s §4.5 resolution order takes `accepted_date` first, so it would mark these `knowledge_source='accepted'`, never fire the `estimated` DQ flag, and pass the `knowledge_date >= fiscal_date` tripwire on equality. Silent 60–90 day look-ahead on every pre-1994 record. |
| 2 | **FMP has no restatement history, by construction.** It returns one version per period — today's version. Your build prompt already knows this (§4.6). | **Structural.** All history before `fafnir`'s first load is single-versioned restated data. This is the exact bias the academic literature quantifies, and it is not fixable in-house. |
| 3 | **Premium is NOT truncating history at 30 years.** AAPL daily prices returned **from 1985-01-02**; annual income statements **from FY1985**. | The #1 justification in your own upgrade memo — "Premium caps history, so deep history may be a fiction" — **is false**. It measurably reaches 1985 today. |
| 4 | **Sharadar Core US Equities Bundle costs $299–$499/yr on a personal licence and has genuine as-reported PIT dimensions** (ARQ/ARY/ART, indexed to the SEC filing datekey, restatements excluded). | The missing piece exists, is affordable, and is purpose-built for exactly this. |
| 5 | **FMP Premium *does* serve mutual fund NAVs** — `VFIAX` returned a complete daily series back to **2000-11-13, its actual inception date**. What Ultimate adds for funds is *holdings*, not NAVs. | Removes a second reason to upgrade. §3.6. |
| 6 | **But FMP covers only 3 of your 8 held funds.** `DODIX` (to 1990), `PIMIX` and `GSIYX` are complete; `HHDFX`, `DODWX`, `LCGJX` and `HRNOX` are **absent from the security master entirely**; `GSIMX` returns **one orphan bar**. The gap is arbitrary — `DODIX` works and `DODWX` doesn't; `GSIYX` works and `GSIMX`, its sibling share class, doesn't. | **You need a second fund source.** The one-row case is the hazardous one: it loads silently. §3.8, §5.3. |

**Recommendation:**

1. **Do not buy FMP Ultimate.** Findings #3 and #5 both kill its headline justifications. (~$1,080/yr saved.)
2. **Raise `request_rate_per_min` from 280 to ~700** — free, and you are throttled to a tier below the one you pay for.
3. **Add Sharadar Core US Equities Bundle** (~$299–$499/yr) as the point-in-time fundamentals source. Keep FMP Premium ($59/mo) for prices, corporate actions, reference data and its deeper pre-1998 price history.
4. **Add a fund vendor for the five FMP misses** — trial Tiingo's free tier first (§5.3); likely $0, worst case ~$300/yr. Sharadar does *not* solve this: SFP covers ETFs, CEFs, ETNs and ETDs, **not mutual funds**. Neither does Ultimate.
5. **Total: ~$1,010–$1,510/yr** — within budget, and still *cheaper than the FMP Ultimate path you were already considering* (~$1,788/yr), which would have solved none of these three problems.

There is one caveat that could invalidate #3 entirely, and you need to read §7.2 before acting on it.

---

## 2. What `fafnir` actually requires

Derived from the repo, ADRs 0001–0007, and the fundamentals build prompt. This is the yardstick; everything below is measured against it.

| # | Requirement | Where it comes from | Negotiable? |
|---|---|---|---|
| R1 | **Raw, unadjusted OHLCV** — as-traded, not split- or dividend-adjusted | ADR 0001, ADR 0004 | **No.** The whole adjustment-factor design depends on it. |
| R2 | **Price history to 1990** | `doc/backfill.md` | Soft — a proven floor is acceptable if documented |
| R3 | **Point-in-time fundamentals with real filing/acceptance dates** | Build prompt §4.5; the `knowledge_date` model | **No.** This is the milestone's entire purpose. |
| R4 | **Restatement history / as-originally-reported figures** | Build prompt §4.6; ADR 0002's bitemporal promise | Currently accepted as unmet (accumulate forward). See §4. |
| R5 | **Survivorship-bias-free universe incl. delisted names** | Build prompt §7: *"a fundamentals history containing only survivors is worse than no history, because it looks complete"* | **No.** Stated as a hard requirement. |
| R6 | **Splits & dividends with ex-dates, full depth** | ADR 0007, `core.corporate_action` | **No.** |
| R7 | **Null ≠ zero for absent line items** | Build prompt §4.3: *"never `COALESCE(x, 0)` at the ingest boundary"* | **No.** Corrupts every ratio with that item in the denominator. |
| R8 | **Bulk/whole-universe delivery** for backfill | Build prompt §7 (126k requests otherwise) | Strongly preferred |
| R9 | **Personal-use licensing, $1–3k/yr** | Your constraint | **No.** |
| R10 | **NAV history + distributions for a small, declared mutual fund watchlist** | Your requirement; ADR 0006's curated fund universe already anticipates it | **No**, but scope is deliberately small |

R3, R4, R5 and R7 are the ones that separate vendors. R1, R2, R6, R8 are widely available. R10 turns out to be cheaper than expected — see §5.3.

---

## 3. Empirical findings — measured, not read

Your existing project docs correctly insist that *"this repository does not adopt an endpoint on the strength of its documentation."* I applied the same rule to this assessment. Every finding here was measured against your live FMP Premium key on 2026-09-02.

### 3.1 The synthetic filing-date defect — the most important finding

`AAPL` annual income statement, `fiscal_date` → `filingDate` lag, in days:

```
  1985  1985-09-30 -> 1985-09-30    0   accepted 00:00:00   <-- fabricated
  1986  1986-09-30 -> 1986-09-30    0   accepted 00:00:00   <-- fabricated
  ...  (1987, 1988, 1989, 1990, 1991, 1992, 1993 all identical)
  1993  1993-09-30 -> 1993-09-30    0   accepted 00:00:00   <-- fabricated
  ----------------------------------------------------------------
  1994  1994-09-30 -> 1994-12-13   74   accepted 00:00:00   <-- real
  1995  1995-09-29 -> 1995-12-19   81
  ...
  2025  2025-09-27 -> 2025-10-31   34
```

**9 of 41 annual records carry a fabricated zero-lag filing date.** The break is exactly at FY1994, which lines up with the SEC's EDGAR electronic-filing phase-in (1993–1996). Before EDGAR, FMP has no filing date and **substitutes the period-end date rather than returning null**.

Why this is worse than a null:

- Your `knowledge_date` resolution (§4.5) tries `accepted_date` → `filing_date` → estimate. `acceptedDate` **is populated**, so branch 1 always wins.
- `knowledge_source` is recorded as `'accepted'`. The `fundamentals_estimated_knowledge_date` DQ flag **never fires**.
- The `CHECK (knowledge_date >= fiscal_date)` tripwire is satisfied — by equality.
- Every downstream PIT read therefore believes the market knew FY1990's results **on the last day of FY1990**.

That is a clean 60–90 day look-ahead, invisible to every guard you built, on precisely the deep history the 1990 backfill floor exists to capture. A value factor built on it would look excellent and be fake.

Corroborating scale: an independent PIT study of 313,406 filings across 5,194 US companies measured a **mean 66-day / median 60-day** lag between fiscal period end and public availability, 90th percentile 90 days. FMP is asserting zero for pre-1994.

**This is fixable on your side** — see §8.1 — and the fix is worth doing regardless of which vendor you end up on.

Quarterly data holds up better: `GE` Q3 1996 shows `filingDate` 1996-11-14 (45-day lag), which is real. The `acceptedDate` *time* component is `00:00:00` until ~2002, but the *date* is genuine, which is all a date-grained `knowledge_date` needs.

### 3.2 History depth — the upgrade memo's premise is wrong

Your project doc ranked "Premium caps history at 30 years; the 1990 floor may be a fiction" as the **highest-priority correctness risk** and the leading justification for the $1,080/yr Ultimate upgrade.

Measured on your Premium key:

| Test | Result |
|---|---|
| `AAPL` daily prices, requested from 1985-01-01 | **4,043 bars returned, from 1985-01-02** |
| `AAPL` annual income statement, `limit=120` | **41 rows, earliest FY1985** |
| `GE` quarterly income statement, `limit=120` | 120 rows to 1996-09-30 — hit the *request* cap, not the feed's |

Premium's "Up to 30 Years of Historical Data" is marketing copy, not a feed truncation. **Finding #1 of your upgrade memo is resolved: no re-backfill is needed, and the strongest argument for Ultimate evaporates.**

### 3.3 The default price endpoint is fully split-adjusted — confirmation of the ADR 0004 trap

`AAPL` 1985-01-02 close came back as **$0.12444** with volume **175,302,572**.

AAPL's cumulative split factor since 1985 is 2 × 2 × 2 × 7 × 4 = **224** (1987, 2000, 2005, 2014, 2020). $0.12444 × 224 = **$27.87**, which is a plausible as-traded January 1985 price; 175.3M ÷ 224 = ~783k shares, a plausible as-traded volume. The series is adjusted, and so is the volume.

This is consistent with ADR 0004's own verification (raw 1990-01-02 close ≈ $39.20; ÷112 remaining factor ≈ $0.35 adjusted). **`fafnir` is not affected** — it uses the `non-split-adjusted` endpoint per ADR 0004. But it is a live demonstration that FMP's *default* price path is a trap, and it is the same trap waiting for `eod-bulk` if you ever adopt it without probing. Your ADR was right, and the gate you put on `eod-bulk` should stay.

### 3.4 Zero-instead-null in the fundamentals payload — violates R7 at the source

`GE` Q3 1997 returns:

```
researchAndDevelopmentExpenses : 0
interestExpense                : 0
interestIncome                 : 0
netInterestIncome              : 0
depreciationAndAmortization    : 1,049,000,000
otherExpenses                  : 1,049,000,000     <-- identical, suspicious
```

General Electric in 1997 had material R&D and enormous interest expense — it owned GE Capital. These are **not** zeros; they are unmapped fields rendered as zero.

Your build prompt §4.3 is explicit: *"All line-item columns are nullable — a genuinely absent line item is not the same as zero, and conflating them corrupts every ratio with that item in the denominator."* FMP has already destroyed that distinction upstream. `fafnir` cannot recover it, because a real zero and a missing value arrive identically.

Practical impact: `interest_coverage`, `capex_to_sales`, and any R&D-based quality factor are silently wrong for older records — and wrong in the direction of *looking computable* rather than returning NULL.

### 3.5 Delisted coverage — a warning sign, not a verdict

Three canonical delisted names returned **no fundamentals at all**:

| Symbol | Company | Result |
|---|---|---|
| `LEH` | Lehman Brothers | *No data returned* |
| `ENRNQ` | Enron | *No data returned* |
| `SUNEQ` | SunEdison | *No data returned* |

This is **not conclusive** — FMP may key delisted names under different symbology, and the delisted-companies endpoint may need to be queried separately. But R5 is a hard requirement in your own build prompt, and three-for-three empty on the most famous delisted US companies in existence is a signal that deserves a definitive probe before the fundamentals backfill runs. Independent research found no third-party audit quantifying FMP's delisted depth either way.

### 3.6 Mutual fund NAVs — Premium already serves them

You noted that Premium excludes mutual fund NAVs. Measured on your key, that is not what happens:

| Test | Result |
|---|---|
| `quote` for `VTSAX, VFIAX, FXAIX, PRWCX` | **All four returned**, with correct fund names, NAV as `price`, and `volume: 0` |
| `VFIAX` daily history, 2024-01-01 → 2026-08-31 | 668 rows |
| `VFIAX` daily history requested from **1995-01-01** | 1,289 rows, **earliest bar 2000-11-13** |

That earliest date is not a truncation — **2000-11-13 is VFIAX's actual inception date.** The series starts where the fund starts, which is the correct and complete answer. Cross-checks against known history hold up too: the returned NAV declines from $124.88 (Nov 2000) to $114.92 (Dec 2005), which is the right shape and roughly the right level for an S&P 500 index fund across the dot-com drawdown.

So the NAV series is **raw, unadjusted NAV** — no distribution adjustment applied. For `fafnir` that is *good news*: it matches `core.daily_price`'s raw-price contract exactly, and it means no ADR 0004-style double-adjustment trap on the fund path.

Two structural observations from the payload shape:

- **Every fund row has `open == high == low == close` and `volume == 0`.** Funds price once daily at NAV; there is no intraday range and no exchange volume. This is correct, but it will interact with your DQ suite — any zero-volume check, any OHLC-range check, and any outlier check keyed on intraday spread will either fire on every fund row or silently pass. Fund rows need to be excluded from those checks by security type, not by symbol list.
- **The `Ultimate` fund feature is holdings, not prices.** Your upgrade memo already recorded this correctly ("ETF & mutual fund holdings"). Buying Ultimate to get fund NAVs would be buying something you already have.

### 3.7 The real fund gap: distributions

Mutual funds distribute both income dividends and capital gains, and NAV drops by the distribution amount on the ex-date. Without distribution data, that drop reads as a loss.

I checked whether the distribution is inferable from the NAV series alone. It is not:

```
VFIAX, December 2025 (distribution season)
  2025-12-19   632.42
  2025-12-22   634.76      <- +2.34
  2025-12-23   637.65
  2025-12-24   639.70
  2025-12-26   639.57      <- -0.13
```

A quarterly income distribution on an S&P 500 index fund is roughly 0.3% of NAV — about $1.90 here — which is comfortably inside ordinary daily noise. **You cannot recover it by looking for a drop.** It has to come from a distribution feed.

The magnitude of getting this wrong is not marginal. Using the actual measured endpoints of the returned series:

```
VFIAX  2000-11-13 -> 2026-08-31   (25.80 years)
  NAV-only:                 5.688x    =  6.97 %/yr
  + ~1.8 %/yr distributions: 8.748x   =  ~54% more ending value
```

A NAV-only series understates the quarter-century outcome by roughly half. This is the fund-side equivalent of the ADR 0004 adjustment problem: internally consistent, passes every structural check, and wrong only in the number that matters.

**One caveat on scope:** I could not test FMP's dividends endpoint directly — the MCP server exposes price, quote and fundamentals tools but no dividends tool. FMP's dividend documentation uses only stock examples and its fund marketing page is silent on distributions, so coverage is **unverified, and I would not assume it**. §5.3 gives you the one-line test to settle it.

### 3.8 Coverage of your actual watchlist — 3 of 8

§3.6 tested well-known index funds and they all worked. Tested against the eight funds `fafnir` actually holds, the picture is very different:

| Ticker | Fund | Quote | History | Rows | Earliest | Verdict |
|---|---|---|---|---|---|---|
| `DODIX` | Dodge & Cox Income Fund, Class I | ✅ | ✅ | 9,234 | **1990-01-02** | **Full** — reaches your backfill floor exactly |
| `PIMIX` | PIMCO Income Fund, Institutional | ✅ | ✅ | 4,886 | 2007-04-02 | **Full** — inception-complete |
| `GSIYX` | GS GQG Partners Intl Opportunities, R6 | ✅ | ✅ | 2,440 | 2016-12-15 | **Full** — inception-complete |
| `GSIMX` | GS GQG Partners Intl Opportunities, **Institutional** | ❌ | ⚠️ | **1** | 2022-07-29 | **Broken** — a single orphan bar |
| `HHDFX` | Hamlin High Dividend Equity, Institutional | ❌ | ❌ | 0 | — | **Absent** |
| `DODWX` | Dodge & Cox Global Stock Fund, Class I | ❌ | ❌ | 0 | — | **Absent** |
| `LCGJX` | William Blair Large Cap Growth, R6 | ❌ | ❌ | 0 | — | **Absent** |
| `HRNOX` | Hood River New Opportunities, Institutional | ❌ | ❌ | 0 | — | **Absent** |

**37.5% usable coverage.** Three findings follow, and they matter more than the headline number.

**The gap is arbitrary, not systematic.** It is tempting to read this as "FMP covers big funds and misses boutiques," but the data refuses that explanation:

- `DODIX` is covered back to 1990. `DODWX` — *same firm, same fund family, a flagship Dodge & Cox product* — is absent entirely.
- `GSIYX` and `GSIMX` are **two share classes of the same portfolio**. One is fully covered from inception; the other returns a single bar from 2022.

So this is not a fund-size or fund-family boundary you could reason about and design around. It is per-share-class inconsistency inside the same fund, which means **coverage has to be established per symbol, empirically, and re-checked** — you cannot infer it.

**`GSIMX` is the dangerous row, not the missing four.** An absent symbol fails loudly: zero rows, and your build prompt's "fail loudly on an empty load" stance catches it. A symbol returning exactly one bar from four years ago **succeeds quietly** — it lands a payload, writes a watermark, and marks the security as loaded. `check_freshness` is currently keyed to `exchange_code="NASDAQ"` and would need funds explicitly in scope to catch it. This is precisely the partial-coverage-masquerading-as-coverage failure your DQ design exists to prevent.

**The four missing funds are not a symbology problem.** `search_symbol` returns nothing for `HHDFX`, `DODWX`, `LCGJX` or `HRNOX` — they are not in FMP's security master under any spelling. All four are real, currently-traded, currently-open funds; I verified each against Fidelity's fund research, Morningstar and the sponsors' own pages.

**One ambiguity worth closing.** The MCP wrapper reports "No data returned," which could be masking an HTTP 402 (plan-gated) rather than an empty result. That `search_symbol` also comes back empty points to genuine absence rather than gating — search is a cheap, ungated endpoint — but it is worth one raw call to be sure:

```
curl -si "https://financialmodelingprep.com/stable/search-symbol?query=DODWX&apikey=..."
```

A `402` means Ultimate might fix it. An empty `[]` with `200` means it never will, because the symbol simply isn't in the database. I expect the latter, and note that all four missing funds are US-domiciled — so Ultimate's "Global Coverage" is not the relevant lever either way.

---

## 4. The structural problem FMP cannot solve

Everything in §3 is a defect that can be measured, flagged, or worked around. This one cannot.

**FMP returns exactly one version of each fiscal period: the current one.** Your build prompt already states this plainly (§4.6): *"the bitemporal history is something fafnir accumulates, not something it downloads... `version_no = 1` on an old period means 'as FMP reports it today', not 'as originally filed'."*

The consequence is that `fafnir`'s entire pre-2026 fundamentals history — 36 years of it — is **restated data wearing a bitemporal schema**. The tables have `valid_from`, `version_no`, `is_restatement` and `content_hash`; they will be correct and useful going forward; and they will be single-versioned and restatement-contaminated for every period that closed before your first load.

Two independent lines of evidence on how much this matters:

**Measured restatement frequency.** S&P Global's own point-in-time study found **78% of companies restate audited annual total revenue at least once within 400 days of original filing**. A separate PIT dataset flagged **18,734 rows where a later filing revised a previously reported value by more than 0.5%**.

**Measured backtest distortion.** S&P Global ran 48 backtests (Earnings Yield and CFROIC, multiple regions) comparing true PIT against lagged-fundamentals approximations:

- US Large Cap: minimal, <5 bps — tight modern filing deadlines protect you
- **US Small Cap (Next 2000): 2-month lag overstated PIT returns by up to 15 bps; 3-month lag understated by up to 14 bps** — note the sign flips, so no fixed lag rescues you
- Europe: 40 of 48 factor-region combinations overstated
- Emerging markets: cumulative return differences reached **39%** (3.808% vs 4.717%)

Their conclusion: static lag-based approximations *"are not capable of replicating the analytical results derived from Point-In-Time models."*

The older academic literature reinforces it. Banz & Breen (1986) is the foundational paper establishing that survivorship and look-ahead bias in Compustat-style databases materially change measured size and earnings-yield effects. A 2020 literature synthesis catalogues vendor-specific distortions of comparable magnitude — Compustat overstating gross margin by 14.3% through standardization choices, SIC-code disagreement rates of 36–80%, and the blunt finding from McGuire et al. (2016) that *"researchers would likely come to different conclusions based on database used."*

**Read against `fafnir`'s own stated purpose, this is the decisive point.** You did not build a screener. You built a bitemporal warehouse with a look-ahead tripwire at the storage layer, a standing invariant test asserting `count(*) WHERE knowledge_date > as_of_date = 0`, and an ADR devoted to the double-adjustment failure mode. That is a research-grade design. Feeding it single-version restated fundamentals with fabricated pre-1994 filing dates is the one thing that would make all of that rigour decorative.

---

## 5. The vendor landscape

### 5.1 Institutional tier — the benchmark, and why it's out

| Vendor | Cost | Verdict for `fafnir` |
|---|---|---|
| **CRSP** (Morningstar, acquired Feb 2026 for $365M) | No public pricing; institutional only | **Out.** The gold standard for survivorship-free US prices to 1926 with PERMNO identity resolution and delisting returns. CRSP's own site turns individuals away explicitly: *"CRSP databases are designed for and delivered to licensees at academic institutions, government agencies, and investment practitioners."* |
| **Compustat Point-in-Time** (S&P Global) | No public pricing; via WRDS | **Out.** The PIT gold standard — original-as-reported preserved alongside restatements. Access requires a WRDS institutional site licence. |
| **WRDS** | Institutional site licence, no public individual price | **Out.** No self-serve individual path. |
| **Bloomberg / FactSet / LSEG** | ~$12k–$32k/yr per seat (third-party estimates) | **Out on cost, and out on licence anyway.** Terminal seats do not license warehouse-building; LSEG's developer terms explicitly forbid reproducing or storing content absent a separate licence, and Bloomberg sells that separately as Data License. |
| **Databento** | $199/mo Standard, or pay-as-you-go | **Out for this purpose.** Excellent modern market-data infrastructure, but **no fundamentals at all**, and only ~7 years of OHLCV on the entry tier. Irrelevant to a fundamentals warehouse. |

Worth internalizing: the institutional tier is not merely *better*, it is the thing the academic literature benchmarks everything else against. You are not buying it, so the honest posture is to **document what you're giving up** rather than pretend the gap doesn't exist.

### 5.2 The realistic candidates

| Vendor | Cost (personal) | Unadj. prices | PIT / as-reported | Restatements | Delisted | Bulk | Verdict |
|---|---|---|---|---|---|---|---|
| **FMP Premium** *(current)* | **$708/yr** | ✅ (`non-split-adjusted`) | ⚠️ filing dates real from ~1994, **fabricated before** | ❌ none | ⚠️ unproven | ❌ (Ultimate only) | **Keep as spine** |
| **FMP Ultimate** | $1,788/yr | ✅ | same as Premium | ❌ none | ⚠️ | ✅ 18 endpoints | **Don't buy — see §6.1** |
| **Sharadar Core Bundle** | **$299–$499/yr** | ✅ `closeunadj` | ✅ **ARQ/ARY/ART, datekey-indexed, restatements excluded** | ✅ **AR vs MR dimensions** | ✅ ~18k cos., "99% survivorship-free" | ✅ full-table CSV | **Add this** |
| **EODHD All-In-One** | $999/yr | ✅ raw + separate `adjusted_close` | ⚠️ `filing_date` yes; as-reported vs restated unverified | ❓ unverified | ✅ (full only post-2018) | ✅ whole-exchange, 1 call | Strong runner-up |
| **Intrinio Individual** | $1,800/yr | ✅ | ⚠️ "as-reported (XBRL)" offered; no documented AR/MR flag found | ❓ unverified | ✅ (delisted prices from 2007 only) | ✅ API/CSV/S3/Snowflake | Plausible, pricier, less proven |
| **Tiingo Power** | $300/yr | ✅ `close` + `adjClose` | ⚠️ an `asReported` boolean exists; mechanics undocumented | ❓ | ❓ unverified | ❌ per-symbol only | Good prices, weak fundamentals |
| **Polygon → "Massive"** | $2,388/yr (Advanced) | ✅ `unadjusted=true` | `filing_date` + `source_filing_url` | ❓ | ✅ `active=false` | ✅ S3 flat files | **Fundamentals only to 2009.** Fails R2/R3. Also rebranded in 2026, and an independent audit found unreliable `delisted_utc` and spurious split records. |
| **sec-api.io** | $588–$2,388/yr | n/a | ✅ **inherently PIT** — every filing is an immutable dated snapshot, 1993–present | ✅ by construction | ✅ all filers | ✅ bulk/S3 | Interesting fallback; you build the PIT join yourself |
| **SimFin** | $180–$852/yr | ❓ | ❌ **not verified as PIT** | ❌ | ❓ | Limited | Cheap; insufficient rigour |
| **Norgate** | $270–$788/yr | ✅ | ❌ **fundamentals are "current" only** | ❌ | ✅ prices to 1950 | Proprietary desktop feed | Superb survivorship-free *prices*; wrong product for fundamentals |
| **Marketstack** | $1,536/yr (Business) | ⚠️ | ❓ | ❓ | ❓ | ❌ | **For US equities it resells Tiingo** (their own FAQ). No reason to prefer it over Tiingo direct. |
| **Zacks / Calcbench / Daloopa / QuoteMedia** | Sales-quote or >$5k | — | Zacks and Calcbench are genuinely PIT | ✅ | ✅ | ✅ | Out on price or opacity |

### 5.2b Can Sharadar replace FMP outright? — the SEP field spec says no

Reasonable challenge: if 1990–2026 prices are *already backfilled*, the deep-history argument for FMP is sunk cost. Sharadar SEP covers 21,000+ tickers including delisted, ACTIONS covers splits and dividends, TICKERS covers the security master — and the ongoing daily flow only needs coverage from today forward, where SEP's 1998 floor is irrelevant. On that reading FMP is $708/yr for nothing.

The argument is right about almost everything. It fails on one field list.

**SEP provides exactly one unadjusted price and no unadjusted volume:**

| Field | Sharadar's own description |
|---|---|
| `open` | "Open Price — **Split Adjusted**" |
| `high` | "High Price — **Split Adjusted**" |
| `low` | "Low Price — **Split Adjusted**" |
| `close` | "Close Price — **Split Adjusted**" |
| `volume` | "Volume — **Split Adjusted**" |
| `closeadj` | "Close Price — Adjusted for Splits Dividends and Spinoffs" |
| `closeunadj` | "Close Price — **Unadjusted**" |

`closeunadj` is the only raw field. Sharadar's own docs concede the rest requires "full imputation for full OHLCV."

**Why this binds on `fafnir` specifically**, from its own schema:

- `core.daily_price` declares `open`, `high`, `low`, `close` **`NOT NULL`** and `volume BIGINT NOT NULL`. A close-only feed cannot load without either imputation or a schema change.
- Six CHECK constraints police the bar (`high >= low, open, close`; `low <= open, close`; all `> 0`). Independently-rounded adjusted fields scaled by a ratio can invert those relationships on near-equal values and quarantine real bars.
- `mart.v_daily_price_adjusted` back-adjusts **all five** fields. OHLC are load-bearing, not decorative.
- `_VOLUME_ALIASES` prefers `unadjustedVolume` precisely because, in the loader's own words, *"a volume that arrived already split-adjusted would be inflated by the split ratio SQUARED."* **SEP has no unadjusted volume field at all.**
- `price_scale_collapse` exists to catch adjusted data sitting in a raw column. Feeding SEP's OHLCV to it would be deliberately supplying the thing the check was written to detect.

**Imputation does work arithmetically.** `raw_x = x_splitadj × (closeunadj / close)` recovers all five fields, volume included. The cost is precision and provenance, and precision degrades exactly where the history is deepest:

```
AAPL 1990-01-02, cumulative split factor 224, true raw open $39.50
  adjusted fields stored at 2 dp -> recovered $39.2000   (-0.76 %)
  adjusted fields stored at 4 dp -> recovered $39.4912   (-0.02 %)
  adjusted fields stored at 6 dp -> recovered $39.4999   (-0.00 %)
```

Tolerable at 6 dp, visible at 4, unusable at 2 — and it worsens as cumulative split factors grow, which is to say on exactly the long-lived deep-history names the 1990 floor exists to capture.

**So the honest statement is narrower than "keep FMP":** what FMP still uniquely supplies is *observed* raw OHLCV and *observed* raw volume. Everything else on the price side, Sharadar does as well or better. Whether that is worth $708/yr is a judgement about how literally ADR 0001's raw-is-observed commitment should be read — see §6.4.

### 5.3 Mutual funds — the cheapest path

Scope matters here. You want a **handful** of funds, not a fund universe. That rules out paying a vendor premium for breadth you will not use, and it makes some otherwise-unserious options viable.

| Source | Fund NAVs | Distributions | Income vs cap-gains split | Cost | Verdict |
|---|---|---|---|---|---|
| **FMP Premium** *(have it)* | ✅ **proven, to inception** | ❓ unverified; docs are stock-only and silent on funds | ❌ single `dividend` field, no split even for stocks | $0 more | **Use for NAVs.** Test for distributions. |
| **FMP Ultimate** | same as Premium | same as Premium | ❌ | +$1,080/yr | **No.** Adds holdings only. |
| **Sharadar SFP** | ❌ **ETFs, CEFs, ETNs, ETDs only — no mutual funds** | n/a | n/a | — | **Ruled out.** Does not cover the asset class. |
| **Tiingo** | ✅ 30+ yrs, **on the free tier** | ✅ docs state *"Stocks, ETFs, and Mutual Funds are supported"* | ❌ single `distribution` field | **$0** (free tier: 500 symbols/mo) | **Best API option.** See caveat. |
| **EODHD** | ✅ | ⚠️ fund coverage on the dividends endpoint unverified | ❌ | $240–720/yr | Redundant if Tiingo works |
| **Twelve Data** | ✅ but gated to **Pro, $229/mo** | — | — | $2,748/yr | Ruled out on price |
| **Fund sponsor sites** (Vanguard, Fidelity, T. Rowe) | ✅ | ✅ authoritative | ✅ **the only confirmed source that splits them** | $0, manual | **Use as the reconciliation check** |
| **SEC N-PORT / N-CEN** | ❌ holdings & annual reports, not NAV series | ❌ | — | $0 | Use for *holdings*, not prices |
| **Yahoo / yfinance** | ✅ | ⚠️ conflates income and cap-gains into one figure; gaps and rate-limiting reported | ❌ | $0 | Unofficial, ToS-grey — avoid for a warehouse |

**Recommended fund stack — revised after §3.8:**

My earlier conclusion here was that FMP handles fund NAVs at zero incremental cost. **That is true for the asset class in general and false for your actual watchlist**, where FMP covers 3 of 8. The recommendation changes accordingly:

1. **Settle the 402-vs-empty question first** (§3.8). One `curl`. It decides whether this is a plan problem or a coverage problem, and I expect coverage.
2. **Keep FMP for the three it covers well** — `DODIX` (to 1990), `PIMIX` and `GSIYX` are inception-complete and in the right raw shape. There is no reason to move them.
3. **Trial Tiingo's free tier against all eight tickers before anything else.** It costs nothing, it claims 60,504 ETFs and mutual funds with 30+ years of history on the free plan, and its corporate-actions docs explicitly name mutual funds — so it is the only candidate that could plausibly close both the NAV gap *and* the distribution gap in one move. Test the five failures specifically; general coverage claims mean nothing here, as §3.8 shows.
4. **If Tiingo covers the gap, use it for all eight** rather than splitting the watchlist across two vendors by accident of coverage. One fund vendor with uniform semantics is worth more than a slightly deeper history on three symbols.
5. **If Tiingo also misses them, try EODHD**, then fall back to the sponsors' own published NAV and distribution history. At eight funds, a small scheduled scraper against sponsor pages is a legitimate engineering answer — and for the income-versus-capital-gains split it is the *only* confirmed answer regardless of vendor.
6. **Use `GSIYX` as a documented proxy for `GSIMX` only if you must.** They are share classes of one portfolio, so the NAVs differ by fee drag alone — but that drag compounds, and a proxy must be recorded as such on the security row, never silently substituted.
7. **If you ever want fund holdings, use SEC N-PORT, not FMP Ultimate.** Every mutual fund files complete quarterly portfolio holdings with the SEC, free. Paying $1,080/yr for holdings on a handful of funds is poor value against a free primary source — and Ultimate would not fix the coverage gap anyway.

**Schema note for `fafnir`.** A capital gains distribution is neither a dividend nor a split — it is a third corporate-action type. Folding it into `core.corporate_action` as a dividend gives correct total-return math but loses a distinction that matters for tax-aware analysis, and short-term and long-term capital gains are themselves separate. Add explicit action types (`distribution_income`, `distribution_cap_gain_short`, `distribution_cap_gain_long`) rather than overloading the dividend type. This is additive and cheap now; it is a migration later.

## 6. The verdict

### 6.1 Am I using the right vendor? — Yes, for what it's good at

FMP earns its place, and the case is stronger than the complaint list suggests:

- **Genuine 1985 depth on prices *and* fundamentals**, measured. That beats Sharadar's 1998 floor by 13 years and beats Polygon's 2009 fundamentals floor outright.
- **A correct unadjusted price feed** (`non-split-adjusted`), which is R1 and which several competitors fumble — an independent test found Marketstack returning identical "adjusted" and "unadjusted" series despite intervening dividends, and Finnhub adjusting for splits only.
- **Breadth per dollar is genuinely unmatched** at $59/mo: statements, prices, actions, profiles, screener, 13F, transcripts, ETF holdings, economics, treasury rates — all under one auth and one client.
- **`fafnir` is already built around it.** 87 modules, 19 migrations, 32.4k LOC, ADRs 0004/0005/0006/0007 encoding hard-won knowledge of FMP's specific quirks. That is a real asset, and throwing it away for a marginal vendor upgrade would be poor engineering.

The reputation research is mixed but not disqualifying. Trustpilot sits at 3.7–3.8/5 across ~89 reviews with a polarized distribution. The **best-corroborated complaint is not about data at all** — it is billing: auto-renewal without clear consent, continued charging after cancellation, and a hard no-refund policy, reported consistently across Trustpilot, SourceForge and independent reviews spanning 2024–2026. The second best-corroborated is the v3→stable API migration breaking client libraries (confirmed by a GitHub issue, G2 reviews, and FMP's own changelog, which shows legacy routes hidden behind login as of Aug 2025). Data-accuracy complaints exist but are mostly single anecdotes.

One structural concern worth naming: FMP's changelog shows it **retroactively changes methodology on historical series** — "Total debt definition expanded to include capital lease obligations per ASC 842" (Jan 2025), "Forward EBIT and EBITDA recalculated bottom-up" (Aug 2026), "Ratios TTM currency errors corrected" (Nov 2024) — with no versioning mechanism. Your decision to keep vendor-computed metrics **out of scope** and derive your own from raw statements was exactly right, and this is the evidence for it. Put this in ADR 0006.

### 6.2 Is it the right *sole* vendor? — No

It fails R3 (fabricated pre-1994 filing dates), R4 (no restatement history), R7 (zero-instead-null), and possibly R5 (delisted coverage unproven). R4 in particular is not a bug to be fixed but a property of the product.

### 6.3 What I recommend

**Keep FMP Premium as the spine. Add Sharadar Core US Equities Bundle for point-in-time fundamentals.**

Sharadar's ARQ/ARY/ART dimensions are, verbatim from their documentation, a *"point-in-time view with data time-indexed to the date the form 10 regulatory filing was submitted to the SEC"* that **"excludes restatements"** — with MRQ/MRY/MRT as the restated companion series. That is precisely `fafnir`'s knowledge-timeline model, already resolved by the vendor, over ~18,000 active *and delisted* US companies at "99% survivorship bias free," with an ACTIONS table covering splits, dividends, spinoffs, ticker changes and delistings back to 1998, `closeunadj` for R1, and pre-prepared full-table bulk downloads for R8.

**Why not switch to Sharadar entirely?**

Because of history depth — and here is the part that makes the trade-off much cheaper than it looks.

Sharadar starts at **January 1998**. `fafnir`'s floor is 1990. On its face that is an 8-year loss. But §3.1 established that **FMP's fundamentals filing dates are fabricated before 1994** and only become real from 1994–1996 onward. So the 1990–1997 fundamentals history that FMP uniquely offers is *precisely the history whose knowledge dates cannot support point-in-time research*. You are not giving up 8 years of usable PIT history; you are giving up roughly 2–4 years of it, plus some years that were never PIT-valid to begin with.

Meanwhile FMP's **prices** back to 1985 are real, unadjusted, and genuinely valuable — long-horizon price-based work, adjustment factors, and the trading calendar all benefit. So:

| Domain | Vendor | Rationale |
|---|---|---|
| Daily prices, adjustment factors | **FMP** | Real depth to 1985; correct unadjusted feed; already built |
| Corporate actions | **FMP** *(cross-check vs Sharadar ACTIONS)* | ADR 0007's calendar sweep already works; Sharadar gives a free second opinion from 1998 |
| Security master, reference data | **FMP** | Already built; screener and profile paths work |
| **Fundamentals + PIT panel** | **Sharadar SF1** | ARQ/MRQ is the requirement, solved |
| Fundamentals 1990–1997 | **FMP, quarantined** | Load it, mark `knowledge_source='estimated'`, exclude from the panel by default |
| **Mutual fund NAVs — `DODIX`, `PIMIX`, `GSIYX`** | **FMP** | Inception-complete, raw, right shape (§3.6) |
| **Mutual fund NAVs — the other five** | **Tiingo (trial free tier), else sponsor scrape** | FMP does not have them at all (§3.8) |
| **Mutual fund distributions** | **Tiingo, reconciled against sponsors** | Sharadar does not cover mutual funds; FMP fund coverage unverified |
| Mutual fund holdings *(if ever needed)* | **SEC N-PORT, free** | Not worth $1,080/yr of Ultimate for a handful of funds |

**Cost:** FMP Premium $708/yr + Sharadar Bundle ~$299–$499/yr + $0–300/yr for funds = **~$1,010–$1,510/yr**. Within your budget, and **cheaper than the FMP Ultimate upgrade you were already contemplating** (~$1,788/yr) while solving problems Ultimate does not touch.

**Bonus:** running two independent fundamentals feeds gives you a reconciliation capability neither provides alone. `doc/backfill.md`'s `volume_ambiguous` verdict exists because two feeds agreeing proves nothing — but two feeds *disagreeing* on a balance-sheet identity is a high-quality error signal, and it is the single cheapest data-quality instrument available to you.

---

### 6.4 What FMP is still worth, honestly ranked

With the backfill already banked, most of FMP's value is sunk. Ranked by what it still buys:

| # | What FMP still uniquely provides | Worth $708/yr? |
|---|---|---|
| 1 | **Observed raw OHLC** — Sharadar gives `closeunadj` only | **This is the whole case.** |
| 2 | **Observed raw volume** (`unadjustedVolume`) — SEP has no raw volume field | Part of the same case |
| 3 | Ability to re-pull or repair 1990–1997 | Only if `landing.fmp_raw` was pruned — **check this first** |
| 4 | The 3 of 8 mutual funds it covers | Weak — a fund vendor is needed anyway (§3.8) |
| 5 | Intraday, if that milestone ever lands | Speculative; Sharadar has none |
| 6 | Economic series / treasury rates | **No** — FRED is free, authoritative, and out of scope per the build prompt |
| 7 | Index constituents | **No** — Sharadar carries S&P 500 membership back to **1957** |
| 8 | Company profiles, news, sector aggregates | **No** — out of scope or derivable from `core` |
| 9 | A second opinion for cross-vendor reconciliation | Real methodological value, but a luxury at this price |

Rows 6–9 do not justify the subscription. Rows 1–2 might. Row 3 is probably already covered by your own landing layer — **`landing.fmp_raw` holds the original payloads**, so if backfill-era rows were retained, cancelling FMP does not destroy provenance and row 3 evaporates.

**The middle path worth pricing: FMP Starter, $22/mo ($264/yr).**

Starter's binding limit is *5 years of history* — which no longer matters, because the deep history is already in the table. What Starter would still give you is observed raw OHLCV for the **nightly incremental flow**, repair capability for recent dates, and the three mutual funds. At 300 req/min an 8,000-symbol nightly load takes ~27 minutes, which is acceptable.

That preserves the only two things FMP still uniquely provides, at **37% of the cost**. Two things to verify before switching: that the `non-split-adjusted` endpoint is available on Starter (unverified — this is the whole point of the exercise), and that Starter's 20 GB bandwidth covers the nightly load.

**Decision summary:**

| If you… | Then | Annual price side |
|---|---|---|
| Hold ADR 0001 literally — raw means observed | **FMP Starter + Sharadar** | ~$563–$763 |
| …and want repair depth or intraday later | **FMP Premium + Sharadar** | ~$1,007–$1,207 |
| Accept imputed OHLCV, documented in an ADR | **Sharadar alone** | ~$299–$499 |

All three are inside budget. The third is defensible *if* you supersede ADR 0001 in writing, add a DQ check that the imputation reproduces `closeunadj` exactly, and accept that `price_scale_collapse` needs rethinking. What is not defensible is drifting into the third option without noticing you left the first.

---

## 7. Risks and caveats

### 7.1 What I could not verify

| Open question | Why it matters | How to close it |
|---|---|---|
| Does FMP actually carry delisted-company fundamentals? | R5 is a hard requirement | `probe-fundamentals` against a known delisted set, using FMP's delisted-companies endpoint for correct symbology |
| Where exactly does the synthetic-filing-date boundary fall across the universe? | Determines the honest fundamentals floor | Compute `filingDate - date` across the probe set; find the year where zero-lag stops |
| Sharadar's exact **full-history** Bundle price | $299 is the entry tier; full history may be $499 | Their subscribe page shows the tier ladder once selected |
| Sharadar's SF1 field inventory vs your §4.2 column map | Determines migration effort | Free tier covers all 30 Dow companies — probe it before paying |
| Whether Sharadar SEP prices reconcile with FMP's | Two price feeds must agree or you have a problem | Generalize `probe_prices`' arithmetic test to a third feed |
| **Does FMP return distributions for mutual fund symbols?** | Decides whether you need a second fund vendor at all | `GET /stable/dividends?symbol=VFIAX` against a known ex-date. One call. |
| Whether Tiingo's fund distributions endpoint is out of beta | The fallback if FMP fails | Email Tiingo support; their Jan 2025 changelog still said Beta |

### 7.2 The licensing caveat — read this before subscribing

Sharadar's cheap tier is a **Personal Use License**. You told me this is personal research only, and on that basis it fits.

But the boundary is drawn tightly, and it is drawn around *your situation*, not your intent. QuantRocket, a Sharadar reseller, states the rule plainly: the Non-Professional licence requires certifying you *"will use the software solely for personal, non-business purposes"*, while the Professional licence is required if you **"manage other people's money, work in finance, run a business, or collaborate with others"** — and explicitly names **"Investment advisors/RIAs"**. Critically: *"Based on our agreement with Sharadar, professional users must purchase Sharadar data from Nasdaq Data Link"* — where pricing is login-gated and not publicly disclosed.

I flag this because this workspace carries an RIA checklist skill covering trading, investment process and market data. If `fafnir` is ever plausibly connected to advisory work — even indirectly, even as internal research that informs client portfolios — the Personal Use License is the wrong licence, the Professional price is unknown and likely well above your budget, and the whole recommendation needs revisiting. **Please confirm which side of that line you're on before subscribing.** If you land on the professional side, EODHD All-In-One ($999/yr, with a documented commercial-licensing path) becomes the better second vendor despite weaker PIT guarantees.

### 7.3 Residual risks

| Risk | Severity | Mitigation |
|---|---|---|
| Two vendors means two symbologies, two universes, two `security_id` mappings | **High (effort)** | ADR 0002's surrogate key exists for exactly this. Map on CIK where possible, ticker+date otherwise; store both vendor tickers on `core.security` |
| Sharadar's 1998 floor becomes the real fundamentals floor | Medium | Document it. Load FMP 1990–1997 as a quarantined, clearly-labelled second source |
| Sharadar is a small company; Nasdaq Data Link distribution is opaque | Medium | Bulk full-table CSV export means you can hold a local copy. Take one immediately on subscribing |
| The zero-instead-null problem persists in Sharadar too | Medium | Probe it. Add a DQ check counting exact-zero values on line items that are implausibly zero for large filers |
| FMP billing/auto-renewal complaints | Low–Medium | Best-corroborated complaint cluster in the research. Calendar the renewal date; don't rely on the cancellation flow |
| Scope creep into a full migration | Medium | Explicitly decline it. FMP stays the spine. |
| **Fund NAV rows break DQ checks** — `O==H==L==C`, `volume==0` on every row | Medium | Gate zero-volume, OHLC-range and intraday-spread checks on security type, not symbol lists (§3.6) |
| **Fund total return computed without distributions** | **High** | ~54% understatement over 25 years on VFIAX (§3.7). Add a `fund_missing_distributions` DQ check that flags any fund with NAV history but zero distribution records over a trailing year |
| Capital gains distributions folded into the dividend type | Medium | Add explicit distribution action types now, while it is additive (§5.3) |
| **A fund that returns 1–2 bars loads silently and looks covered** (`GSIMX`) | **High** | Add a `fund_insufficient_history` DQ check: any declared fund with fewer than N bars, or whose latest bar is more than 5 trading days stale, is an `error`. Do not rely on `check_freshness` — it is keyed to `exchange_code="NASDAQ"` |
| Vendor fund coverage is per-share-class and unpredictable | Medium | Establish coverage per symbol empirically at declaration time; re-verify on a schedule. Never infer coverage from a sibling class or fund family |

---

## 8. Recommended sequence

### 8.1 Do this week, free, regardless of any vendor decision

1. **Raise `request_rate_per_min` 280 → 700.** You are on Premium (750/min) and throttled to a Starter-tier number. Nightly price load drops from ~29 min to ~11 min. Files: `etc/fafnirrc:39`, `config.py:127`, `sources/base.py:81`, `sources/fmp.py:104`.

2. **Add a look-ahead DQ check for implausible filing lag.** This is the single highest-value change in this report:

   ```
   check_name: fundamentals_implausible_filing_lag
   rule:       knowledge_date - fiscal_date < 5 days  ->  severity 'error'
   action:     force knowledge_source = 'estimated', apply the statutory
               fallback lag, and exclude from mart.fundamental_panel by default
   ```

   Your existing `CHECK (knowledge_date >= fiscal_date)` catches time travel. It does not catch *instantaneous* filing, which is the failure mode FMP actually exhibits. Add it before the fundamentals backfill runs, not after.

3. **Tighten the `knowledge_date` resolution in §4.5.** Insert a plausibility gate ahead of branch 1: if `accepted_date` has a `00:00:00` time component *and* equals `fiscal_date`, treat it as absent and fall through to the estimate branch.

4. **Add `fundamentals_suspicious_zero`** — count exact-zero values on `interest_expense`, `research_and_development_expenses` and `depreciation_and_amortization` for filers above a revenue threshold. §3.4 shows these are unmapped-as-zero, and you cannot fix what you cannot see.

5. **Cancel the Ultimate upgrade plan.** §3.2 removes its leading justification and §3.6 removes the mutual fund one. Revisit only if 13F or targeted intraday become real milestones on their own merits — and note that fund *holdings*, the remaining fund-side draw, are free from SEC N-PORT.

6. **Settle the fund distributions question with one API call:**
   ```
   GET /stable/dividends?symbol=DODIX&apikey=...
   ```
   Compare against a known ex-date. Use a fund FMP actually covers, or you will not learn anything.

7. **Settle the 402-vs-absent question for the five missing funds:**
   ```
   curl -si "https://.../stable/search-symbol?query=DODWX&apikey=..."
   ```
   `402` means plan-gated and Ultimate might help. `200` with `[]` means genuinely absent and no FMP tier will fix it.

8. **Trial Tiingo's free tier against all eight tickers.** Zero cost, and it decides whether the fund problem costs $0 or ~$300/yr. Test the five FMP misses specifically — §3.8 shows that general coverage claims predict nothing at the individual-symbol level.

9. **Add the `fund_insufficient_history` DQ check before loading any fund.** `GSIMX`'s single bar would otherwise enter the warehouse as a successfully-loaded security.

### 8.2 Then, in order

6. **Take Sharadar's free tier** (all 30 Dow companies, full history) and run `probe-fundamentals` against it. Compare field inventory to your §4.2 map, verify ARQ vs MRQ genuinely differ on a known restatement (MSFT FY2015 is already in your test plan and is a good case), and confirm `datekey` semantics.

7. **Resolve the licence question in §7.2.** Do not subscribe until this is settled.

8. **Subscribe to the Sharadar Bundle at full history.** Immediately take a full bulk CSV export of SF1, SEP, ACTIONS and TICKERS as a local snapshot.

9. **Build the fundamentals milestone against Sharadar, not FMP.** Your build prompt's PR sequence survives almost unchanged — PR 1 becomes `probe-sharadar`, PR 3's loader targets SF1 with the ARQ dimension, and §4.6's SCD-2 machinery becomes *genuinely* bitemporal rather than forward-accumulating, because SF1 supplies the restatement history you were planning to accrue over years.

10. **Write two ADRs.**
    - `0008-multi-vendor-source-strategy.md` — FMP for prices/actions/reference, Sharadar for PIT fundamentals; the `security_id` mapping rule; the reconciliation checks.
    - `0009-fundamentals-history-floor.md` — records that the fundamentals floor is 1998 (Sharadar PIT) with 1990–1997 available from FMP as a labelled non-PIT source, and *why*: because FMP's pre-1994 filing dates are fabricated.

11. **Close ADR 0007's option 7 in writing** — there is no bulk splits/dividends endpoint on FMP Ultimate, so your calendar-sweep design stands as correct. This was already established in your upgrade memo and just needs recording.

12. **Extend ADR 0006 (fund universe) with the distribution model.** Record that fund NAV rows are raw and single-valued (`O==H==L==C`, `volume==0`), that DQ checks must be gated by security type, that capital gains distributions are distinct action types from dividends, and that fund holdings — if ever wanted — come from SEC N-PORT rather than a vendor tier.

---

## 9. Answering the question directly

> **Am I using the right vendor for this project?**

For prices, corporate actions, reference data and breadth-per-dollar: **yes, clearly.** FMP reaches 1985 on your current plan, provides a correct unadjusted feed, and `fafnir` already encodes hard-won knowledge of its quirks. Migrating away would destroy real value for a marginal gain.

For point-in-time fundamentals — the milestone you are about to build, and the thing the entire bitemporal architecture exists to serve: **no.** FMP has no restatement history by construction, and it fabricates filing dates for pre-1994 records in a way that defeats every guard you built. That is not a vendor you can validate your way out of.

> **Who would you recommend?**

**Both.** Keep FMP Premium ($708/yr) as the spine; add Sharadar Core US Equities Bundle ($299–$499/yr) as the point-in-time fundamentals source. Total ~$1,010–$1,210/yr — under budget, and cheaper than the FMP Ultimate upgrade you were considering, which would not have solved this problem.

The deciding argument is not that Sharadar is a better vendor in general. It is that `fafnir` has a specific, unusual, and well-designed requirement — a defensible knowledge timeline — and exactly one affordable vendor sells that as a product rather than as something you assemble yourself and hope is right.

> **And what about the mutual funds?**

Not a reason to upgrade — but a reason to add a third source, which is not where I expected this to land.

FMP is not gated on the *asset class*: it returns fund NAVs to inception, raw, in exactly the shape `core.daily_price` wants. But tested against the eight funds you actually hold, it covers **three**. `DODIX` reaches 1990 and `DODWX` — same firm, flagship product — is absent entirely. `GSIYX` is complete from inception and `GSIMX`, a different share class of the identical portfolio, returns one bar from 2022. There is no rule there to design around, which means fund coverage must be established per symbol and re-verified, never inferred.

Ultimate does not fix this: it adds holdings, not symbols, and all five problem funds are US-domiciled so global coverage is irrelevant. Sharadar does not fix it either — SFP covers ETFs, CEFs, ETNs and ETDs, not mutual funds. Trial Tiingo's free tier against all eight; that is a zero-cost experiment that decides whether this costs nothing or ~$300/yr.

Two correctness notes carry more weight than the coverage count. **Distributions** are a genuine gap at every tier and every vendor — a NAV-only VFIAX series understates the 25-year outcome by roughly half, and the distribution is far too small relative to daily noise to recover from the price series. And **`GSIMX`'s single orphan bar is more dangerous than the four outright failures**, because an empty load fails loudly while a one-row load succeeds quietly and marks the security as covered.

That last point is really the theme of this whole assessment. The costly errors here — fabricated filing dates, restated fundamentals wearing a bitemporal schema, adjusted prices in a raw column, a fund with one bar — are not the ones that throw. They are the ones that load cleanly, satisfy every constraint, and are wrong only in the number you were trying to measure. `fafnir` is unusually well designed to catch that class of error. This report's recommendations are mostly about pointing that machinery at the places it is not yet aimed.

---

## Sources

**Vendor documentation**
- [Sharadar — Subscribe / pricing](https://sharadar.com/subscribe) · [SF1 fundamentals docs](https://sharadar.com/docs/fundamentals) · [SEP prices docs](https://sharadar.com/prices) · [Homepage / bulk downloads](https://sharadar.com/)
- [FMP — Pricing plans](https://site.financialmodelingprep.com/developer/docs/pricing) · [Changelog](https://site.financialmodelingprep.com/developer/docs/changelog) · [Delisted Companies API](https://site.financialmodelingprep.com/developer/docs/delisted-companies-api)
- [EODHD — Pricing](https://eodhd.com/pricing) · [Bulk EOD/splits/dividends API](https://eodhd.com/financial-apis/bulk-api-eod-splits-dividends) · [Commercial vs personal licence](https://eodhd.com/financial-apis/commercial-vs-personal-license-use)
- [Intrinio — Pricing](https://intrinio.com/pricing) · [Tiingo — Pricing](https://www.tiingo.com/about/pricing) · [Massive (ex-Polygon) — Pricing](https://massive.com/pricing) · [Norgate — Packages](https://norgatedata.com/stockmarketpackages.php) · [SimFin — Prices](https://www.simfin.com/en/prices/) · [sec-api.io](https://sec-api.io/) · [Databento — Pricing](https://databento.com/pricing)
- [Marketstack — FAQ (confirms US data licensed from Tiingo)](https://marketstack.com/faq)
- [QuantRocket — Sharadar data pricing & licence tiers](https://www.quantrocket.com/pricing/data/sharadar/)

**Institutional / reference tier**
- [CRSP — Subscription information](https://www.crsp.org/subscription-information/) · [WRDS — CRSP vendor page](https://wrds-www.wharton.upenn.edu/pages/about/data-vendors/center-for-research-in-security-prices-crsp/)
- [Compustat — Product brochure (PDF)](https://www.spglobal.com/marketintelligence/en/documents/spgmi375-03321-compustat_brochure_digital_letter_ss3.pdf)
- [LSEG — Developer terms of use](https://developers.lseg.com/en/terms-of-use) · [Bloomberg — Data License](https://professional.bloomberg.com/products/data/data-management/data-license)

**Point-in-time and data-quality research**
- [S&P Global — Point-In-Time vs. Lagged Fundamentals (PDF)](https://www.spglobal.com/content/dam/spglobal/mi/en/documents/general/sp-capitaliq-quantamental-point-in-time-vs-lagged-fundamentals.pdf) — the 48-backtest study, 78% restatement figure, and 15/14 bps small-cap deviations
- [Lookahead Bias in Fundamental Backtests: 66 Days, Measured](https://tradevodata.com/blog/lookahead-bias-fundamental-backtests) — 313,406 filings, mean 66-day lag, 18,734 material revisions
- [Banz & Breen (1986), *Journal of Finance*](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1986.tb04548.x) — foundational survivorship/look-ahead paper
- [Data Quality Problems Troubling Business and Financial Researchers (2020)](https://digitalcommons.wcupa.edu/cgi/viewcontent.cgi?article=1013&context=lib_facpub) — literature synthesis with quantified vendor distortions

**Reputation and independent testing**
- [FMP on Trustpilot](https://www.trustpilot.com/review/financialmodelingprep.com) · [SourceForge](https://sourceforge.net/software/product/Financial-Modeling-Prep/) · [G2](https://g2.com/products/financial-modeling-prep/reviews)
- [TradingBrokers — FMP review (Jun 2026)](https://tradingbrokers.com/financial-modeling-prep-review/) · [QuantLabsNet — critical FMP review](https://www.quantlabsnet.com/post/data-provider-disaster-a-financial-modeling-prep-api-review-alternatives)
- [FinancialModelingPrep.NET issue #145 — v3→stable breaking changes](https://github.com/MatthiWare/FinancialModelingPrep.NET/issues/145)
- [Portfolio Optimizer — Selecting a stock market data web API](https://portfoliooptimizer.io/blog/selecting-a-stock-market-data-web-api-not-so-simple/) — independent adjusted-price accuracy testing
- [Stonks Capital — "Massive Problems"](https://stonkscapital.substack.com/p/massive-problems-part-1) — independent Polygon/Massive audit
- [QuantRocket forum — Sharadar fundamental data issue](https://support.quantrocket.com/t/sharadar-fundamental-data-issue/1882) · [Elite Trader — Test cases for assessing fundamentals feed quality](https://www.elitetrader.com/et/threads/test-cases-for-assessing-quality-of-fundamentals-feeds.348258/)

**Mutual funds**
- [FMP — ETF & Fund Holdings API](https://site.financialmodelingprep.com/developer/docs/stable/holdings) · [Mutual Fund Disclosures API](https://site.financialmodelingprep.com/developer/docs/stable/mutual-fund-disclosures) · [Dividends Company API](https://site.financialmodelingprep.com/developer/docs/stable/dividends-company)
- [Sharadar Fund Prices (SFP) docs — ETFs/CEFs/ETNs/ETDs, no mutual funds](https://sharadar.com/docs/funds)
- [Tiingo — Distributions API, states mutual funds supported](https://www.tiingo.com/documentation/corporate-actions/dividends) · [Tiingo — Mutual fund prices on the free tier](https://www.tiingo.com/blog/mutual-fund-prices/) · [Tiingo changelog (Beta status)](https://www.tiingo.com/documentation/general/changelog)
- [EODHD — Splits & Dividends API](https://eodhd.com/financial-apis/api-splits-dividends) · [EODHD — Fundamentals incl. mutual funds](https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds)
- [yfinance issue #2666 — Yahoo conflates dividends and capital gains](https://github.com/ranaroussi/yfinance/issues/2666)
- [SEC Form N-PORT — free quarterly fund holdings](https://sec-api.io/datasets/form-nport)

**Primary measurement**
- Live FMP Premium API calls made 2026-09-02 against your own key: `AAPL` annual income statement (41 rows, FY1985–FY2025), `GE` quarterly income statement (120 rows to 1996-09-30), `AAPL` daily prices 1985-01-01→2000-12-31 (4,043 bars), empty results for `LEH`, `ENRNQ`, `SUNEQ`, `VFIAX` daily NAV history (1,289 rows from its 2000-11-13 inception; 668 rows 2024–2026; 12 rows Dec 2025), and `quote` for `VTSAX, VFIAX, FXAIX, PRWCX`.

### Confidence

- **High:** the synthetic pre-1994 filing dates; 1985 price and fundamentals depth on Premium; the default price endpoint being split-adjusted; zero-instead-null in older statements; Sharadar's AR/MR dimension semantics and personal-tier pricing; FMP's lack of restatement history; **FMP Premium serving mutual fund NAVs to inception**; **Sharadar SFP excluding mutual funds**; fund NAV rows being raw and single-valued.
- **Medium:** delisted fundamentals coverage on FMP (three empty probes, not a proof); Sharadar's exact full-history Bundle price; how well Sharadar's field inventory maps to your §4.2 contract; the ~54% distribution understatement (arithmetic is sound, but the 1.8%/yr average yield is an assumption, not a measurement).
- **Low / must be probed:** where the synthetic-date boundary falls universe-wide; whether Sharadar shares the zero-instead-null defect; Sharadar SEP vs FMP price reconciliation; **whether FMP's dividends endpoint covers mutual fund symbols at all**; whether Tiingo's distributions endpoint has left beta; **whether Tiingo/EODHD cover the five funds FMP misses**.

### Revision history

- **Rev. 1 (2026-09-02)** — initial assessment.
- **Rev. 2 (2026-09-02)** — added mutual fund coverage (§3.6, §3.7, §5.3) after the requirement was raised.
- **Rev. 3 (2026-09-02)** — added §3.8 after testing the eight held funds. **Corrects Rev. 2's claim that funds cost nothing incremental**: FMP covers 3 of 8, so a second fund source is required. Cost range revised to $1,010–$1,510/yr.
- **Rev. 4 (2026-09-02)** — added §5.2b and §6.4 in response to "why keep FMP at all, given the backfill is done?" **Narrows the case for FMP to two things**: observed raw OHLC and observed raw volume, neither of which Sharadar SEP provides (`closeunadj` is its only unadjusted field, and it has no raw volume). Adds **FMP Starter at $264/yr** as a middle path, and a three-way decision table. Most of FMP's remaining value is genuinely sunk.
