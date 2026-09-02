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

## Library use

The DataFrame contracts are identical across sources, so downstream code is
source-agnostic. `duk.datasource.db` and `duk.datasource.live` expose
`price_history(...)` and `screen(...)` returning the same shapes; the pure compute
modules (`duk.indicators`, `duk.return_utils`, `duk.rates_utils`, `duk.stats`)
operate on those frames unchanged.
