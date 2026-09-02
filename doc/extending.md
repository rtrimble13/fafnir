# Extending Fafnir

Fafnir is built to grow. This guide covers the planned fast-follows and the
patterns to add new sources, tables, and consumers.

## Roadmap

| Milestone | Adds | Status |
|---|---|---|
| **Initial release** | security master, daily OHLCV, corporate actions, dual-mode duk | ✅ shipped |
| **Fundamentals** | income/balance/cash-flow statements, key metrics, ratios (bitemporal) | planned |
| **Economic series** | FRED, BLS, BEA time series; treasury curve for `duk yc` (db) | planned |
| **MCP server** | agentic read access over `mart` | planned |
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

## Adding the MCP server

The read seam is `mart` (+ `core` read views). An MCP server should:

- connect as the least-privilege `fafnir_app` role (mart read-only);
- expose tools mirroring `duk` db-mode reads (price history adjusted/raw, screen,
  security lookup) returning the same shapes;
- reuse `fafnir.db.repository` read functions or the `duk.datasource.db` adapters
  rather than re-implementing SQL.

Because adjusted prices are derived in `mart.v_daily_price_adjusted`, MCP clients
get point-in-time-stable data for free.

Where the server runs, which identity it connects as, and why it exposes no
free-form SQL tool are settled in
[ADR 0008](adr/0008-remote-duk-access-and-mcp.md) — which also covers reaching the
warehouse from a laptop under a person's own credentials, since the MCP server uses
the same path.

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
