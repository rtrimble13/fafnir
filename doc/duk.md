# duk — the data-access CLI

`duk` is fafnir's primary read client. It keeps its original command surface and
adds a global `--source/-S` to choose between the warehouse and the live API.

## Sources

| Source | Reads from | When to use |
|---|---|---|
| `db` (default when a DSN is set) | fafnir PostgreSQL `mart`/`core` | normal use; fast, offline, point-in-time stable |
| `live` | FMP API directly | ad-hoc symbols not yet in the warehouse, reconciliation, no-DB use |

The source is a **group-level** option, so it comes before the subcommand:

```bash
duk -S db   ph AAPL --adj
duk -S live ph AAPL --adj
```

The default is `[general].default_source` in `~/.dukrc` / `~/.fafnirrc` (which
defaults to `db` when a database DSN is configured, else `live`).

## Configuration

`duk` reads `~/.dukrc` (TOML). Relevant keys:

```toml
[api]
fmp_key = ""              # or FMP_API_KEY env (live mode)

[database]
dsn = "host=localhost dbname=fafnir user=fafnir_app"   # or FAFNIR_DSN env (db mode)

[general]
default_source = "db"     # "db" or "live"
log_level = "info"
```

For db mode, point `duk` at the least-privilege `fafnir_app` role (mart read-only).
Every relation `duk` reads is a `mart` (or `ref`) one, so that role is sufficient --
see [ADR 0009](adr/0009-mart-is-the-read-seam.md). It was not before migration 0020,
when symbol resolution still read `core`.

## Commands

| Command | Description | db mode | live mode |
|---|---|---|---|
| `ph SYMBOL` | price history (OHLCV, raw or `--adj`) | ✅ warehouse | ✅ FMP |

`ph` without `--adj` means **raw** — prices as they actually traded, so a split shows
up as a real jump. Both sources honour that: `db` reads `mart.v_daily_price_raw`, and
`live` reads FMP's `historical-price-eod/non-split-adjusted` (not `.../full`, which
is already split-adjusted). That is what makes the two sources comparable, and what
`scripts/reconcile.sh` relies on. With `--adj`, `db` reads
`mart.v_daily_price_adjusted` (split + dividend, point-in-time stable) while `live`
reads FMP's `dividend-adjusted` endpoint — close, but a moving vendor snapshot
rather than a reproducible series, so small differences on old dates are expected.
| `ls` | list/screen securities, sectors, industries | ✅ `mart.security_latest` / `ref.*` | ✅ FMP |
| `ls SYMBOL\|NAME` | everything the warehouse holds on one company | ✅ `mart.v_security_*` | ✖ db only |
| `yc` | treasury yield curve | ⤳ falls back to live | ✅ FMP |
| `rc` | return calculations from an input file | source-agnostic | source-agnostic |
| `ti` | technical indicators (sma/ema/rsi/macd) from a file | source-agnostic | source-agnostic |

> `yc` is **live-only** until the economic-series fast-follow adds treasury data to
> the warehouse; in db mode it logs a warning and reads live (requires an FMP key).

## Examples

`-n` counts **bars**, not calendar days: `-n 5` returns five trading periods at the
selected `--frequency`, whichever weekends and holidays fall inside them. The query
window is widened accordingly and the series trimmed back to `-n` rows, so `-n` pairs
with `--start-date` (the first N bars from that date) or `--end-date` (the last N up
to it); all three together is an error.

```bash
# Adjusted daily closes from the warehouse
duk -S db ph AAPL --adj --close

# Weekly OHLC, last 52 weeks
duk -S db ph AAPL -f week -n 52 --ohlc

# The first five trading days of 1990
duk -S db ph AAPL --start-date 1990-01-01 -n 5

# Prices print at 2dp by default; -p widens it
duk -S db ph AAPL --adj -p 4

# Screen tech names by market cap (warehouse snapshot)
duk -S db ls --sector Technology --market-cap ">1000000000"

# Export to CSV, then compute a 20-day SMA (pure compute, no source needed)
duk -S db ph AAPL --close -o prices.csv
duk ti sma -i prices.csv -c close -w 20

# Reconcile a symbol: same series from both sources should match
diff <(duk -S db ph AAPL --close --csv) <(duk -S live ph AAPL --close --csv)
```

### Price formatting

`ph` prints prices at two decimals (`250.00`). Sub-penny prices are **not** floored
to `0.00`: anything too small for the requested precision keeps four significant
digits instead (`0.0003123`), so a back-adjusted series with a long split record
still reports a real traded price. `-p/--precision` changes the decimal places.

Formatting is applied at the output boundary only. The DataFrame keeps full
precision, `--json` stays numeric, and `-o` files are written unrounded unless you
pass `-p` explicitly -- they feed the `ti`/`rc` compute path, where quantizing a
sub-dollar series would introduce real error.

## `ls QUERY` — one company, everything held

`ls` with a positional argument switches from listing many securities to
summarizing one:

```bash
duk -S db ls AAPL                 # by ticker
duk -S db ls "Apple Inc"          # by company name (case-insensitive)
duk -S db ls AAPL --json          # one nested object
duk -S db ls AAPL --csv -o a.csv  # one flat row
```

Four sections, always all four — "not loaded" and "nothing to report" are
different claims about a warehouse, so a section is never silently omitted:

1. **Meta** — ticker, name, sector, industry, market cap, beta, listing status,
   IPO/delisting dates, CIK/ISIN/CUSIP.
2. **Price history** — coverage span and bar count, last close and volume, the
   52-week range, trailing 1M/3M/6M/YTD/1Y returns, annualised volatility and max
   drawdown. Coverage is counted on the **raw** bars; the statistics are computed
   on the **adjusted** series, which is the basis a current price should be
   compared against.
3. **Corporate actions** — split and dividend counts and boundaries, the trailing
   twelve months of dividends and the yield that implies, and adjustment-factor
   state.
4. **Data quality** — open flags for that security, grouped by check, naming the
   bars they point at. Full triage stays with `fafnir dq list --detail --symbol X`
   on the warehouse host; see [ADR 0009](adr/0009-mart-is-the-read-seam.md) for why
   the summary sees counts and keys but not `detail`.

**Fundamentals** is a fifth section that reports "not loaded" until that milestone
lands. It is a capability probe, not a stub: when `mart.v_security_fundamentals_latest`
exists, the section starts rendering with no `duk` release. `duk` derives the
multiples it prints (P/E, P/S, P/B, net margin, ROE) from the statements shown
beside them rather than storing vendor ratios, so the arithmetic is checkable; a
quarterly statement is annualised ×4 and says so.

### Resolution

Ticker first, name second — a ticker is an exact intent and never loses to a
substring match, so `ls CAT` is Caterpillar even if another company has "cat" in
its name. The ladder is: live ticker → primary symbol → a ticker the company
traded under before a rename (reported as *"Matched a former ticker: …"*) → company
name, exact match first, then `ILIKE`.

A name matching several companies prints a did-you-mean table and exits 1. It never
picks the first — answering confidently about the wrong company is the one failure
that cannot be spotted from the output.

### Rules and exit codes

| Situation | Behaviour |
|---|---|
| `QUERY` + a screening or list flag | error, exit 1 — one selects a company, the other selects many |
| `QUERY` + `--summary` | error, exit 1 — `--summary` means "row count" in list mode; the profile *is* the summary |
| no match | `No company found matching 'X'.` on stderr, exit 1 |
| several name matches | candidate table on stderr, exit 1 |
| `-S live` | error, exit 1 — three of the four sections describe the warehouse, so falling back would answer a different question |
| `-n/--limit` | ignored |

`--json` in profile mode is a **nested object**, where list mode emits an array of
records. That divergence is deliberate: flattening the report into a record array
would destroy the DQ breakdown. `--csv` gives a single flat row of scalars, in which
the DQ table collapses to an `open_dq_flags` count.

## Library use

The DataFrame contracts are identical across sources, so downstream code is
source-agnostic. `duk.datasource.db` and `duk.datasource.live` expose
`price_history(...)` and `screen(...)` returning the same shapes; the pure compute
modules (`duk.indicators`, `duk.return_utils`, `duk.rates_utils`, `duk.stats`)
operate on those frames unchanged.

The company summary follows the same split. `duk.datasource.db.resolve_company()`
and `.company_summary()` do the I/O and return plain dicts; `duk.company_summary`
is pure functions over those dicts (`build_summary`, `render_text`, `render_flat`,
`render_candidates`) with no SQL and no click, so the whole report can be built and
asserted without a database. `build_summary`'s output is the `--json` contract.
