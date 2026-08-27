# ADR 0005: The universe maintains itself (new listings and ticker renames)

- Status: Accepted
- Date: 2026-08-27
- Extends: [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md)

## Context

Up to migration 0010 the security master was a **build** step. `fafnir ingest
securities` ran once during `scripts/initial_backfill.sh` and never again:
`scripts/daily_update.sh` went straight from `ensure-horizon` to the delisting
sweep and prices. Two consequences, both silent.

**New listings never entered scope.** Every IPO, spin-off and new ETF after
installation day was invisible. Nothing failed and no flag was raised — the
warehouse simply described a universe that was frozen on the day it was built, and
the freshness checks all passed because every security it *did* know about was up
to date. The longer a deployment ran, the more of the market it quietly missed.

**Renames forked a company's identity.** This is the worse of the two, because it
corrupts data rather than merely omitting it. To the screener a renamed ticker is
just a ticker; the payload carries no hint that META is what FB became. So even
with the security master running nightly:

1. `META` matches no active row under 0009's partial unique index
   `(source, primary_symbol, COALESCE(exchange_code,''))  WHERE delisted_date IS NULL`,
   so the upsert **inserts** and mints a second `security_id`.
2. The company's bars, corporate actions, adjustment factors and — expensively —
   its `ops.load_watermark` row all stay on the `FB` row.
3. Nothing ever closes the `FB` row. A rename is not a delisting, so it never
   appears in the delisted feed; `is_actively_trading` stays true and the price
   loader asks FMP for `FB` bars every night, forever, for a ticker that no longer
   trades.
4. The new `META` row, having no watermark, re-backfills history that fafnir
   already holds under a different id.

The result is one company as two entities: a truncated series under each, joins
that silently miss half the history, and a backtest whose universe contains a
ghost. ADR 0002 already built the mechanism to prevent exactly this —
`core.symbol_xref` maps tickers to a `security_id` **over time** — but nothing was
writing the rename into it.

## Decision

Universe maintenance becomes part of the nightly job, in three steps that run
**before** any market data is pulled:

```
symbol-changes  →  securities  →  delisted  →  prices → actions → adjust → ...
```

1. **`fafnir ingest symbol-changes`** (new) reads FMP's `symbol-change` feed and
   applies each rename to the security that already holds the history: close the
   old ticker's xref period the day before the change, open a new period for the
   new ticker against the same `security_id`, move `primary_symbol` across. One
   company stays one entity; the watermark survives, so the next price pull is a
   tail, not a re-backfill.
2. **`fafnir ingest securities`** re-reads the screener, so a security that lists
   today enters scope today. It reports which tickers were new. A newly minted
   security has no watermark, so the price step in the same run pulls its whole
   available history with no extra scheduling.
3. **`fafnir ingest delisted`** keeps its place before prices. Its one change is
   forced by the resolution fallback below: it resolves a feed row through
   `active_security_for_symbol`, so a retired ticker can never stamp a one-way
   delisting on the live security that ticker was renamed away from.

Renames go first *because* of the trap above: run the security master first and it
mints the duplicate before the rename can be applied to the original.

Every rename fafnir tracks is recorded in `core.symbol_change` (migration 0011)
with a status: `applied`, `conflict`, or `ignored`. The table is what makes the
nightly sweep idempotent — the same feed tail is re-read every night, and a
re-applied rename would move a ticker that has legitimately moved on since.

Three sub-decisions worth stating:

- **A rename is never applied to a delisted issuer** (`ignored`). A ticker
  reappearing under a dead company's symbol is *reuse*, and 0009 already handles it
  by minting a new `security_id`. Resurrecting the dead row is the exact failure
  0009 was written to prevent.
- **An empty duplicate is folded; a duplicate with history is a conflict.** If the
  security master ran before the rename was known, the new ticker already exists as
  a bare row. When it holds no bars, actions or factors it is absorbed into the
  surviving security — the one place in fafnir where a security row is deleted,
  guarded by that emptiness check. When it *does* carry history, nothing is
  changed: two price histories are not a loader's to merge. It is recorded as
  `conflict`, raised as a `symbol_change_conflict` DQ flag, and surfaced by
  `fafnir status` until a human resolves it.
- **A company's former ticker still resolves.** Once `FB` is closed in the xref and
  is nobody's `primary_symbol`, it would otherwise address nothing.
  `resolve_security_id` gained a last-resort lookup of the most recently *closed*
  xref period, after the open period and after `primary_symbol` — so a reused
  ticker still resolves to its live owner, a delisted name still resolves to itself,
  and `duk ph FB` reaches the company whose history it is. `duk.datasource.db`
  carries the same three queries, as it must. That fallback belongs to the **read**
  path: a loader that writes a one-way flag keyed on a ticker from a global feed —
  the delisting sweep — must resolve through `active_security_for_symbol` instead,
  or a row for the retired `FB` would stamp a delisting on the live `META` it was
  renamed away from, silently dropping it out of the active price universe.

### Amendment: the venue is not part of a company's identity

Review of this change surfaced a second way the nightly security master could fork
an identity, latent until it started running nightly. The key from 0003/0009 was
`(source, primary_symbol, exchange)`, which treats the *listing venue* as part of
who a company is. It is not: a company moving from NYSE to NASDAQ is the same
issuer with the same history. But the key changed, so the upsert inserted — and the
consequences compounded:

- a second **listed** `security_id` appeared for one ticker;
- `upsert_symbol_xref` **repointed** the ticker's open period to the new, empty row,
  so the symbol resolved to a security with no bars — `duk ph ABC` returned nothing
  while years of history sat unreachable on the old id;
- the new row had no watermark, so the next price run re-downloaded the entire
  history into it;
- the old row stayed `is_actively_trading`, so it was polled forever.

Migration 0012 keys a listed security on `(source, primary_symbol)` and lets the
exchange be the mutable attribute it always was. This is safe because a US ticker is
unique across the national market system — NYSE, NASDAQ, AMEX, BATS and CBOE do not
assign one symbol to two issuers — and FMP namespaces foreign venues with a suffix
(`2958.HK`, `SAP.DE`), so the symbol alone is unambiguous in every universe fafnir
loads. Ticker *reuse* is still handled by 0009's `WHERE delisted_date IS NULL`,
which 0012 keeps.

0012 also repairs databases that already forked, using the same rule as the rename
sweep: an empty duplicate is folded into the row holding the history, and a
duplicate that carries its own history stops the migration with both ids named,
because merging two price histories is a human's decision.

The residual risk of that key is stated plainly: if two *listed* securities from one
source ever did share a symbol, the second would silently UPDATE the first and its
bars would attach to the wrong `security_id`. That risk is accepted, but not left
silent. Every security-master update now compares the incoming company name against
the stored one and raises an advisory `security_company_name_drift` flag when the
two do not look like the same company, which is what such a collision would look
like from the outside. It is deliberately a `warn` rather than an error: a genuine
same-ticker rebrand trips it too, so it is a queue a human reads, not a load that
fails. The alternative — keying on a vendor-independent identifier (CUSIP/ISIN/CIK)
— remains the right long-term answer, and is blocked only by FMP serving those
fields solely from the per-symbol `profile` endpoint rather than the bulk screener
the nightly load uses.

## Consequences

- The nightly job costs one extra screener pass (a few MB) and one small rename
  request. Bandwidth against the 50 GB/month budget is unaffected in practice.
- `fafnir status` gains a `New (7d)` count and an unapplied-renames queue.
- Renames are reconciled **from the point this starts running**, exactly like the
  delisting sweep (`ingest/delisted.py`). A rename that happened while fafnir was
  not watching, and whose duplicate has since accumulated its own history, needs
  the manual path in [operations.md](../operations.md#when-a-rename-cannot-be-applied-automatically).
- `core.symbol_change` is also a research artifact: it answers "what was this
  company called before?" without a scan, in both directions.
