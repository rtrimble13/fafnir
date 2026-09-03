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
problems.

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
