# Ingestion

How data flows from FMP into the warehouse, and the guarantees that make it
research-grade.

## Principles

- **Land raw, then transform.** Every response is persisted to `landing.fmp_raw`
  (payload + hash) before transformation, so the original is always recoverable.
- **Validate at the boundary.** Each row is typed and sanity-checked (cross-field,
  ranges, nulls). Failures are **quarantined** into `ops.data_quality_flag`, never
  silently dropped.
- **Idempotent.** Loads upsert on the natural key (`ON CONFLICT ... DO UPDATE`).
  Re-pulling a window converges to the same state.
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
| `ingest actions` | `splits`, `dividends` | `core.corporate_action` | (security_id, action_type, ex_date) |
| `adjust` | (derived) | `core.adjustment_factor` | (security_id, effective_date) |

> **Verify before the first production backfill:** the `symbol-change` payload
> field names (`date`, `oldSymbol`, `newSymbol`, `companyName`) and the
> `splits` / `dividends` stable field names (`numerator`/`denominator`, `dividend`/`adjDividend`) should
> be confirmed against a live response for a known symbol (e.g. AAPL). The loader
> already tolerates the common alternates; the endpoint paths are centralized as
> constants in `FMPClient` for a one-line correction.

> **`ingest actions` is the one load that is not incremental.** It has no watermark,
> and it selects every row in `core.security` — delisted names included — so each
> nightly run re-downloads the complete split and dividend history of ~21k symbols in
> ~42.8k requests (~2.5 hours) to capture a few hundred changed rows. A per-symbol
> watermark does not fix it: the per-symbol endpoints take no window, so the request
> count, not the payload, is the cost. See
> [adr/0007](adr/0007-incremental-corporate-actions.md) for the evaluated options and
> the proposed market-wide calendar sweep.

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

- **NAV bars.** A fund payload carries a close and no open/high/low. `_validate_bar`
  expands it to `open = high = low = close` **only** when the security's `asset_type`
  is in `NAV_ASSET_TYPES` — for an equity a missing OHLC field is still a defect and
  still quarantines. A field that is *present but unusable* (zero, sub-resolution) is
  quarantined on either.
- **Evening NAV.** Fund NAV is struck at 4pm ET and posted that evening, later than
  equity EOD, so a nightly run timed for equities finds funds a day behind.
  `check_freshness` gives `NAV_LAGGING_ASSET_TYPES` one **trading day** of slack —
  without it the queue would grow by one row per fund per night forever, since the
  flag's `record_key` is the security's own last date and changes daily.
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

**Retiring a fund.** The delisted feed does not carry funds (and `MUTF` is outside
`SCREENER_EXCHANGES`, so the sweep cannot reach them). `fafnir track rm VFIAX` stops
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

## The adjustment step

`fafnir adjust` recomputes `core.adjustment_factor` from `core.corporate_action`:

- split num:den → price × (den/num), volume × (num/den) for prior dates;
- cash dividend D vs prior close P → price × ((P−D)/P) for prior dates.

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
`security_id`; because that security has no `ops.load_watermark` row, the price
step in the same run leaves its window unbounded and pulls the symbol's whole
available history (§*Watermarks*). Nothing has to be scheduled per security. The
loader reports which tickers were new — `Loaded 21412 securities (3 new)` — so the
nightly log distinguishes a refresh from an arrival.

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

A rename for a ticker fafnir does not track is counted and dropped, not recorded:
the feed is global across every venue, and the audit table is not a copy of it.

If the security master ran before the rename was known and minted the new ticker as
its own row, the sweep folds that duplicate back in — but only when it is still
empty (no bars, no actions, no factors). That fold is the one place fafnir deletes
a security; retention exists so history is never lost, and a stub has none. A
duplicate that *has* accumulated history is a `conflict` instead: merging two price
histories is not a decision a loader should make silently.

## Order of operations (daily)

```
ensure-partitions → symbol-changes → securities → delisted →
prices → actions → adjust → refresh-marts → dq run
```

The universe is reconciled before any data is pulled, so prices run against what is
actually trading today. `ingest delisted` keeps its place and its behaviour, with
one correction the rename step forces: it resolves a feed row through
`active_security_for_symbol`, not `resolve_security_id`. The read path deliberately
falls back to a ticker a company used to trade under, and a delisted feed reports
retired tickers -- so resolving that way would let a row for the retired `FB` stamp
a one-way delisting on the live `META` security. Prices precede actions so dividend adjustment can value
against fresh closes. `scripts/daily_update.sh` encodes this order.

## Watermarks and the endpoint string

`ops.load_watermark` is keyed on `(source, endpoint, security_id)`, so the endpoint
path is part of ingestion state, not just a URL. Changing it retires every existing
watermark and makes each symbol look new — which on an incremental run means an
unbounded request, and an unbounded request is capped at 5000 bars (~19.8 years).

`load_prices` therefore refuses to run incrementally when watermarks exist only
under the retired `historical-price-eod/full` endpoint, directing the operator to a
re-backfill instead. If you ever change a loader's endpoint again, plan the
watermark migration at the same time.
