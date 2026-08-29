# ADR 0006: A declared universe alongside the screened one (select mutual funds)

- Status: Accepted
- Date: 2026-08-29
- Extends: [ADR 0005](0005-automatic-universe-maintenance.md),
  [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md)
- Depends on: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md),
  [ADR 0004](0004-unadjusted-price-feed.md)

## Context

A portfolio held outside fafnir contains open-end mutual funds. Their price history
should live in the warehouse for the same reason everything else does: so the return
and indicator work reads one source of truth instead of a spreadsheet.

The full fund universe is explicitly **not** wanted. FMP lists tens of thousands of
share classes; each would cost a nightly EOD request forever, and essentially none of
them would ever be read. This is a handful of symbols, chosen by a human, that must
be as well-maintained as everything else in `core`.

**Why the existing machinery cannot reach them.** ADR 0005 made the universe
self-maintaining by re-reading `company-screener` nightly, one paged pass per venue
(`SCREENER_EXCHANGES`), and keeping only rows whose normalized exchange is in
`US_EXCHANGES`. An open-end mutual fund has no listing venue — it is struck at NAV
once a day, not traded on an exchange — so it is not in the screener at all, and
`_is_us` would reject it if it were. No amount of nightly upkeep will ever mint it.
The screened universe is a *discovered* universe; a fund is a *declared* one, and
fafnir has no way to declare anything.

**Why hand-inserting the row is not the answer.** `INSERT INTO core.security ...`
produces a working `security_id` and everything downstream would pick it up. It also
produces a row with no lineage, no reason recorded, no way to reproduce it from
`migrate` + `seed`, and nothing that tells the next operator (or the next `reset_data.sh`)
that the row is deliberate rather than an anomaly. The security master would then be
partly declarative and partly folklore.

## Decision

Add a **declared universe** as a first-class input to the security master, sitting
beside the screened one. Three pieces; everything downstream is untouched.

### 1. `ref.tracked_symbol` — the declaration

Operator-curated reference data, in `ref` beside the other slowly-changing dimensions.
Migration `0018_tracked_symbol`:

```sql
CREATE TABLE ref.tracked_symbol (
    source        TEXT NOT NULL DEFAULT 'fmp',
    symbol        TEXT NOT NULL,
    asset_type    TEXT NOT NULL DEFAULT 'fund'
                     CHECK (asset_type IN ('equity', 'etf', 'fund', 'other')),
    exchange_code TEXT REFERENCES ref.exchange (exchange_code),
    note          TEXT,                      -- why this symbol is tracked
    is_tracked    BOOLEAN NOT NULL DEFAULT TRUE,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    untracked_at  TIMESTAMPTZ,
    PRIMARY KEY (source, symbol),
    CONSTRAINT ck_tracked_symbol_untracked
        CHECK (is_tracked OR untracked_at IS NOT NULL)
);
```

Grain: one row per `(source, symbol)` — the same soft key the security master already
upserts on (0009/0012), so a declaration and a screened row can never fork into two
`security_id`s.

The same migration seeds the pseudo-venue the FK needs:

```sql
INSERT INTO ref.exchange (exchange_code, exchange_name, country, timezone)
VALUES ('MUTF', 'US open-end mutual funds (NAV, no venue)', 'US', 'America/New_York')
ON CONFLICT (exchange_code) DO NOTHING;
```

It lives in the migration rather than `sql/seeds/`, because the loader has a foreign
key on it — it is a dependency of the schema, not optional reference data.

`MUTF` earns its keep beyond documentation: `ingest delisted` only considers feed rows
whose venue is in `SCREENER_EXCHANGES`, so a fund is *structurally* outside the
delisting sweep. It cannot be marked dead by an equity feed that has never heard of it.

### 2. `fafnir ingest tracked` — the loader

`src/fafnir/ingest/tracked.py`, mirroring `security_master.py`: for each row where
`is_tracked`, fetch FMP `profile`, `land_payload` it, and `repo.upsert_security(...)`
with `asset_type`/`exchange_code` taken from the declaration (not from the vendor —
the declaration is the authority on what this symbol *is*), then `upsert_symbol_xref`.
Wrapped in `RunLog` like every other loader, so it appears in `ops.ingestion_run`.

It slots into `scripts/daily_update.sh` immediately **after** `ingest securities`:

```
symbol-changes → securities → tracked → delisted → prices → actions → adjust → marts → dq
```

After, not before, so that a symbol appearing in both universes (someone tracks an
ETF that the screener also returns) converges on the declared attributes rather than
racing. Both writers use the same conflict key, so the second one is a refresh.

Operator surface:

```bash
fafnir track add VFIAX --note "core US equity sleeve"
fafnir track list
fafnir track rm  VFIAX --closed 2027-03-31   # merged/liquidated
fafnir ingest tracked                        # nightly; also run by daily_update.sh
```

`track rm` requires the operator to say which retirement this is. `--closed <date>`
stamps `delisted_date` and closes the xref period — the ordinary delisting path, history
retained per ADR 0002. Without it, the fund is merely untracked: pulls stop, the row
stays active, and `check_freshness` will flag it stale every night. Making the operator
choose is what keeps a quiet un-track from becoming a permanent DQ flag.

### 3. Nothing downstream changes

Prices, actions, factors, marts and `duk` all key off `core.security`, never off the
screener. Once the fund holds a `security_id`:

- `fafnir ingest prices` includes it — its default symbol list is
  `SELECT primary_symbol FROM core.security WHERE is_actively_trading`.
- It has no watermark, so the first run pulls its full available history, exactly like
  an IPO under ADR 0005.
- `mart.v_daily_price_adjusted` and `mart.security_latest` cover it with no change.
- `duk ph VFIAX --adj -S db` works, and `duk ls --isFund` already exists as a filter.
- The gap check runs every security against the NASDAQ calendar; funds strike NAV on
  the same US business days, so it is correct for them unchanged.

## Two changes the fund grain does require

Everything above is additive. Two places assume an exchange-traded bar and need a
narrow, asset-type-gated allowance.

**NAV bars are not OHLCV.** A fund has one price a day and no volume. Where FMP
returns a NAV-only payload, `_validate_bar` currently rejects every bar with
`missing_or_nonnumeric_ohlc` — a fund would ingest zero rows and generate one DQ flag
per day of history. The fix belongs at the transform boundary in
`ingest/daily_price.py`: `load_symbol_prices` already resolves the security, so it can
read `asset_type` at the same time and pass `nav_only=True`, under which a bar carrying
a close but no open/high/low is expanded to `open = high = low = close = NAV`. Volume
already defaults to 0. A present open/high/low is still used as given and still faces
the cross-field CHECKs, and for an equity the missing-field rejection is unchanged —
a silently OHLC-less equity bar remains a defect, not a NAV.

The row that results satisfies every constraint on `core.daily_price` as written
(`high >= low`, positivity, `volume >= 0`), so the fact table, its partitioning and
the Timescale path are untouched.

**NAV is published after the equity close.** Fund NAV is struck at 4pm ET and posted
in the evening, later than equity EOD. A nightly run timed for equities will find the
fund one day behind and `check_freshness` will flag it every night — a standing,
self-renewing flag keyed on a date that changes daily, which is exactly the unbounded
queue growth the check was written to avoid. Rather than delay the whole job for a
handful of symbols, give `check_freshness` a one-business-day allowance for
`asset_type = 'fund'`.

## Distributions

Funds pay income dividends and short/long-term capital-gain distributions. NAV drops
by the full distributed amount on the ex-date — economically identical to a cash
dividend, and the same arithmetic `ingest/adjustments.py` already applies:
`price × (P − D) / P`, with `P` the last NAV before ex-date. So distributions load as
`action_type = 'dividend'` in `core.corporate_action`. **No new action type, no change
to the adjustment math, no change to the mart view.**

If FMP labels the distribution kind, an additive nullable `dividend_kind`
(`income` / `short_term_gain` / `long_term_gain` / `return_of_capital`, NULL for
equities) is worth carrying for later income analysis. It changes nothing about
adjustment and should not gate this work.

## The one thing that must be probed first

ADR 0001 and ADR 0004 rest on a precondition: the feed behind `core.daily_price` is
**genuinely unadjusted**, because fafnir's own factors are the only adjustment in the
system. That precondition is verified for equities and **unverified for funds**. Three
questions, answerable in about three API calls, decide whether this design is correct:

1. Does `historical-price-eod/non-split-adjusted?symbol=<fund>` return bars at all, or
   does the unadjusted variant only cover exchange-traded symbols?
2. **Is the NAV series raw or total-return?** Take a fund with a known December
   capital-gain distribution and compare the close-to-close move on the ex-date against
   the distribution amount from the `dividends` endpoint. If NAV drops by roughly the
   distribution, the feed is raw and this design is correct as written. If NAV does
   *not* drop, the vendor has already reinvested distributions, and loading them as
   corporate actions would adjust every one of them twice — the exact failure ADR 0004
   was written about, reproduced on a new asset class.
3. Does the `dividends` endpoint return fund distributions, and is `dividend` the
   as-declared per-share amount (not a restated one)?

`fafnir source probe-prices` already exists to answer question 2 for equities and should
be extended to funds. Do this before the migration, not after the backfill: if the
answer to 2 is "already adjusted", the decision changes — funds would carry no
corporate actions and `mart.v_daily_price_adjusted` would return their series
unadjusted (factor 1.0), which is correct but must be a deliberate, documented choice
rather than a discovered one.

## Alternatives rejected

**A separate `core.fund_nav` table.** Same grain `(security_id, date)`, same exact-NUMERIC
money, same adjustment model. A second fact table would fork every reader — the mart
view, `duk.datasource.db.price_history`, all three DQ checks, the partitioning scheme
and the Timescale path — to express a distinction that is one boolean on
`core.security`. A NAV is a price.

**Loading the full fund universe.** Tens of thousands of share classes, each a nightly
request against a 50 GB/month budget, essentially none of them read. The declared
universe exists precisely so scope is a decision rather than a side effect.

**A new `asset_type`.** `'fund'` is already in the CHECK on `core.security.asset_type`
and `is_fund` is already a column. The dimension was built for this; nothing to add.

**Widening the screener.** There is no venue to screen on. The absence is the point.

## Consequences

- The security master gains a second, declarative input. `core.security` is no longer
  reproducible from FMP alone — it is reproducible from FMP **plus** `ref.tracked_symbol`,
  which is versioned data in the warehouse and included in backups.
- Nightly cost is about four requests per tracked fund (profile, EOD tail, splits,
  dividends) and a few hundred KB. Ten funds is noise against the budget.
- `mart.security_latest` will carry funds with NULL `market_cap_usd`, `beta`, `sector`
  and `industry`. Existing screens filter on those columns and so exclude funds
  naturally; `--isFund` selects them.
- A tracked symbol that collides with a listed ticker would upsert onto the existing
  security rather than minting a second one — correct behaviour, and the existing
  `security_company_name_drift` check is the safety net if the two are genuinely
  different issuers.
- The mechanism generalizes beyond funds at no extra cost: a foreign listing, a closed-end
  fund, an index proxy — anything with an FMP symbol and no screener row — is one
  `fafnir track add` away.

## Implementation

Landed as described, with two clarifications the code forced:

**Following a rename.** `ref.tracked_symbol` names a ticker forever, and
`ingest symbol-changes` can rename the security underneath it. Upserting on the
stale ticker would mint a second `security_id` and strand the fund's bars,
watermark and actions on the first — the fork ADR 0005 exists to prevent, except
that here the screener cannot rescue it, because the declaration goes on naming the
old ticker. So `ingest tracked` resolves identity *before* it fetches: it follows a
closed xref period to the listed security that used to carry the ticker and moves
the declaration onto the current one. A ticker *reused* by a new issuer after the
old one delisted is excluded (delisted rows are not followed) and mints fresh, as
0009 already guarantees for the screened universe.

**Close is validated first.** The NAV allowance stands the close in for a missing
open/high/low, so the close has to be known before the other three are judged.
`_validate_bar` therefore evaluates `close, open, high, low` rather than
`open, high, low, close`. Only an *absent* field is stood in for — a present but
unusable one (zero, negative, sub-resolution, out of range) is bad data on a fund
exactly as on an equity, and is quarantined either way.

## Implementation checklist

| Area | Change |
|---|---|
| `sql/migrations/0018_tracked_symbol.{up,down}.sql` | `ref.tracked_symbol`; seed `MUTF` venue |
| `src/fafnir/db/repository.py` | `upsert_tracked_symbol`, `list_tracked_symbols`, `untrack_symbol`, `security_asset_type`, plus `listed_security_for_declaration` / `retarget_tracked_symbol` for the rename case above |
| `src/fafnir/ingest/tracked.py` | new loader (`RunLog`, `land_payload`, `upsert_security`, `upsert_symbol_xref`) |
| `src/fafnir/ingest/daily_price.py` | `nav_only` allowance in `_validate_bar` / `load_symbol_prices` |
| `src/fafnir/dq/checks.py` | one-business-day freshness allowance for `asset_type = 'fund'` |
| `src/fafnir/sources/probe.py` | fund NAV raw-vs-total-return probe |
| `src/fafnir/cli.py` | `fafnir track add\|list\|rm`; `fafnir ingest tracked`; `fafnir source probe-fund` |
| `scripts/daily_update.sh`, `scripts/initial_backfill.sh` | `ingest tracked` after `ingest securities` |
| Docs | data dictionary (`ref.tracked_symbol`), ingestion (endpoint map), architecture (ERD + universe section), README |

**Tests.** Unit: NAV-only expansion produces a clean bar for a fund and still
quarantines a missing-OHLC equity bar; `ref.tracked_symbol` upsert idempotency.
Integration: track a fund, run `tracked → prices → actions → adjust`, assert one
`security_id`, assert the adjusted close on a distribution ex-date equals
`NAV × (NAV − D) / NAV`, re-run and assert no new rows, and assert a nightly
`ingest securities` + `ingest delisted` leaves the fund untouched.
