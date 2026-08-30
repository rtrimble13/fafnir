# ADR 0007: Corporate actions load incrementally (market-wide calendar sweep)

- Status: Accepted (implemented; **adoption gated** — see *Implementation status*)
- Date: 2026-08-29
- Implemented: 2026-08-30
- Depends on: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md),
  [ADR 0004](0004-unadjusted-price-feed.md),
  [ADR 0006](0006-curated-fund-universe.md)

## Context

Every other market-data load in fafnir is incremental. Corporate actions are not.
`fafnir ingest actions` re-downloads the complete split and dividend history of
every security, every night, and nothing on that path reads or writes
`ops.load_watermark`.

### What that costs

Two things compound.

**The symbol list is unfiltered.** `ingest_actions` (`src/fafnir/cli.py:412`) selects
every row in `core.security`. The price loader deliberately does not
(`src/fafnir/cli.py:322` restricts to `is_actively_trading` unless `--include-inactive`
is passed) for a reason that applies here with more force: a delisted security will
never have another bar *and can never have another corporate action*, yet it is polled
forever. A recent nightly log line reads `Loaded 21412 securities`, so the list is
~21.4k names and grows with every delisting.

**Each symbol costs two unbounded requests.** `load_actions` calls `fmp.splits(symbol)`
and `fmp.dividends(symbol)` (`src/fafnir/ingest/corporate_actions.py`), neither of
which takes a window. Each returns the symbol's entire history.

| | today |
|---|---|
| Requests per night | ~21.4k × 2 ≈ **42,800** |
| Wall clock at `request_rate_per_min = 280` | **~2.5 hours** |
| Restricted to the ~8k active names | ~16,000 requests, ~1 hour |
| `landing.fmp_raw` rows written per night | ~42,800 |
| Rows that actually changed | a few hundred, market-wide |

Across the whole US market a typical session produces a few hundred new dividend
records and a handful of splits. Everything else fetched, validated and upserted is
byte-identical to what is already stored.

Bandwidth is not the binding constraint — a full actions pass is roughly 50–100 MB
against the 50 GB/month budget. The binding constraints are the **nightly wall clock**
and the **write amplification**, and the second one has a tail: `fafnir adjust` then
recomputes `core.adjustment_factor` for *every* security that has any action at all
(`adjustments.adjust_all`), issuing a `close_before` query per dividend event. Two full
refreshes chained end to end, to capture a few hundred rows.

### Why a watermark alone does not fix it

The obvious move — do for actions what `daily_price` does for prices — does not work,
and seeing why is what picks the design.

The price watermark pays off because `historical-price-eod` takes `from`/`to`. The
request count there is one per symbol whether or not a watermark exists; the watermark
shrinks the *payload* from twenty years to five days. Corporate actions have the
opposite cost profile: the payload is already tiny and the **request count** is the
whole expense. Narrowing 42,800 small payloads still costs 42,800 requests and still
takes 2.5 hours.

So the lever is not "ask for less per symbol". It is **stop asking per symbol.**

## Options considered

| # | Option | Requests/night | Freshness | Build | Risk |
|---|---|---|---|---|---|
| 0 | Status quo | ~42,800 | same day | — | grows without bound |
| 1 | Active-only symbol list | ~16,000 | same day | trivial | none |
| 2 | Payload-hash short-circuit | ~16,000 | same day | small | none (no API saving) |
| 3 | Per-symbol windowed pull | ~16,000 | same day | small | no API saving |
| 4 | Rotating 1/N refresh | ~16,000/N | up to N days late | small | wrong adjusted prices for N days |
| 5 | **Market-wide calendar sweep + watermark** | **~4 + first-loads** | same day | medium | vendor coverage (verifiable) |
| 6 | Price-jump-triggered pull | small | same day | large | circular, unreliable |
| 7 | Bulk endpoints | 1–2 | same day | small | plan availability unknown |

**1 — Restrict the symbol list to actively-trading securities.** Cuts ~60% for a
one-line change and is correct on its own terms: a delisted security's action history
is frozen. It should ship regardless of which of the others is chosen, with
`--include-inactive` mirroring `ingest prices` so a backfill can still reach everything.
It does not solve the problem — 16,000 requests is still an hour a night — but it stops
the cost growing with each delisting.

**2 — Payload-hash short-circuit.** `landing.fmp_raw` already stores `payload_hash`.
Compare the new payload's hash against the last landed one for `(endpoint, symbol)` and,
when they match, skip the parse, the upserts and the security's adjustment recompute.
Saves nothing on the API — the payload has already been paid for by the time the hash
is known — but it collapses the *downstream* half of the cost, and it hands `fafnir
adjust` the changed-security set it currently lacks. Worth keeping as a companion to
whatever fixes the requests; not an answer by itself.

**3 — Per-symbol windowed pull.** `dividends` accepts `limit`; whether the stable
per-symbol endpoints honour `from`/`to` needs confirming. Either way this narrows bytes,
not requests, and bytes are not the constraint. Useful as the *fallback* if option 5
fails its probe.

**4 — Rotating refresh (1/N of the universe per night).** Divides the cost by N with no
new endpoints and no vendor risk, and a per-security watermark is exactly the state it
needs. Rejected as the primary mechanism because of what it does to correctness: an
ex-date that lands on a night when its security is not in the rotation leaves that
security's `adjustment_factor` stale for up to N days, so every adjusted price for that
name — including today's — is wrong by the dividend or the split ratio until its turn
comes round. Trading a known-correct series for a cheaper one is not a trade this
warehouse should make. The mechanism is still valuable, at a low rate, as a
*reconciliation* rather than as the load path (see the decision).

**5 — Market-wide calendar sweep.** FMP's stable API exposes `splits-calendar` and
`dividends-calendar`, which take `from`/`to` (documented maximum window: 3 months) and
return the events for **every symbol** in that window. One request covers what 21,400
per-symbol requests cover today, because the natural grain of the question "what
happened since I last looked?" is the date, not the symbol. This is the only option that
changes the order of magnitude.

**6 — Infer from prices.** Pull actions only for symbols whose close-to-close move is
unexplained. Rejected: it is circular (the raw price series is what the actions are
needed to interpret), it cannot see a dividend smaller than the day's volatility, and it
would put a heuristic between the vendor's record and `core.corporate_action`.

**7 — Bulk endpoints.** FMP publishes bulk CSV downloads on some plans. If splits and
dividends are among them on Professional, that is option 5 with fewer moving parts.
Worth checking during the probe below; not something to design around unconfirmed.

## Decision

Adopt **option 5** as the nightly path, with **1** shipped immediately and independently,
**2** as the companion that scopes the adjustment recompute, and **4** demoted to a slow
reconciliation that keeps the whole thing honest.

### 1. Two calendar endpoints on `FMPClient`

```python
EP_SPLITS_CALENDAR    = "splits-calendar"
EP_DIVIDENDS_CALENDAR = "dividends-calendar"
ACTIONS_CHUNK_DAYS    = 80        # documented cap is 3 months; leave headroom
```

Chunk a wide window into ≤80-day slices and stitch them, exactly as `eod_raw` chunks
around `EOD_MAX_ROWS`. A nightly run asks for one short window and costs one request per
endpoint; only a catch-up after an outage pays for extra slices.

### 2. One whole-endpoint watermark — no migration needed

`ops.load_watermark` already carries `security_id BIGINT NOT NULL DEFAULT 0` with the
column comment `0 = whole-endpoint (non per-symbol)`, and `repo.get_watermark` /
`repo.set_watermark` already default that argument to 0. The sweep's state is one row:

```
(source='fmp', endpoint='corporate-actions-calendar', security_id=0)
```

Migration 0004 anticipated this case; nothing new is required to use it.

### 3. The window: watermark − overlap, to **today**, never later

```
from = watermark - actions_overlap_days      # new [general] key, default 7
to   = today
```

A wider overlap than the price loader's 5 days, because a dividend amendment reaches
the feed later than a price correction does. A missed night widens the window
automatically; there is nothing to catch up by hand.

### 4. Never ingest a future ex-date

This is the one hazard the calendar feed introduces, and it is not obvious.

A dividend appears on `dividends-calendar` when it is **declared**, with an `ex-date`
weeks in the future. Store that row in `core.corporate_action` and `fafnir adjust` will
derive an `adjustment_factor` whose `effective_date` is in the future — and
`mart.v_daily_price_adjusted` selects, for a price at date *t*, the row with the
**smallest `effective_date` greater than *t***
(`sql/migrations/0007_marts.up.sql:32-39`). Today's close would therefore be
back-adjusted for a dividend that has not gone ex. The entire adjusted series would
move on the *announcement* date and move again on the ex-date, and the point-in-time
stability ADR 0001 rests on would be gone — silently, with every constraint satisfied.

Clamp in two places, because the vendor may return rows outside the requested window:
bound the request at `to = today`, **and** drop `ex_date > today` at the transform
boundary. An event enters the warehouse on the day it goes ex, which is the day the
price it adjusts arrives.

### 5. Rows for symbols fafnir does not hold

The calendar covers the whole market; most rows will not resolve to a `security_id`.
Count them and move on — no warning per row, no DQ flag. Filtering ~99% of a payload is
this loader's normal state, not an anomaly.

### 6. First load stays per symbol

A security minted last night — an IPO, or a fund just declared under ADR 0006 — has no
action history in the warehouse, and the sweep only ever looks forward from its
watermark. Keep `load_actions` exactly as it is for any security with no
`(source, 'corporate-actions', security_id)` watermark, and write that watermark when it
succeeds. This is the same shape the price loader already has: **no watermark → full
history, once.** It also makes `initial_backfill.sh` correct unchanged.

### 7. Funds stay on the per-symbol path

A declared fund has no listing venue (ADR 0006), so an exchange-oriented calendar feed
cannot be assumed to carry its distributions. The declared universe is a handful of
symbols; keep `asset_type IN NAV_ASSET_TYPES` on the per-symbol pull unconditionally
until a probe demonstrates the calendar covers them. A few dozen requests a night is not
a cost worth a coverage gap in the series the operator actually reads.

### 8. Landing and watermark discipline

Land **one** `landing.fmp_raw` row per calendar request (`endpoint='dividends-calendar'`,
`params={'from':…, 'to':…}`, `symbol=NULL`) rather than one per symbol, so landing growth
becomes proportional to events rather than to universe size.

Advance the sweep watermark to `to` once the window is fetched and transformed. Note the
deliberate difference from `daily_price`: there, a quarantined bar *holds* the watermark
so a later correction can still land. Here the watermark is global, so holding it on one
malformed row would stall corporate actions for the entire market on account of one
security. The row is flagged (`add_dq_flag_once`, unchanged) and the `actions_overlap_days`
re-sweep is what gives it another chance.

### 9. Companion: scope the adjustment recompute

The sweep knows exactly which `security_id`s received an action. Return that set from
`load_actions` and let `fafnir adjust` take it (`--changed-since <run_id>`, or a set
passed through `daily_update.sh`), instead of recomputing every security that has ever
had an action. Nightly, that is a few hundred securities instead of tens of thousands.
Full recompute stays available and stays the backfill path.

### 10. Safety net: a slow rotating reconciliation

Each night, take 1/30th of the active universe (~270 symbols, ~540 requests), pull it the
old way, and **diff** it against what is stored rather than blindly upserting. Differences
raise `corporate_action_drift` in the DQ queue. Every security is reconciled monthly at
~1.3% of today's cost.

This is what makes the calendar path trustworthy: coverage gaps are found by
construction, on a schedule, instead of by someone eventually noticing a wrong adjusted
price. It is option 4's mechanism used for what it is good at.

### Resulting cost

| | today | after |
|---|---|---|
| Requests/night | ~42,800 | ~4 sweep (2 pages x 2 endpoints) + ~540 reconciliation + first-loads + funds |
| Wall clock | ~2.5 h | **< 3 min** |
| `landing.fmp_raw` rows/night | ~42,800 | ~2 + reconciliation |
| Securities recomputed by `adjust` | all with actions | those that changed |

## Implementation status

All of it is built and tested. What is **not** done is the switch, because it cannot
be: adoption is gated on `fafnir source probe-actions` passing against a live API key,
which only the operator has.

| Piece | Where |
|---|---|
| `splits-calendar` / `dividends-calendar`, window-chunked | `FMPClient.splits_calendar` / `.dividends_calendar` |
| Sweep, first-load, funds, reconciliation, mode dispatch | `fafnir.ingest.corporate_actions` |
| Whole-endpoint + per-security watermarks | `ops.load_watermark` as-is — **no migration** |
| Future-ex-date guard | `corporate_actions._within_horizon` |
| Changed-set | `repo.upsert_corporate_action` returns whether it wrote |
| Scoped recompute | `fafnir adjust --changed` |
| Adoption gate | `fafnir source probe-actions` |
| Config | `[general] actions_mode`, `actions_overlap_days`, `actions_reconcile_buckets` |

`actions_mode` ships as `"symbol"` — the pre-ADR behaviour, minus the delisted names
(below). To adopt:

```bash
fafnir source probe-actions                 # gate; writes nothing, exits non-zero on fail
fafnir source probe-fund <YOUR FUND>        # only if you hold funds
# then in ~/.fafnirrc:  [general]  actions_mode = "auto"
```

`scripts/daily_update.sh` and `scripts/initial_backfill.sh` are already wired for both
modes; nothing about them changes when the flag flips.

### Three things the implementation settled that the proposal left open

**The future-ex-date guard was already needed.** The hazard was written up as something
the *calendar* introduces. It is not: the per-symbol `dividends` endpoint returns
declared-but-not-yet-ex rows too, so the original loader has been able to store a future
ex-date all along. Putting the clamp in the shared transform rather than in the sweep
fixes the existing path as well.

**The reconciliation repairs as well as reports.** The proposal said "diff rather than
blindly upsert", to keep a coverage gap from being papered over. Shipped: it does both.
The `corporate_action_drift` flag is the evidence and it names the security; leaving
known-wrong data in place to make a point would be the worse trade, and "quarantine the
anomaly, fix the data" is what the rest of the system already does.

**Only settled events are judged for drift.** The first end-to-end run flagged an event
as `withdrawn_by_source` that had merely not reached both feeds yet. The two do not
update in lockstep, so comparing a same-night ex-date reports drift for every security
that just went ex, every night — the queue-degrades-into-a-log failure that
`add_dq_flag_once` and migration 0016 exist to prevent. Drift is therefore only reported
for ex-dates older than `actions_overlap_days`; anything newer is still repaired, just
not called a discrepancy.

### How the changed-set is computed

`fafnir adjust --changed` needs "this run changed these securities" to be a true
statement, and an unconditional `ON CONFLICT DO UPDATE` rewrites every row it touches —
which would make the changed set the whole universe and the incremental recompute a full
one wearing a different flag. So the upsert carries a `WHERE ... IS DISTINCT FROM` that
suppresses a no-op write, and `ingestion_run_id` / `loaded_at` are now stamped only when
the row actually moves. `core.corporate_action.ingestion_run_id` therefore means "the run
that last changed this action" rather than "the run that last looked at it".

## Postscript: the probe earned its keep on the first run (2026-08-30)

`fafnir source probe-actions` failed on its first live run, reporting KO's 2026-06-15
and SPY's 2026-06-18 dividends as `calendar_incomplete` — events the per-symbol feed
had and the calendar did not. Read literally that is the verdict that stops this whole
decision. It was wrong, and how it was wrong is worth keeping.

**It was a truncated response, not a coverage gap.** Both missing events sat in the
oldest three weeks of the requested window; AAPL, in the *same* 80-day slice but with
an August ex-date, matched fine. So what separated found from missing was the **date**,
not the security — and a real coverage gap is a property of the security. KO is a NYSE
mega-cap common stock; AAPL and MSFT are too, and they matched. No coverage story
divides those three.

Measured directly against the feed:

```
from=2026-06-01 to=2026-08-19   4000 rows   earliest returned 2026-07-30
from=2026-06-01 to=2026-06-30   4000 rows   earliest returned 2026-06-23
from=2026-06-15 to=2026-06-15    904 rows   KO present
```

**A calendar response carries at most 4000 rows and drops the oldest to fit**, with
nothing in the payload saying so — an 80-day request came back holding 21 days, a
30-day request came back holding 8. `limit=20000` still returned 4000, so the cap is
not a default to raise. `page=1` returned a *different* 4000 rows covering an older
range, so the endpoint paginates backwards through time.

Three things follow.

**The fix is paging, not a smaller chunk.** Event density swings about 2.5× across the
year — ~190/day across late July and August, ~500/day at the June quarter-end where
ex-dates cluster. Any fixed day-count narrow enough for the dense weeks is wasteful
for the rest of the year, and any count chosen for the average silently loses data
four times a year. Paging is self-tuning; a day count is a guess that fails quietly
when it is wrong. `FMPClient._paged` already existed for the screener and now serves
the calendars too, keyed on `(symbol, date)` — a symbol-only fingerprint would call
page 1 a repeat of page 0 and stop after one page, reintroducing the same loss.

**The original ADR's request estimate was optimistic.** The nightly window is ~9 days
at ~500 events/day, so the sweep costs 2 pages per endpoint rather than 1. Four
requests instead of two, against ~16,000 — the conclusion is unchanged, the arithmetic
was wrong.

**The nightly path was affected, not just the probe.** This was first written up as a
probe-only problem on the grounds that a 9-day window was nowhere near the cap. At
4000 rows and ~500 events/day it is over it. The sweep would have truncated its own
overlap window every night, advanced the watermark to today regardless, and never
re-read the dropped days — losing real events until the monthly reconciliation
happened to catch them.

**What was actually missing was a check.** `eod_raw` warns when a response comes back
at its row limit, for precisely this reason, and `_actions_calendar` — written
directly below it — did not. That single omission is why a client-side truncation
presented as a vendor coverage gap. The probe now separates them: when every missing
event predates the earliest row the calendar returned at all, and the response was
large enough for its span to mean anything, the verdict is `calendar_truncated` and
says to fix the client rather than to draw conclusions about the feed.

The gate worked. An unverified switch would have shipped a warehouse that silently
dropped corporate actions, and the first symptom would have been a wrong adjusted
price noticed months later.

## Before adopting: probe the feed

This repository does not adopt an endpoint on the strength of its documentation — see
`fafnir source probe-prices` and `probe-fund`. Same gate here.

`fafnir source probe-actions --symbols AAPL,MSFT,SPY,VFIAX --days 90` pulls each symbol
both ways — per symbol (today's path) and out of the calendar window — and diffs them:

| Verdict | Means | Do |
|---|---|---|
| `calendar_complete` | every per-symbol event in the window appears in the calendar, with equal values | Adopt the sweep. |
| `calendar_incomplete` | events missing from the calendar | **Stop.** Keep the per-symbol path for the affected asset type. |
| `field_mismatch` | same events, different field names or values | Fix the transform, re-probe. |
| `no_events` | nothing went ex in the window | Widen `--days` or pick a dividend payer. |

Non-zero exit on anything but a pass, so it can gate the rollout script. Costs
`2 + 2N` requests and writes nothing.

The probe is also where these get confirmed against a live response, in keeping with the
existing note in `doc/ingestion.md`: the calendar payload's field names (`symbol`, `date`,
`numerator`/`denominator`, `dividend`/`adjDividend`, `recordDate`, `paymentDate`,
`declarationDate`), the documented 3-month window cap, that both endpoints are on the
Professional plan, and whether a bulk splits/dividends download (option 7) is available.

## Consequences

**Positive**

- The nightly job drops ~2.5 hours and ~42,800 requests, and stops growing with the
  delisted tail.
- Corporate actions gain the property every other loader already has: a missed night is
  caught up by widening a window, not by re-reading all of history.
- `landing.fmp_raw` stops accreting ~42,800 rows a night for a few hundred events.
- `fafnir adjust` gets a changed-set and stops being a second full refresh.
- A newly listed or newly declared security still gets its complete history, by the same
  no-watermark rule prices already use.

**Negative**

- A market-wide dependency: one bad calendar response now affects every security rather
  than one symbol. Mitigated by the overlap re-sweep and the monthly reconciliation, and
  bounded by the fact that a missing event is detectable rather than silent.
- Two more endpoints to keep working, and a vendor coverage assumption that has to be
  re-verified rather than assumed (hence the probe and the rotation).
- Cancelled or withdrawn actions are still not deleted — the loader upserts and never
  removes. That is equally true today; the reconciliation diff in §10 is the first place
  fafnir would be able to *detect* one, which is a precondition for ever handling it.
- More states to reason about on one path: sweep, first-load, fund, reconciliation.

**Neutral**

- `core.corporate_action`, `core.adjustment_factor`, the mart view and everything `duk`
  reads are untouched. This ADR changes how rows arrive, not what they mean.
- `scripts/initial_backfill.sh` keeps its per-symbol full pull; that run is what mints the
  per-security watermarks the nightly path then relies on.

## Rollout

- [x] Active-only symbol list plus `--include-inactive` — independent of everything
      else, and in effect now: ~60% off the nightly cost with no behaviour to verify,
      because a delisted security cannot have another corporate action.
- [x] Calendar client methods, window-chunked under the 3-month cap.
- [x] The sweep, first-load, fund and reconciliation paths behind
      `fafnir ingest actions --mode symbol|calendar|auto`, defaulting to `symbol`.
- [x] `fafnir source probe-actions`, exiting non-zero so it can gate a script.
- [x] `fafnir adjust --changed`, and `daily_update.sh` using it.
- [ ] **Operator:** run `probe-actions` against a live key. If it passes, set
      `actions_mode = "auto"`. If it does not, the verdict says which asset type to
      keep on the per-symbol path — do not flip.
- [ ] **Operator:** watch `corporate_action_drift` for the first month. An empty queue
      after a full reconciliation cycle is the evidence that the sweep is complete;
      that is the point at which this decision is confirmed rather than merely made.
