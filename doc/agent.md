# Running an operations agent on the warehouse host

An agent that triages the data-quality queue, diagnoses the nightly automations,
and answers questions about what the warehouse holds — running as an unprivileged
OS user on the fafnir host, beside the data.

Decided in **[ADR 0010](adr/0010-on-host-operations-agent.md)**, which depends on
[ADR 0008](adr/0008-remote-duk-access-and-mcp.md) (per-agent roles) and
[ADR 0009](adr/0009-mart-is-the-read-seam.md) (`mart` is the read seam). Read the
ADR for *why*; this is *how*.

## The model in one picture

```
                 ┌──────────────────────────────────────────────┐
  claude (OS)    │  claude CLI ──stdio──► fafnir-mcp             │
                 │      │                  --profile ops  ───────┼──► claude_ops
                 │      │                                        │      └─ fafnir_ops
                 │      │                                        │         core mart ref
                 │      │                                        │         ops landing meta
                 │      │                                        │         SELECT only
                 │      └── Bash (allowlisted) ──►               │
                 │            sudo -u fafnir fafnir … ───────────┼──► fafnir_ingest
                 └──────────────────────────────────────────────┘         (peer auth)
```

Three properties hold this together:

- **The agent has no writable database role.** Reads go through a role that holds
  `SELECT` and nothing else. `fafnir-mcp` refuses to start as a role that can
  write, so this is a mechanism rather than a convention.
- **Every change runs the existing CLI**, which already holds the guard rails —
  ids-xor-filters, fatal unknown symbol, the merge-rename identifier checks,
  `--dry-run`, `--by`/`--note` provenance.
- **Nothing is a secret.** No password is issued to any of it; peer authentication
  over the Unix socket makes the OS identity the credential.

## Install

### 1. The role (once per database, superuser)

Migration `0021` creates `fafnir_ops` when the migrator has `CREATEROLE`. On a
least-privilege install it does not, so **`fafnir db migrate` fails with an
actionable error until the role exists** — the same behaviour `0001` has for the
other three roles.

PostgreSQL roles are **cluster-global**, not per-database, so the role may already
exist because another database on this cluster has it — in which case `migrate`
just works and this step is a no-op. Running it is harmless either way:

```bash
# "role already exists" here means nothing to fix -- carry on to migrate.
sudo -u postgres psql -d fafnir -c 'CREATE ROLE fafnir_ops NOLOGIN;'
sudo -u fafnir fafnir db migrate           # applies 0021
```

`fafnir_ops` is `NOLOGIN` on purpose: it is a group that holds grants, never a
principal that connects.

### 2. The OS account and the per-agent roles

```bash
sudo adduser --disabled-password --gecos '' claude
```

```sql
-- Two roles, because the agent reads at two tiers and they should be
-- distinguishable in pg_stat_activity, pg_stat_statements and the log.
CREATE ROLE claude_ops LOGIN IN ROLE fafnir_ops;   -- the MCP ops profile
CREATE ROLE claude_app LOGIN IN ROLE fafnir_app;   -- duk spot checks

ALTER ROLE claude_ops SET default_transaction_read_only = on;
ALTER ROLE claude_ops SET statement_timeout = '30s';
ALTER ROLE claude_ops SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE claude_ops CONNECTION LIMIT 4;

ALTER ROLE claude_app SET default_transaction_read_only = on;
ALTER ROLE claude_app SET statement_timeout = '20s';
ALTER ROLE claude_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE claude_app CONNECTION LIMIT 4;
```

The timeouts are not decoration. An unbounded query is the cheapest denial of
service against the warehouse, and a model writes unbounded queries by accident;
`fafnir-mcp --check` warns when a role has none.

### 3. Authentication (`pg_ident.conf`, `pg_hba.conf`)

One OS user, two roles it may become:

```
# /etc/postgresql/16/main/pg_ident.conf
fafnirmap  claude  claude_ops
fafnirmap  claude  claude_app
```

`pg_hba.conf` today carries the role-specific line install
[§3.6](install_hetzner.md#36-password-free-local-auth-for-the-nightly-job-recommended)
writes, which does not cover the new roles. Generalise it to the group form
ADR 0008 §2 proposes — it then covers every present and future member with no
further edits — keeping all of it **above** the generic `local all all peer` line,
since first match wins:

```
local   all   +fafnir_ops     peer map=fafnirmap
local   all   +fafnir_app     peer map=fafnirmap
local   all   fafnir_ingest   peer map=fafnirmap
```

> This is the one step that touches a file the nightly job depends on. Reload and
> confirm the nightly path still authenticates **before** going further:
>
> ```bash
> sudo systemctl reload postgresql@16-main
> sudo -u fafnir psql -d fafnir -c 'select 1'     # must still work
> sudo -u claude psql -d fafnir -U claude_ops -c 'select current_user'
> ```

### 4. `sudo`, narrowly

```bash
sudo visudo -f /etc/sudoers.d/claude-fafnir
```

Contents in [`etc/agent/sudoers.example`](../etc/agent/sudoers.example): one
target user, one absolute binary. Not the venv's `python` (arbitrary code as the
write identity), not `scripts/*` (`reset_data.sh` lives there), not `postgres`.

### 5. The MCP server

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/pip install -e '/opt/fafnir[mcp]'
sudo -u claude FAFNIR_DSN='dbname=fafnir user=claude_ops' \
     /opt/fafnir/.venv/bin/fafnir-mcp --profile ops --check
```

`--check` is the step to run first. It reports the role, whether the connection is
read-only, the statement timeout, USAGE on each schema the profile needs, and —
the one that matters — whether the role can write. Expect:

```
role:    claude_ops
read-only default: on
statement_timeout: 30s
schema access:
  mart      yes
  ref       yes
  core      yes
  ops       yes
  landing   yes
  meta      yes

can write core.daily_price: no

OK
```

Then register it. Copy [`etc/agent/mcp.json.example`](../etc/agent/mcp.json.example)
to the agent's working directory as `.mcp.json`. Note what is absent from it: no
host, no port, no password. An empty host is the local socket, which is what makes
peer authentication apply.

### 6. The agent's own config

| File | From | Why |
|---|---|---|
| `~claude/.claude/settings.json` | [`etc/agent/claude-settings.json.example`](../etc/agent/claude-settings.json.example) | the permission boundary |
| `~claude/.dukrc` | [`etc/agent/dukrc.example`](../etc/agent/dukrc.example) | `duk -S db` spot checks as `claude_app` |
| the `fafnir-dba` skill | `.claude/skills/fafnir-dba/` in the checkout | the triage playbooks and standing rules |

**Do not give the `claude` user a `~/.fafnirrc`.** The deployment's names
`fafnir_ingest`, the write role; a copy would hand the agent the write identity
for every CLI read. `fafnir-mcp` will not read it either — its DSN is explicit or
absent, for exactly this reason.

## What it can and cannot do

| | |
|---|---|
| **Reads, unattended** | every MCP tool; `scripts/monitor.sh`; `systemctl status`/`list-timers`; `journalctl -u fafnir-*`; `sudo -u fafnir fafnir status`/`dq list`/`db status`; `duk -S db …` |
| **Changes, on approval** | `ingest prices\|actions\|delisted\|symbol-changes`, `adjust`, `db refresh-marts`, `dq resolve`/`reopen`, `track rm`, `security merge-rename`/`dismiss-rename` — each after showing its `--dry-run` |
| **Refused outright** | `psql`, `pg_dump`, `sudo -u postgres`, `reset_data.sh`, `db migrate`/`rollback`, `systemctl restart`/`stop`, reading `fafnir.env` / `.pgpass` / `.fafnirrc`, editing `/opt/fafnir` |

`psql` is denied deliberately: it is the hole through which the tier model leaks,
since the `claude` user could open it as any role `pg_ident` permits. SQL goes
through `sql_read`, which runs in a read-only transaction, refuses anything that
is not a single `SELECT`, and caps rows.

Editing `/opt/fafnir` is denied because **the checkout is deployed, not
developed**: a fix made there is invisible to the repository, destroyed by the
next `git pull`, and leaves the deployed code untrustworthy in a way nothing
detects. Code changes go through a branch and a PR.

## Auditing it

Every resolution the agent makes is attributed, because the skill pins
`--by claude`:

```sql
-- everything the agent has ever closed, and why
SELECT dq_flag_id, check_name, security_id, resolved_at, resolution_note
  FROM ops.data_quality_flag
 WHERE resolved_by LIKE 'claude%' ORDER BY resolved_at DESC;

-- its live queries, separable from human traffic by application_name
SELECT usename, application_name, state, query_start, left(query, 120)
  FROM pg_stat_activity WHERE usename LIKE 'claude%';
```

**Read those notes for the first month.** The likeliest damage from this
arrangement is not dramatic — it is an agent closing flags plausibly and steadily
while the data rots. `fafnir dq reopen <id>` reverses any of it, and the note goes
with it. This is the same discipline `operations.md` prescribes for watching
`corporate_action_drift` through its first thirty nights, for the same reason: a
mechanism that looks right is not yet evidence that it is.

## Revoking it

Two independent switches, either sufficient for its half:

```sql
ALTER ROLE claude_ops NOLOGIN;   -- authorization: it can no longer read
ALTER ROLE claude_app NOLOGIN;
```

```bash
sudo rm /etc/sudoers.d/claude-fafnir   # effects: it can no longer change anything
```

Neither disturbs the nightly job, any other person's access, or the other tier.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `fafnir db migrate`: *role fafnir_ops is missing and the current user lacks CREATEROLE* | 0021 cannot create the role it grants to, and no other database on this cluster has it | `CREATE ROLE fafnir_ops NOLOGIN;` as a superuser, then re-migrate (step 1) |
| `0021` applied without the `CREATE ROLE` step | Roles are cluster-global; another database on this cluster already had `fafnir_ops` | Nothing to do — verify with `fafnir-mcp --profile ops --check` |
| `fafnir-mcp: refusing to start … can INSERT` | the DSN names a write role, usually because `FAFNIR_DSN` was unset and something supplied `fafnir_ingest` | point it at `claude_ops`; never at `fafnir_ingest` |
| `fafnir-mcp: no DSN` | `FAFNIR_DSN` missing from the MCP `env` block | step 5; `~/.fafnirrc` is deliberately not a fallback |
| `--check`: *no USAGE on core, ops, landing, meta* | the role is not a member of `fafnir_ops` | `GRANT fafnir_ops TO claude_ops;` |
| *the role cannot authenticate* | the `pg_hba` peer rule landed **below** `local all all peer`, or `pg_ident` lacks the mapping | step 3 — order is first-match-wins |
| Tools work, `sudo -u fafnir fafnir …` does not | sudoers rule absent, or the binary path differs | step 4; check `/opt/fafnir/.venv/bin/fafnir` exists |
| `sql_read`: *refused: that statement writes* | a `SELECT … INTO`, or a data-modifying CTE | intended — changes go through the CLI |
| `sql_read`: *exceeded the role's statement_timeout* | an unbounded query | narrow it: a date range, a `security_id`, or an aggregate |
| Agent reports a screen that looks stale | `mart.security_latest` is materialized | `fafnir db refresh-marts`; or read `mart.v_security_profile`, which is live |
