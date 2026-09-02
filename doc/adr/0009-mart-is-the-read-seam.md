# ADR 0009: `mart` is the whole read seam, and definer-rights views are how

- Status: Accepted
- Date: 2026-09-02
- Depends on: [ADR 0008 §4](0008-remote-duk-access-and-mcp.md)
- Implemented by: `sql/migrations/0020_company_summary_marts.up.sql`

## Context

Migration `0001` gives `fafnir_app` — the role `duk -S db`, external apps and (per
ADR 0008) every per-person and per-agent role inherit from — `SELECT` on `mart` and
`ref`, and nothing else. `fafnir_read` adds `core`. Neither reaches `ops`, which is
granted to `fafnir_ingest` alone.

Two reads then had nowhere to go:

1. **`duk`'s own price path.** It read `core.symbol_xref` to resolve a ticker and
   `core.daily_price` for raw bars. Measured as `fafnir_app`: both denied, so
   `duk -S db ph` failed at *resolution*, before either price relation, with and
   without `--adj`. It had gone unnoticed because the developer and nightly paths
   run as `fafnir_ingest`, and install §11's other example (`ls --sector`, pure
   `mart`) worked.
2. **A per-security data-quality summary** for `duk ls <ticker>`. `ops` is
   unreachable by *every* read role, so no amount of picking a different connection
   role would have served it.

## Decision

**`mart` is the complete read seam.** No client outside the write path reads `core`
or `ops` directly; anything a reader needs exists as a `mart` relation. Migration
`0020` adds the six views that make this true, and `duk.datasource.db` names `mart`
throughout.

**Definer rights are the mechanism, and are load-bearing.** A PostgreSQL view runs
with its *owner's* privileges unless created `WITH (security_invoker = true)`. The
`mart` views are owned by the migrator, which holds `core` and `ops`, so a
`mart`-only role reads through them without ever being granted the schemas beneath.

Three rules follow, and they are the reason this is an ADR rather than a commit
message:

- **Never set `security_invoker = true` on a `mart` view.** It would silently
  restore the failure this exists to fix — silently, because the developer writing
  it connects as a role that can read `core` anyway.
- **Adding a `mart` view is a grant.** It gives every `mart` reader — every person,
  every agent — whatever it selects. It is a deliberate act, reviewed as one, not a
  convenience for one caller.
- **Prefer a view that depends on no column you may need to re-type.** Migration
  `0013` had to drop `mart.v_daily_price_adjusted` to widen
  `core.adjustment_factor`'s numerics; a second dependent view would have doubled
  that cost for every future change. `mart.v_security_action_summary` therefore
  takes `COUNT(*)` and `MAX(effective_date)` from that table and nothing else — a
  client wanting back-adjustment depth reads `price_factor` from
  `mart.v_daily_price_adjusted`, where the dependency already exists.

### What the DQ window exposes, and what it does not

`mart.v_security_dq_open` is the first `ops` relation ever readable outside
`fafnir_ingest`, so its column list is a decision rather than a default. Open flags
only; `record_key` in, `detail` out.

- `record_key` holds `{"trade_date"}`, `{"symbol","date"}`, `{"ex_date"}`,
  `{"last_date"}`, `{"effective_date"}` — dates and tickers, every one derived from
  data a `mart` reader already reads in full through `mart.v_daily_price_raw`. It
  costs nothing and is what lets a summary name the offending bar instead of saying
  only that something is wrong.
- `detail` is mostly derived values too — but `adjustment_failed` writes
  `{"error": "<ExcType>: <message>"}`, a raw Python exception string that can carry
  psycopg internals, constraint names and paths. It is the one DQ field not derived
  from readable market data, and it stays behind `fafnir dq list`.
- `resolved_by` / `resolution_note` — the human judgements from `0017` — are kept
  off the seam by the `resolved_at IS NULL` filter, since
  `ck_dq_flag_resolution_provenance` forces both `NULL` while a flag is open.
  **That safety is in the `WHERE` clause, not the column list.** Anyone widening
  this view to show resolved flags removes it, and must exclude the columns
  explicitly instead. A test asserts their absence so the trap is sprung in CI
  rather than in production.

Triage detail stays on the warehouse host as `fafnir_ingest`. A laptop or agent
learns *that* a series carries two open `gap` flags and *which* bars; it does not
get the queue.

## Consequences

- `duk -S db` works as `fafnir_app`, which is what install §11, `architecture.md`'s
  role table and ADR 0008 all already claimed. The claim is now tested, not
  asserted: `test_migrations_least_privilege.py` reads every relation `duk` names
  as `fafnir_app`, and separately proves the tables beneath are still denied.
- **A recurring cost, accepted deliberately.** Every future `core` table a reader
  needs wants a `mart` view — fundamentals and economic series each add one. The
  failure mode is a forgotten view, discovered late by whoever is on the
  least-privilege path. Two guards: the privilege test above, and a unit test
  asserting `duk.datasource.db` names no `core`/`ops` relation, which runs without
  a database so it fires on every run rather than only where `FAFNIR_TEST_DSN` is
  set. The second matters more — the breakage this ADR fixes survived precisely
  because the check that would have caught it needed a database nobody had
  configured.
- `fafnir_read` is unchanged and keeps `core`. Research and notebooks lose nothing;
  this changes only which relations *`duk`* names.
- Rolling `0020` back returns `duk` to needing `core`, so it must be rolled back
  together with the `duk` version that names those relations. Recorded in the
  down-migration.

## Alternatives considered

**Document `fafnir_read` as `duk`'s role; demote `fafnir_app` to screening-only.**
Zero code, and an honest description of how the system ran. Rejected: it makes
`core` a de facto public API the moment an agent reads it, which forfeits the
freedom to change a fact table's grain that `extending.md` relies on; it leaves an
agent able to ask for the whole warehouse unfiltered (`statement_timeout` bounds the
damage, not the attempt); it removes the guarantee that a client cannot read
unadjusted prices believing they are adjusted; and it leaves `fafnir_app` with no
user. It also would not have avoided editing ADR 0008, since the per-person roles
would have had to inherit `fafnir_read` instead. The difference between the two
options was two views.

**`GRANT USAGE ON SCHEMA core TO fafnir_app`.** One line — and, because `0001`
already grants `SELECT` on `core`'s tables to `fafnir_app` by default privileges, it
makes the two read roles identical. That is the previous alternative with extra
steps and a redundant role.

**`SECURITY DEFINER` functions instead of views.** Equivalent privilege behaviour
with a worse interface: no relational composition, no query planning through the
call, and a larger surface to audit. Views are the right shape for reads.
