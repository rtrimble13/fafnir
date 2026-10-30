# fafnir

A research-grade **financial market data warehouse** on PostgreSQL + Python, with
**`duk`** as its data-access CLI. fafnir is the durable source of truth that
multiple independent apps and agentic/MCP clients read from; `duk` is the primary
client.

- **Runs on Linux** with PostgreSQL 16 (schema designed to adopt TimescaleDB later
  without a grain change).
- **Sources:** **Sharadar** is the system of record: identity, dead-issuer history
  from 1998, the nightly prices and actions since the cutover of 2026-10-22, and the
  fundamentals ([doc/plans/sharadar-adoption.md](doc/plans/sharadar-adoption.md)).
  Financial Modeling Prep (FMP) built the warehouse and now feeds only what Sharadar
  does not: funds and unlinked securities.
- **Correct by construction:** money is exact `NUMERIC`; prices are stored
  *unadjusted* (Sharadar's as-traded close, and FMP's `non-split-adjusted` endpoint for
  what FMP still feeds), so fafnir's own factors are the only adjustment ever
  applied; raw OHLCV is immutable, and adjusted prices are *derived on read*
  (point-in-time stable); delisted securities are retained (no survivorship bias);
  every load is idempotent and logged.

> Status: a self-maintaining security master (new listings, renames and delistings
> reconciled nightly from Sharadar's identity, the permaticker), daily OHLCV with the
> dead issuers from 1998, corporate actions, **point-in-time fundamentals** from
> Sharadar SF1 with a monthly factor panel (`doc/fundamentals.md`), the multi-source
> `duk` CLI, and an MCP server for agentic access (`doc/agent.md`). The roadmap is in
> `doc/extending.md`.

## Architecture at a glance

```
Sharadar (primary) · FMP (secondary)
        │  (throttled, retrying loaders)
        ▼
   landing  ──►  core (constrained truth)  ──►  mart (derived read views)
                    │                                  │
              ops (lineage, DQ, watermarks)            ▼
                                              duk (db mode) · apps · MCP
```

Schemas: `landing` (raw immutable payloads, the Sharadar file ledger) → `core`
(security master, raw daily prices, corporate actions, versioned statements) → `mart`
(adjusted prices, screening snapshot, point-in-time statements, the factor panel), with
`ref` (exchanges/sectors/calendar, and the declared universe), `ops` (ingestion
runs, DQ flags, watermarks), and `meta` (migrations). See **[doc/architecture.md](doc/architecture.md)** and the
**[data dictionary](doc/data_dictionary.md)**.

## Quick start (bare-metal Postgres)

> Installing on a fresh cloud server? **[doc/install_hetzner.md](doc/install_hetzner.md)**
> is a step-by-step walkthrough for a Hetzner Cloud host (Ubuntu 24.04 + PostgreSQL 16),
> from provisioning through nightly scheduling and backups.

```bash
# 1. Install
make build                      # pip install -e .[dev]

# 2. Configure
cp etc/fafnirrc ~/.fafnirrc      # set [database] + FMP_API_KEY (env preferred)
export FMP_API_KEY=...           # never commit keys
export SHARADAR_API_KEY=...      # env only; a host keeps it in /etc/fafnir/fafnir.env

# 3. Create DB, roles, schema, seeds
scripts/setup_db.sh             # roles + createdb (via FAFNIR_ADMIN_DSN) + migrate + seed
                                # the schema itself is created by the ordinary
                                # fafnir_ingest role -- no superuser needed

# 4. Initial build from FMP (security master → prices → actions → adjust).
#    A fresh install starts here, in FMP mode: live securities are minted from
#    FMP's screener. Sharadar comes after (below).
fafnir ingest securities --limit 500     # or full universe (omit --limit)
fafnir ingest prices  --symbols AAPL,MSFT --from 2020-01-01
fafnir ingest actions --symbols AAPL,MSFT
fafnir adjust
fafnir db refresh-marts
fafnir dq run
fafnir status

# 5. Track a mutual fund (or anything else the screener cannot reach)
fafnir source probe-fund VFIAX                   # confirm the NAV series is raw
fafnir track add VFIAX --note "core US equity sleeve"
fafnir ingest tracked                            # mint it; nightly job does this too
fafnir ingest prices --symbols VFIAX             # no watermark yet -> full history

# 6. Triage what the checks flagged
fafnir dq list                                   # open flags by check and severity
fafnir dq list --detail --check gap --symbol AAPL
fafnir dq resolve 12841 --note "exchange holiday, no bar expected"
fafnir dq run --engine both                      # + dorq, in shadow (doc/dorq.md)
fafnir dq compare --labels-from-db               # dorq vs the SQL checks and labels
fafnir dq label add --security-id 601133 --first 2026-10-05 --kind feed_gap \
    --verified -m "trades on the issuer page; FMP has no bar"   # state a label
fafnir source vendor-gaps                        # live names FMP stopped serving

# 7. Read with duk (the warehouse is the default source)
duk ph AAPL --adj
duk ph cik:51143                          # or name a security by CIK/ISIN/CUSIP
duk ph CC@2008-06-30                      # the security that traded as CC that day
duk ls --sector Technology
duk fs AAPL --as-of 2015-06-30            # a statement as known on a date
duk fa AAPL                               # the security's factor row
duk -S fmp ph AAPL                        # the same series straight from a vendor
duk -S sharadar ph AAPL
duk ti sma -i prices.csv -c close -w 20   # pure compute, source-agnostic
```

**Then Sharadar.** On top of the FMP build come Sharadar's files, the links from
FMP's securities to Sharadar's permatickers, and the dead issuers FMP never had
([doc/backfill.md › Sharadar initial population](doc/backfill.md#sharadar-initial-population)),
then the switch of the nightly to Sharadar
([doc/operations.md › Cutover to Sharadar as primary](doc/operations.md#cutover-to-sharadar-as-primary)).

Daily upkeep is a single scheduled job running `scripts/daily_update.sh` — see
**[doc/operations.md](doc/operations.md)**, and `scripts/install_timers.sh` for
the systemd timers (nightly update, DQ sweep, weekly reconciliation, logical dump,
off-server copy). It maintains the universe as well as the data. In Sharadar mode,
production's since 2026-10-22, `fafnir ingest sharadar` reads the night's TICKERS,
ACTIONS and price deltas and applies renames, delistings and new listings through the
permaticker (FB → META keeps one `security_id`; a security that listed today enters
scope today), then loads its prices and actions. The fundamentals follow from SF1's
delta, FMP refreshes what it alone still feeds (the declared mutual funds and the
securities with no Sharadar link), and the factors and marts are rebuilt. A new
listing is minted from its first price in the universe step's window, and its bars
from that window load the same night.

## `duk` — one CLI, three sources

`duk` keeps its full command surface (`ph`, `yc`, `ls`, `rc`, `ti`) and adds a
global `--source/-S`:

- `-S db` (**the default**): reads the fafnir warehouse. Without a DSN configured,
  `duk` says so and names the vendor flags rather than silently reading a vendor.
- `-S fmp`: reads the FMP API directly (`-S live` is the older spelling and still
  works).
- `-S sharadar`: reads Sharadar's price tables directly (`ph` only; needs
  `SHARADAR_API_KEY`).

Return and indicator commands (`rc`, `ti`) operate on input files and are
source-agnostic. `yc` (yield curve) reads FMP's treasury rates in every mode.
Reading the same symbol from two sources is how a series is checked:
`diff <(duk ph AAPL --close --csv) <(duk -S sharadar ph AAPL --close --csv)`.

Anywhere `duk` takes a ticker it also takes a **CIK, ISIN or CUSIP**, prefixed so
the intent is explicit:

```bash
duk -S db ph cik:51143             # IBM's prices, by SEC filer number
duk -S db ls cusip:459200101       # IBM's company summary
duk -S db ph isin:US4592001014 --adj
```

Leading zeros, case and grouping are ignored; a bare argument still means a ticker
(or, for `ls`, a company name). When an identifier matches several securities --
one CIK covers every share class of an issuer -- `duk` lists them and exits rather
than guessing. See **[doc/duk.md](doc/duk.md)**.

## Documentation

| Doc | Contents |
|---|---|
| [architecture.md](doc/architecture.md) | Layers, ERD, role model, design decisions |
| [data_dictionary.md](doc/data_dictionary.md) | Every table/column: grain, source, units, adjustment, cadence |
| [ingestion.md](doc/ingestion.md) | Sharadar's files and nightly, FMP as the secondary vendor, precedence, idempotency, watermarks |
| [install_hetzner.md](doc/install_hetzner.md) | Fresh install on a Hetzner Cloud server: provision → Postgres → fafnir → cron |
| [backfill.md](doc/backfill.md) | Initial setup & historical backfill: FMP first, then Sharadar's population, dead issuers and fundamentals |
| [operations.md](doc/operations.md) | Daily upkeep runbook, cron, backfill, recovery |
| [duk.md](doc/duk.md) | CLI usage: the warehouse (`db`) vs a vendor (`fmp`, `sharadar`) |
| [fundamentals.md](doc/fundamentals.md) | The statements and the factor panel: reading them point in time, without look-ahead |
| [agent.md](doc/agent.md) | Run an operations agent on the host: DQ triage, automations, data questions |
| [extending.md](doc/extending.md) | The roadmap; add a source, a table, or an MCP tool |
| [adr/](doc/adr/) | Architecture decision records |
| [plans/sharadar-adoption.md](doc/plans/sharadar-adoption.md) | Living plan for adopting Sharadar: phases, working agreements, decision log |

## Development

```bash
make test          # unit tests (no DB)
make test-int      # integration tests (needs FAFNIR_TEST_DSN)
make fmt           # black + isort + flake8
```

## License

MIT — see [LICENSE](LICENSE).
