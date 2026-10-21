# Data semantics: the traps that produce confidently wrong answers

Each of these has a specific plausible wrong answer attached. That is why it is
here — none of them announce themselves, and every one of them reads as a data
defect when it is actually a misreading.

## 1. Raw is not adjusted

`mart.v_daily_price_raw` is **as traded**. A 4:1 split is a real 75% drop in it,
because that is what the tape says (ADR 0001, ADR 0004 — fafnir ingests
unadjusted and derives adjustment on read).

> **Wrong answer:** "AAPL fell 75% in August 2020." That was a 4:1 split.

`mart.v_daily_price_adjusted` is split/dividend adjusted and point-in-time stable.
**Always say which one you read.** For returns, volatility, drawdown, or any
comparison across a split date, you want adjusted. For "what did it actually trade
at", you want raw.

The corollary, for triage: an `outlier` flag on a split date is *expected* in raw.
The diagnostic is whether the **adjusted** series also jumps.

## 2. `mart.security_latest` is materialized and refresh-lagged

It is a MATERIALIZED view, refreshed by `fafnir db refresh-marts` at the end of
the nightly job. Everything `screen_securities` returns comes from it.

> **Wrong answer:** a screen that silently predates last night's load, reported as
> current.

`mart.v_security_profile` is an ordinary view and is **live** — use it (via
`security_profile`) when currency matters. If a screen looks stale, check whether
`refresh-marts` ran: `ingestion_runs` and the journal will say.

## 3. `market_cap_usd` and `beta` are a screener snapshot, not history

They are refreshed with the security master and overwritten each time. There is no
time series behind them.

> **Wrong answer:** "market cap over time" — a series that does not exist. Any
> such request has to be built from adjusted price × shares outstanding, and
> shares outstanding is not in the warehouse yet (fundamentals is a planned
> milestone).

## 4. Delisted securities are retained, deliberately

No survivorship bias is a design goal, not an oversight. The warehouse keeps
companies that stopped trading, with every bar.

> **Wrong answer:** a screen or a universe count that includes companies dead for
> years, described as "the current universe".

Filter `is_actively_trading` unless you specifically want the historical
universe. When you do include them, say so.

**Issuers that died before fafnir first ran** are minted from Sharadar
(`core.security.source = 'sharadar'`, #71) when they last traded on or after
1998-01-01. Until the Sharadar backfill (#75) loads them, those rows hold **no
bars**: a count of delisted securities is larger than the set with history. Before
1998 no vendor records delistings, so a cross-section is survivor-only there
(ADR 0016).

> **Wrong answer:** "the warehouse has no survivorship bias" for a pre-1998 date,
> or a delisted universe whose bars are assumed to exist because the row does.

## 5. One `security_id` survives a rename

FB → META keeps one `security_id` and one continuous price history. The ticker
lives in `core.symbol_xref` over time: `valid_to IS NULL` is the current ticker,
`valid_to IS NOT NULL` is one it used to trade under.

> **Wrong answer:** "META has no history before 2022."

The resolver ladder tries the live ticker, then the primary symbol, then a former
ticker — so `resolve_symbol('FB')` finds META and says it matched a former ticker.
An unapplied rename is the opposite problem: the company's identity is split
across two `security_id`s, and neither has the whole history. `fafnir status`
reports those under **Renames**.

## 6. `ttm_dividend_amount` is anchored to the security's own last ex-date

Not to `now()`. Deliberately: a view whose output changes with the clock cannot be
compared between two runs, and a delisted security would report a TTM of zero
rather than "nothing since it stopped trading".

> **Wrong answer:** a stale yield on a delisted name, presented as current. Always
> check `last_dividend_date` before turning TTM into a yield.

## 7. Money is `NUMERIC(20,6)`, and the cliff is 5e-7

Below roughly 5e-7 a price cannot be represented; between ~5e-7 and 1.5e-6 the bar
**stores with `open = high = low = close`** rather than being rejected, and that
is indistinguishable downstream from a genuine no-trade day.

> **Wrong answer:** volatility or returns computed across a run of flattened
> bars. They are fictional, not approximate.

`price_scale_collapse` flags exactly this. Its `detail` holds the source high/low
that `core.daily_price` no longer has.

Related: decimals arrive from the MCP tools as **strings**, not floats, so exact
NUMERIC money is not silently turned into binary floating point at the last step.
Convert deliberately if you need to compute.

## 8. `price_*` DQ flags repeat; every other check does not

`price_*` is one row per *detection* (by design — the count drives the watermark
hold logic). Everything else is one row per *open condition*.

> **Wrong answer:** "the DQ queue is exploding" when one stuck symbol is
> re-quarantining the same bar nightly.

`dq_totals` separates them: read `distinct_condition_flags` for the count of
problems. Two `price_*` checks are not quarantines and do not repeat:
`price_scale_collapse` and `price_restatement_conflict` (the bar was stored, or kept)
are one row per open condition. `price_split_scale_mismatch` is a quarantine and
repeats like the rest.

## 9. Open DQ is a count of problems, not of runs

A standing condition is flagged once and stays one row until someone sets
`resolved_at`, however many nights it goes unfixed. So a **rising** count means
new problems. Nothing closes flags automatically.

## 10. `mart.v_security_dq_open` shows open flags only, and never `detail`

The read profile's `dq_summary` reads that view. It deliberately withholds
`detail`, resolved rows, `resolved_by` and `resolution_note` (ADR 0009).

> **Wrong answer:** "this security is clean" when it has a resolved history full
> of caveats, or "there is no detail" when there is and you were looking at the
> wrong tier.

Use `dq_queue` (ops profile) for detail and resolution history.

## 11. `ops` is invisible to every read role

`ops` is granted to `fafnir_ingest` (write) and `fafnir_ops` (the agent read
tier). Neither `fafnir_app` nor `fafnir_read` can see it at all.

> **Wrong answer:** concluding lineage does not exist because a mart-only
> connection could not find it.

## 12. `vwap` is nullable and unconstrained

An unusable vwap is dropped rather than failing an otherwise good bar. A NULL vwap
is normal and is not a data-quality problem.

## 13. Volume on the adjusted series is back-adjusted

A deep forward-split history multiplies volume by the cumulative split ratio, and
it can exceed int64 — which is why the adjusted path falls back to exact Python
ints. Adjusted volume is not "shares that traded that day"; raw volume is.

## 14. One ticker's vendor history can span several issuers

FMP serves a ticker's whole history, whoever used it. A new ETF or SPAC minted with
a full backfill therefore arrives carrying the bars of every earlier instrument on
that ticker: CAPA (a 2026 ETF) held a 2003–08 stock and the 2020–21 HighCape SPAC;
BID (a 2026 SPAC) holds Sotheby's 2003–2019.

> **Wrong answer:** "Tribeca Strategic Acquisition has traded since 2003", or
> deleting the earlier bars as junk when they are the warehouse's only copy of a
> delisted company.

The first bar of the real listing is usually ~$25 for an ETF and ~$10 for a SPAC,
after a gap of months or years. Split the history at gaps before reading any
statistic across it.

## 15. The "unadjusted" feed is sometimes adjusted, and a split row can be invented

Some histories arrive already back-adjusted for later reverse splits — a leveraged
ETF opening at 6,229 on 46 shares (SMUP), or trading at 30,000–108,000 (WZRD). And
FMP has stored split rows that exactly match a history it holds at the wrong scale
(HUN's "10:1 split" in 2014; AKR's closes up to 149,613,176), so the adjusted series
is smooth and both series are wrong.

> **Wrong answer:** "the adjusted series is smooth, so the split is real." Check the
> price levels against what the instrument could have traded at.

## 16. The trading calendar is wrong for MLK Day 1990–1997

`ref.trading_calendar` marks Martin Luther King Jr. Day closed from 1990, but NYSE
first closed for it in 1998. Real bars exist on those dates (BXMT, ALNT, AIRT, PRG,
ASRV, ARWR, UEIC).

> **Wrong answer:** treating those bars as non-session prints to delete. The
> loader's non-session set-aside also drops them on any backfill.

## 17. Some ETFs' newest bar is provisional

For several thinly traded ETFs FMP serves a last bar that it restates the next night,
sometimes from or to the wrong scale (XNDX, MILK, MSEP, NODE; VDG's *restated* bar was
the bad one).

> **Wrong answer:** an outlier on the newest bar read as a market move, or deleted
> as a bad bar, on its first night. Recheck it after the next load.

## 18. Warrants, rights and units are not in the universe (ADR 0012)

From the release that carries ADR 0012, `ingest securities` skips them. The ones
minted before it stay until an operator runs `fafnir security descope`, once. Until
then the load no longer refreshes them, though their prices still load. Whether it
has run is an `ops.ingestion_run` row with endpoint `security-descope`. After it, a
warrant, right or unit that *is* present was kept on purpose. Either it was
declared (`fafnir track list`), which is for tickers that still list, or it was
named with `--keep` on that run (the audit row's `kept_by_option`), as Granite
REIT's delisted `GRP-UN` was. SPAC class A shares are still held either way.

**Never propose `track add` for a delisted ticker.** `ingest tracked` looks only for
a *listed* security under a declared ticker. It finds none, and mints a new, active
one that the price step backfills with a duplicate of the history. If FMP has no
profile for the ticker, it raises `tracked_symbol_unknown_to_source` every night
instead.

> **Wrong answer:** "fafnir has no data for ABCDW" read as a load failure, or a
> SPAC's unit or warrant history expected next to its class A shares. Equally, a
> warrant still held before the descope has run is not a filter failure.

## 19. A dividend can be stored twice across a ticker change

After a rename, FMP files a distribution again under the new ticker. When a split
takes effect on the record date, it also restates the amount on the new share
basis and dates it at the ticker change.

OPNW (VerifyMe → OpenWorld, 2026-09/10) carried two rows with the same record and
payment dates: $0.15 ex 09-29 under VRME, and $1.50 "ex 10-01" under OPNW. That was
one dividend counted twice, and it put the adjusted prices about 17% low.

When a split and a dividend share an ex-date, the stored dividend must be in
**pre-split** terms. The factor is valued against the prior raw close, which is the
pre-split close. A post-split amount there is wrong either way. After a large
enough reverse split, like OPNW's 1-for-10, it exceeds that close: the factor goes
negative (`dividend_exceeds_price`) and the dividend is skipped. After a forward
split it is silently too small, and nothing flags it.

> **Wrong answer:** two dividends with the same record and payment dates read as two
> distributions, or the post-split amount "corrected" onto the split's ex-date.

## 20. A venue transfer can arrive as a delisting

FMP's delisted feed has reported a move between exchanges as a delisting. ET, SUN,
SUNC, USAC, DDS and DDT moved from the NYSE to the Texas Stock Exchange (**TXSE**;
TSX is Toronto) after the 2026-10-02 close and kept their tickers. The nightly
sweep stamped them delisted 2026-10-05, and they stopped receiving bars.

FMP's profile and screener are no better guide. A week after the move they still
named ET's venue as the NYSE and called it `isActivelyTrading: false`, while
quoting its price and that day's volume. FMP had no code for TXSE. **Its price feed
is the evidence:** a security that delisted has no bar after its delisting date.

Since ADR 0014:

- **Caught.** The delisting sweep declines a recent delisting the vendor has bars
  after, and flags `security_delisting_contradicted`. The nightly price universe
  keeps a listed security with a bar in the last 30 days even when FMP calls it
  inactive, and the universe load raises `security_inactive_but_trading` when it
  turns such a row inactive.
- **Decided** by an operator, or by you after a dry run and an explicit yes:
  `security venue-transfer` for a move between listed venues, `security delist`
  for a delisting, such as a move to OTC (`references/dq-playbooks.md`).
- **The venue is fafnir's own.** `exchange_code = 'TXSE'` with a non-null
  `venue_override` means the move was recorded by hand. The nightly load keeps it
  until FMP names a venue other than `venue_override_left`.

Before reporting a large, liquid name as delisted, check for bars after its
`delisted_date` and for these flags, then look for a public notice of a move.

> **Wrong answer:** "ET was delisted on 2026-10-05". It changed venue.

## 21. FMP restates bars, and the warehouse keeps the first print

FMP's first print of a bar is usually right; what it serves on a later read of the
same date often is not. From 2026-09-16 the nightly re-read of the last five
sessions stored another fund's prices over first prints (OUSL ~11 -> ~58, QCJL ~26
-> 0.07), prices at the scale of a split not yet gone ex (GCDT x6, ZBAO x50, volumes
unchanged), and a phantom split's scale (SKYD x2). An operator re-fetch once
returned prices right and volume x50.

Since v1.5.0 the nightly load keeps the stored bar when the re-read moves the close
by more than 1.25x or the volume by more than 3x, either way, and flags
`price_restatement_conflict` with both sides. Only an explicit `ingest prices --from`
overwrites, and it prints what it restated.

> **Wrong answer:** "the vendor corrected it, so the stored bar is stale." Read the
> conflict's `detail`: the first print is usually the right one, and when it is not
> (SKYD 10-02), a probe and a re-fetch put it right in one step.

> **Wrong answer:** "core.daily_price.loaded_at says the bar was loaded last night,
> so that is what dorq read." The 5-session re-read rewrites `loaded_at` and
> `ingestion_run_id` on every overlap; `dq compare` reads the run's own holding
> snapshot instead.

## 22. A row's `source` is who supplied it, not who feeds the security (ADR 0015)

Once Sharadar loads, one security's history can hold rows from two vendors.
`core.daily_price.source` and `core.corporate_action.source` name the party that
wrote **that row**; `core.security.source` names the vendor that **minted** the
security. Neither says which vendor loads its next bar: that is the open link with
`is_feed = true` in `core.security_vendor_xref` (or, while FMP is primary, `source =
'fmp'`).

> **Wrong answer:** "AAPL is an FMP security, so this 2019 bar came from FMP." Read
> the bar's own `source`. A Sharadar correction may have replaced it, and
> `ops.vendor_correction` holds the FMP row it replaced.

> **Wrong answer:** "the vendors disagree on this date, so one of them is broken."
> A divergence can be a convention (consolidated vs primary-exchange volume). The
> reconciliation's category says which; only categories the operator chose to
> correct are ever replaced.

Precedence is `operator > sharadar > tiingo > fmp`. FMP never overwrites a Sharadar
row, and no vendor overwrites an operator row. `mart` views do not expose `source`
(ADR 0009); answer provenance questions on the ops tier.

## 23. Sharadar-sourced raw history is derived; the split-ratio check guards it

A `core.daily_price` row with `source = 'sharadar'` was not observed raw. SEP serves
split-adjusted prices and a raw close (`closeunadj`); fafnir recovers the raw open,
high, low and volume with the product of the ACTIONS splits after the date (#75,
ADR 0004's amendment). The close is exact to SEP's 3 decimal places; the others are
off by up to 0.0005 × the cumulative split factor (#49). A recovered high that fell
below the close, or a low above it, within that rounding was set to the close
(counted `rounding_clamped` on the run); before that fix, run 403 quarantined such
bars as `price_cross_field_violation` instead.

> **Wrong answer:** treating a Sharadar bar's open/high/low on a split-heavy dead
> issuer as exact to the cent, or ignoring `sharadar_split_ratio_mismatch`.

Where the vendor's own `closeunadj / close` disagrees with ACTIONS, the bar was
recovered with the vendor's ratio and the security carries
`sharadar_split_ratio_mismatch`: a split is missing from ACTIONS or wrong there,
so the security's adjustment factors (built from ACTIONS, #73) are suspect too.
