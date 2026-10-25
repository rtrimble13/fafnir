# ADR 0019: fafnir derives its metrics; vendor metrics are not read

- Status: Accepted
- Date: 2026-10-25
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
