# ADR 0010: An on-host operations agent — a fourth read tier, and mutations stay on the CLI

- Status: Accepted
- Date: 2026-09-03
- Depends on: [ADR 0008](0008-remote-duk-access-and-mcp.md) (per-agent roles),
  [ADR 0009](0009-mart-is-the-read-seam.md) (`mart` is the read seam)
- Implemented by: `sql/migrations/0021_ops_reader_role.up.sql`, `src/fafnir_mcp/`,
  `.claude/skills/fafnir-dba/`
- Related: [plan: database-operations agent](../plans/db-operations-agent.md),
  [doc/agent.md](../agent.md)

## Context

ADR 0008 designs an MCP server for a **laptop**: a stdio child of an AI client,
tunnelled to the warehouse, authenticating as a per-person agent role that is a
member of `fafnir_app`. ADR 0009 then makes `mart` the whole of what such a role
can see, and draws one line explicitly — `mart.v_security_dq_open` shows open flags
only, `record_key` in, `detail` out, because `adjustment_failed` writes a raw Python
exception string into `detail`.

That surface is exactly right for the job it was designed for: reading market data,
and being able to ask whether a series is trustworthy before reasoning over it.

A second job has since been asked for, and it is a different job: an agent running
**on the warehouse host**, beside the data, that triages the data-quality queue,
assists with the nightly automations, and explains what the warehouse holds. The
first of those cannot be done on the ADR 0008 surface at all:

| What triage needs | Reachable by `fafnir_app`? |
|---|---|
| `ops.data_quality_flag.detail` — the measured move, the prior close, the lost high/low | **No** — deliberately excluded from the mart view |
| resolved flags, `resolved_by`, `resolution_note` — what was decided before | **No** — the open-only filter is what keeps them off the seam |
| `ops.ingestion_run` — which load wrote the flag, and whether it failed | **No** — `ops` is granted to `fafnir_ingest` alone |
| `landing.fmp_raw.payload` — what the vendor actually sent | **No** — `landing` is unreachable by every read role |
| changing anything | **No** — and correctly so |

So the question this ADR settles is not "how do we build the ADR 0008 server" — that
is unchanged and still wanted. It is: **what identity does an on-host operations
agent hold, and what can it do if every tool argument goes wrong at once?**

### What being on the host changes, and what it does not

ADR 0008 §"Alternatives" rejects *"MCP as an HTTP/SSE service on the warehouse
host"* — because it needs a port, a certificate, and an authorization system of its
own, next to the Postgres roles that already work. Every one of those objections is
about the **service**, not the **host**. A stdio child process of a local `claude`
listens on nothing, terminates no TLS, and authorizes nothing itself. That rejection
does not bind this decision, and the reasoning behind it — *the Postgres role is the
authorization system* — is what this ADR applies.

What genuinely changes is the blast radius. On a laptop the worst outcome of a
confused agent is a slow query. On the host it is a data-quality queue quietly
closed, or a security merged into another. That asymmetry is what the three tiers
below are arranged around.

## Decision

**Three tiers. The third is not a database role.**

### 1. Read — `fafnir_app`, unchanged

The ADR 0008 surface, built as ADR 0008 specifies: parameterized tools over `mart`
and `ref`, no free-form SQL, every result row-capped. A per-agent login role that is
a member of `fafnir_app`. This tier is not host-specific and works over the §11
tunnel from a laptop exactly as written.

### 2. Ops read — `fafnir_ops`, a new functional role

Migration `0021` adds a fourth functional role holding `SELECT` on `core`, `mart`,
`ref`, `ops`, `landing` and `meta`, and nothing else: no write privilege of any
shape, no `CREATE` on any schema, no sequence access. It is `NOLOGIN` — a group that
holds grants, with per-agent login roles as members, which is ADR 0008's identity
model applied one tier up. `meta` is included because *"is this host running the
schema the repo expects?"* is an operations question and `meta.schema_migration` is
its answer.

**It is a role, not `mart` views, and that is the load-bearing part.** ADR 0009's
second rule is that adding a `mart` view is a grant to *every* mart reader — every
person, every laptop, every agent. Serving this agent's need for `detail` by
relaxing `mart.v_security_dq_open` would therefore hand raw exception strings and
human-written resolution notes to the entire read seam, silently, in a one-line
diff that looks like a convenience. A separate role opens a separate door.
`test_ops_tier_did_not_widen_the_app_tier` compares the two roles directly so that
mistake fails a test rather than passing review.

`GRANT fafnir_read TO fafnir_ops` is deliberately **not** how the `core`/`mart`/`ref`
half is inherited. Granting a role to a role needs `ADMIN OPTION` on the grantee,
which the migrator holds only when it created that role itself — on a least-privilege
install where a superuser pre-created the roles (install §3.5) it does not, so the
statement would fail on precisely the deployments this project targets. Granting the
object privileges directly needs only ownership, which the migrator always has.

### 3. Free-form SQL is permitted in tier 2, and forbidden in tier 1

ADR 0008 states: **no free-form SQL tool** — *"it is the one addition that would turn
a prompt injection into an arbitrary query, and it buys an agent nothing that a named
tool cannot."* Both halves are true of tier 1. Neither survives contact with tier 2:

- **"buys nothing a named tool cannot."** *"Which securities have three or more open
  `gap` flags whose dates all fall on days when most of the universe also has no
  bar?"* is the question that separates an exchange holiday from a broken loader,
  and it is not a tool signature. A fixed tool set for investigation is a fixed set
  of hypotheses — which is the opposite of investigating.
- **"turns a prompt injection into an arbitrary query."** True, and the honest answer
  is that the tool schema was never what bounded it. `fafnir_ops` is
  `default_transaction_read_only`, has no write privilege to fall back on, runs under
  a `statement_timeout`, and the server opens a `READ ONLY` transaction and rejects
  anything that is not a single `SELECT`/`WITH` before sending it. The worst
  arbitrary query is a slow read that is killed. That is ADR 0008's own principle —
  *"the boundary that holds is the database role"* — applied rather than contradicted.

The permission is therefore **scoped, not general**: `sql_read` is registered only
under `--profile ops`, by not existing at all under `--profile read` rather than by a
check at call time. Anything reachable from a laptop keeps ADR 0008's rule intact.

If any one of those five constraints is dropped — ops profile, on-host, read-only
role, read-only transaction with a statement allowlist, row cap — this is no longer
the exception that was argued for.

### 4. Mutations run the `fafnir` CLI, not MCP tools

The agent holds **no writable database role at all.** Every change goes through
`fafnir <verb>` as the `fafnir` OS user (`sudo -u fafnir`, one narrow sudoers rule),
under a Bash allowlist that puts every mutation behind a permission prompt.

Three reasons, in order of weight:

- **The guard rails exist, once.** `dq resolve` refuses ids-and-filters and
  ids-nor-filters, makes an unknown `--symbol` fatal, never rewrites an
  already-resolved flag, and confirms a filtered resolve. `security merge-rename`
  refuses unless the vendor identifiers agree and the overlapping sessions agree on
  OHLC. `security dismiss-rename` requires a reason. Re-implementing these behind
  MCP tools means two copies of every rule and a drift discovered the night they
  disagree — the duplication ADR 0008 already declined for reads (*"`duk.datasource.db`
  is already a library"*).
- **`--dry-run` and `--yes` already are the interaction model.** The agent runs the
  dry run, shows it, and only then runs the effect. An MCP tool would have to invent
  that ceremony and then be trusted to observe it.
- **Attribution is free, and is the audit.** `--by` defaults to the OS user and the
  skill pins it to `claude`, so `WHERE resolved_by LIKE 'claude%'` is the complete
  record of everything the agent ever closed, and `fafnir dq reopen` reverses any of
  it.

The cost, stated plainly: the agent must not parse CLI output to *reason*. It does
not have to — tier 2 reads the same rows as SQL, and `fafnir dq list --json` exists.
**The CLI is for effects; tier 2 is for facts.** Blurring that is how a second,
untested read path gets built by accident.

## Consequences

- **`fafnir db migrate` fails on a least-privilege install until `fafnir_ops`
  exists.** Migration `0021` cannot `CREATE ROLE` without `CREATEROLE`, so it raises
  the same actionable, `42501`-coded error `0001` raises for the other three. The
  upgrade step is one statement as a superuser, and it is in the runbook
  ([doc/agent.md](../agent.md), [operations.md](../operations.md)). This is a
  deliberate repeat of an existing behaviour rather than a new one to learn.
- **A fourth role to keep in mind when adding a schema.** Anything new that the
  operational record should include needs `fafnir_ops` in its grants —
  `ALTER DEFAULT PRIVILEGES` covers tables added by later migrations, but a new
  *schema* does not grant itself. The same recurring tax ADR 0008 §4 named for
  `mart` views, for the same reason: it forces the decision to be made.
- **The agent's credential cannot change data.** Revocation has the two independent
  switches ADR 0008 established: `ALTER ROLE claude_ops NOLOGIN` (authorization) or
  removing the sudoers rule (effects). Either alone is sufficient for its half.
- **Vendor text is now an untrusted input path that reaches a decision-maker.**
  `core.company_profile.description` and `core.security.company_name` are
  third-party strings the agent reads and summarises. Nothing about the tunnel ever
  blocked this; it is new only because there is now a model on the reading end. It
  is mitigated, not eliminated: every role the agent holds is read-only, tier 3 is
  an allowlist the model cannot widen, every mutation is a prompt, and the skill
  states that vendor-sourced text is data. The residual risk is an injected
  instruction persuading the agent to *ask* for a mutation a human then approves —
  which is why the dry run is shown before the effect, and why the effect is a
  prompt at all.
- **Queue churn is the likeliest quiet damage**, not a dramatic one: an agent that
  closes flags plausibly and steadily while the data rots. `resolved_by` makes it
  visible and `dq reopen` makes it reversible; the skill's standing rule is that
  resolving is a judgement, never a repair, and that a filtered resolve is always
  preceded by its own dry run.
- **Per-agent roles stay out of migrations**, exactly as ADR 0008 requires. `0021`
  owns the functional role and the grants; who is a member of it is a deployment
  fact, documented in [doc/agent.md](../agent.md).

---

## Amendment, 2026-09-04: proactive triage sweeps

**The tier model is unchanged.** No new role, no new privilege, no write tool. This
records a change in how the agent is *asked* to work, and the mitigations that came
with it.

### What changed

The skill previously described a reactive loop: a person asks about a security or a
check, the agent investigates that one thing. It now also supports a **sweep** — on
request, the agent works the whole open queue on its own initiative, groups it by
condition, and proposes one batched `dq resolve` per group.

Two options were considered and rejected:

- **Unattended resolution for a narrow allowlist.** It would have weakened this
  ADR's central claim from *every mutation is an interactive approval* to *every
  judgement call is*, and the boundary between those two is a matter of opinion
  about which checks are "obvious" — precisely the opinion a confused agent holds
  most confidently. The prompt stays.
- **A nightly timer running the sweep unattended.** That is the configuration in
  which the prompt-injection path this ADR already names (*"vendor text is now an
  untrusted input path that reaches a decision-maker"*) has no human in the loop at
  all. Rejected for that reason alone; the residual risk was acceptable only
  *because* a human sees the dry run.

### Why bulk needed its own rules

Working one flag is a judgement. Working eighty is a judgement that decays: the
second flag looks like the first, and somewhere around the fortieth *"looks like"*
has replaced *"was checked"*. The queue churn named under Consequences above is not
a hypothetical for a sweep — it is its default failure mode.

Three mitigations, all of them reads:

- **`dq_triage`** (ops profile, read-only) returns `cohort_size` — how many
  securities share a `check_name` on a `record_key` date, aggregated over the
  **whole** open queue rather than the returned page. A cohort counted after
  `LIMIT` shrinks with the page size, which would make a missed load look like a
  market fact at `limit=20`: the exact error the column exists to prevent. One
  security with a gap is a market fact; two hundred is one missed load, and the
  sweep must propose a re-ingest rather than two hundred resolutions.
- **`prior_resolutions`** surfaces a condition that was closed before and came
  back. At two, the sweep stops. A condition recurring after two closures is a
  defect nobody repaired, and the third closure is the churn itself.
- **`NEVER_AUTO_RESOLVE`** is a floor in code, mirroring the skill's standing rule
  5. It is advisory rather than enforced — the effect runs through the CLI, which
  is where this ADR deliberately put it — but it appears per row in the tool
  output, so it is in front of the decision rather than something to have
  remembered. `test_never_auto_matches_the_skill` parses `SKILL.md` and fails if
  the two lists diverge, which is the same anti-duplication argument §4 makes
  against re-implementing the CLI's guards, applied to a policy that genuinely does
  have to exist twice.

### What this does not change

- Mutations remain CLI-only. `dq_triage` reads; it resolves nothing. Adding a
  `dq_resolve` MCP tool would still mean two copies of the CLI's guards.
- The dry-run-then-effect ceremony is unchanged, and now applies per batch rather
  than per flag.
- `--by claude` still makes `WHERE resolved_by LIKE 'claude%'` the complete record,
  and `fafnir dq reopen` still reverses any of it. Batching makes both *more*
  important: read the notes, and read them by batch.
