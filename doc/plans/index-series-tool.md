# Plan: a tool for synthetic index and benchmark price series

- Status: **proposed** (2026-11-08). Nothing below is built. The three design decisions
  in *B. Decisions* are recommendations for the owner; each names the alternative it
  rejects and why. Phase 0 turns the accepted ones into ADRs.
- Scope: a standalone C++ command-line tool that creates and maintains synthetic index
  and benchmark price series (sector and industry indices, factor indices on
  fundamentals, total-return series with dividends and corporate actions), sourcing
  every market datum from fafnir by running `duk`, and writing CSV, JSON and Parquet.
  The changes fafnir and portable each need to receive it are part of the plan.
- What this plan rests on, in both repositories:
  - portable has already decided that this tool is separate from it, and has written
    the file it will read: [ADR 0030](https://github.com/rtrimble13/portable/blob/main/docs/adr/0030-default-benchmark-is-cash-at-zero.md)
    ("the owner will build a separate tool that constructs benchmark series"),
    [ADR 0020 R8](https://github.com/rtrimble13/portable/blob/main/docs/adr/0020-historical-price-import.md)
    ("the benchmark's total-return series must be sourced, not synthesized ... external
    to `portable`"), and the contract
    [docs/benchmark-series.md](https://github.com/rtrimble13/portable/blob/main/docs/benchmark-series.md).
  - fafnir has already built the inputs for it: `duk mc` and `duk ph --mcap` exist
    because "market cap is wanted daily, as the weights of custom cap-weighted indexes"
    ([ADR 0019, amendment](../adr/0019-derived-metrics-not-vendor-metrics.md#amendment-2026-11-daily-market-cap-and-the-share-factor)),
    and every read a client needs goes through `duk` over the `mart` seam
    ([ADR 0009](../adr/0009-mart-is-the-read-seam.md)).
- Related: portable issue [#6](https://github.com/rtrimble13/portable/issues/6) (`pert`
  benchmarks, `PORT-GIPS-G01`–`G05`), portable
  [ADR 0032](https://github.com/rtrimble13/portable/blob/main/docs/adr/0032-fafnir-through-duk-alone.md)
  (the pattern this tool copies for reading fafnir), [extending.md](../extending.md)
  (the recipe for a new fafnir source, which the ingest half follows).

---

## A. What the two codebases hold today

The findings that shape the design. Each is a fact about the code as of fafnir v3.1.7
and portable's `main` at 2026-11-08, with the file that shows it.

### A.1 fafnir: the inputs exist, the membership data does not

| Need | What exists | Where |
|---|---|---|
| Prices, exact | `duk ph SYMBOL --close --json --exact`: `YYYY-MM-DD` dates, stored decimals as JSON strings. One symbol per call. `--exact` is refused with `-f` other than `day`, with `-p`, `--summary` or a vendor source | `src/duk/cli.py:428-801`, [duk.md › Exact prices](../duk.md#exact-prices---exact) |
| Market cap and shares, many securities in one read | `duk mc --symbols-file F --exact`: `date, symbol, security_id, close_raw, shares_outstanding, share_factor, market_cap, currency, fiscal_date, known_from`. Point in time: the count steps on `known_from`, moves with splits between filings, never looks ahead. Days with no cap are **dropped** (funds, ETFs, secondary share classes, before the first filing, more than 730 days after the last). No float adjustment | `src/duk/datasource/db.py:444-583`, migration 0042, [duk.md › `mc`](../duk.md#mc-and-ph---mcap--daily-market-cap) |
| Dividends and splits | `duk ca SYMBOL --exact`: one row per event, `ex_date, action_type, split_numerator, split_denominator, dividend_amount, currency, record_date, payment_date, declaration_date, …`. As declared. One dividend per ex-date (a regular and a special on one day are **summed**); **no special/regular flag**. Sharadar rows have no record or pay date | `core.corporate_action`, `mart.v_corporate_action` (0033), `src/fafnir/ingest/sharadar_actions.py:98-145` |
| Spin-offs, mergers, delisting reasons | **Not reachable through `duk`.** Spin-offs are events in `core.vendor_security_event` (core only); the delist reason and acquirer are in `mart.v_security_lifecycle`, which no `duk` command reads | [data_dictionary.md](../data_dictionary.md), `v_security_lifecycle` |
| Adjusted prices | `duk ph --adj`: split **and dividend** back-adjusted (dividend factor `(P−D)/P` on the prior close), derived on read, exact Decimal at 28 digits. Every new action rescales the whole earlier history. No split-only series | [ADR 0001](../adr/0001-raw-prices-plus-adjustment-factors.md), `src/fafnir/ingest/adjustments.py:115-140` |
| Identity | `duk th`: every period a ticker named a security, with `security_id`; `sid:<id>` names a security exactly. Dead issuers kept, survivorship-complete from 1998 | [ADR 0016](../adr/0016-sharadar-identity-and-survivorship-floor.md), [duk.md › Naming a security by identifier](../duk.md#naming-a-security-by-identifier) |
| Calendar | `duk tc VENUE -s -e`: open days, ad-hoc closures. NYSE, NASDAQ, AMEX, BATS, OTC; no fallback for a venue without one | `ref.trading_calendar` |
| Sector and industry membership | `duk ls --sector X [--include-delisted]`: a screen of **today's** classification from a refresh-lagged snapshot, with **no `security_id` and no as-of date**. Classification is not effective-dated anywhere in the warehouse ("Classification is today's", [fundamentals.md](../fundamentals.md)) | `src/duk/datasource/db.py:594-814`, `mart.security_latest` |
| Factors | `duk fa SYMBOL --as-of D`: one security, month-end grain, the whole panel row (value, quality, safety, growth, payout; `market_cap`, `shares_outstanding_as_of`, `share_factor`). No bulk form | `mart.fundamental_panel`, `src/duk/cli.py:2629-2771` |
| Index membership history | Sharadar's SP500 constituents table (from 1957) is allowed by the file ledger's CHECK (migration 0030) but **has never been fetched**; `SharadarClient.TABLES` does not list it | `src/fafnir/sources/sharadar.py:177-197` |
| A place to store computed series | **None.** No table for index or benchmark levels, no `fafnir ingest <file>` command, no series concept. [extending.md](../extending.md) sketches the tidy shape a vendor time series would take (`core.economic_series` / `core.economic_observation`), never built | `src/fafnir/cli.py` |

Two consequences for the design. First, `duk` supplies everything a cap-, equal- or
price-weighted index of listed equities needs today, with one read per security for
prices and actions and one read for all market caps. Second, three things an index
needs are missing and are fafnir's to add: membership as of a date (with
`security_id`), spin-off and delisting events through `duk`, and a destination for the
levels. Phase 1 and Phase 5 are those additions.

### A.2 portable: the consumer is designed, waiting for the first file

- **Tables exist, commands do not.** `benchmark` (`return_type NOT NULL`, no default;
  `periodicity`; `is_net_of_withholding`; `rebalance_rule`; `is_blend`),
  `benchmark_component`, `benchmark_level`, `portfolio_benchmark` are in schema 0001.
  `services/benchmarks.py` resolves the benchmark in effect and refuses a price-only
  series (`PT-E-GIPS-PRICE-ONLY-BENCHMARK`). `pt benchmark show` exists. **Importing a
  series and assigning it have no command**: ADR 0030 says both "wait for the owner's
  benchmark tool", so that the first real file settles any detail the format leaves open.
- **The file format is written.** `docs/benchmark-series.md`: one JSON object keyed by
  benchmark name; `return_type`, `levels` as `{date: "decimal string"}`, `description`
  required; `periodicity`, `is_net_of_withholding`, `currency` optional; for a blend,
  `components` with effective-dated weights summing to exactly 1 and a `rebalance_rule`
  (`monthly | quarterly | annual | none | drift_band:N`). Levels, not returns. Byte
  deterministic. `FileProvider.benchmark_series` already reads the required fields.
- **portable itself must never synthesise a total return** from prices plus dividends
  (ADR 0006, `providers/fafnir.py:38-40`, issue #6). That rule is about *where* the
  methodology lives: in portable, an ad-hoc reinvestment would be an untested number
  beside the portfolio's. The tool planned here is the one place that owns the
  methodology, documents it, tests it, and declares `return_type` on its output. It is
  "a new provider that genuinely has total-return series", in issue #6's words.
- **Its C++ is acceleration only.** ADR 0008: every C++ path has a Python reference
  written first, a differential test, no native-only functionality, and money never
  crosses the pybind11 boundary as `double`. The whole product must be complete with
  `PORTABLE_BUILD_NATIVE=OFF`. The only C++ today is a probe. CI builds it on Linux and
  Windows.
- **Its conventions worth copying**: exact decimal text everywhere (ADR 0005: `prec=34`,
  `ROUND_HALF_EVEN`, decimals as strings in JSON, never rounded in JSON or CSV), an
  explicit `--as-of` on every command, structured output on stdout and everything else
  on stderr, stable error codes, exit codes `0/1/2/3/4/5/6`, and a no-float lint.

---

## B. Decisions

### B.1 Standalone repository, not a part of portable

**Recommendation: a new repository, sibling to `fafnir`, `portable` and `po`.** It
depends on `duk` being on `PATH` and on nothing else from either repository.

Why not inside portable:

1. **portable has already decided it.** ADR 0030 and ADR 0020 R8 both record the owner's
   decision that benchmark series are produced by a separate tool and *sourced* by
   portable. Issue #6 forbids the synthesis inside portable. Reversing that is a new
   ADR superseding two, and the reasons they give still hold.
2. **A C++-first CLI cannot satisfy portable's C++ rule.** ADR 0008 makes C++ optional
   acceleration of a normative Python implementation, with no native-only capability.
   An index tool written in C++ inside portable would either be Python with an
   optional C++ kernel (not what is asked), or a standing violation of the invariant
   that `PORTABLE_BUILD_NATIVE=OFF` is the complete product.
3. **The dependency points the other way.** portable *consumes* benchmark files; fafnir
   *consumes* index levels (workflow 1). A producer that lives inside one consumer and
   writes to the other is coupling in the wrong direction. portable's own rule for
   fafnir, "no CLI imports another CLI", is the same instinct.
4. **The domain is different.** portable is tax-aware portfolio accounting for one
   owner's accounts; an index has no accounts, no cash, no lots, no taxes, no
   settlement. The invariants that make portable trustworthy (cash conserved, lots sum
   to positions, append-only ledger) are not the invariants of an index (divisor
   continuity, weights sum to one, split invariance). Sharing a repository would mean
   sharing a lint, a CI and a release cadence for two things that never share a test.

Why not inside fafnir either: fafnir is Python and SQL over PostgreSQL, has no C++
toolchain, and its one rule for computed figures is "derive on read in `mart`"
(ADR 0019). A cap-weighted sector total-return index *could* be a `mart` function the
way `fn_daily_market_cap` is, and that is the honest alternative (B.1a below). But a
tool that reconstitutes membership on a schedule, resets weights, carries a divisor
forward and keeps its history stable when a dividend is corrected is stateful in a way a
view is not, and the methodologies wanted (equal weight, factor ranks, custom weights,
blends) are a program, not a query. fafnir's part is to *supply* and to *receive*.

**B.1a The alternative kept in view.** If, after Phase 2, the only series in use are
cap-weighted sector and industry indices, a `mart.fn_sector_index(sector, start, end)`
in fafnir would deliver them with no new repository. The plan is phased so that this
can still be chosen at the Phase 2 boundary.

**Conventions.** Match `po`'s C++ conventions as portable did, so a later merge anywhere
is a merge and not a rewrite: CMake ≥ 3.20, C++17, `FetchContent` with `GIT_SHALLOW`,
CLI11 2.4.2, nlohmann/json 3.11.3, toml++ 3.4.0, spdlog 1.14.1, Catch2 v3, option flags
with a project prefix. Build on Linux and Windows in CI from the first commit, because
the owner works on Windows.

**Name.** This plan calls it *the index tool*. A short command name is the owner's to
pick; the rest of this document uses `idx` as a placeholder only.

### B.2 Do not use `.port` files or portable's infrastructure; reuse its contracts

**Recommendation: no `.port` files, no import of `portable_core`.** Reuse portable's
*contracts* (the benchmark file format, the decimal and determinism rules, the CLI
shape) and portable's *public JSON output* where a portfolio's own weights are needed.

Why a synthetic portfolio in a `.port` file is the wrong model:

- **An index is a divisor, not a ledger.** Every index provider computes
  `Level_t = Σ pᵢ,t · sᵢ,t / D_t`, and every event (a rebalance, a reconstitution, a
  split under price weighting, a special dividend) is a divisor adjustment that keeps
  the level continuous. Modelling a rebalance as trades in an account means inventing
  proceeds, fees, lot relief and cash that the index does not have, then proving cash is
  conserved and lots sum to positions in a thing that has neither. The invariants would
  be fighting the model.
- **portable's pricing rule is the opposite of an index's.** portable values positions
  at unadjusted prices with explicit corporate-action transactions, because a portfolio
  really holds shares. An index tool also uses unadjusted prices and explicit actions
  (B.4), but its unit is the divisor-scaled basket, and its "corporate action" is a
  share-count and divisor update, not a lot adjustment.
- **The `.port` schema is portable's, versioned and migrated.** A C++ tool reading or
  writing it would couple to SQL that portable confines to `persistence/`, exactly the
  coupling portable's ADR 0032 removed from its own fafnir adapter. A schema bump in
  portable would break the tool silently.
- **Nothing in `.port` is needed.** Tax lots, accounts, relief methods and the tax engine
  are the whole of portable's weight, and none applies.

What *is* reused, by contract:

| Reused | How |
|---|---|
| `docs/benchmark-series.md` | One of the tool's output formats, exactly. The first real file is what lets portable build `pt benchmark import` and `pt benchmark assign` (ADR 0030) |
| ADR 0005's decimal rules | Exact decimal arithmetic, decimals as strings in JSON, full precision in CSV and Parquet, rounding only in the human table |
| CLAUDE.md invariant 6 | Every command takes `--as-of`; same inputs, identical bytes; no wall clock in output |
| ADR 0032's read pattern | Every fafnir read is one `duk` command with `--json --exact`; exit 2 or an `Error:` line stops the run; anything else is one security's gap, refused by name |
| The CLI shape and exit codes | `idx <noun> <verb>`, `--format`, `--dry-run`, `-v`, stable error codes, exit `0/1/2/3/4/5/6` |
| portable's public JSON | A *portfolio-weighted custom benchmark* (GIPS AO 24.C.29) needs the portfolio's asset-class weights at each period end. The tool takes them from a `pt … --format json` export passed as a file, never from the `.port` file itself |

### B.3 Constituent data: declarative definitions in version control, derived membership in a ledger the tool writes

**Recommendation.** Three kinds of file, all plain text, none a database:

1. **Index definitions** (TOML, one file per index, under version control). The
   methodology in full: universe rule, weighting, rebalance and reconstitution
   frequency, base date and base level, return type, corporate-action policy, data
   source and as-of. A definition is the *only* hand-written input for a rule-based
   index. Changing one is a methodology change and is treated as a new version of the
   series (C.6).
2. **Explicit membership files** (CSV, effective-dated, keyed by `security_id`) for
   indices whose constituents are *declared* rather than *derived*: a replication of a
   published index, a hand-picked basket, a custom list. Columns:
   `security_id, valid_from, valid_to, weight_override, note`. Tickers are not keys:
   fafnir reuses them (`duk th` is how a ticker becomes a `security_id` at authoring
   time, and the file records the id).
3. **The membership ledger** (CSV or Parquet, written by the tool, never by hand): for
   each reconstitution date, the constituents selected, the inputs that selected them
   (`market_cap`, rank, classification as read that day) and the `duk` facts behind
   them (`known_from`, `share_factor`). This is the point-in-time record. It is the
   evidence for an audit, the input a re-run checks itself against, and, from the first
   live run onward, the only place a classification *as of a date* exists at all,
   since fafnir does not keep one (A.1).

The store on disk, one directory per index:

```
indices/
  us-energy-tr.toml                 # the definition (hand-written)
  us-energy-tr/
    membership.csv                  # the ledger (tool-written, append-only by date)
    weights.csv                     # shares and weights set at each rebalance
    divisor.csv                     # every divisor change, with its cause
    levels.csv                      # the series: date, price level, total-return level
    manifest.json                   # definition hash, duk version, fafnir migration,
                                    # warehouse as-of, input hashes, tool version
```

Why files and not a database: the state is small (an index is a few thousand
membership rows and ten thousand levels), it diffs in git, it needs no server, and the
owner already runs one database that is the source of truth for market data. A second
database would be a second thing to back up and migrate for data that is entirely
derivable from the first plus the definition. If the series count grows past what a
directory tree serves, SQLite is the obvious step, and the file layout above maps onto
tables one to one.

**The incremental update.** `idx update <index> --as-of D` reads the manifest, replays
nothing, and extends the series from its last date to `D`, *after* checking that the
warehouse still agrees with the ledger for a short trailing window (the last
reconstitution's inputs and the last twenty sessions' prices). A disagreement (a
corrected price, a restated share count, a new dividend inside the window) is reported
by name and the run stops; `--rebuild` recomputes the whole series from the base date
and reports every level that changed. This is portable's `apply_or_rebuild` and
`pt validate` instinct applied to an index: a silently drifting history is the failure
mode, so the tool either proves the history still holds or rebuilds it visibly.

**Look-ahead.** Market cap from `duk mc` is point in time by construction (A.1).
Factors from `duk fa --as-of` are point in time (the panel's look-ahead invariant).
Classification is **not**: a sector index reconstituted on a date in 2015 using today's
sector is survivorship- and reclassification-biased for its history, and the tool must
say so on every series whose universe rule uses classification, until fafnir has an
effective-dated classification (Phase 6). From the first live run forward, the ledger
records what the warehouse said that day, so the live history is clean.

### B.4 Prices: unadjusted closes and explicit actions, with `--adj` as the cross-check

The tool reads **raw** closes (`ph --exact`, `mc`'s `close_raw`) and the action events
(`ca --exact`), and applies splits and dividends itself. It does not build on
`ph --adj`, for two reasons: fafnir's adjusted series rescales its whole history
whenever an action lands or is corrected (ADR 0001), so a level computed from it would
not be stable; and a total-return index reinvests the day's dividends across the
*index*, not into the paying stock, which is what per-security adjustment does.

`ph --adj` is kept as the independent check: a single-constituent total-return index
must equal the ratio of that security's adjusted closes, to the rounding fafnir
documents. That test is the differential test for the whole dividend path.

### B.5 Arithmetic: exact decimal, with a Python reference as the oracle

**No binary floating point in any price, weight, divisor or level path**, the same rule
as fafnir's `NUMERIC` and portable's invariant 1. In C++ that means a decimal library,
not `double`. Recommendation: **libmpdec (mpdecimal)**, the library behind Python's
`decimal` module, at a 34-digit context with `ROUND_HALF_EVEN`, portable's ADR 0005
context exactly. The point is not precision for its own sake: with the same library
and context, a Python reference implementation of the index arithmetic produces the
*same digits*, so the C++ can be tested against an oracle written in the language the
owner's other two repositories trust, and a disagreement is a bug rather than a
tolerance debate. Fixed-point `int128` was considered and rejected: a divisor is a
quotient of sums of products and does not fit a fixed scale, and its rounding would not
match any reference.

The Python reference is a few hundred lines in the tool's own `tests/reference/`, used
only by tests. It is not a second product and has no CLI. Every methodology in C.2
has a golden case computed by it.

### B.6 Output: CSV and JSON always; Parquet as an optional build component

CSV and JSON need no dependency and are always built. **Parquet needs Apache Arrow
C++**, which is not practical to fetch at configure time and is heavy on Windows.
Recommendation: `IDX_BUILD_PARQUET` (default `ON` where Arrow is found, via vcpkg on
Windows and the distribution's package on Linux), with a clear runtime refusal
("built without Parquet; use `--format csv|json`") when it is off. Parquet columns for
money are Arrow `decimal128(38, 18)`, so the exact decimal survives the file; a
`double` column would silently undo B.5 at the last step. The CI matrix builds one job
with Parquet off, as portable builds one job with its extension off, so the refusal is
a tested path.

JSON has two shapes: the tool's own envelope (every series with its definition,
membership and manifest) and portable's `benchmark-series.md` file, which is a
selection of the former with the field names portable reads.

---

## C. The tool

### C.1 Commands

```
idx define   <file.toml>                 # validate a definition; print the resolved methodology
idx build    <index> --as-of D           # compute the whole series from the base date
idx update   <index> --as-of D           # extend it; verify the trailing window; refuse or stop
idx members  <index> --on D              # constituents and weights on a date, with their inputs
idx show     <index> [--from --to]       # levels and returns
idx export   <index> --format csv|json|parquet|portable-benchmark [-o PATH]
idx blend    <file.toml>                 # a series of series: components, weights, rebalance rule
idx verify   <index>                     # the invariants in C.5 over the stored series; the --adj cross-check
idx fafnir   <index> --out FILE          # the file `fafnir ingest series` reads (Phase 5)
```

Every command: `--as-of` (no implicit "now"), `--format table|csv|json`, `--dry-run` on
anything that writes, `-v/-vv`, `--quiet`, `--no-color`. Structured output on stdout,
everything else on stderr. Exit codes as portable's: `0` ok, `1` generic, `2` usage,
`3` store or file, `4` validation (a definition that does not resolve), `5` data
unavailable (`duk` absent, the warehouse unreachable, a price missing), `6` the
trailing-window check failed.

### C.2 Methodologies

| Weighting | Shares `sᵢ` at a rebalance | Between rebalances | Notes |
|---|---|---|---|
| Market-cap | ∝ `market_cap` from `duk mc` (count × share factor × close) | drift with price; count updates per `share_update` | `share_update = rebalance` (index-provider convention: counts fixed between scheduled updates, splits applied as they occur) or `daily` (every `known_from` step, which is what `mc` returns). Disclosed in the manifest |
| Equal | `sᵢ = (L / n) / pᵢ` | drift | A rebalance resets to equal; a reconstitution adds at equal weight |
| Price | `sᵢ = 1` | — | A split adjusts the divisor so the level is continuous (Dow convention) |
| Custom | from `weight_override` or a weights file per rebalance date | drift or held, per definition | Weights must sum to exactly 1 on each date, refused otherwise |
| Factor | rank or score from `duk fa --as-of` fields (`earnings_yield`, `book_to_price`, `gross_profitability`, …), top/bottom fraction, then one of the above within the selection | drift | Month-end grain only, because the panel is monthly; a factor index with a weekly rebalance is refused |

**Universe rules** (for a derived membership): `sector = …`, `industry = …`,
`cap_rank <= N`, `min_market_cap`, `min_history_days`, `exclude_asset_types = [etf,
fund]`, `primary_class_only = true` (so GOOG and GOOGL are not both held; `duk mc`
already caps only the primary class), an explicit membership file, or a combination.

**Rebalance and reconstitution** are two schedules: `rebalance = daily | weekly |
monthly | quarterly | semi-annual | annual | none | drift_band:N`, and
`reconstitute = monthly | quarterly | semi-annual | annual | never`. Schedule dates are
trading days from `duk tc` for the index's `calendar` (`last` or `first` session of the
period, declared); weights set at the close of the schedule date apply from the next
session. `none` is a buy-and-hold basket; `drift_band:N` mirrors portable's rule for
blends. Monthly rebalanced and drifting versions of the same definition must differ,
which is portable's `test_blend_rebalancing_is_explicit` test, run here on the producer.

**Return types.** `price` (dividends ignored), `total_return` (the day's dividends,
`Σ dᵢ,t · sᵢ,t / D_t`, reinvested in the index on the ex-date, the standard provider
method), `net_total_return` (a declared withholding rate applied to dividends, for a
series that must state `is_net_of_withholding = true`). Every series computes both the
price and the total-return level; `return_type` on an export says which one is the
series. `TR ≥ PR` on every date, with equality while no dividend has gone ex.

### C.3 Corporate actions and events

| Event | Treatment | Source | Gap |
|---|---|---|---|
| Split | `sᵢ × ratio`, `pᵢ / ratio`; value unchanged; divisor unchanged except under price weighting | `duk ca --type split` | — |
| Ordinary cash dividend | price index: nothing; TR index: reinvested on the ex-date | `duk ca --type dividend` | — |
| Special dividend | provider practice is a price (divisor) adjustment. fafnir has **no special flag** and sums two dividends on one ex-date (A.1). Policy: `special_dividend = none` (treat all as ordinary, disclosed) or `threshold:P` (a dividend over `P`% of the prior close adjusts the divisor). Refuse a dividend ≥ the prior close by name | `duk ca` | fafnir: a `is_special` flag needs a vendor source; Sharadar ACTIONS does not distinguish |
| Delisting of a constituent | removed at the last traded close, proceeds spread over the remaining constituents by divisor adjustment on the next session; `delisting_price = last | zero` for a bankruptcy. Disclosed per event | `duk th` (`delisted_date`) today; the reason needs Phase 1 | `duk lf` (lifecycle), Phase 1 |
| Spin-off | **cannot be handled from `duk` today**: the parent's raw close drops on the ex-date with no action row, so a sector index would book a loss that did not happen. fafnir's own `--adj` has the same blind spot. Until Phase 1 exposes the event, the tool refuses to carry a constituent across a date on which the warehouse holds a spin-off event for it, which it can only know once that event is readable. Interim: flag a raw-close gap larger than `gap_threshold` with no action explaining it, exit 6 | — | Phase 1: `duk ev` over `core.vendor_security_event` through a `mart` view; and a probe of whether Sharadar's ACTIONS spin-off rows carry a usable value |
| Missing bar on an open session | carry the last close for up to `max_stale_sessions` (default 1), flagged; beyond it, exit 5 naming the security and the dates | `duk tc` vs `duk ph` | — |
| Ticker change, venue transfer | nothing: the key is `security_id` | `duk th` | — |

### C.4 Reading fafnir

One `duk` command per read, `-S db --json --exact`, no shell, `FAFNIR_DSN` passed
through to the child only if the tool was given one. Dates must be `YYYY-MM-DD`, every
number a string, every key known; anything else is refused as unreadable, as portable's
adapter does. Reads per daily update of an `n`-constituent index: one `mc` for all
caps and raw closes, `n` `ph` calls only for constituents `mc` dropped (funds, ETFs,
pre-filing days), one `ca` per constituent with an ex-date in the window (Phase 1 makes
this one call), one `tc`. At about 0.25 s a process, a 500-name index updates in under
a minute and backfills 25 years in a few minutes, so bulk forms of `ph` and `ca` are a
convenience, not a prerequisite. The manifest records `duk --version` and the fafnir
migration the warehouse reports, so a series can say which warehouse made it.

### C.5 Invariants, each a test

1. **Determinism**: the same definition against the same warehouse facts gives identical
   bytes; the manifest's input hashes prove which facts.
2. **Divisor continuity**: a rebalance, reconstitution, delisting or special dividend
   leaves the level on its date unchanged.
3. **Split invariance**: inserting a split into a fixture changes no level (price
   weighting excepted, where the divisor absorbs it and the level is still unchanged).
4. **Weights sum to one** at every rebalance, exactly, in decimal; cap weights are
   proportional to `mc`.
5. **Single-constituent identity**: an index of one security has price levels
   proportional to its raw closes and total-return levels equal to its `--adj` close
   ratio within fafnir's documented rounding.
6. **TR ≥ PR**, with equality until the first ex-date.
7. **Rebalancing is explicit**: monthly and drifting variants differ.
8. **No float**: a lint over the C++ sources refuses `double` and `float` outside a
   named allow-list (the Arrow binding's schema call and nothing else).
9. **Python oracle agreement**: every golden case equals the reference's digits.

### C.6 Versioning a series

A series is identified by its name and a **definition hash**. A change to the
definition (weighting, frequency, universe rule, action policy) is a new version, and
the tool writes it beside the old one rather than over it; the manifest says which
version is current and from which date. A re-run that changes levels because the
warehouse changed (a correction) keeps the version and records the diff. The export to
portable carries `description` with the version, since a benchmark change is a
disclosed event there (`PORT-GIPS-G04`), and `inception_date` (the first live
calculation) beside the history's first date, because a history computed before the
index existed is backtested and must say so (`PORT-GIPS-J04`). That field is not in
`benchmark-series.md` today; adding it is a one-line portable docs change, raised with
the first file.

---

## D. Workflows

**Workflow 2, research outside fafnir**, is the tool alone: define, build, export as
CSV, JSON or Parquet, and compare against anything (`duk rc` reads the CSV directly).
The portable export is the special case where the series becomes the owner's
benchmark: `idx export … --format portable-benchmark`, then, in portable,
`pt benchmark import` and `pt benchmark assign` (to be built against the first file,
ADR 0030).

**Workflow 1, levels loaded back into fafnir**, adds Phase 5: `idx fafnir <index>`
writes the file `fafnir ingest series` reads; the nightly job runs the update and the
ingest after the prices and actions have loaded and `fafnir adjust` has run; the
levels become readable through `mart` and a `duk` command, and usable by the factor
panel (sector-relative returns, momentum against a sector, a market series for beta).
fafnir receives *levels and the membership ledger*; it does not recompute them, so
the warehouse never carries a second implementation of the methodology (the rule
ADR 0019 applies to vendor metrics, applied here to the tool's).

---

## E. Phases

Each phase ends with its tests green on Linux and Windows and its docs in the same
change. Issues are opened per item when a phase starts; this plan is brought current
when one closes.

### Phase 0 — Decide (this plan)

- ⬜ The owner accepts or amends B.1–B.6; the name is chosen; the repository is created.
- ⬜ ADR in the new repository: standalone, `duk`-only reads, exact decimal, files not a
  database (B.1–B.3, B.5).
- ⬜ fafnir ADR 0020: computed series as a first-class ingested source (the Phase 5
  schema and precedence), proposed now so Phase 1's `duk` additions and Phase 5's
  tables are designed together.
- ⬜ Two probes in fafnir, recorded in [backfill.md](../backfill.md): whether Sharadar's
  SP500 table is worth fetching (membership history from 1957) and what its ACTIONS
  spin-off rows carry.

### Phase 1 — `duk` enablers (fafnir)

Small, each with a pinned-JSON integration test like `test_db_portable_reads_integration.py`:

- ⬜ `duk ls --sector X --industry Y --ids --as-of D`: `security_id` on every screen row,
  and membership judged on `D` (listed and trading that day per `v_security_lifecycle`,
  classification as held). The as-of is honest about classification being today's
  until Phase 6.
- ⬜ `duk ca --symbols-file F`: actions for many securities in one read, `security_id`
  on each row (it is already a column).
- ⬜ `duk ph --symbols-file F --close --exact`: raw closes for many securities, for the
  constituents `mc` drops. (Alternatively `mc --keep-uncapped`; decide on the probe of
  which is cheaper in SQL.)
- ⬜ `duk lf SYMBOL...`: `mart.v_security_lifecycle` as a command (first and last trade
  date, delist reason, acquirer).
- ⬜ `duk ev SYMBOL...`: spin-offs, mergers and acquisitions from
  `core.vendor_security_event` through a new `mart` view, a deliberate grant under
  ADR 0009.
- ⬜ `duk fa --symbols-file F --as-of D`: the factor rows of many securities at one
  month-end.

### Phase 2 — The engine

- ⬜ Repository skeleton on `po`'s conventions; CI on Linux and Windows; the no-float
  lint; libmpdec vendored or fetched.
- ⬜ `duk` reader with the exact-JSON contract and the refusal rules.
- ⬜ Definitions (TOML) and their validation; the store layout and manifest.
- ⬜ The divisor model; cap, equal and price weighting; the calendar schedules; splits
  and ordinary dividends; price and total-return levels.
- ⬜ `build`, `update` with the trailing-window check, `show`, `members`, `verify`,
  `export` to CSV and JSON.
- ⬜ The Python reference and the golden cases; invariants C.5 1–7 and 9.
- **Decision point (B.1a):** with sector cap-weighted indices working end to end, confirm
  that the remaining methodologies justify the tool over a `mart` function.

### Phase 3 — Membership

- ⬜ Derived universes (sector, industry, cap rank, filters) on Phase 1's `ls --ids
  --as-of`; the membership ledger; reconstitution schedules; delisting handling on
  `duk lf`.
- ⬜ Explicit membership files; the `duk th` authoring helper that turns a ticker list
  into ids with their valid periods.
- ⬜ Spin-off handling on `duk ev`, or the gap refusal if Phase 1's probe found nothing
  usable.
- ⬜ Classification look-ahead disclosure on every series whose rule uses it.

### Phase 4 — Factor indices, blends, the portable export, Parquet

- ⬜ Factor selection on `duk fa --symbols-file --as-of`; month-end only.
- ⬜ Custom weights; `idx blend` over the tool's own series and external level files;
  `drift_band:N`.
- ⬜ `--format portable-benchmark`, byte-identical to `benchmark-series.md`; then, in
  portable, `pt benchmark import` and `pt benchmark assign` against the first real file
  (ADR 0030), and the `inception_date` field added to the format.
- ⬜ Parquet behind `IDX_BUILD_PARQUET`, decimals as `decimal128`; the Parquet-off CI job.

### Phase 5 — Levels back into fafnir

- ⬜ Migration: `core.series(series_id, name, version, kind, return_type, currency,
  definition_hash, methodology_doc, source)` and `core.series_level(series_id,
  level_date, level, loaded_at, ingestion_run_id)`, with `core.series_member` for the
  ledger if the panel wants it; `source = 'index-tool'` as a new provenance value
  under ADR 0015; `mart.v_series_level` as the read; least-privilege grants and the
  `test_migrations_least_privilege.py` extension.
- ⬜ `fafnir ingest series --file F`: idempotent, logged in `RunLog`, a run that changes
  an existing level keeps the replaced value in `ops.vendor_correction` as a price
  correction does.
- ⬜ `duk sl <series> [-s -e] [--exact]`: the levels, read through `mart`.
- ⬜ DQ checks on a series: a gap against its calendar, a daily return outside a band.
- ⬜ The nightly step in `scripts/daily_update.sh`, after `fafnir adjust` and before
  `refresh-marts`; the panel's first use of a series (sector-relative return) under its
  own issue.

### Phase 6 — Effective-dated classification (fafnir, separately decided)

- ⬜ Record sector and industry per security with `valid_from`/`valid_to`, written when
  the nightly observes a change; `duk ls --as-of` reads it. Removes the look-ahead the
  tool discloses from Phase 3 on. Sharadar's SP500 table, if Phase 0's probe says yes,
  lands here as the first explicit membership history.

---

## F. Open questions for the owner

1. **Name and repository.** `idx` is a placeholder.
2. **Cap-weighting convention.** Share counts fixed between scheduled updates (the
   provider convention, `share_update = rebalance`) or every filing (`daily`, what `mc`
   returns)? The plan defaults to `rebalance` and makes it a per-index choice.
3. **Special dividends.** With no flag in the warehouse, treat all as ordinary and
   disclose, or adopt a threshold rule? The plan defaults to ordinary with a disclosed
   `threshold` option.
4. **Parquet's dependency.** Arrow via vcpkg on Windows is a real setup cost. Is Parquet
   wanted for the first release, or is CSV plus JSON enough until a consumer needs it?
5. **The B.1a boundary.** If the first year's use is only sector and industry
   cap-weighted indices, is a `mart` function in fafnir preferred over a second
   repository? Phase 2 is laid out so the choice is still open then.
6. **Float adjustment.** SF1 carries no float, so every cap weight is full market cap.
   Is a float-adjusted variant wanted, and from which vendor would the float come?
