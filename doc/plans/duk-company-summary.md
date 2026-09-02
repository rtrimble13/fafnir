# Plan: a per-company summary for `duk ls`

- Status: **phase 1 implemented** (migration 0020, `mart` seam, ADR 0009);
  phases 2-3 (`ls QUERY` itself) not started
- Scope: `duk -S db ls <TICKER>` / `duk -S db ls "<company name>"`
- Touches: `sql/migrations/0020_*`, `src/duk/datasource/db.py`, `src/duk/cli.py`,
  new `src/duk/company_summary.py`, `doc/duk.md`, `doc/data_dictionary.md`, tests

## What is being asked for

One command that answers "what does fafnir actually hold on this company?" in a
single screen:

1. **Meta** — ticker, name, market cap, sector, industry, venue, listing state.
2. **Price history + corporate actions** — coverage and summary statistics.
3. **Fundamentals** — summary stats, once that milestone lands.
4. **DQ** — the open flags in the queue for that symbol, if any.

Today `duk ls` has no positional argument at all: it lists (`--sectors`,
`--industries`, actively-trading) or screens (`--sector`, `--market-cap`, …).
Everything below is additive to that surface.

## Command surface

```bash
duk -S db ls AAPL                    # by ticker (resolver ladder, incl. renamed tickers)
duk -S db ls "Apple Inc"             # by company name (case-insensitive)
duk -S db ls AAPL --json             # the whole summary as one nested object
duk -S db ls AAPL -o aapl.json --json
```

`ls` gains one optional positional argument, `QUERY`. Its presence selects
**profile mode**; its absence leaves list/screen mode exactly as it is today.

### Rules

| Situation | Behaviour |
|---|---|
| `QUERY` + any screening/list flag (`--sector`, `--market-cap`, `--sectors`, …) | error, exit 1: *"a QUERY selects one company; screening flags select many — pick one"* |
| `QUERY` + `--summary` | error, exit 1 (`--summary` means "row count" in list mode; the profile *is* the summary) |
| `QUERY` matches nothing | `No company found matching 'XYZ'.` on stderr, exit 1 |
| `QUERY` matches several names | print a candidate table (SYMBOL, NAME, EXCHANGE, STATUS) and exit 1 — a "did you mean", never a guess |
| `-S live` | error, exit 1: *"ls QUERY reads the warehouse; re-run with -S db"* (see [Live mode](#live-mode)) |
| `-q/--quiet` | suppress stdout; `-o` still writes |
| `-v/--verbose` | unchanged (console logging) |

`--limit/-n` is ignored in profile mode (documented, not an error — it is a
group-shaped flag people leave in their shell history).

### Resolution ladder

Ticker first, name second — a ticker is an exact, unambiguous intent and must never
lose to a substring name match.

1. **Live ticker** — `symbol_xref` where `valid_to IS NULL`, primary first.
2. **Primary symbol** — `core.security.primary_symbol`, preferring the live issuer.
3. **Historical ticker** — `symbol_xref` where `valid_to IS NOT NULL` (a ticker the
   company traded under before a rename). Rendered with a note:
   *"Matched a former ticker: XYZ traded as XYZ until 2024-03-11."*
4. **Company name** — exact (case-insensitive) match wins outright; otherwise
   `ILIKE '%query%'`, ranked prefix-match-first then alphabetically, capped at 20
   candidates for the disambiguation table.

Steps 1–3 are the ladder `duk.datasource.db._resolve_security_id` already
implements (and which is duplicated from `fafnir.db.repository.resolve_security_id`
on purpose — see that module's comment). **Do not add a fourth copy**: extend the
existing function with an optional `by_name` fallback, and keep the three SQL
constants as the single statement of the ladder.

## The read seam: six new `mart` views

The summary needs facts from `core` (prices, actions, adjustment factors) and from
`ops` (the DQ queue). Neither is reachable by the role duk-db is *documented* to
connect as: migration `0001` grants `fafnir_app` schema `USAGE` on `mart` and `ref`
only, and grants **nothing at all on `ops`** to any read role.

So the summary reads `mart`, and one migration adds the views that make that
possible — plus the two from ADR 0008 §4 that finish the seam. Views are unmaterialized (a profile lookup must be current, unlike
`mart.security_latest`, which is a scheduled screening snapshot) and are owned by
the migrating role, so they run with the owner's privileges — that is what lets a
`mart`-only role read an `ops`-derived aggregate without ever being granted `ops`.

> **This is a deliberate crossing of the role boundary** and deserves its own short
> ADR (0009) alongside the migration. The mechanism is verified, not assumed: on the
> scratch cluster above, `fafnir_app` reads the `mart.v_security_dq_open` definition
> below and gets its counts back, while `SELECT … FROM ops.data_quality_flag` as the
> same role still fails with `permission denied for schema ops`. The exposure is
> narrowed on purpose: the DQ
> view emits **counts and dates only**, never `detail`/`record_key`, so the read
> seam cannot be used to walk the queue. Flag detail stays behind
> `fafnir dq list`, which runs as `fafnir_ingest`.

### `sql/migrations/0020_company_summary_marts.up.sql`

**1. `mart.v_symbol_lookup`** — a thin passthrough that puts the resolver ladder in
`mart`:

```sql
CREATE VIEW mart.v_symbol_lookup AS
SELECT x.symbol, x.security_id, x.is_primary, x.valid_from, x.valid_to, x.source
FROM core.symbol_xref x
UNION ALL
SELECT s.primary_symbol, s.security_id, TRUE, '1900-01-01'::date, NULL, s.source
FROM core.security s
WHERE NOT EXISTS (SELECT 1 FROM core.symbol_xref x2
                   WHERE x2.security_id = s.security_id AND x2.symbol = s.primary_symbol);
```

Same rows, same precedence as the current three-query ladder — proved by a test
(below), not by assertion.

**1b. `mart.v_daily_price_raw`** — the raw counterpart to `v_daily_price_adjusted`,
so the unadjusted series has a `mart` name too:

```sql
CREATE VIEW mart.v_daily_price_raw AS
SELECT security_id, trade_date, open, high, low, close, volume
FROM core.daily_price;
```

Unadjusted, as traded (ADR 0001/0004) — the point of the explicit name is that a
client cannot read unadjusted prices while believing they are adjusted. Not part of
the company summary itself; it is here because this is the migration that makes
`mart` complete (see [the gap](#a-pre-existing-gap-this-surfaces) above).

**2. `mart.v_security_profile`** — one row per security: `security_id`, `symbol`,
`company_name`, `asset_type`, `exchange_code` + `exchange_name`, `sector_name`,
`industry_name`, `currency`, `country`, `cik`/`isin`/`cusip`, `is_actively_trading`,
`is_etf`, `is_fund`, `ipo_date`, `delisted_date`, `market_cap_usd`, `beta`,
`first_seen_at`, `updated_at`, and `description` from `core.company_profile`.

Not `mart.security_latest`: that matview is the screening contract, is
refresh-lagged, and carries no description, no identifiers, no listing dates.

**3. `mart.v_security_price_coverage`** — cheap per-security aggregates over
`core.daily_price`, grouped by `security_id` so the filter pushes down to the
grouping key and only that security's partition slices are touched:

`first_trade_date`, `last_trade_date`, `bar_count`, `distinct_years`,
`min_close`, `max_close`, `last_close`, `last_volume`, `avg_volume_60d`,
`zero_volume_bars`, `calendar_span_days`.

Richer statistics (trailing returns, annualised volatility, max drawdown, 52-week
range on the **adjusted** series) are **not** SQL — they are computed in Python from
one bounded `price_history(..., adjusted=True)` pull, reusing
`duk.return_utils` and `duk.stats`. That is where those formulas already live and
are already tested.

**4. `mart.v_security_action_summary`** — per security, from
`core.corporate_action` + `core.adjustment_factor`: `split_count`,
`first_split_date`, `last_split_date`, `last_split_ratio`, `dividend_count`,
`first_dividend_date`, `last_dividend_date`, `last_dividend_amount`,
`ttm_dividend_amount` (sum over ex-dates in the trailing 365 days),
`adjustment_factor_rows`, `latest_factor_effective_date`,
`cumulative_price_factor_earliest` (how much back-adjustment the oldest bar carries).

**5. `mart.v_security_dq_open`** — the narrow `ops` window. One row per open flag,
not per group: the aggregation belongs in the caller, because naming the offending
bar is what makes the section actionable.

```sql
CREATE VIEW mart.v_security_dq_open AS
SELECT dq_flag_id, security_id, check_name, severity, record_key, detected_at
FROM ops.data_quality_flag
WHERE resolved_at IS NULL AND security_id IS NOT NULL;
```

**Why `record_key` is in and `detail` is out** — the columns differ in kind, and the
narrowing instinct aims at the wrong one:

- `record_key` holds only `{"trade_date": …}`, `{"symbol", "date"}`, `{"ex_date"}`,
  `{"last_date"}`, `{"effective_date"}` — dates and tickers, every one derived from
  data a `mart` reader already reads in full. It costs nothing to expose and it is
  what turns *"something is wrong in this series"* into *"distrust the 2026-07-14
  bar"*.
- `detail` is mostly derived values too (`{"move": 0.62, …}`,
  `{"prior_close", "dividend"}`) — but `adjustment_failed` writes
  `{"error": f"{type(exc).__name__}: {exc}"}`, a raw Python exception string that can
  carry psycopg internals, constraint names and paths. That is the one DQ field not
  derived from readable market data, and the reason `detail` stays behind
  `fafnir dq list`.

`resolved_by` and `resolution_note` — the human judgements from migration 0017 — need
no decision: `ck_dq_flag_resolution_provenance` forces both to `NULL` whenever
`resolved_at` is, so the open-only filter already guarantees no human-written text
reaches the seam. That safety rests on the `WHERE`, not on the column list, which is
worth knowing before anyone relaxes the filter to show resolved flags.

`.down.sql` drops all six.

`ALTER DEFAULT PRIVILEGES … IN SCHEMA mart GRANT SELECT ON TABLES` (migration 0001)
covers views, so no per-view grant is needed — but the least-privilege test must
prove it rather than trust it.

### A pre-existing gap this surfaces

`duk -S db ph` cannot run as `fafnir_app` at all. Measured on a scratch cluster with
all 19 migrations applied and the three roles created as a real install creates them:

| Read, as `fafnir_app` | Result |
|---|---|
| `core.symbol_xref` (the first query `_resolve_security_id` runs) | `ERROR: permission denied for schema core` |
| `core.daily_price` (raw `ph`) | `ERROR: permission denied for schema core` |
| `mart.v_daily_price_adjusted` (`ph --adj`) | ✅ `100.5000000` |
| `mart.security_latest` (`ls` screening) | ✅ |
| `ops.data_quality_flag` | `ERROR: permission denied for schema ops` |

Resolution fails **before** either price relation is reached, so `ph` is broken as
`fafnir_app` with and without `--adj` — even though the adjusted view itself is
perfectly readable. It works in practice because nobody runs it as that role: on the
warehouse host `duk` picks up `FAFNIR_DSN` from `/etc/fafnir/fafnir.env`, which is
`user=fafnir_ingest` over the socket ([install §4.4](../install_hetzner.md), and §8
says so outright — *"duk here picks up FAFNIR_DSN from the env file, so it reads as
fafnir_ingest"*). `fafnir_read` works too. The only `fafnir_app` path is §11's laptop
tunnel, whose two example commands split: `ls --sector Technology` works (pure
`mart`), `ph AAPL --adj --close -n 10` cannot. Half of it working is presumably why
this has gone unnoticed.

The install doc already half-knows: its troubleshooting table carries
*"`permission denied for schema core` from `duk` → correct by design — use `mart.*`
views, or connect as `fafnir_read`"*, while §11 and the §12 acceptance checklist both
still instruct `duk -S db ph SPY --adj -n 5  # reads as fafnir_app`.

**Decided, and done in phase 1.** `mart` becomes the complete read seam for `duk`, and the
role model becomes true as documented rather than aspirational.
[ADR 0008 §4](../adr/0008-remote-duk-access-and-mcp.md) records the decision and the
alternative that was rejected. Two consequences for this plan:

- `mart.v_symbol_lookup` (already listed below) serves `ph` as well as `ls`, and
  `_resolve_security_id` switches to it.
- **`mart.v_daily_price_raw` joins this migration** — the raw counterpart to
  `v_daily_price_adjusted`, so `price_history(adjusted=False)` stops naming `core`.
  With it, `duk.datasource.db` names no `core` relation at all.

Verified on the scratch cluster: through the view, as `fafnir_app`, on a ten-year
partitioned table, the plan is byte-identical to the direct read (`Seq Scan on
daily_price_y2024` — one partition of eleven). Views inline; pruning survives; there
is no query-cost argument against this.

Two rules that come with it, both belonging in ADR 0009 alongside the `ops` window:
a `mart` view must never set `security_invoker = true` (definer rights are the whole
mechanism), and adding a `mart` view grants every `mart` reader whatever it selects
— so it is a deliberate act, not a convenience.

Correcting install §11/§12, which today instruct `duk -S db ph SPY --adj` as
`fafnir_app`, is part of phase 1: the commands they name become true.

### What phase 1 landed

Six views in `sql/migrations/0020_company_summary_marts.up.sql`, `duk.datasource.db`
naming no `core` relation, ADR 0009, and four tests. Two things came out of building
it that the plan had not anticipated:

- **`v_security_action_summary` cannot select `cumulative_price_factor`.** The first
  draft did, and broke `test_rolling_0013_back_clears_a_security_rather_than_half_its_chain`:
  migration 0013 re-types that column and has to drop `mart.v_daily_price_adjusted`
  to do it, so a second dependent view doubles the cost of every future change to
  it. The field is gone; a client wanting back-adjustment depth reads `price_factor`
  from the adjusted view, where the dependency already exists. `earliest_cumulative_price_factor`
  is therefore **not** in the summary contract.
- **The guard that matters most is the unit test.** `test_mart_read_seam.py`
  asserts `duk.datasource.db` names no `core`/`ops` relation *without needing a
  database*, so it fires on every run. The privilege test is stronger but needs a
  DSN whose role can create roles and databases — and the breakage this plan fixes
  survived precisely because the check that would have caught it needed a database
  nobody had configured.

## Code changes

### `src/duk/datasource/db.py`

```python
def resolve_company(*, dsn, query) -> list[dict]      # candidates; [] when nothing matches
def company_summary(*, dsn, security_id) -> dict      # meta / prices / actions / fundamentals / dq
```

`company_summary` issues one connection, four/five statements, and returns plain
dicts and Decimals — no formatting, no click. It calls `price_history` internally
for the adjusted series used by the return statistics (bounded: `--start-date` of
`last_trade_date - 5y`, so a 30-year name does not pull 8,000 rows to compute a
1-year return).

### `src/duk/company_summary.py` (new)

Pure assembly and rendering, no SQL, no I/O:

- `build_summary(raw: dict) -> dict` — derives the computed statistics (trailing
  1M/3M/6M/1Y/YTD returns, annualised volatility, max drawdown, 52-week range,
  trailing dividend yield = `ttm_dividend_amount / last_close`), and normalises
  Decimals to floats for JSON.
- `render_text(summary) -> str` — the sectioned report.
- `render_flat(summary) -> dict` — the one-row scalar view used by `--csv`.

Formatting helpers live here: `fmt_big` (`3.42T`, `54.2M`), `fmt_pct`, `fmt_date`,
and `-` for every missing value. Prices go through the existing
`duk.format_utils.round_price`, so a back-adjusted sub-penny price stays legible
rather than printing `0.00` — the same rule `ph` already follows.

Keeping this module free of `click` and `psycopg` is what makes the whole report
testable without a database, which is how the rest of `duk`'s compute modules are
built. Note `src/duk` does **not** import `fafnir` anywhere; the small aligned-table
helper is written locally rather than reaching into `fafnir/cli.py`'s private
`_echo_table`.

### `src/duk/cli.py`

`ls` gains `@click.argument("query", required=False)`, the mutual-exclusion checks
above, and a branch that calls the two datasource functions and echoes
`render_text` / JSON / flat CSV. The existing list and screen branches are
untouched — the new branch returns before them.

## Output format

| Flag | Profile mode output |
|---|---|
| *(default)* | sectioned text report (below) |
| `--json` | one nested object: `{meta, prices, actions, fundamentals, dq}` |
| `--csv` | one row of scalar fields (meta + statistics); the DQ per-check breakdown is dropped, with a footer note naming the count |
| `-o PATH` | as today: writes the chosen format |

`--json` in profile mode is a nested object, where list mode emits an array of
records. That divergence is deliberate and must be documented in `doc/duk.md` —
scripts branch on the command shape, and forcing the profile into a record array
would flatten away the DQ breakdown.

### Mock (text)

```
AAPL  Apple Inc.                                          NASDAQ · US · USD
--------------------------------------------------------------------------
Sector       : Technology                Industry : Consumer Electronics
Market cap   : 3.42T                     Beta     : 1.24
Status       : actively trading          IPO      : 1980-12-12
Identifiers  : CIK 0000320193 · ISIN US0378331005 · CUSIP 037833100
Profile as of: 2026-08-29  (market cap and beta refresh with the security master)

PRICE HISTORY (raw bars; statistics on the adjusted series)
  Coverage   : 1980-12-12 .. 2026-08-29   11,231 bars over 46 years
  Last close : 232.14 on 2026-08-29       volume 41,208,300  (60d avg 52.8M)
  52w range  : 164.08 .. 260.10
  Returns    : 1M +2.4%   3M +7.1%   6M +11.9%   YTD +14.2%   1Y +18.6%
  Ann. vol   : 24.3% (1y)                Max drawdown (1y) : -12.8%
  Gaps       : 3 bars with zero volume

CORPORATE ACTIONS
  Splits     : 5    last 4-for-1 on 2020-08-31
  Dividends  : 128  last 0.250000 ex 2026-08-08   TTM 0.990000 (0.43% yield)
  Adjustment : 133 factor rows, latest ex-boundary 2026-08-08
               oldest bar carries a cumulative price factor of 0.00089286

FUNDAMENTALS
  Not loaded — the fundamentals milestone is planned (doc/extending.md).

DATA QUALITY (open flags)
  CHECK                  SEV    FLAGS  FIRST SEEN  LAST SEEN  KEYS
  ---------------------  -----  -----  ----------  ---------  ------------------------
  gap                    warn       2  2026-07-14  2026-08-02  2026-07-14, 2026-08-02
  price_scale_collapse   error      1  2026-08-15  2026-08-15  2026-08-15
  Detail: fafnir dq list --detail --symbol AAPL
```

When the queue is clean, that last section is one line: `No open DQ flags.` —
present, not omitted, because "no section" and "no flags" are different claims.

## Fundamentals: build the seam now, fill it later

Fundamentals are not in the warehouse yet ([extending.md](../extending.md)
roadmap), so section 3 ships as a **capability probe** rather than a stub:

```sql
SELECT to_regclass('mart.v_security_fundamentals_latest') IS NOT NULL;
```

- View absent → render *"Not loaded — the fundamentals milestone is planned"*, and
  `fundamentals: null` in JSON.
- View present → read and render it, no new `duk` release required.

That makes the contract the fundamentals milestone has to satisfy explicit, and it
is worth writing down here so it is designed once. Expected columns, latest
point-in-time row per security (`filing_date <= now()`, `valid_to IS NULL`, latest
`fiscal_date`), per the bitemporal shape extending.md already specifies:

`fiscal_date`, `period`, `filing_date`, `reported_currency`, `revenue`,
`net_income`, `eps_diluted`, `ebitda`, `total_assets`, `total_liabilities`,
`total_equity`, `operating_cash_flow`, `free_cash_flow`, `shares_diluted`.

`duk` derives the ratios it prints (P/E, P/S, P/B, gross/net margin, ROE) from
those plus `market_cap_usd` — vendor-computed ratios are not stored, so the
displayed number always matches the statements shown next to it.

## Tests

**Unit** (`test/duk/test_company_summary.py`, no DB):
- `build_summary` return/volatility/drawdown arithmetic against a fixed series.
- missing values render `-`; sub-penny prices keep significant digits.
- `fundamentals=None` renders the planned-milestone line, not an empty section.
- `--json` key set is asserted explicitly — it is a public contract.

**Unit** (`test/duk/test_cli_ls_profile.py`, `CliRunner`, datasource monkeypatched):
- `QUERY` + `--sector` errors; `QUERY` + `--summary` errors; `-S live` errors.
- no match → exit 1 with the message; several matches → candidate table, exit 1.
- list and screen modes still behave exactly as before (regression).

**Integration** (`test/duk/test_db_company_summary.py`, needs `FAFNIR_TEST_DSN`):
- seed a security with prices, one split, two dividends, two open DQ flags and one
  resolved one → assert every number in the report, and that the resolved flag is
  **not** counted.
- resolution by exact name, by former ticker, and the ambiguous-name path.
- `mart.v_symbol_lookup` returns the same `security_id` as the existing three-query
  ladder for live, primary, and historical tickers.
- `mart.v_daily_price_raw` and `core.daily_price` return identical rows for a
  security across a date range — the `ph` series must not move by a digit when the
  relation name changes. Worth asserting on the DataFrame, not just the SQL, since
  that is the contract `scripts/reconcile.sh` depends on.

**Privilege** (extend `test/fafnir/test_migrations_least_privilege.py`):
- `fafnir_app` can `SELECT` all six new views;
- `fafnir_app` still **cannot** read `ops.data_quality_flag` directly.
  That pair is the whole argument for the view-owner approach; if it is not tested,
  it is not true. Note this suite skips unless the test DSN's role can create roles
  and databases — a green run on a restricted DSN proves nothing here, which is how
  the `ph`-as-`fafnir_app` breakage above survived this long.

**Migration** (`test/fafnir/test_migrations.py` pattern): up/down round trip.

## Documentation

- `doc/duk.md` — a `ls QUERY` section: the ladder, the flag rules, the JSON shape
  divergence, worked examples.
- `doc/data_dictionary.md` — the six new `mart` views under Schema: `mart`,
  including the note that `v_security_dq_open` is an owner-privilege window onto
  `ops`, and `v_daily_price_raw` beside the adjusted view it mirrors.
- `doc/adr/0009-*.md` — the aggregate-`ops`-through-`mart` decision.
- `doc/operations.md` — `duk -S db ls <TICKER>` as the per-symbol triage entry
  point, next to `fafnir dq list`.
- `doc/install_hetzner.md` §11/§12 — the `ph`-as-`fafnir_app` examples become true
  in phase 1; drop the troubleshooting row that documents the failure.
- `doc/duk.md` — the source table says db-mode raw reads `core.daily_price`; it
  becomes `mart.v_daily_price_raw`.
- `doc/architecture.md` — the role table's `fafnir_app` row ("read **mart** (+ ref)
  only … `duk -S db`") needs no edit; phase 1 is what makes it accurate.
- `doc/index.md`, `README.md` — link and command-table rows.

## Phasing

| Phase | Deliverable | Independently mergeable |
|---|---|---|
| 1 | ✅ **done** — migration 0020 (six views) + [ADR 0009](../adr/0009-mart-is-the-read-seam.md) + privilege/parity tests; `duk.datasource.db` moved onto `mart` throughout; install §11/§12, `duk.md` and the data dictionary corrected | shipped — it fixes `duk -S db ph` for `fafnir_app` |
| 2 | `resolve_company` / `company_summary` in `datasource/db.py`; `company_summary.py` assembly + rendering; unit tests | yes — library-only |
| 3 | `ls QUERY` CLI wiring, output formats, docs, CLI + integration tests | yes — ships the feature |
| 4 | fundamentals section goes live when `mart.v_security_fundamentals_latest` exists | later milestone, no duk change |

## Risks and open questions

1. **Name search cost.** `ILIKE '%…%'` over ~21k `core.security` rows is a
   sub-10ms sequential scan and needs no index. A `pg_trgm` index would be faster
   but the extension needs superuser, which the migrator deliberately does not
   have — so: plain `ILIKE`, and revisit only if the universe grows an order of
   magnitude.
2. **Stale profile numbers.** `market_cap_usd` and `beta` come from the
   company-screener and refresh with the security master; they are a snapshot, not
   history (`core.security` column comments say so). The report prints an
   "as of" line rather than implying they are live.
3. **`price_*` flag counts.** Those repeat per re-detection by design; `fafnir dq
   list` already carries that caveat, and the profile's DQ section must repeat it
   whenever a `price_*` row is shown, or the count reads as a count of problems.
4. **Cost of the whole report.** Five aggregates plus one bounded price pull, all
   on `security_id`. Budget: well under a second on a warm cache. If
   `v_security_price_coverage` disappoints on a 46-year name, the fallback is a
   scheduled matview keyed by `security_id` — the view contract does not change.
5. **`ls` is getting wide.** Profile mode is a genuinely different command sharing
   a name with list/screen. `ls` is what was asked for and matches `ls`'s ordinary
   shell meaning (no argument = the directory, an argument = that entry), so keep
   it — but if the flag matrix keeps growing, `duk co <QUERY>` as an alias is the
   escape hatch.

### Live mode

`-S live` is unsupported in v1, by design and not by omission: three of the four
sections (warehouse coverage, adjustment-factor state, the DQ queue) describe
*fafnir's holdings*, and there is nothing for the FMP path to answer them with. A
live profile would be a different, thinner command. The error names `-S db`
explicitly rather than silently falling back the way `yc` does, because a fallback
here would quietly answer a different question.
