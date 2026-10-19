# Ingestion

How data flows from FMP into the warehouse, and the guarantees that make it
research-grade.

## Principles

- **Land raw, then transform.** Every response is persisted to `landing.fmp_raw`
  (payload + hash) before transformation, so the original is always recoverable.
- **Validate at the boundary.** Each row is typed and sanity-checked (cross-field,
  ranges, nulls). Failures are **quarantined** into `ops.data_quality_flag`, never
  silently dropped.
- **A bar dated on a day the venue was shut is not this security's bar.** Before
  validation, `daily_price._drop_non_session` sets aside any bar whose date falls
  inside the venue calendar's span and is not an open session there. Set aside, not
  quarantined: a quarantine claims a real session's bar was bad, and there was no
  session. The raw payload still lands in full, and the count appears in the run
  stats as `non_session`. FMP returns weekend and holiday bars for some symbols —
  money-market funds strike a NAV seven days a week, and a ticker shared with
  another instrument can carry that instrument's weekend prints. The calendar is
  the security's own `exchange_code`, falling back to `CALENDAR_FALLBACK_EXCHANGE`
  when the venue has none, and is read once per symbol. Dates *outside* the
  calendar's span are unknown rather than closed — the seed writes open days only —
  so those bars are kept.
- **Idempotent.** Loads upsert on the natural key (`ON CONFLICT ... DO UPDATE`).
  Re-pulling a window converges to the same state. An upsert revises only the rows
  its own vendor supplied; a key another party holds is left exactly as it is (see
  [Who may write a row](#who-may-write-a-row-precedence-adr-0015)).
- **Incremental & resumable.** Per-symbol watermarks (`ops.load_watermark`) bound
  each pull to the new tail, with a configurable overlap (`overlap_days`, default 5)
  to absorb late corrections. A load that dies mid-universe resumes from its
  watermarks, not from scratch.
- **Lineage.** Every load opens an `ops.ingestion_run` row (params, window, counts,
  bytes, status).

## Respecting FMP limits (Professional plan)

The source client (`fafnir/sources/fmp.py`) throttles **proactively** to
`request_rate_per_min` (default 280, under the ~300/min ceiling), backs off with
exponential delay + jitter on HTTP 429 and 5xx, retries transient failures, and
**meters bytes** so bandwidth can be tracked against the 50 GB/month budget
(`ops.ingestion_run.bytes_downloaded`). Prefer comma-separated batch pulls where
an endpoint supports them.

## Endpoint → table map

All endpoints are on the `stable/` base, matching what is already proven against
the Professional plan in the original `duk`.

| Loader | FMP endpoint | Target table(s) | Natural key |
|---|---|---|---|
| `ingest securities` | `company-screener` (`stock-list`, `etf-list` for the unfiltered universes) | `core.security`, `core.symbol_xref` | (source, primary_symbol, exchange) |
| `ingest securities --enrich` | `profile` | `core.security`, `core.company_profile` | security_id |
| `ingest tracked` | `profile` | `core.security`, `core.symbol_xref`, `core.company_profile` | (source, primary_symbol) |
| `ingest symbol-changes` | `symbol-change` | `core.security`, `core.symbol_xref`, `core.symbol_change` | (source, old_symbol, new_symbol, change_date) |
| `ingest delisted` | `delisted-companies` | `core.security`, `core.symbol_xref` | security_id |
| `ingest prices` | `historical-price-eod/non-split-adjusted` | `core.daily_price` | (security_id, trade_date) |
| `ingest actions` | `splits`, `dividends`; `splits-calendar`, `dividends-calendar` | `core.corporate_action` | (security_id, action_type, ex_date) |
| `adjust` | (derived) | `core.adjustment_factor` | (security_id, effective_date) |

Sharadar (plan v2): files on disk and a ledger, read by the loaders still to come.

| Command | Sharadar request | Target | Natural key |
|---|---|---|---|
| `source sharadar-bulk` | `GET /v1.0/data/<table>?years=full` → 302 → pre-signed `<table>.csv.zip` | the file under `[sharadar] bulk_dir`; `landing.sharadar_file` | sha256 (one row per content) |
| `source ping`, `source probe-sharadar` | slices of `tickers`, `SEP`, `actions`, `SF1` | nothing (read-only) | — |

> **Verify before the first production backfill:** the `symbol-change` payload
> field names (`date`, `oldSymbol`, `newSymbol`, `companyName`) and the
> `splits` / `dividends` stable field names (`numerator`/`denominator`, `dividend`/`adjDividend`) should
> be confirmed against a live response for a known symbol (e.g. AAPL). The loader
> already tolerates the common alternates; the endpoint paths are centralized as
> constants in `FMPClient` for a one-line correction.


## The declared universe (mutual funds)

`ingest securities` builds a **discovered** universe: it re-reads `company-screener`
per venue and keeps what carries a US exchange. An open-end mutual fund has no venue
— it is struck at NAV once a day rather than traded — so it is not in the screener at
all, and nothing the nightly job does will ever mint it.

`ref.tracked_symbol` is the **declared** universe: the operator's list of symbols the
security master must hold anyway. `ingest tracked` turns each declaration into a
`security_id` via one `profile` request, and from that point nothing downstream knows
the difference — prices, actions, factors, marts and `duk` all key off
`core.security`.

```bash
fafnir source probe-fund VFIAX          # FIRST: is the NAV series raw? (3 requests)
fafnir track add VFIAX --note "core US equity sleeve"
fafnir ingest tracked                   # mints it; also runs nightly
fafnir ingest prices --symbols VFIAX    # no watermark yet -> full history
fafnir ingest actions --symbols VFIAX && fafnir adjust
duk ph VFIAX --adj -S db
```

Order in the nightly job is `securities → tracked → delisted`. Tracked runs *after*
the security master because both write through `repo.upsert_security` on the same
conflict key: going second is what makes the declared `asset_type` and venue the ones
that stand for a symbol that appears in both universes.

Three things follow from the fund grain, and each is handled where it arises:

- **NAV bars.** A fund payload carries a close and either no open/high/low or
  synthetic ones. On a **NAV strike** — a NAV-priced security's bar with no volume —
  `_validate_bar` builds `open = high = low = close` from the close and ignores the
  vendor's open/high/low even when they are present. FMP fills them with a
  distribution-adjusted NAV while the close carries the raw one, and reading them
  quarantined 3,773 of TDEAX's 4,712 bars as cross-field violations. The close itself
  is never synthesized and still faces every check, so a zero, sub-resolution or
  out-of-range close quarantines on any asset type; a fund bar *with* volume (a
  closed-end fund) is read as a session, keeping only the older allowance that an
  absent open/high/low stands in as the close.

  **Which securities are NAV-priced** is `_is_nav_priced`: `asset_type` in
  `NAV_ASSET_TYPES`, or `is_fund` on anything whose `asset_type` is not in
  `SESSION_TRADED_ASSET_TYPES` (`etf` — an ETF prints a session close however the
  vendor labels it). `asset_type` alone was not enough: the screener stores every
  fund as `equity` with `is_fund`, so a rule keyed on `asset_type` matched nothing.
- **Evening NAV.** Fund NAV is struck at 4pm ET and posted that evening, later than
  equity EOD, so a nightly run timed for equities finds funds a day behind.
  `check_freshness` calls a security stale once it is missing
  `STALE_MIN_SESSIONS_BEHIND` (2) open sessions counting the market's latest, and
  gives a NAV-priced one `NAV_LAG_TRADING_DAYS` (1) more — three in total, measured
  in sessions off the calendar so a Monday run does not count the weekend as
  lateness. Two is the base because one was met by FMP's own publishing lag on thin
  names: it wrote about 300 self-clearing flags a night. The check reads
  NAV-pricing the same way the loader does, through `NAV_PRICED_PREDICATE`, and a
  test pins the two constants together. `dq recheck` negates the same CTEs, so a
  flag written under an older threshold clears on the next recheck.
- **Distributions.** Income dividends and capital-gain distributions both drop NAV by
  the distributed amount on the ex-date — arithmetically identical to a cash dividend
  — so they load as `action_type = 'dividend'`. No new action type, no change to the
  adjustment math, no change to the mart view.

**`fafnir source probe-fund <SYMBOL>` before declaring any fund.** ADR 0001 and 0004
rest on the price feed being genuinely unadjusted, and that is verified for equities,
not for funds. The probe measures NAV across the *largest* distribution on record
(biggest, not most recent — only a large one clears that day's market move):

| Verdict | Means | Do |
|---|---|---|
| `nav_raw_confirmed` | NAV fell by about the distribution | Declare the fund; the design works as written. |
| `nav_already_adjusted` | NAV did not fall at all | **Stop.** The feed has reinvested distributions; loading them as corporate actions would adjust every one twice. |
| `ratio_mismatch` | NAV moved, but by neither the distribution nor nothing | Investigate — the distribution record or the NAV series is not what it claims. |
| `no_price_history` | The endpoint serves no bars for this symbol | Widen `--window`; if still empty, this feed cannot price the fund. |
| `inconclusive` | No distribution, or none material enough to separate the two | Probe a fund with a December capital gain. |

The command exits non-zero on anything but a pass or `inconclusive`, so it can gate a
script. It costs 3 requests and writes nothing.

**Retiring a fund.** The delisted feed's rows for funds carry a venue the sweep
does not read, so it cannot reach them. (The recorded `MUTF` venue is not what keeps
them out: the sweep filters on the feed's venue, not the security's.) `fafnir track rm VFIAX` stops
the pulls; `fafnir track rm VFIAX --closed 2027-03-31` also retires the security the
ordinary way — `delisted_date` stamped, ticker period closed, every bar retained. Use
`--closed` when the fund actually closed or merged: an untracked-but-active security
is flagged stale by `dq run` every night.

## Why the *unadjusted* price endpoint

`core.daily_price` is defined as raw, and `core.adjustment_factor` is the only
adjustment fafnir applies. Most FMP price payloads are adjusted before they arrive,
so the endpoint choice is load-bearing:

| Endpoint / field | Adjusted for |
|---|---|
| `historical-price-eod/full` → `close` | splits |
| `historical-price-eod/full` → `adjClose` | splits **and** dividends |
| `historical-price-eod/dividend-adjusted` | splits and dividends |
| **`historical-price-eod/non-split-adjusted`** | **nothing — prices as traded** |

Loading a pre-adjusted series adjusts it twice. AAPL has split 112:1 cumulatively
since 1990, so its true 1990-01-02 close of ~$39.20 arrives from `.../full` as
~$0.35; storing that as raw and applying the 1/112 factor again yields ~$0.003. The
symptom only appears on symbols that have split, and only in deep history.

Two details follow from the endpoint choice:

- The unadjusted payload names its OHLC fields `adjOpen`/`adjHigh`/`adjLow`/
  `adjClose`. That prefix is FMP's naming convention on this family of endpoints,
  **not** a second adjustment. The loader accepts either spelling (preferring the
  unprefixed one) and lands the payload verbatim.
- Dividends are taken from the as-declared `dividend` field, not the restated
  `adjDividend`, so the dividend and the raw prior close it divides into are quoted
  in the same share terms.
- Volume is taken from `unadjustedVolume` where a payload offers it, else `volume`.
  Volume back-adjusts the *opposite* way to price — a split multiplies pre-split
  share counts — so an already-adjusted volume would be inflated by the split ratio
  squared rather than collapsed, with no vanish-to-zero tell and no DQ check to catch
  it. `fafnir source probe-prices` reports a separate volume verdict; see
  [backfill.md](backfill.md#volume-is-checked-separately) for the case the two feeds
  cannot decide on their own.

Full rationale and the migration consequences: [adr/0004](adr/0004-unadjusted-price-feed.md).

## Sharadar's fields, as measured

No Sharadar loader exists yet (#58, #75). These are the facts it will rest on, as
`fafnir source probe-sharadar` measured them on the paid key on 2026-10-19. The
verdict table, and how to re-run the probe, are in
[backfill.md](backfill.md#confirming-the-sharadar-feed).

**Prices (SEP; SFP the same).**
- **Raw close:** `closeunadj`, the close as traded.
- **Split-adjusted:** `open`, `high`, `low`, `close` and `volume`.
- **Split- and dividend-adjusted:** `closeadj`.

On the newest session the raw and split-adjusted closes are equal, as they must be.
`core.daily_price` stays raw (ADR 0004), so the loader recovers raw values with the
vendor's own identities:

```
raw open/high/low = field  × closeunadj / close
raw volume        = volume × close / closeunadj      (rounded to a whole share)
```

Two limits follow from the data:

- **Every price field carries at most 3 decimal places.** The raw close is exact to
  that. A recovered open/high/low carries up to 0.0005 × the cumulative split factor
  of error: about 5.6¢ on a 1998 AAPL bar (×112). Recovery is exact only for the
  close. #75 watches the cases where it matters.
- **Volume is split-adjusted, so not always whole:** 5.5% of bars in the sample. A
  recovered raw volume was whole on 71% of split-affected bars, so the loader rounds.

**Splits (ACTIONS, `action = split`).** `value` is new shares per old: a 4-for-1 is
4, and a 1-for-4 reverse split is 0.25. That is the direction
`core.corporate_action` reads (a 2-for-1 is 2 / 1), so `value` maps to
`split_numerator / split_denominator` unchanged, a reverse split as its rational (1 /
4). Across the sample, `closeunadj /
close` matched the cumulative split product after each date on all 127,726
split-affected bars, within the 3-decimal rounding.

**Dividends (ACTIONS, `action = dividend`).** `value` is **split-adjusted**, not as
declared: AAPL's $0.77 of 2019-11-07 is 0.1925 (÷ 4). `core.corporate_action`
holds the amount as declared, against the raw prior close (ADR 0001), so the loader
multiplies by the product of the splits after the ex-date. A split missing from
ACTIONS would mis-state every earlier dividend as well as the recovered prices.

**Identity.**
- **History is keyed by the current ticker.** META's bars start in 2012, under
  META; `FB` returns nothing.
- **A dead issuer's reused ticker carries a numeric suffix** (`CLA1`): 20% of
  delisted tickers, a third of them on a root a live security trades under today.
- **`permaticker` is the stable key.** It is the same across a security's TICKERS
  rows (stocks, fundamentals, insiders), but SEP and SF1 rows carry only the ticker.
  So a loader resolves rows through TICKERS and keys on `permaticker` (#55), never on
  the ticker of the day.

**Updates.**
- **`lastupdated` moves on the whole history** whenever a split or dividend
  restates `closeadj`. A delta keyed on it re-reads entire histories after every
  dividend. The raw fields it reads do not change, so the loader compares before it
  writes.
- **Filters:** `lastupdated`, `lastupdated.gte`, `date` and `ticker` lists are all
  honoured. **An unknown parameter is ignored, not refused:** a misspelt filter
  returns everything it should have narrowed, so loaders assert their filters in the
  rows they get back.

## What FMP restates, and the guard

The nightly price load re-reads the last `overlap_days` (5) sessions of every
symbol, to pick up late corrections. FMP's first print of a bar is usually right;
what it serves on a later read of the same date often is not. Between 2026-09-16 and
10-14, the re-read replaced first prints with:

- **another instrument's prices**: one fund's history became another's level from
  one night to the next (a ~11 first print re-served at ~58; a ~26 one at 0.07), every
  night, in both directions;
- **a split's scale, before the split**: four sessions re-served at x6 the day after
  a 1:6 reverse split went ex, and five at x50 the week before a 1:50 -- prices only,
  the volumes within 8% of the first prints;
- **a phantom split's scale**: two sessions at a 2:1 scale FMP took from a warrant
  dividend it had recorded as a split.

An operator re-fetch (`ingest prices --symbols S --from D --to D`) repaired those --
and one of them returned the prices right and the volume x50, at the same time.

So a **nightly** load (no `--from`) never overwrites a stored bar by more than
`[prices] restatement_max_ratio` (close, default 1.25) or
`restatement_max_volume_ratio` (volume, default 3), either direction. It keeps the
stored bar and writes `price_restatement_conflict` -- once per open `(symbol,
date)`, with the stored OHLCV and the run that stored it, the incoming OHLCV, both
ratios and any split on file within 30 days. The date still counts as loaded, so the
watermark moves past it. A zero volume on either side is no ratio and restates
nothing.

An **explicit** re-fetch (`--from`) overwrites FMP's own bars, as before, and:
- prints every bar it restated beyond the thresholds, close and volume, old -> new
  (the table that would have shown the x50 volume on the spot);
- records them in its run's `params.restated` (the first 500) and
  `params.restated_count`;
- resolves the open `price_restatement_conflict` on every date it wrote, with
  `operator re-fetch, run N` and the old and new values.

The guard's cost is deliberate. Sometimes the first print is the wrong one (the
phantom-scale bars above; a 0.07 first print later served as 25.52): the guard keeps
it and raises a conflict rather than accept the vendor's correction. A blocked
correction is visible and costs one re-fetch; a silently stored wrong-instrument
restatement cost weeks. There is no "consistent with the neighbours" tie-break: on a
series whose stored history is already the wrong instrument it would accept the
wrong re-read and refuse the right first print. The playbook probes the vendor and
re-fetches the side that is right.

**A new bar at a split's scale.** A bar with no stored copy has nothing to protect,
so the guard above cannot apply. When it is dated within 30 days before a split on
file, its close is the last stored close times the split's price factor (x50 for a
1:50, x0.5 for a 2:1) within 10%, and its volume is within 25% of the 20-session
median times the inverse factor or times one (FMP has served both shapes), it is
quarantined as `price_split_scale_mismatch`: not stored, and holding the watermark
like any quarantine. Splits nearer 1:1 than 3:2 are not judged: a 5:4 split's
factor is inside an ordinary day's range.

**One split on file twice.** `fafnir adjust` flags two splits of one ratio within ten
sessions as `split_duplicate_suspect`, naming the one the raw closes move across
(GLE's 1:10 sat on 10-07 and 10-08, and adjusted prices were x100 before the earlier
date; NXGL's 1:20 on 10-09 and 10-12). Delete the other with `actions delete`, after
`source probe-prices` on the bar before each ex-date shows which one the
split-adjusted feed applies.

**Symbols FMP stopped serving.** `fafnir source vendor-gaps` lists live securities
whose newest payload was an open-ended request that returned nothing after their
last bar, while 95% of their venue moved on and the delisted feed does not name
them. On 2026-10-14 that was ten securities that traded every day (last bars 09-29
to 10-08). The list is for an FMP ticket; their `stale` flags stay open. The
delisting sweep lands its payload (from v1.5.0) so the report can check it.

**A declared ticker over a delisted row.** `ingest tracked` mints nothing for a
declared ticker whose vendor profile names a delisted security (same CUSIP, or the
same normalised name): it flags `tracked_symbol_delisted` instead. BURU was declared
while its only row was delisted, and every night a new row was minted and stamped
delisted by the sweep. `track add` refuses such a ticker unless `--allow-delisted`.

## Corporate actions

`ingest actions` has three modes, set by `[general] actions_mode` and overridable per
run with `--mode`.

| Mode | Requests per night | What it does |
|---|---|---|
| `symbol` | ~2 × active universe | Full split + dividend history for every security. |
| `calendar` | ~4 + first-loads | One market-wide sweep since the watermark. |
| `auto` | ~4 + first-loads + funds | `calendar`, plus per-symbol for what it cannot cover. |

**Why a watermark alone was not the fix.** The price watermark works because
`historical-price-eod` takes `from`/`to`: the request count is one per symbol either
way, and the watermark shrinks the payload. Corporate actions invert that — the payload
is tiny and the request count is the cost, so narrowing 42,800 small payloads still
costs 42,800 requests. The natural grain of "what happened since I last looked?" is the
date, not the security, which is what the calendar endpoints are. See
[adr/0007](adr/0007-incremental-corporate-actions.md).

Four things follow, and each is handled where it arises:

- **A future ex-date is never stored.** Both the calendar and the per-symbol
  `dividends` endpoint return dividends that have been *declared* and have not gone ex.
  Storing one would give the security an `adjustment_factor` whose `effective_date` is
  in the future, and the adjusted view applies to a price at date *t* the factor at the
  smallest `effective_date` greater than *t* — so today's close would be back-adjusted
  for a dividend that has not happened. The window stops at today and the transform
  drops anything past it.
- **No watermark → full history, once.** A security minted last night has nothing for
  the sweep's window to add to, so it gets the per-symbol pull on the run that mints it
  — the same rule the price loader uses. The watermark is stamped with the run date,
  not the last ex-date, or a security that has never paid anything would never get one.
- **Funds stay per-symbol.** A declared fund has no listing venue (ADR 0006), so an
  exchange calendar is not assumed to carry its distributions. A few dozen requests a
  night is cheap next to a coverage gap.
- **Delisted securities are skipped**, as they are for prices: a security that stopped
  trading can never have another corporate action. `--include-inactive` for backfills.

**The calendars have two limits and only one is documented.** The documented one is
the 3-month span between `from` and `to`. The one that actually bites is a **4000-row
ceiling per response, which drops the oldest rows to fit** and says nothing about it —
a 30-day request measured on 2026-08-30 came back holding 8 days. `limit` does not
lift it; `page` walks backwards through the rows, so `_actions_calendar` pages each
slice until a short page. Density swings ~2.5× over the year (ex-dates cluster at
quarter-ends), which is why this is paged rather than chunked to some day count: a
count tuned for August loses data in June, silently. This is the same failure mode
`historical-price-eod` has — see ADR 0007's postscript, and the `eod_raw` warning that
`_actions_calendar` originally lacked.

**`fafnir source probe-actions` before switching to `auto`.** The sweep is only sound
if the calendar carries the same events the per-symbol feeds do, and if it does not the
failure is silent — a missing dividend is not an error, it is an adjusted series that is
quietly wrong. The probe pulls a sample both ways and diffs them:

| Verdict | Means | Do |
|---|---|---|
| `calendar_complete` | every event matched | Set `actions_mode = "auto"`. |
| `calendar_incomplete` | the calendar omits events | **Stop.** Keep `symbol` for that asset type. |
| `calendar_truncated` | every miss predates the earliest row returned | A client paging fault, not a vendor gap. Fix and re-probe; conclude nothing about coverage. |
| `field_mismatch` | same ex-dates, different values | Fix the transform, re-probe. |
| `no_events` | nothing went ex in the window | Widen `--days`, or probe a payer. |

Costs `2 + 2N` requests and writes nothing; exits non-zero on anything but a pass.

**The reconciliation is the safety net.** Each night `actions_reconcile_buckets`
(default 30) means 1/30th of the universe is re-pulled the old way and diffed against
what is stored — every security checked monthly, at ~1.3% of a full refresh. A
difference is repaired *and* raised as `corporate_action_drift`, so a vendor coverage
gap surfaces on a schedule rather than when someone eventually notices a wrong price.
Only ex-dates older than `actions_overlap_days` are judged: the two feeds do not update
in lockstep, and flagging inside that window would file a row for every security that
just went ex, every night.

The drift detail separates four kinds. `missing_from_calendar` is an event the sweep
never saw; `amended` is one whose values moved; `withdrawn_by_source` is a stored
event the feed no longer carries — **reported and kept**, because the feed drops real
events too, and a loader that deleted on every disagreement would be trusting the
vendor on its worst day. TLT's July distribution vanished from its per-symbol payload
while the warehouse held it correctly.

`redated` is the one exception, and the only place the reconciliation deletes.
When the calendar sweep and the per-symbol feed date one distribution differently,
the upsert stores the feed's date and keeps the sweep's, so the dividend sits in
the factor chain twice and no actions mode would ever remove it: PRGMX carried
August 2026 on both 08-31 (0.023823) and 08-28 (0.0238). A settled stored dividend
the feed no longer carries is treated as a re-dated copy only when the feed carries
**exactly one** dividend within `REDATE_WINDOW_DAYS` (5) whose amount agrees within
`REDATE_AMOUNT_TOLERANCE` (1%). Exactly one, not the nearest of several: a fund
accruing daily has a matching neighbour on either side of any dividend the feed
happens to drop, and picking the nearest would delete a real distribution. Two
candidates decline, and the row stays withdrawn. A match is deleted through
`repository.delete_corporate_action` and the security's factors are recomputed
inline — a delete leaves no row stamped with the run, so `adjust --changed` cannot
see it.

## The adjustment step

`fafnir adjust` recomputes `core.adjustment_factor` from `core.corporate_action`:

- split num:den → price × (den/num), volume × (num/den) for prior dates;
- cash dividend D vs prior close P → price × ((P−D)/P) for prior dates.

`fafnir adjust --changed` recomputes only the securities the last corporate-actions
run actually changed — a few hundred on a normal night, against every security in the
warehouse that has ever had an action. That is what `daily_update.sh` runs; bare
`fafnir adjust` stays the backfill path and the way to rebuild after a change to the
factor logic itself.

Cumulative factors are built from latest ex-date backwards. The adjusted view picks,
for each `trade_date`, the factor at the smallest `effective_date` greater than that
date — i.e. the product of every action that happened after it. Prices on/after the
latest ex-date get factor 1.0. This is why adjusted prices are **point-in-time
stable**: they are a deterministic function of the actions known as of a date, not a
frozen snapshot. See [adr/0001](adr/0001-raw-prices-plus-adjustment-factors.md).

## Keeping the universe in scope

The security master is **upkeep, not just build**. `scripts/daily_update.sh` runs
the three universe steps before any market data is pulled, and their order is the
whole design:

```
symbol-changes  →  securities  →  delisted  →  prices ...
   (renames)       (new listings)  (exits)
```

**New listings.** `ingest securities` re-reads the screener nightly, so an IPO, a
spin-off or a new ETF enters scope on its listing day. The upsert mints a
`security_id`; because that security has no `ops.load_watermark` row, the price step
in the same run starts its window at `backfill_start` and pulls the symbol's whole
history (§*Watermarks*). Nothing has to be scheduled per security. The loader
reports which tickers were new — `Loaded 21412 securities (3 new)` — so the nightly
log distinguishes a refresh from an arrival.

`backfill_start` is not decoration. Asking FMP with no start date is not "the whole
history": the vendor applies its own default window of about five years. Every
security that entered the universe after the initial backfill therefore got a
truncated first load — GBF, DDI and MRT hold bars only from 2021 — and each
dividend older than that raised a `dividend_no_prior_close` flag with no price
behind it. `fafnir ingest prices` passes `date(calendar_start_year, 1, 1)`, so the
first load asks for the window the operator configured. An explicit `--from` still
wins, and a security with a watermark still resumes from it less the overlap. This
does not repair a history already truncated; that needs an explicit re-backfill.

**Exchange test issues.** Venues keep synthetic securities trading in production so
member firms can exercise order routing, and the screener serves them like any
listing — ZXZZT ("SuperMontage TEST") sat here as an active security with 4,400
synthetic bars and 2,286 outlier flags. `is_exchange_test_issue` skips an entry
whose symbol matches Nasdaq's `^Z[A-Z]ZZT$` or whose name carries the word TEST
*together with* a venue's name (both words, so "Test Systems Inc." is a company).
Skipped entries are neither written nor flagged, and are counted in
`SecurityLoadResult.skipped_test_issues`. A row already minted for one stays until
an operator removes it.

A venue transfer (NYSE → NASDAQ) is *not* a new listing and does not fork the
security: the exchange is an attribute of the listing, not part of identity, so the
transfer updates the company that already holds the history (0012). Because that
keys a listed security on `(source, symbol)` alone, each security-master update is
checked for **company-name drift** — a name changing into something unrelated while
the ticker stays put is what a violated identity assumption would look like. It
raises an advisory `security_company_name_drift` flag and stores the row anyway;
see [operations.md](operations.md#monitoring).

**Renames.** A rename reaches the screener as nothing more than a new ticker, and
that is the trap: no active row matches `(fmp, 'META', 'NASDAQ')`, so the upsert
mints a *second* `security_id` and the company's bars, corporate actions and price
watermark stay stranded on the FB row — which no delisting sweep will ever close,
because a rename is not a delisting, and which is re-polled every night for bars
that will never come. `ingest symbol-changes` therefore runs **first**, and applies
the rename to the security that already exists:

- the old ticker's `core.symbol_xref` period is closed the day before the change;
- a new period opens for the new ticker against the **same** `security_id`;
- `core.security.primary_symbol` moves across.

One company stays one entity: joins, watermarks and backtests are unaffected, and
`duk ph FB` still resolves — the old ticker falls through to its closed xref period
once no live security claims it (see `resolve_security_id`).

Every observed rename fafnir tracks is recorded in `core.symbol_change`, which is
what makes the nightly sweep idempotent (the same tail is re-read every night) and
what turns a rename that *cannot* be applied into durable evidence:

| status | meaning |
|---|---|
| `applied` | carried onto an existing `security_id` (terminal) |
| `conflict` | the new ticker already belongs to another **listed** security that carries history — a human decides; retried every sweep |
| `ignored` | the old ticker belongs to a delisted issuer, so this is ticker *reuse*, not a rename (0009 already handles it) |

A `conflict` is retried on every sweep because the obstruction can clear itself: the
duplicate that blocked it is merged away, or the old ticker's holder is marked
delisted. When a retry succeeds, the `symbol_change_conflict` flag describing the
conflict stops being true, so the applied branch **closes it in the same unit of
work as the rename**, resolved as `fafnir`, matched on the `(old, new)` record_key
the flag was written with. Nothing else could: the sweep skips a terminal row for
good afterwards, `security merge-rename` refuses once the old ticker is retired,
`dismiss-rename` would record something false (the rename is real), and `dq recheck`
never touches a `NEVER_AUTO_RESOLVE` check. MAPP→MATR sat open for nine days after
its retry applied. The terminal-skip path closes leftovers from before this
existed — that skip is the only place the sweep sees such a row again — and
dismissed and ignored rows are left exactly as they are. The count is reported as
`conflicts_closed`.

A rename for a ticker fafnir does not track is counted and dropped, not recorded:
the feed is global across every venue, and the audit table is not a copy of it.

The vendor keeps listing the *old* ticker on the screener for weeks after a rename,
and nothing in `core.security` holds it any more: the security moved, and nothing
was delisted. So an applied rename also retires its old ticker for the
security-master load. `repo.renamed_away_securities` offers every name the echo is
known to carry: the security's name before the rename (`detail.old_company_name`,
recorded by the sweep and by `merge-rename`), any name a `security merge` recorded
from a re-minted echo (`detail.echo_names`), the security's current name and the
feed's. `is_retired_listing` weighs the screener entry against them with the same
strict normalised-name test it uses for delisted rows. A matching entry is skipped
and logged as `<OLD> (renamed <NEW>)`. A different issuer taking the freed ticker
still lists.

If the security master ran before the rename was known and minted the new ticker as
its own row, the sweep folds that duplicate back in — but only when it is still
empty (no bars, no actions, no factors). That fold is the one place a loader deletes
a security; retention exists so history is never lost, and a stub has none. (An
operator can too: `security merge` folds a duplicate by hand, and `security
descope` removes an instrument kind taken out of scope.) A
duplicate that *has* accumulated history is a `conflict` instead: merging two price
histories is not a decision a loader should make silently.

### Which vendor feeds a security (ADR 0015)

Every FMP loader feeds the securities FMP minted (`core.security.source = 'fmp'`)
and nothing else. The predicate is `repo.FMP_FED_SECURITY`, and the per-symbol guard
is `repo.is_fed_by(db, security_id, "fmp")`. Both apply to the price universe
(`ingest prices`, with or without `--include-inactive`), the corporate-actions
universes (first load, calendar sweep, reconciliation slice, declared funds), the
duplicate-identity check and `security dedupe`, the `source vendor-gaps` report and
`security venue-transfer`.

The reason is how FMP keys history. It serves a ticker's whole history whoever used
it, so a pull into any security FMP did not mint writes another issuer's bars into
it:

- **An operator-minted security** (`security split-history`) holds a history split
  off a ticker that FMP has since given to someone else.
- **A security another vendor minted** shares its ticker with whoever trades under it
  today. A Sharadar dead issuer is the case the Sharadar plan brings.

A per-symbol pull that resolves to such a security logs `which FMP does not feed`
and loads nothing. In `load_prices` stats it counts as `not_fmp_fed`.

Which vendor feeds a security is an explicit assignment, not a guess from `source`.
Until `core.security_vendor_xref` exists (#55), `source = 'fmp'` is the whole FMP
rule, and asking `is_fed_by` about any other vendor raises. Once it exists, a
Sharadar-fed security is the one with an open `is_feed` link.

### Who may write a row: precedence (ADR 0015)

Every row of `core.daily_price` and `core.corporate_action` carries the party that
supplied it in `source`. The parties rank `operator > sharadar > tiingo > fmp`
(`repo.PRECEDENCE`), and both upserts (`upsert_daily_prices`,
`upsert_corporate_action`) follow one rule:

- **A vendor revises its own rows.** FMP re-reading an FMP bar behaves as it always
  did, restatement guard included.
- **It never writes over a row another party holds.** The key is left exactly as it
  is, `source`, `ingestion_run_id` and `loaded_at` included, and counted as
  `skipped_foreign`: on the price upsert's result, in `load_prices` stats, and in
  the run's `params.skipped_foreign`. A date another vendor holds is not FMP's to
  restate either, so the restatement guard neither flags nor resolves anything on
  it. Whether FMP's read disagrees with that row is the reconcile's question.
- **It replaces a lower-ranked vendor's row only when the caller asks:**
  `correct=True` with a `reason` (`reconcile_divergence`; `gap_fill` and
  `restatement` are reserved). Each replaced row is first written to
  `ops.vendor_correction` as it stood, in the same transaction, and the key moves
  to the correcting vendor. An action whose values already agree is not a
  correction. A correction never writes a key an operator edited
  (`ops.operator_override`); that key is counted as `skipped_suppressed`.
- **No vendor writes an operator row**, with or without `correct`, and an operator
  cannot be passed as a correcting writer. Operator edits go through the operator
  commands.

So nothing a vendor already supplied changes until a correcting load runs, and a
correction can be reviewed and undone:

```bash
fafnir vendor corrections list [--symbol S | --security-id N] [--all] [--json]
fafnir vendor corrections revert ID... -m "why" [--dry-run] [--yes]
```

`revert` writes each replaced row back exactly (values, `source`, run and load
time) and recomputes the security's adjustment factors. It refuses a row that
changed after the correction, for example a later revision by the correcting
vendor, and refuses one already reverted. Several ids are reverted together or
not at all. Removing a vendor (ADR 0015 §5) is the same restore applied to every
correction it made.

### Instruments out of scope: warrants, rights and units (ADR 0012)

`ingest securities` does not mint warrants, rights or units. It classifies each
screener entry by ticker with `fafnir.instruments.instrument_kind`:

- Nasdaq's reserved fifth letters W, R and U decide on their own.
- So do the NYSE `-WT` / `-WS` / `-RT` / `-UN` suffixes.
- A three-letter base plus W/R/U, or a fifth-letter Z, counts only when the
  vendor's name names the same kind.

A matching entry is skipped before anything is written, and the run reports it:
`Skipped 12 out-of-scope instruments (warrant/right/unit)`. A few a night is normal
SPAC issuance.

- `[general] exclude_instruments` chooses the kinds. All three are excluded by
  default; `[]` admits everything.
- To keep a single ticker that still lists, declare it with `fafnir track add`.
  Never declare a delisted one: `ingest tracked` would mint it again as a new,
  active security. A delisted security of an excluded kind is kept by naming it
  with `security descope --keep`, as Granite REIT's `GRP-UN` is.
- Rows minted before the filter existed are not refreshed. Remove them with
  `fafnir security descope` (see [operations.md](operations.md#descoping-an-instrument-kind)).

**A rename never changes the instrument.** The vendor's rename feed shuffles one
SPAC's tickers among themselves before launch (`ABCD -> ABCDU`, and back). With the
unit never minted, nothing holds the target ticker, and the sweep would move the
class A share onto it. From there the screener load would skip it as out of scope,
the price step would feed it the unit's bars, and the next descope would delete it.
So `apply_symbol_change` refuses, before the fold and before the free-ticker path,
any rename `fafnir.instruments.rename_changes_instrument` objects to. That covers
a rename between kinds (`ABCD -> ABCDU`, `AAC -> AAC-WT`, `ABCDU -> ABCDW`), and one
ticker being the other plus a designator letter (`ZKP -> ZKPU`). The row is
recorded as a `conflict` with `detail.reason`, flagged once, and retried and
refused nightly until `fafnir security dismiss-rename` closes it.
`security merge-rename` refuses the same renames unless forced. A de-SPAC rename
keeps the kind (`ABCDW -> NEWCW`) and applies as before; funds and ETFs are never
refused.

## Order of operations (daily)

```
ensure-partitions → symbol-changes → securities → delisted →
prices → actions → adjust --changed → refresh-marts
```

then, as its own job after the load, `dq run` (`fafnir-dq`).

The universe is reconciled before any data is pulled, so prices run against what is
actually trading today. `ingest delisted` keeps its place and its behaviour, with
one correction the rename step forces: it resolves a feed row through
`active_security_for_symbol`, not `resolve_security_id`. The read path deliberately
falls back to a ticker a company used to trade under, and a delisted feed reports
retired tickers -- so resolving that way would let a row for the retired `FB` stamp
a one-way delisting on the live `META` security.

Resolving to the security *currently* trading under a ticker is necessary and not
sufficient, because a ticker outlives its issuer: FMP's list still carries the 2018
delisting of an earlier CMDT, and applied by ticker it landed on the PIMCO fund
trading as CMDT today. That fund left the active universe, the next security-master
load minted it a second row, and the ticker forked — four of the six
`security_duplicate_identity` flags left open after the 2026-09-10 cleanup, none of
which any command could fold. `mark_delisted` is one-way, so before it runs the
loader reads the security's own bar span and **declines** a delisting that precedes
the first bar (it describes an earlier holder) or that the security kept printing
bars more than `TRADED_AFTER_GRACE_DAYS` (30) past. The grace covers the normal lag
between a delisting and the feed reporting it. A security with no bars contradicts
nothing and is marked as before.

That bar test cannot catch a **venue transfer**: once a security is marked, the
price step stops loading it, so the bars after the delisting never arrive. ET and
five others moved from the NYSE to the Texas Stock Exchange (TXSE) on 2026-10-05,
and the sweep stamped all six from FMP's delisted feed. FMP's profile was no help:
it went on calling ET inactive on the NYSE. Its price feed was, so the sweep now
asks it ([ADR 0014](adr/0014-venue-transfer-is-not-a-delisting.md)):

- **A recent delisting** is checked first: one within 60 days, with the last
  stored bar within 10 sessions of the date. The sweep fetches the security's bars
  from about 10 sessions before the date, one request. A bar strictly after the
  date declines the record and flags it `security_delisting_contradicted` for an
  operator to decide: `security venue-transfer` if it moved, `security delist` if
  it left scope (a move to OTC keeps printing bars too). A bar *on* the date is
  not enough, because FMP's `delistedDate` is often the last trading day.
- **A record that flag already names** is declined without asking. That covers an
  accepted flag (an operator recorded the move) and an open one (still undecided),
  so an undecided record is never stamped by default.

The run's `params` record `declined` per reason and `prices_requested`.
`rows_quarantined`, which marks the run `partial`, counts every decline that wants
a look, but not `known_transfer`, the expected echo of a move already recorded.

The same move froze these securities a second way: the nightly `ingest securities`
copies the vendor's `isActivelyTrading` onto every listed row, and FMP called them
inactive. So the nightly price and corporate-action universes are "listed, and
either actively trading or with a bar in the last 30 days", not
`is_actively_trading` alone. A venue recorded by `security venue-transfer` is an
override (migration 0027) that the upsert keeps, with the row's activity, until FMP
names a venue other than the one the security left.

Prices precede actions so dividend adjustment can value against fresh closes.
`scripts/daily_update.sh` encodes this order.

Within `ingest actions` the order matters for the same kind of reason: first-loads,
then the calendar sweep, then the reconciliation. First-loads go first because a
security with no history has nothing for the sweep's window to add to. The
reconciliation goes last so that anything the sweep was going to pick up tonight
already has been — otherwise every event it found would look like drift.

## Watermarks and the endpoint string

`ops.load_watermark` is keyed on `(source, endpoint, security_id)`, so the endpoint
path is part of ingestion state, not just a URL. Changing it retires every existing
watermark and makes each symbol look new — which on an incremental run means an
unbounded request, and an unbounded request is capped at 5000 bars (~19.8 years).

`load_prices` therefore refuses to run incrementally when watermarks exist only
under the retired `historical-price-eod/full` endpoint, directing the operator to a
re-backfill instead. If you ever change a loader's endpoint again, plan the
watermark migration at the same time.

The `security_id` half of that key carries meaning too. It is `0` by default, and
migration 0004 comments that as "whole-endpoint (non per-symbol)" — a watermark for a
feed that is not fetched one security at a time. Corporate actions use both halves:

| Key | Means |
|---|---|
| `('fmp', 'corporate-actions', <id>)` | this security's full history has been pulled |
| `('fmp', 'corporate-actions-calendar', 0)` | the market-wide calendar has been read through this date |

The per-security row is a *presence* flag as much as a date — its absence is what puts
a newly minted security on the full-history path — so it is stamped with the run date
rather than the security's last ex-date.
