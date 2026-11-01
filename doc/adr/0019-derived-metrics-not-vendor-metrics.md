# ADR 0019: fafnir derives its metrics; vendor metrics are not read

- Status: Accepted
- Date: 2026-10-25
- Amended: 2026-11-01 — [daily market cap, and the share factor](#amendment-2026-11-daily-market-cap-and-the-share-factor)
  (migration 0042).
- Implemented by: #87 (`extra`), #95 (TTM), #100 (the factor panel), #101 (industry
  aggregates)
- Related: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md) (derive on read),
  [ADR 0004](0004-unadjusted-price-feed.md) (the double-adjustment failure, whose
  analogue this is), [ADR 0018](0018-bitemporal-fundamentals.md) (the statements read
  here, and their share terms), the build plan
  [fundamentals-sf1.md](../plans/fundamentals-sf1.md)

## Context

SF1 carries more than statements. The probe (#83) found:

- **36 columns of vendor metrics.** Ratios, margins, averages, enterprise value, and
  per-share values.
- **11 columns measured on the row's `date`.** Price, market cap, EV, P/E, P/S, P/B and
  dividend yield. A first-reported row and its restated twin differ on these only because
  their dates differ.
- **Per-share values in today's share terms, to 3 decimals.** Recovered to as-reported, an
  EPS is off from net income ÷ shares by a median of $0.005, and by up to $0.52.

A vendor metric is not point-in-time stable. The vendor recomputes it when it restates a
statement or re-expresses it for a split, prices it on its own date and price convention,
and cannot be reconciled with the warehouse's own prices.

There is also the trap ADR 0004 met in prices. A per-share figure and a price in different
share terms give a ratio that is wrong by the split product, plausibly and silently.
AAPL's splits since 1990 multiply to 112.

## Decision

1. **fafnir computes every metric on read**, in `mart`, from `core` statements and `core`
   prices. SF1's metrics, its date-measured columns and its per-share values are kept in
   the statement row's `extra` (`vendor_metric`) and **never read by `mart`**. A
   `core.vendor_metric` table stays possible later; it is not built.
2. **Per-share values are derived:**
   - EPS = `net_income_common` / `weighted_average_shares`, in one share terms;
   - DPS from `core.corporate_action` dividends, which are stored as declared.
3. **One expression puts a share count in the as-of date's terms:** × the product of the
   warehouse's splits with an ex-date in `(known_from, t]`. A version is stored in the
   terms of its `known_from` (ADR 0018). The prices it meets are raw on `t` (ADR 0001).
   Prices and share counts are never mixed across share terms.
4. **Aggregate over aggregate first.**
   - Market cap ÷ net income, not price ÷ EPS: split-immune by construction.
   - Value factors are yields (fundamental ÷ price), so a negative earnings figure stays
     information instead of becoming a meaningless multiple.
5. **Market cap at t** = the raw close on t × `shares_outstanding` of the latest period
   known at t, in t's terms. Never `core.security.market_cap_usd` (a vendor snapshot),
   never SF1's `marketcap`. For a multi-class issuer it is approximate, as SF1's own is.
6. **Denominator discipline.**
   - A ratio whose denominator is NULL, zero, or negative where negative is meaningless
     is NULL. That covers the zeros SF1 writes for absent items.
   - Numerators keep their sign.
   - Nothing is winsorized, clipped or filled in the warehouse.
7. **Standing tests:** AAPL's P/E between 1998 and 2000 within a plausible band (the
   split-era trap), and the panel's look-ahead invariant (ADR 0018).

## Consequences

- **Every factor is reproducible from `core`.** None drifts when Sharadar recomputes its
  metrics.
- **The vendor's metrics stay available for comparison** in `extra`, and nothing depends
  on them. If they cost more than they are worth (over 2 GB, measured in #87), they are
  not stored, and they stay in the ledger's file.
- **Enterprise value omits minority interest and preferred stock.** SF1 has no balance
  items for them. The panel's column comment says so.
- **A zero SF1 wrote for an absent item reads as 0 in a numerator** (an R&D intensity of
  zero, say). `fundamentals_suspicious_zero` watches the items a company reports in
  neighbouring periods.
- **The panel pays for its arithmetic at refresh.** The month-end grain bounds it.

## Alternatives considered

- **Read SF1's metrics.** Rejected: measured on the vendor's own date and price,
  recomputed without notice, and impossible to reconcile with the warehouse's prices.
- **Recover SF1's EPS to as-reported.** Rejected: 3 decimals in today's terms lose up to
  $0.52 a share once multiplied back.
- **Keep everything in today's share terms, and prices with it.** Rejected: today's terms
  move at every split, so the stored figures and every price beside them would be
  re-expressed again and again. Raw prices are ADR 0001's rule.

## Amendment (2026-11): daily market cap, and the share factor

Market cap is wanted daily, as the weights of custom cap-weighted indexes. Decision 5's
expression is evaluated on every bar, and one SF1 column is admitted to it.

**Daily, derived on read.** `mart.fn_daily_market_cap(security_ids, start, end)` returns,
for each raw bar:

- the raw close × `shares_outstanding` of the latest balance sheet in force that day
  (ended within 730 days), in that day's share terms (decision 3), × the share factor
  below;
- the balance sheet the count came from (`fiscal_date`, `known_from`).

Nothing is stored, as ADR 0001 stores no adjusted price. The value on a past day is
unchanged by anything known later: a restatement is known from the day fafnir observes
it (ADR 0018), and a later split converts the count on and after its ex-date only. A
vendor correction or a corrected split changes it, as it should. A materialized table
was rejected: tens of millions of rows to rebuild whenever a split or a correction
lands. A computation in duk was rejected too: a third copy of the expression, and
nothing for SQL or MCP readers. `duk mc`, `duk ph --mcap` and the MCP tool
`market_cap_history` read the function.

**The share factor is a share-terms conversion, not a vendor metric.** SF1's
`sharefactor` turns its count into the traded line's:

- an ADR's ratio: TSM's 0.2, five ordinary shares to one ADR;
- a multi-class issuer's conversion into its primary class: BRK.B's about 1.52.

Sharadar's own `marketcap` is `closeunadj × sharesbas × sharefactor`. Measured on
2026-10-30, AAPL's DAILY figure is 344.63 × 14,594,180,000 (factor 1). Without the
factor, TSM's derived cap was five times too high and BRK.B's a third too low. It
differs from the columns decision 1 excludes:

- it is not measured on a date or a price;
- the vendor does not recompute it from either;
- it changes only when the share structure does.

So it is read. It stays in `extra.vendor_metric` (a typed column would change every
statement's content hash, and so restate every period), and
`mart.v_share_factor_versions` exposes it on each income version's own timelines.

- **The one key of `extra` that `mart` reads.** It is used where it is a positive
  number.
- **Which factor applies.** The same period's income version in force on the day.
- **Absent is 1.** Where SF1 gives none, the factor is 1: one class, no ADR.

**Coverage, as decision 5 had it.** A cap exists where SF1 holds a count: never for a
fund, an ETF, or a security Sharadar does not feed. A secondary share class has none of
its own (ADR 0018 §6). Its issuer's primary class carries a cap counting every class's
shares, as SF1's does, so an index holding both classes holds the issuer twice. There
is no float adjustment: SF1 carries no float.

**Consequences.**

- `mart.v_security_attributes` applies the factor (0042). `mart.security_latest` takes
  it on its next refresh.
- The factor panel (0037) does not apply it yet. Its `market_cap` and value yields for a
  security whose factor is not 1 differ from the daily function until the panel is
  regenerated. A test pins the difference, so the fix shows up there.
