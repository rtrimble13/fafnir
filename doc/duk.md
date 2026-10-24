# duk — the data-access CLI

`duk` is fafnir's primary read client. It keeps its original command surface and
adds a global `--source/-S` to choose between the warehouse and a vendor's API.

## Sources

| Source | Reads from | When to use |
|---|---|---|
| `db` (**default**) | fafnir PostgreSQL `mart` | normal use; fast, offline, point-in-time stable, every security the warehouse holds |
| `fmp` | FMP API directly | a symbol not yet in the warehouse; checking a series against FMP; no-DB use. `live` is the older name and still works |
| `sharadar` | Sharadar's SEP/SFP price tables directly | checking a series against Sharadar; reading a security before it is loaded. `ph` only |

The source is a **group-level** option, so it comes before the subcommand:

```bash
duk ph AAPL --adj                 # the warehouse
duk -S fmp ph AAPL --adj          # FMP
duk -S sharadar ph AAPL --adj     # Sharadar
```

**The warehouse is the default** whether or not a DSN is configured. Without one,
`duk` stops with *no fafnir DSN configured* and names the vendor flags; it never
silently answers from a vendor when the warehouse was meant (before 1.6 it fell
back to FMP). Set `[general].default_source` to `fmp` or `sharadar` for a machine
that has no warehouse.

Each vendor source needs that vendor's key, and nothing else: `-S fmp` reads
`FMP_API_KEY` / `[api].fmp_key`, `-S sharadar` reads `SHARADAR_API_KEY` /
`[api].sharadar_key`. A missing key is refused before any request is made.

### What each source can serve

| Command | `db` | `fmp` | `sharadar` |
|---|---|---|---|
| `ph` | ✅ | ✅ | ✅ |
| `ph cik:` / `isin:` / `cusip:` | ✅ warehouse identifiers | ✅ FMP search | ✖ pass a ticker, or resolve with `-S db` / `-S fmp` |
| `ls` (list, screen) | ✅ | ✅ | ✖ |
| `ls SYMBOL` (company summary) | ✅ | ✖ | ✖ |
| `yc` | ⤳ falls back to FMP until the FRED series land | ✅ | ✖ no treasury data |
| `rc`, `ti` | source-agnostic | source-agnostic | source-agnostic |

### The Sharadar source

Sharadar's price tables carry split-adjusted `open`/`high`/`low`/`close`/`volume`,
a fully adjusted `closeadj`, and one as-traded field, `closeunadj`. `duk ph`
without `--adj` means **raw**, so the raw series is recovered with the vendor's own
identities (`OpenUnadj = Open × CloseUnadj / Close`, `VolumeUnadj = Volume × Close /
CloseUnadj`): exact on bars with no later split, the vendor's rounding on bars
with one. With `--adj`, every price is scaled by `closeadj / close`. SEP (stocks)
is read first and SFP (ETFs, closed-end funds) when SEP has no rows, so one
spelling of a ticker works for both. Sharadar has no mutual funds.

> **duk makes no Sharadar request of its own.** `-S sharadar` goes through the
> warehouse's client, `fafnir.sources.sharadar.SharadarClient` (ADR 0017): the same
> base URL, tables, throttle, retries and redaction the loaders and
> `fafnir source probe-sharadar` use, with the key in the `x-api-key` header rather
> than the URL. duk keeps only the frame shaping above. If a request fails, the error
> names `[api].sharadar_base_url` / `SHARADAR_BASE_URL`, which override the base URL;
> a correction the probe makes to the client serves duk as it stands
> ([plan](plans/sharadar-adoption.md)).

## Configuration

`duk` reads `~/.dukrc` (TOML). Relevant keys:

```toml
[api]
fmp_key = ""              # or FMP_API_KEY env (-S fmp)
sharadar_key = ""         # or SHARADAR_API_KEY env (-S sharadar)
# sharadar_base_url = "https://api.sharadar.com/v1.0/data"   # override if the probe says so

[database]
dsn = "host=localhost dbname=fafnir user=fafnir_app"   # or FAFNIR_DSN env (-S db)

[general]
default_source = "db"     # "db" (default), "fmp" or "sharadar"
log_level = "info"
```

For db mode, point `duk` at the least-privilege `fafnir_app` role (mart read-only).
Every relation `duk` reads is a `mart` (or `ref`) one, so that role is sufficient --
see [ADR 0009](adr/0009-mart-is-the-read-seam.md). It was not before migration 0020,
when symbol resolution still read `core`.

## Commands

| Command | Description | `db` | `fmp` |
|---|---|---|---|
| `ph SYMBOL` | price history (OHLCV, raw or `--adj`) | ✅ warehouse | ✅ FMP (and ✅ `sharadar`) |
| `ls` | list/screen securities, sectors, industries | ✅ `mart.security_latest` / `ref.*` | ✅ FMP |
| `ls SYMBOL\|NAME` | everything the warehouse holds on one company | ✅ `mart.v_security_*` | ✖ db only |
| `yc` | treasury yield curve | ⤳ falls back to FMP | ✅ FMP |
| `rc` | return calculations from an input file | source-agnostic | source-agnostic |
| `ti` | technical indicators (sma/ema/rsi/macd) from a file | source-agnostic | source-agnostic |

`ph` without `--adj` means **raw** — prices as they actually traded, so a split shows
up as a real jump. Every source honours that: `db` reads `mart.v_daily_price_raw`,
`fmp` reads FMP's `historical-price-eod/non-split-adjusted` (not `.../full`, which
is already split-adjusted), and `sharadar` recovers the as-traded series from
`closeunadj`. That is what makes the sources comparable, and what
`scripts/reconcile.sh` relies on. With `--adj`, `db` reads
`mart.v_daily_price_adjusted` (split + dividend, point-in-time stable) while the
vendors return their own adjusted series — close, but a moving vendor snapshot
rather than a reproducible one, so small differences on old dates are expected.

> `yc` reads FMP until the economic-series fast-follow adds treasury data to the
> warehouse; in db mode it logs a warning and reads FMP (requires an FMP key).

## Examples

`-n` counts **bars**, not calendar days: `-n 5` returns five trading periods at the
selected `--frequency`, whichever weekends and holidays fall inside them. The query
window is widened accordingly and the series trimmed back to `-n` rows, so `-n` pairs
with `--start-date` (the first N bars from that date) or `--end-date` (the last N up
to it); all three together is an error.

```bash
# Adjusted daily closes from the warehouse (the default source)
duk ph AAPL --adj --close

# Weekly OHLC, last 52 weeks
duk ph AAPL -f week -n 52 --ohlc

# The first five trading days of 1990
duk ph AAPL --start-date 1990-01-01 -n 5

# Prices print at 2dp by default; -p widens it
duk ph AAPL --adj -p 4

# Screen tech names by market cap (warehouse snapshot, actively trading)
duk ls --sector Technology --market-cap ">1000000000"

# The same sector with the companies that stopped trading
duk ls --sector Technology --include-delisted

# Export to CSV, then compute a 20-day SMA (pure compute, no source needed)
duk ph AAPL --close -o prices.csv
duk ti sma -i prices.csv -c close -w 20

# Reconcile a symbol: the same raw series from the warehouse and a vendor should match
diff <(duk ph AAPL --close --csv) <(duk -S fmp ph AAPL --close --csv)
diff <(duk ph AAPL --close --csv) <(duk -S sharadar ph AAPL --close --csv)
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

### Exact prices: `--exact`

The DataFrame behind `ph` is floating point, so `--json` prints a stored
`13247980.736555` as `13247980.7365550008`. That is fine for a chart and wrong
for a program that keeps money exact, which then has to round the noise back
off at a scale it has to know.

`--exact` prints every value as the text the warehouse stores. Prices keep their
scale (`189.120000`), volume is an integer, and dates are `YYYY-MM-DD`. In
`--json` the numbers are strings, so a reader that parses JSON numbers as floats
cannot round one by accident:

```bash
duk -S db ph isin:US0378331005 --close --json --exact -s 2024-01-02 -e 2024-01-03
[{"date": "2024-01-02", "close": "185.640000"}, {"date": "2024-01-03", "close": "184.250000"}]
```

It is refused (exit 2) wherever it cannot be kept, rather than printing something
rounded under its name:
- with a vendor source (FMP and Sharadar send floats);
- with any `--frequency` but `day` (resampling computes new bars);
- with `--summary`;
- with `-p` (every stored digit is printed).

portable reads prices this way (its ADR 0029).

## Naming a security by identifier

Anywhere `duk` takes a ticker, it also takes a **CIK, ISIN or CUSIP**, written
`scheme:value`:

```bash
duk -S db ph  cik:51143            # IBM, by SEC filer number
duk -S db ph  isin:US4592001014 --adj
duk -S db ls  cusip:459200101      # the full company summary
duk -S db ls  cik:0000051143       # zero-padding is optional
```

| Scheme | Value | Notes |
|---|---|---|
| `cik:` | digits | leading zeros optional — `51143` and `0000051143` are the same filer |
| `isin:` | 12 characters | case and grouping are ignored: `isin:"us4592 0010 14"` works |
| `cusip:` | 8 or 9 characters | the check digit may be dropped |
| `ticker:` / `symbol:` | a ticker | an escape hatch, for when the argument must not be read as anything else |

**The prefix is required.** An identifier is not guessable from its shape — `51143`
is a plausible CIK and a plausible nothing — so a bare argument still means exactly
what it always did: a ticker, or for `ls` a company name. An *unrecognised* prefix
is not an error either, so `ls "Baker: A History"` is still a name search.

A malformed value is refused rather than passed on (`'cik:five' is not a CIK:
expected digits, e.g. cik:51143`). A transposed identifier that came back "no data
found" would read as *the warehouse does not hold this security*, which is a
different and much more alarming claim.

### One identifier, several securities

A CIK identifies an **issuer**, and an issuer can have more than one listed class.
When an identifier matches several securities, `duk` prints them and exits 1 — it
never picks:

```
$ duk -S db ph cik:1652044
CIK 1652044 matches 2 securities. Re-run with one of these tickers:
SYMBOL  NAME          EXCHANGE  STATUS
------  ------------  --------  ------
GOOG    Alphabet Inc  NYSE      active
GOOGL   Alphabet Inc  NYSE      active
```

### How resolution works, per source

In **db mode** the warehouse's own `cik`/`isin`/`cusip` columns are matched, on
normalised forms of both the typed and the stored value (migration 0023 indexes
exactly those expressions). CUSIP and ISIN also cross-check each other: a North
American ISIN is literally `country || CUSIP || check digit`, so `cusip:459200101`
still finds a security whose `cusip` the vendor left null but whose `isin` it
filled in, and vice versa. The resolved `security_id` — not the ticker it maps to —
is what the price query then uses, so a **reused ticker** cannot redirect the
answer to its current owner.

With **`-S fmp`** there is no security master, so the identifier is handed to FMP's
own `search-cik` / `search-isin` / `search-cusip` and the ticker that comes back is
used from there. **`-S sharadar`** refuses an identifier: Sharadar's TICKERS table
carries CUSIPs and the CIK, but `duk` does not read it yet, and guessing a ticker
would be worse than stopping. `ls QUERY` is db-only in every case.

Identifiers work on the MCP surface too: every tool taking a `symbol` accepts
`cik:`/`isin:`/`cusip:` (see [agent.md](agent.md)).

### A ticker on a date: `TICKER@YYYY-MM-DD`

A ticker is reused across issuers. Circuit City and Chemours were both `CC`, and
since the dead issuers were minted from Sharadar (#71) the warehouse holds both. A
bare ticker means the security that holds it now, or held it last. To name the one
that traded under it on a day, add the date (#80):

```bash
duk -S db ph CC@2008-06-30 --close     # Circuit City
duk -S db ph CC@2020-06-30 --close     # Chemours
duk -S db ls CC@2008-06-30             # the company summary, the same way
```

- **What it reads.** The ticker's periods in `mart.v_symbol_lookup` that contain
  the date; a period's `valid_to` is its last day. A period that starts on
  1900-01-01 is the "first seen" fallback for a ticker whose start fafnir never
  learned, so it counts only from its security's first bar. Of several, the
  latest start wins, then the primary period, then the lowest id.
- **Nobody is an answer.** A date on which no security traded under the ticker
  finds nothing: `CC@2012-01-03`, after Circuit City died and before Chemours
  listed. It does not fall back to the bare ticker, because the date was the
  question.
- **`-S db` only.** The vendors have no ticker history to read it against, so
  `-S fmp` and `-S sharadar` refuse it. A date after `@` that does not parse is an
  error (`TICKER@YYYY-MM-DD`). A name that merely contains `@` is still a name.
- **The bare ticker says when it is ambiguous.** In db mode, `ph` and `ls` with a
  ticker that has named more than one security still answer as before, and print
  the others on stderr with the dated form that reaches each (dates illustrative):

```
Note: CC has named 2 securities. The bare ticker reads the one holding it now, or
last; for another, name a day it traded:
NAME IT AS     NAME                     EXCHANGE  STATUS
-------------  -----------------------  --------  --------
CC@2026-10-21  Chemours Co              NYSE      active
CC@2009-03-06  Circuit City Stores Inc  NYSE      delisted
```

On the MCP surface, `resolve_symbol` takes `as_of`, and every tool taking a
`symbol` accepts the `CC@2008-06-30` form.

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

A `cik:`/`isin:`/`cusip:` argument short-circuits that ladder entirely — the user
named a key, so no rung below can improve on the answer, and falling through would
end in a name search for the literal string `"cik:51143"`.

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
| malformed identifier | error naming the scheme, exit 1 |
| identifier matching several securities | candidate table on stderr, exit 1 |
| `-S fmp` / `-S sharadar` | error, exit 1 — three of the four sections describe the warehouse, so falling back would answer a different question |
| `-n/--limit` | ignored |

`--json` in profile mode is a **nested object**, where list mode emits an array of
records. That divergence is deliberate: flattening the report into a record array
would destroy the DQ breakdown. `--csv` gives a single flat row of scalars, in which
the DQ table collapses to an `open_dq_flags` count.

## Reading dead issuers

The warehouse keeps every security that stopped trading. Since #71 that includes
the issuers that died before fafnir first ran, minted from Sharadar
(`source = 'sharadar'`), so from 1998 it is free of survivorship bias
([ADR 0016](adr/0016-sharadar-identity-and-survivorship-floor.md)). With `-S db`:

- **A screen is actively trading only** (#79). There are thousands of dead issuers,
  and a screen that forgot the filter would return several times the market as it
  trades now. `--include-delisted` adds them back; it reads the warehouse only, and
  contradicts `--is-actively-trading`. The MCP `screen_securities` tool works the
  same way, with `include_delisted=true`. Plain `duk ls` lists actively trading
  names, as it always has.
- **One company resolves whether it lives or not.** `duk ls <ticker>` and
  `duk ph <ticker>` reach a dead issuer by the ticker it traded under, if no live
  security has since taken it; listed securities come first in a did-you-mean
  table.
- **Why it stopped** is in `mart.v_security_lifecycle`: the vendor's terminal event
  (acquired, merged away, liquidated, delisted), the acquirer, and the trading
  span ([data dictionary](data_dictionary.md)). A mart-only role reads it, through
  the MCP server's SQL tool or `psql`.

## Library use

The DataFrame contracts are identical across sources, so downstream code is
source-agnostic. `duk.datasource.db`, `duk.datasource.live` (FMP) and
`duk.datasource.sharadar` expose `price_history(...)` returning the same shape (the
first two also `screen(...)`); the pure compute
modules (`duk.indicators`, `duk.return_utils`, `duk.rates_utils`, `duk.stats`)
operate on those frames unchanged.

The company summary follows the same split. `duk.datasource.db.resolve_company()`
and `.company_summary()` do the I/O and return plain dicts; `duk.company_summary`
is pure functions over those dicts (`build_summary`, `render_text`, `render_flat`,
`render_candidates`) with no SQL and no click, so the whole report can be built and
asserted without a database. `build_summary`'s output is the `--json` contract.

## From Claude Code on a laptop

A Claude Code session on your laptop reads the warehouse the way you do: its Bash
tool runs `duk` as your OS user, with your `~/.dukrc` and your SSH tunnel. Three
things need settling first: the shape of the tunnel, the role Claude's queries run
as, and Claude Code's Bash sandbox. What follows was measured on Ubuntu 24.04
(OpenSSH 9.6p1, PostgreSQL 16, every migration applied), running `duk` directly and
inside `@anthropic-ai/sandbox-runtime` 0.0.79, the package Claude Code builds its
sandbox on. Where a line rests on Claude Code's documentation instead, it says so.

### The server: two roles and one `pg_hba` line

Add yourself as a reader with **both** of a person's roles, `rob` for you and
`rob_mcp` for your agents, as
[operations.md](operations.md#adding-a-reader-a-person-or-an-agent) describes. That
includes the `+fafnir_app` rule in `pg_hba.conf`: it is what lets the OS user `rob`
become `rob_mcp`. Without it the generic `local all all peer` line admits only a role
named exactly like the OS user, and every Claude connection fails with
`Peer authentication failed for user "rob_mcp"`.

### The tunnel: a socket, kept up by a service

Forward the server's Postgres socket to a **local socket file**, not a local port,
and run it as a service outside Claude:

```
# ~/.ssh/config                      (once: mkdir -m 700 ~/.fafnir)
Host fafnir-tunnel
    HostName <SERVER_IP>
    User rob
    IdentityFile ~/.ssh/id_ed25519_fafnir
    IdentitiesOnly yes
    LocalForward %d/.fafnir/.s.PGSQL.15432 /var/run/postgresql/.s.PGSQL.5432
    StreamLocalBindUnlink yes
    ExitOnForwardFailure yes
    ServerAliveInterval 15
    ServerAliveCountMax 3
    ControlMaster no
    BatchMode yes
```

```ini
# ~/.config/systemd/user/fafnir-tunnel.service     (Linux)
[Unit]
Description=SSH tunnel to the fafnir warehouse
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N fafnir-tunnel
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`systemctl --user enable --now fafnir-tunnel`. On macOS, run the same
`/usr/bin/ssh -N fafnir-tunnel` as a launchd agent in `~/Library/LaunchAgents` with
`RunAtLoad` and `KeepAlive`.

- **A socket, not `127.0.0.1:15432`.** On Linux a sandboxed command's `localhost` is
  its own, so a port forward cannot be reached from inside the sandbox under any
  setting, while a socket can be let through. A port forward also authenticates
  *anyone* on the laptop who can reach it as you: a second local user connected
  through one as `rob`. The socket is created mode `0600`, OpenSSH's default
  `StreamLocalBindMask`, and the same user got `Permission denied`.
- **A supervised `ssh -N`, not `ssh -fN` with `ControlPersist`.** ADR 0008's
  `ControlPersist 30m` closes the tunnel 30 minutes after its last forwarded
  connection, and `duk` holds one only while a command runs, so the tunnel is gone
  half an hour after the last query. Here a dead link makes ssh exit and the service
  restarts it: the tunnel was back seconds after the server dropped the session,
  the restart delay plus one SSH handshake.
- **Outside Claude.** Plain `ssh` cannot connect from inside the sandbox, and
  Claude has no need to hold the tunnel. `BatchMode yes` makes ssh fail rather than
  prompt, so the key comes from your ssh-agent. Give it a passphrase: commands
  Claude runs can read `~/.ssh`.

Your own `~/.dukrc` then names the socket's **directory**, as an absolute path
(libpq does not expand `~`, and appends `.s.PGSQL.<port>` itself):

```toml
[database]
dsn = "host=/home/rob/.fafnir port=15432 dbname=fafnir user=rob application_name=duk"
```

### Claude Code settings

Merge
[`etc/agent/claude-code-laptop.settings.json.example`](../etc/agent/claude-code-laptop.settings.json.example)
into `~/.claude/settings.json`:

```json
{
  "env": {
    "FAFNIR_DSN": "host=/home/rob/.fafnir port=15432 dbname=fafnir user=rob_mcp application_name=duk-claude"
  },
  "permissions": { "allow": ["Bash(duk *)"] },
  "sandbox": { "excludedCommands": ["duk *"] }
}
```

- **`env.FAFNIR_DSN`** runs Claude's `duk` as `rob_mcp`. Its queries are then told
  apart from yours in `pg_stat_activity` and the server log, get the agent budget
  (20 s statement timeout, 4 connections), and `ALTER ROLE rob_mcp NOLOGIN` stops
  them while you keep working. The variable wins over `~/.dukrc`. This is
  attribution, not enforcement: your OS account maps to both roles, and both read
  `mart` and `ref` only, read-only, so nothing Claude can reach changes.
- **`permissions.allow`** spares a prompt on every `duk` call.
- **`sandbox.excludedCommands`** is for when the sandbox is on (below), and inert
  while it is off.

User-level, not a project's `.claude/settings.json`: the DSN names you, and where an
administrator makes the sandbox admin-required, Claude Code ignores
`excludedCommands` entries in a project's `.claude/` files.

### The Bash sandbox

Claude Code's Bash sandbox is **off by default**, and then nothing more is needed.
Turned on (`/sandbox`, or `sandbox.enabled`), it stops a sandboxed `duk` reaching
the tunnel's socket:

| Sandbox setting | Linux (measured) | macOS (Claude Code docs; not measured) |
|---|---|---|
| none | ✖ `connection is bad: no error details available`. Only with Claude Code's optional seccomp filter installed; without it Unix sockets are not blocked at all | ✖ |
| `network.allowUnixSockets: ["<socket path>"]` | ✖ ignored: seccomp cannot filter by path | ✅ |
| `network.allowAllUnixSockets: true` | ✅ but opens *every* socket to every sandboxed command, `docker.sock` included | ✅ same caveat |
| `excludedCommands: ["duk *"]` | ✅ `duk` runs unsandboxed, as `duk` run directly did | ✅ |
| a `127.0.0.1` port forward | ✖ under every setting | needs `allowLocalBinding`, which opens every localhost port |

The example uses `excludedCommands`, which works on both. An excluded command runs
with your full access, `-o` to any path included; that is the trade. On macOS,
`allowUnixSockets` with the socket's absolute path is narrower, since `duk` stays
sandboxed, if you would rather.

`excludedCommands` matches the text of the whole call, and every command in the call
has to match. `duk ph AAPL --adj -n 20` leaves the sandbox; `duk ph AAPL | head`,
`cd x && duk …` and `duk … > out.csv` stay in it and fail as above. Limit with `-n`,
save with `-o`. `rc` and `ti` read local files and need no exception at all.

### Tell Claude what the failures look like

`duk` reports psycopg's connection errors, which name neither the tunnel nor the
sandbox. A few lines in the `CLAUDE.md` of the project where Claude uses `duk`:

```markdown
## fafnir data
- Read with `duk …` (the warehouse is the default source), one plain `duk`
  command per call: no pipes, redirects, `cd` or `&&`. Limit rows with `-n`; save
  with `-o file.csv`.
- "Connection refused" or "No such file or directory" on ~/.fafnir/.s.PGSQL.15432:
  the SSH tunnel is down. Tell me; never start ssh yourself.
- "connection is bad: no error details available": the Bash sandbox blocked the
  socket (doc/duk.md, "The Bash sandbox").
```

### Or the MCP server

`fafnir-mcp --profile read` serves the same reads as MCP tools, with result sizes
capped. MCP servers run outside the Bash sandbox, so the section above does not
apply to it. Register it from
[`etc/agent/mcp-laptop.json.example`](../etc/agent/mcp-laptop.json.example) with
`FAFNIR_DSN` pointed at the socket (`host=/home/rob/.fafnir port=15432 dbname=fafnir
user=rob_mcp application_name=fafnir-mcp`); through the tunnel,
`fafnir-mcp --profile read --check` reports `rob_mcp`, read-only, with `mart` and
`ref` only. One caveat: on a socket DSN, its connection error says PostgreSQL is not
running, because a socket DSN means "on the host" to it. On a laptop it means the
tunnel is down. Reach for `duk` when Claude should write files (`-o`) or run `rc` and
`ti` on them.
