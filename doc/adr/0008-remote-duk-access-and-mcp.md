# ADR 0008: Remote `duk` under per-person credentials, and a local stdio MCP server

- Status: Proposed
- Date: 2026-09-02
- Depends on: [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md)
- Related: [install_hetzner.md §11](../install_hetzner.md), [extending.md](../extending.md),
  [duk.md](../duk.md)

## Context

`duk` is used two ways today. On the warehouse host it runs as the `fafnir` OS user
over the Unix socket, with no password anywhere (`peer` + the `fafnirmap` ident map,
install §3.6). From a laptop it runs through an SSH tunnel and authenticates as the
**shared** `fafnir_app` role with a **shared** password in `~/.pgpass` (install §11).

Two things are wanted:

1. **Remote reads under a person's own credentials**, not a role the whole
   installation shares.
2. **An MCP server**, so the same reads are available to agentic workflows.
   `extending.md` already reserves the seam: connect as `fafnir_app`, mirror the
   `duk` db-mode reads, reuse `duk.datasource.db` rather than re-implementing SQL.

### What is wrong with the shared role

`fafnir_app` is correctly scoped — `SELECT` on `mart` and `ref`, nothing else
(migration `0001`). The problem is not its privileges, it is that it has no owner:

- **Nothing is attributable.** `pg_stat_activity`, `pg_stat_statements` and the
  server log all say `fafnir_app`. A query that pinned a core for an hour cannot be
  traced to a person, a laptop, or an agent.
- **Nothing is revocable in isolation.** Removing one person's access means rotating
  the password for everyone and every app that holds it.
- **The credential spreads.** A shared secret in `~/.pgpass` on every machine that
  ever reads the warehouse is the one artifact that is hardest to inventory and
  hardest to expire.

### What the MCP requirement adds

An MCP server is a new class of consumer: its queries are chosen by a model, from
text that may include content nobody on this side wrote. That does not make it
dangerous — reads are reads — but it does settle a design question up front. A tool
schema is a *usability* boundary. It shapes what a well-behaved model does; it stops
nothing. The boundary that holds is the database role.

So the MCP question is not "which tools are safe to expose" but "what can the
identity behind those tools do at all, if every argument goes wrong at once".

## Decision

Three parts, each of which stands on its own but which fit together into a single
credential story: **the SSH key is the only secret; the Postgres role is the only
authorization.**

### 1. Transport — forward the Unix socket, not the TCP port

Postgres keeps `listen_addresses = 'localhost'` and 5432 stays closed at both
firewalls (install §3.4, §3.7, §1.3). Tunnel over the SSH port already allowed:

```bash
# On the laptop. Note the remote *socket path*, not 127.0.0.1:5432.
ssh -N -L 15432:/var/run/postgresql/.s.PGSQL.5432 rob@<SERVER_IP>
```

Forwarding to the socket rather than the loopback port is what makes part 2
possible. The connection arrives at Postgres as a `local` connection, opened by the
`sshd` process running as the authenticated user — so `peer` authentication sees
that user's OS identity, exactly as it does for the nightly job.

Keep the tunnel alive without thinking about it, via `~/.ssh/config`:

```
Host fafnir
    HostName <SERVER_IP>
    User rob
    LocalForward 15432 /var/run/postgresql/.s.PGSQL.5432
    ExitOnForwardFailure yes
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 30m
    ServerAliveInterval 30
```

`ssh -fN fafnir` once per session; every later `ssh fafnir` reuses the multiplexed
connection. A user-level `systemd --user` unit or a launchd agent works too, and is
what to reach for if the MCP server should be able to start cold.

### 2. Identity — one OS account and one (or two) Postgres roles per person

No database password is issued at all.

**On the server, once per person** (superuser; per-person roles are deliberately
*not* in a migration — they are deployment facts, not schema):

```bash
sudo adduser --disabled-password --gecos '' rob
sudo install -d -m 700 -o rob -g rob /home/rob/.ssh
# rob's public key only -- the SSH key is now the credential for everything below.
sudo -u rob tee /home/rob/.ssh/authorized_keys <<'KEY'
restrict,port-forwarding,command="/usr/sbin/nologin" ssh-ed25519 AAAA... rob@laptop
KEY
sudo chmod 600 /home/rob/.ssh/authorized_keys
```

`restrict` turns everything off; `port-forwarding` re-enables just the tunnel, and
the forced `command` means an interactive session gets nothing. `ssh -N` never runs
the command, so the tunnel is unaffected.

```sql
-- fafnir_app stops being a login role and becomes the group that holds the grants.
-- Members inherit SELECT on mart + ref and nothing else, with no new GRANT needed.
CREATE ROLE rob     LOGIN IN ROLE fafnir_app;   -- interactive duk
CREATE ROLE rob_mcp LOGIN IN ROLE fafnir_app;   -- agent traffic

ALTER ROLE rob     SET default_transaction_read_only = on;
ALTER ROLE rob     SET statement_timeout = '60s';
ALTER ROLE rob     SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE rob     CONNECTION LIMIT 8;

-- The agent gets its own budget: shorter queries, fewer connections.
ALTER ROLE rob_mcp SET default_transaction_read_only = on;
ALTER ROLE rob_mcp SET statement_timeout = '20s';
ALTER ROLE rob_mcp SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE rob_mcp CONNECTION LIMIT 4;
```

```
# /etc/postgresql/16/main/pg_ident.conf -- one OS user, two roles it may become.
fafnirmap  rob  rob
fafnirmap  rob  rob_mcp
```

The existing `pg_hba.conf` rule generalises from `fafnir_ingest` to the map:

```
# Must stay ABOVE the generic "local all all peer" line -- first match wins.
local   all   +fafnir_app   peer map=fafnirmap
```

Nothing else changes. `fafnir_app` keeps every grant it has; `ALTER ROLE fafnir_app
NOLOGIN` once no deployment connects as it directly.

Why two roles per person rather than one: agent traffic and human traffic then
separate in `pg_stat_activity`, `pg_stat_statements` and the log without any
convention to remember, get independent timeouts and connection budgets, and can be
throttled or revoked independently — `ALTER ROLE rob_mcp NOLOGIN` stops the agents
and leaves the person working.

Revocation has two independent switches, either sufficient: remove the key
(transport) or `REVOKE fafnir_app FROM rob` / `ALTER ROLE rob NOLOGIN`
(authorization).

**On the laptop**, `duk` needs no code change — it already takes a full DSN:

```toml
# ~/.dukrc
[database]
dsn = "host=127.0.0.1 port=15432 dbname=fafnir user=rob application_name=duk"

[general]
default_source = "db"
```

No `~/.pgpass`, no `PGPASSWORD`, no secret on disk.

### 3. MCP — a stdio server on the laptop, not a service on the warehouse

Add `src/fafnir_mcp/` with a `fafnir-mcp` console script and an `mcp` optional
dependency group. It runs as a **child process of the AI client**, speaks stdio,
listens on no port, and holds no secret. Its DSN points through the same tunnel and
authenticates as `rob_mcp`:

```jsonc
{
  "mcpServers": {
    "fafnir": {
      "command": "/Users/rob/.venvs/fafnir/bin/fafnir-mcp",
      "env": {
        "FAFNIR_DSN": "host=127.0.0.1 port=15432 dbname=fafnir user=rob_mcp application_name=fafnir-mcp"
      }
    }
  }
}
```

Tools mirror the `duk` db-mode surface and call `duk.datasource.db` directly, as
`extending.md` prescribes — same SQL, same DataFrame contracts, same symbol
resolution (`_resolve_security_id`), so an agent and a human reading the same
security get the same series:

| Tool | Backed by |
|---|---|
| `price_history` (raw or adjusted) | `db.price_history` → `core.daily_price` / `mart.v_daily_price_adjusted` |
| `screen_securities` | `db.screen` → `mart.security_latest` |
| `list_sectors` / `list_industries` | `db.list_sectors` / `db.list_industries` |
| `resolve_symbol` | `db._resolve_security_id` + `mart.security_latest` |
| `returns`, `indicator` | `duk.return_utils`, `duk.indicators` — pure compute on a prior result |

Design rules that follow from "the role is the boundary":

- **No free-form SQL tool.** Parameterized tools only. It is the one addition that
  would turn a prompt injection into an arbitrary query, and it buys an agent
  nothing that a named tool cannot.
- **Every result is row-capped** with an explicit `truncated` flag, both because
  context is finite and because an unbounded result is the cheapest denial of
  service against the tunnel.
- **Errors are structured messages, not tracebacks** — and a failed connection says
  *"the SSH tunnel to the warehouse is not up"*, since that is the failure that will
  actually happen.
- Dates in, dates out, ISO; no locale-dependent formatting. `duk`'s CLI formatting
  layer stays in the CLI.

## Alternatives considered

**Expose 5432 to the internet over TLS.** Workable — Cloud Firewall pinned to source
IPs, a real certificate, `hostssl`, clients on `sslmode=verify-full`. It buys nothing
the tunnel does not already provide, costs a certificate lifecycle, and breaks the
day a home IP changes. Install §11 already reaches this conclusion.

**TCP forward + per-person `scram-sha-256` passwords.** The honest runner-up. Its one
real advantage: a TCP forward can be pinned in `authorized_keys` with
`permitopen="127.0.0.1:5432"`, which the socket variant cannot express — so under the
chosen option, someone who can SSH can also forward to other local ports. That is a
person who is already trusted with an account on the host, and both firewalls still
stand between them and anything else. The cost of the alternative is a password per
person to distribute, store and rotate, which is the exact problem being solved.
Choose it only where the forward must be constrained.

**Remote execution — `ssh fafnir duk -S db ph AAPL --adj`.** No tunnel, no local
psycopg, output over stdout. But a forced-command wrapper has to allowlist argv to
stay safe, and `rc`/`ti` stop working on local files, which is half of what `duk` is
for. Reasonable as a convenience alias; not the access model.

**MCP as an HTTP/SSE service on the warehouse host.** Rejected. It needs a port, a
certificate, and an authorization system of its own — a second one, next to the
Postgres roles that already work — and it places a model-driven query engine *inside*
the trust boundary rather than outside it. Revisit only when several people or hosted
agents need it, at which point that authorization system is the whole design problem.

**MCP shelling out to the `duk` CLI.** Re-parsing formatted human output, with a
second error surface and a second set of type coercions. `duk.datasource.db` is
already a library.

**A VPN (WireGuard / Tailscale).** Strictly better than a tunnel once there are
several machines or a phone, and compatible with everything above. It is more
infrastructure than one person needs, and — the point worth keeping — it solves
transport only. The per-person role is the part that matters, and it is unchanged
either way.

## Consequences

- **No database password exists** for human or agent reads. The credential is the
  SSH key, which is already the credential for the host.
- **Every query is attributable** to a person and a channel. `application_name`
  distinguishes `duk` from `fafnir-mcp` within a person's traffic.
- **Adding a person** is: OS account + key, two lines in `pg_ident.conf`, two
  `CREATE ROLE ... IN ROLE fafnir_app`. Removing one is a single command in either
  layer.
- **The tunnel must be up.** `ControlPersist` or a user-level service handles it;
  the MCP server must fail loudly and specifically when it is not.
- **`mart` is the whole agent-visible world.** Anything an agent should be able to
  read has to exist in `mart` (or a `mart` view) — which is the existing read seam,
  and keeps `landing` and `core` out of reach by construction. Adjusted prices stay
  point-in-time stable for free (`mart.v_daily_price_adjusted`).
- **Install §11 stays valid** as the shared-role path until this is implemented; it
  should then be rewritten to the per-person model, with the shared `fafnir_app`
  password retired.
- Per-person roles are **not** managed by migrations. `0001` continues to own the
  three functional roles and the grants; who is a member of `fafnir_app` is a
  deployment fact, documented in the operations runbook.

## Implementation notes

Roughly in order, each independently useful:

1. **Docs first, no code**: rewrite install §11 for the socket forward + per-person
   roles; add an "adding a reader" section to `operations.md`; note the
   password-free DSN in `duk.md`.
2. `src/fafnir_mcp/` — server, tool definitions, row caps, connection diagnostics;
   `mcp` as an optional dependency group; `fafnir-mcp` console script.
3. Unit tests for tool argument validation and truncation; an integration test
   (`FAFNIR_TEST_DSN`) asserting `price_history` through MCP and through
   `duk -S db` return the identical series.
4. A `scripts/` helper that checks the tunnel (`ssh -O check fafnir`) and reports
   plainly, for use in both the MCP error path and a smoke test.
5. Optional once nothing depends on it: `ALTER ROLE fafnir_app NOLOGIN`.
