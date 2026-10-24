# ADR 0017: Vendor access is one library inside fafnir, not a distribution per vendor

- Status: **Proposed** (draft, 2026-10-24)
- Date: 2026-10-24
- Depends on: [ADR 0011](0011-versioning.md) (one version, deployed by `git pull`),
  [ADR 0008](0008-remote-duk-access-and-mcp.md) (the MCP server is built on
  `duk.datasource.db`)
- Related: [ADR 0007](0007-incremental-corporate-actions.md) (an endpoint is adopted
  on a probe's evidence), [ADR 0015](0015-multi-vendor-provenance-and-precedence.md)
  (provenance, licence hygiene), [extending.md](../extending.md) (the add-a-source
  recipe), [duk.md](../duk.md), the
  [Sharadar adoption plan](../plans/sharadar-adoption.md)

## Context

Three changes were put forward together on 2026-10-24:

1. rename `duk` to `fafi`, keeping it the read-only client of the warehouse;
2. a standalone Sharadar CLI, with its own distribution, as fafnir's upstream source
   and as a tool for calling Sharadar directly, with fafnir's Sharadar-sourcing code
   ported into it;
3. the same for FMP.

The motive is sound: vendor access should be written once, usable without the
warehouse, and clearly separated from what the warehouse does with the data. This
ADR records what the code showed when the three were weighed, and what is done
instead.

### What the repository already has

`src/fafnir/sources/` is a vendor layer. `base.py` gives every client throttling,
retry with backoff, byte metering, secret redaction and payload hashing; `fmp.py`
and `sharadar.py` are domain clients on top. None of the four modules
(`base`, `fmp`, `sharadar`, `sharadar_files`) imports the database or a loader.
`duk -S sharadar` already takes its base URL, price tables and payload parser from
`fafnir.sources.sharadar`, which is the library boundary working as intended.

### What is wrong is duplication, not packaging

| | `duk/fmp_api.py` | `fafnir/sources/fmp.py` |
|---|---|---|
| lines | 1,281 | 501 |
| shared code | none | none |
| retry, throttle, byte metering | its own, no metering | `BaseSource` |
| redaction list | five query parameters | the same five plus the pre-signed download credentials (`x-amz-*`) |

Two FMP clients exist and have already drifted. `duk/sharadar_api.py` is half way:
it shares the constants and the parser but keeps its own `requests.get`, and it
sends the key as a query parameter where `SharadarClient` sends it in the
`x-api-key` header so that a URL rendered into an exception cannot carry it. A
third copy of each client, in a package of its own, would not remove either of
these; deleting the copies in `duk` does.

### What the warehouse needs from a client

A loader takes the parsed payload *and* the raw bytes (`BaseSource._get` returns
both), hashes the payload into `landing`, and writes `bytes_downloaded` and
`request_count` into `ops.ingestion_run`. Sharadar's file reader types every value
from its text as `Decimal`, never a float. A tool whose primary purpose is to call
a vendor from a shell wants CSV and DataFrames. `duk/fmp_api.py` is what a
CLI-first vendor client becomes: pandas out, floats, no metering, and unusable for
ingestion, which is why `FMPClient` was written beside it. A standalone CLI designed
around its own use would recreate that split at a package boundary.

### Client and loader change together

Of the last 80 commits, 7 touched `fafnir/sources/`; 6 of those also touched the
loaders, the repository or the CLI. `duk` changed in 4, every one alongside
`fafnir` or the MCP server. The next phases of the plan make the coupling tighter:
fundamentals on SF1 (P4) changes client constants, file typing and loaders in one
step; Tiingo and FRED (P5) add two vendors. One distribution per vendor would be
four or five, each with its own release, config file and test doubles, for code
that moves in lockstep with the loaders.

### A separate distribution undoes ADR 0011

ADR 0011 chose one version written in source and a deployment that is a `git pull`
into an editable install, because a version computed when pip runs goes stale.
A vendor package pinned from `pyproject.toml` brings the staleness back: pulling
fafnir no longer updates the client, and `fafnir --version` stops describing the
vendor code that is running. The repository builds with plain setuptools; several
distributions in one repository would need a workspace tool it does not have.
Nor is `duk` separable: `fafnir_mcp.tools` is built on `duk.datasource.db`
(ADR 0008), migration `0023` indexes expressions that must match `duk`'s SQL
character for character, and `test_mart_read_seam` asserts `duk`'s resolution
ladder matches `repository.resolve_security_id`.

### FMP is the wrong vendor for a standalone product

The plan reduces FMP to a lower tier, keeps it as the secondary feed, and leaves
cancellation to the licence letter (#43, #128). The tier's contents are not yet
verified. The treasury-curve and identifier-search endpoints exist only in `duk`'s
FMP client, and the plan moves the yield curve to FRED.

### Keys and configuration would fragment

`FafnirConfig.sharadar_api_key` reads `SHARADAR_API_KEY` from the environment only,
by design, so the warehouse's primary key is never in a file a session or a backup
copies. `duk` reads `[api].sharadar_key` from `~/.dukrc`. A third CLI is a third
policy for the same key on the same host.

## Decision

### 1. `fafnir.sources` is the only vendor code, and may not import the warehouse

Every HTTP request to a vendor, every endpoint constant and every vendor file
parser lives in `src/fafnir/sources/`. A module there imports the standard library,
`requests`, `fafnir.logging_config` and other `fafnir.sources` modules, and nothing
else: no `fafnir.db`, no `fafnir.ingest`, no `fafnir.config`, no `duk`. A test
(`test/fafnir/test_sources_boundary.py`) parses the imports and fails on anything
else, so the boundary cannot erode quietly.

Three things in `sources/` today are warehouse code and move out:

| Module | Why it is not vendor code | Where it goes |
|---|---|---|
| `sources/probe.py` | imports the price loader's field aliases and `_validate_bar`; it verifies the feed against the loader's assumptions | `ingest/fmp_probe.py` |
| `sources/sharadar_files.py`: `ledger_path`, `checked_path`, `rows(db, …)` | read `landing.sharadar_file` through the repository | `ingest/sharadar_ledger.py` |
| `sources/vendor_gaps.py` | a report over `core`, `landing` and `ops` | `ingest/vendor_gaps.py` |

`sharadar_probe.py` stays: it reads only the client, and is exactly the kind of
vendor diagnostic a standalone user would want.

### 2. `duk` keeps no HTTP code

`duk`'s vendor sources are adapters that turn a shared client's rows into the
DataFrame contract the CLI and the compute modules share. `duk/sharadar_api.py`
calls `SharadarClient.query`; `duk/fmp_api.py` keeps its function names (the CLI,
`duk.ls_utils` and `duk.datasource.live` call them) but every one of them calls
`FMPClient`, which gained the endpoints only `duk` read: treasury rates, the
`search-cik` / `search-isin` / `search-cusip` lookups, the dividend-adjusted bars,
the actively-trading list and a filtered one-page screen. With that, one redaction
list, one retry policy and one set of endpoint constants serve the warehouse,
`duk` and the MCP server, and the probe (#49) corrects one module.

### 3. A vendor console script, if one is wanted, is an entry point in this distribution

A `sharadar` (or `fmp`) command that queries a table, downloads a bulk file without
recording it, pings and probes is a thin layer over the existing client and costs
one line in `[project.scripts]`. It is built when someone needs it, not ahead of
that, and it shares the client, the version and the deploy with everything else.

### 4. No separate distribution now; one condition would reopen it

If installing the clients on a machine that has no warehouse, without `psycopg`,
`pandas` and `scipy`, becomes a real need, the unit to split out is **one**
`fafnir-vendors` package holding `base`, every client and every file reader, never
one package per vendor. That is reconsidered after P4 (SF1) has shipped, when the
Sharadar client has stopped changing with the loaders; not before.

### 5. The rename is not decided here

`duk` → `fafi` is a naming question with its own trade-offs and is independent of
the vendor layer. It should be decided on its own. If it is done: `duk` stays as an
alias entry point and `~/.dukrc` is still read for at least one release, because
the name is in `~/.dukrc`, the default output directory, `application_name`
values in DSNs, `Bash(duk *)` allowlists in agent settings, `scripts/reconcile.sh`
and its systemd timer. One caution for that decision: `duk` versus `fafnir`
currently spells the reader-versus-writer boundary in the command name itself,
and `fafi` versus `fafnir` is easier to confuse in an allowlist.

## Alternatives considered

- **One standalone distribution per vendor** (the proposal). Rejected for the
  reasons in the context: it adds a package boundary where the code co-changes, it
  undoes ADR 0011's deploy guarantee, it does not by itself remove the duplicate
  clients, and it multiplies with every vendor the plan adds.
- **One `fafnir-vendors` distribution now.** Closer to right, and kept as the
  fallback in §4. Deferred because the Sharadar client is still being corrected by
  the probe and extended for SF1, and no machine needs the clients without the
  warehouse today.
- **Leave `duk`'s clients as they are.** Rejected: the drift is already measurable,
  and every endpoint correction has to be made twice.

## Consequences

- `duk -S sharadar` gains the client's retry, throttle and header-borne key; its
  error messages are `SourceError`'s, redacted the same way the warehouse's are.
- `duk -S fmp`, `duk yc` and `duk ls -S fmp` gain the same: `FMPClient` carries
  their five endpoints (dividend-adjusted bars, treasury rates, the actively-trading
  list, the identifier searches, the filtered screen), `duk.fmp_api` keeps only the
  frame shaping, and the boundary test asserts nothing under `duk` imports
  `requests` or names a vendor host. One visible change: `duk -S fmp ph` now returns
  one bar per date in ascending order, as the warehouse's reads always did, and a
  long `--from` is chunked below the vendor's 5000-bar cap instead of silently
  truncated.
- `fafnir.sources` can be imported with the `requests` package alone, which is what
  §4 would need, and what a notebook needs today.
- Callers of the three moved modules change their imports; the CLI commands under
  `fafnir source` keep their names.
- The plan's decision log records this ADR; `extending.md`'s recipe gains the
  import rule.

## Implementation notes

Roughly in order, each independently useful:

1. **This ADR**, the boundary test and the three moves in §1; `duk/sharadar_api.py`
   onto `SharadarClient` (§2, Sharadar half). *Started 2026-10-24.*
2. `FMPClient.treasury_rates`, `search_identifier`, `eod_dividend_adjusted`,
   `actively_trading_list` and `screen`; `duk/fmp_api.py` loses its request code
   and calls them; the boundary test asserts `duk` imports neither `requests` nor
   a vendor URL. *Shipped 2026-10-24.*
3. `redact_secrets` has one home, `fafnir.sources.base`; `duk.fmp_api` re-exports
   it. *Shipped 2026-10-24, with 2.*
4. The console scripts of §3, if and when wanted.
5. Mark this ADR Accepted once 1-3 have run on the host and `duk -S fmp ph`,
   `duk yc` and `duk -S fmp ls --sectors` answer as before.
