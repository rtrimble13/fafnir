# fafnir

A research-grade **financial market data warehouse** on PostgreSQL + Python, with
**`duk`** as its data-access CLI. fafnir is the durable source of truth that
multiple independent apps and agentic/MCP clients read from; `duk` is the primary
client.

- **Runs on Linux** with PostgreSQL 16 (schema designed to adopt TimescaleDB later
  without a grain change).
- **Primary source:** Financial Modeling Prep (FMP, Professional plan). Economic
  sources (FRED, BLS, BEA) are fast-follows.
- **Correct by construction:** money is exact `NUMERIC`; prices are ingested
  *unadjusted* (FMP's `historical-price-eod/non-split-adjusted`, so fafnir's own
  factors are the only adjustment ever applied), raw OHLCV is immutable, and
  adjusted prices are *derived on read* (point-in-time stable); delisted securities
  are retained (no survivorship bias); every load is idempotent and logged.

> Status: **initial release** — self-maintaining security master (new listings and
> ticker renames reconciled nightly), daily OHLCV, corporate actions, the
> dual-mode `duk` CLI, and an MCP server for agentic access
> (`doc/agent.md`). Fundamentals and economic time series are the next
> milestones (see `doc/extending.md`).

## Architecture at a glance

```
FMP / FRED / BLS / BEA
        │  (throttled, retrying loaders)
        ▼
   landing  ──►  core (constrained truth)  ──►  mart (derived read views)
                    │                                  │
              ops (lineage, DQ, watermarks)            ▼
                                              duk (db mode) · apps · MCP
```

Schemas: `landing` (raw immutable payloads) → `core` (security master, raw daily
prices, corporate actions) → `mart` (adjusted prices, screening snapshot), with
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

# 3. Create DB, roles, schema, seeds
scripts/setup_db.sh             # roles + createdb (via FAFNIR_ADMIN_DSN) + migrate + seed
                                # the schema itself is created by the ordinary
                                # fafnir_ingest role -- no superuser needed

# 4. Initial build (security master → prices → actions → adjust)
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

# 7. Read with duk (db mode)
duk ph AAPL --adj -S db
duk ls --sector Technology -S db
duk ti sma -i prices.csv -c close -w 20   # pure compute, source-agnostic
```

Daily upkeep is a single scheduled job running `scripts/daily_update.sh` — see
**[doc/operations.md](doc/operations.md)**, and `scripts/install_timers.sh` for
the systemd timers (nightly update, DQ sweep, weekly reconciliation, logical dump,
off-server copy). It maintains the universe as well as the
data: each night it applies ticker renames to the security that already holds the
history (FB → META keeps one `security_id`), re-reads the screener so a security
that listed today enters scope today, refreshes the *declared* universe (mutual
funds and anything else with no listing venue to be screened on), and marks what
stopped trading — then loads
prices, actions and factors. A newly listed security has no watermark, so it gets
its full available history on the same run.

## `duk` — dual-mode data access

`duk` keeps its full command surface (`ph`, `yc`, `ls`, `rc`, `ti`) and adds a
global `--source/-S`:

- `-S db` (default when a DSN is configured): reads the fafnir warehouse.
- `-S live`: reads the FMP API directly (the original standalone behaviour).

Return and indicator commands (`rc`, `ti`) operate on input files and are
source-agnostic. `yc` (yield curve) is live-only until the economic-series
fast-follow lands.

## Documentation

| Doc | Contents |
|---|---|
| [architecture.md](doc/architecture.md) | Layers, ERD, role model, design decisions |
| [data_dictionary.md](doc/data_dictionary.md) | Every table/column: grain, source, units, adjustment, cadence |
| [ingestion.md](doc/ingestion.md) | FMP endpoint→table map, idempotency, watermarks |
| [install_hetzner.md](doc/install_hetzner.md) | Fresh install on a Hetzner Cloud server: provision → Postgres → fafnir → cron |
| [backfill.md](doc/backfill.md) | Initial setup & historical backfill (bandwidth, chunking, resumability) |
| [operations.md](doc/operations.md) | Daily upkeep runbook, cron, backfill, recovery |
| [duk.md](doc/duk.md) | CLI usage (live vs db) |
| [agent.md](doc/agent.md) | Run an operations agent on the host: DQ triage, automations, data questions |
| [extending.md](doc/extending.md) | Add a source (FRED/BLS/BEA), a table, or an MCP tool |
| [adr/](doc/adr/) | Architecture decision records |

## Development

```bash
make test          # unit tests (no DB)
make test-int      # integration tests (needs FAFNIR_TEST_DSN)
make fmt           # black + isort + flake8
```

## License

MIT — see [LICENSE](LICENSE).
