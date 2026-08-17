# ADR 0004: Pull prices from FMP's unadjusted EOD endpoint

- Status: Accepted
- Date: 2026-08-17
- Amends: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md)

## Context

[ADR 0001](0001-raw-prices-plus-adjustment-factors.md) defines `core.daily_price` as
**raw** OHLCV and makes `core.adjustment_factor` the only adjustment in the system.
That design is only correct if the feed behind it is genuinely unadjusted.

It was not. The loader read `historical-price-eod/full`, and ADR 0001's context
section described that payload as carrying a raw `close`. It does not:

| Field / endpoint | Adjusted for |
|---|---|
| `historical-price-eod/full` → `close` | splits |
| `historical-price-eod/full` → `adjClose` | splits **and** dividends |
| `historical-price-eod/dividend-adjusted` | splits and dividends |
| `historical-price-eod/non-split-adjusted` | **nothing — prices as traded** |

So every bar in `core.daily_price` was already split-adjusted, and `fafnir adjust`
applied the split factor a **second** time. The error is invisible on a symbol that
has never split and compounds with every split for one that has:

AAPL has split 2:1, 2:1, 7:1 and 4:1 since 1990 — 112:1 cumulatively. Its true
1990-01-02 close was ~$39.20. `.../full` returns ~$0.35 (39.20 ÷ 112), which was
stored as "raw"; the view then multiplied by the 1/112 factor again and reported
**~$0.0031**. Deep history collapsed toward zero, and log-returns across it were
meaningless.

Note the failure is silent by construction. The stored series is internally
consistent, monotone, and passes the outlier check — because a pre-adjusted feed has
no split jumps to flag. Only comparing a level against a known price reveals it.

## Decision

Pull daily OHLCV from **`historical-price-eod/non-split-adjusted`**.

- `FMPClient.EP_EOD_RAW` names the endpoint; the method is `eod_raw`, not `eod_full`,
  so nothing in the codebase describes this feed as "full" again.
- That endpoint labels its OHLC fields `adjOpen`/`adjHigh`/`adjLow`/`adjClose` — an
  FMP naming convention shared with the dividend-adjusted payload, **not** a second
  adjustment. `fafnir.ingest.daily_price` accepts either spelling at the transform
  boundary, preferring the unprefixed names, and lands payloads verbatim.
- Dividend amounts keep coming from the as-declared `dividend` field rather than the
  restated `adjDividend`, so the dividend D and the prior close P in the
  `(P − D) / P` factor are quoted in the same share terms.
- `duk`'s live price path uses the same endpoint, so `duk -S live` and `duk -S db`
  are comparable and `scripts/reconcile.sh` diffs like with like.

## Consequences

- **The endpoint string is also the watermark key** (`ops.load_watermark.endpoint`),
  so the switch orphans every existing watermark. That is the desired outcome — the
  stored rows are wrong and must be replaced — but it makes the next *incremental*
  run dangerous: with no watermark the loader would request each symbol with no
  `from` bound, hit the 5000-bar cap, and silently refill with ~20 years of history
  while everything older stayed split-adjusted. `load_prices` therefore **refuses**
  an incremental run when watermarks exist only under the retired endpoint, and
  points at the re-backfill runbook in [backfill.md](../backfill.md).
- Existing warehouses need a **full re-backfill**, not a top-up. See
  "Switching to the unadjusted price feed" in [backfill.md](../backfill.md).
- Raw prices reintroduce split-sized jumps into `core.daily_price`. This is correct
  and expected; `dq.check_outliers` already excludes moves that land on a split
  ex-date, so a correctly-loaded split does not raise a flag — but a **missing**
  corporate action now shows up as an outlier, which is a genuine improvement in
  detectability over the pre-adjusted feed.
- Symbols with no splits are unaffected, which is why the bug survived the initial
  test suite: every fixture used flat, split-free prices.
- **Volume is exposed to the same class of error, in the opposite direction.**
  `core.adjustment_factor` back-adjusts volume by `num/den` — a split *multiplies*
  pre-split share counts — so a volume that arrives already split-adjusted is
  inflated by the ratio **squared**, not driven toward zero. AAPL's 1990 volume would
  come out 12,544× too large. That has no vanish-to-zero tell and no DQ check behind
  it, so the loader now prefers `unadjustedVolume` over `volume` wherever a payload
  carries it (it is raw by definition), and `fafnir source probe-prices` reports a
  separate volume verdict.

  Whether FMP split-adjusts `volume` on `.../full` is not settled by its docs; the
  legacy v3 payload exposed both `volume` and `unadjustedVolume`, which only makes
  sense if the former is adjusted. Note the two feeds alone cannot always decide it:
  "never adjusts volume" and "adjusts it on both endpoints" look identical from the
  outside. `unadjustedVolume` is the tiebreaker where present; where it is absent the
  probe reports `volume_ambiguous` rather than guessing.

## Alternatives considered

- **Keep `.../full` and skip fafnir's split factor, applying only dividends.**
  Rejected: it re-imports the drift ADR 0001 exists to avoid — FMP re-scales its
  split-adjusted series on every new split, so `core.daily_price` would stop being
  immutable and point-in-time reproducible.
- **Derive raw prices by un-applying the split factor to `.../full`.** Rejected as
  strictly worse: same arithmetic, but the stored "raw" price becomes a function of
  the corporate-action table rather than an independent observation, so an error in
  a split ratio would corrupt raw history instead of only the adjusted view.
