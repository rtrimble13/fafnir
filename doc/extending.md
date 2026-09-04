# Extending Fafnir

Fafnir is built to grow. This guide covers the planned fast-follows and the
patterns to add new sources, tables, and consumers.

## Roadmap

| Milestone | Adds | Status |
|---|---|---|
| **Initial release** | security master, daily OHLCV, corporate actions, dual-mode duk | ✅ shipped |
| **Fundamentals** | income/balance/cash-flow statements, key metrics, ratios (bitemporal) | planned |
| **Economic series** | FRED, BLS, BEA time series; treasury curve for `duk yc` (db) | planned |
| **MCP server** | agentic read access over `mart`, plus an on-host ops tier | ✅ shipped |
| **Intraday** | `core.intraday_bar` (TIMESTAMPTZ) | planned |

## Adding a new data source (FRED / BLS / BEA)

1. **Client.** Add `src/fafnir/sources/<name>.py` subclassing `BaseSource`
   (`sources/base.py`) — you inherit throttling, retry/backoff, and bandwidth
   metering. Implement domain methods returning parsed payloads. Add the API key to
   `FafnirConfig` (already stubbed: `fred_key`, `bls_key`, `bea_key`).
2. **Schema.** Add a migration `sql/migrations/NNNN_<name>.up.sql` (+ `.down.sql`)
   for the new tables. Economic series fit a tidy shape:
   `core.economic_series(series_id, source, title, units, frequency, seasonal_adj)`
   and `core.economic_observation(series_id, obs_date, value, vintage_date, loaded_at)`
   — note `vintage_date` for point-in-time (ALFRED-style) revisions.
3. **Loader.** Add `src/fafnir/ingest/<name>.py` using `RunLog`, `land_payload`,
   watermarks, and key-based upserts — mirror `ingest/daily_price.py`.
4. **CLI.** Add an `ingest <name>` subcommand in `fafnir/cli.py`.
5. **Tests.** Unit-test validation; integration-test idempotency + a point-in-time
   read.

## Adding fundamentals (bitemporal)

Model statements **bitemporally** so restatements version rather than overwrite:

```
core.income_statement(security_id, fiscal_date, period,
                      filing_date, valid_from, valid_to,
                      revenue NUMERIC(24,2), net_income NUMERIC(24,2), ...,
                      reported_currency, ingestion_run_id, loaded_at)
  grain: (security_id, fiscal_date, period, filing_date)
```

Point-in-time queries filter `filing_date <= as_of_date`. `period` is pinned to
FMP's vocabulary (`annual`/`quarter`) at the boundary. The surrogate id and the
three-timeline design (ADR 0002) make this additive.

## The MCP server

**Shipped** — `src/fafnir_mcp/`, a `fafnir-mcp` console script, and an `mcp`
optional dependency group. Two profiles, and the difference between them is a
privilege decision rather than a convenience:

| Profile | Role | Reads | Tools |
|---|---|---|---|
| `read` | member of `fafnir_app` | `mart`, `ref` | `resolve_symbol`, `price_history`, `screen_securities`, `list_sectors`/`list_industries`, `security_profile`, `dq_summary` |
| `ops` | member of `fafnir_ops` (0021) | + `core`, `ops`, `landing`, `meta` | + `dq_queue`, `dq_triage`, `dq_totals`, `ingestion_runs`, `watermarks`, `landing_payload`, `schema_state`, `sql_read` |

The `read` profile is the surface [ADR 0008 §3](adr/0008-remote-duk-access-and-mcp.md)
specifies and runs from a laptop through the §11 tunnel unchanged. The `ops`
profile is for an agent on the warehouse host and is settled in
[ADR 0010](adr/0010-on-host-operations-agent.md) — including why it may run
free-form read-only SQL when the `read` profile may not. Deploying either is
[doc/agent.md](agent.md).

Three rules for anyone extending it:

- **Reuse `duk.datasource.db`.** Every read-profile tool is backed by it, so an
  agent and a person reading the same security get the same rows. A tool with its
  own SQL for something `duk` already reads is a second read path that will
  eventually disagree with the first.
- **The tool layer imports no MCP SDK.** `fafnir_mcp/tools.py` takes plain
  arguments and returns plain dicts; `server.py` is the only SDK-facing file.
  That keeps validation, row caps and error wording unit-testable without an SDK
  installed, and confines a major SDK bump to one module.
- **Profiles are enforced by registration, not by a check at call time.** An
  ops-only tool must not exist on the read profile — `test_profiles.py` asserts
  the registered tool set for exactly this reason, since both profiles are built
  by the same function and the read profile looks complete either way.

A new tool needs: the implementation in `tools.py`, registration in `server.py`
under the right profile, its name in `test_profiles.py`'s `READ_TOOLS` or
`OPS_ONLY_TOOLS`, and — if it reads a new relation — a `mart` view or a
`fafnir_ops` grant, whichever tier it belongs to.

## Schema change hygiene

- Migrations are code: versioned, reviewed, reversible, one logical change each.
  Never edit an applied migration (the runner detects checksum drift) — add a new
  one.
  - The single exception is a revision that leaves an already-migrated database
    *identical* — e.g. relaxing the privileges a statement needs. Then record the
    previous checksum in `SUPERSEDED_CHECKSUMS` (`src/fafnir/db/migrate.py`) so
    existing deployments are re-stamped instead of reporting drift they cannot
    act on. Anything that touches the schema still needs a new migration.
- Backfills are separate, batched, restartable — not an unbounded `UPDATE`.
- Never change a fact table's grain in place; that is a data-loss risk. Plan it
  explicitly with a new table + migration.
