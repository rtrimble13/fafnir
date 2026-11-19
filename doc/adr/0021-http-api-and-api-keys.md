# ADR 0021: An HTTP API for `duk`, authenticated by API key

- Status: Accepted
- Date: 2026-11-17
- Implemented by: migration 0046 (`ops.api_key`), `src/fafnir_api/` (`keys.py`,
  `app.py`, `cli.py`; the `fafnir-api` console script and the `api` extra),
  `fafnir api key create | list | revoke`, `src/duk/datasource/wire.py`,
  `src/duk/datasource/remote.py`, `src/fafnir/sources/warehouse.py` (the
  request itself), `src/duk/datasource/db.py` (`_served`,
  `SERVED_READS`), `src/duk/datasource/base.py` (`ApiAddress`),
  `src/duk/config.py` (`FAFNIR_API_URL`, `FAFNIR_API_KEY`, `[api].fafnir_url`,
  `[api].fafnir_key`), `etc/api/`
- Amends: [ADR 0008](0008-remote-duk-access-and-mcp.md), which rejected an HTTP
  service on the warehouse host. The tunnel of ADR 0008 stays, unchanged and still
  the stronger path; this adds a second one beside it.
- Related: [ADR 0009](0009-mart-is-the-read-seam.md) (`mart` is the read seam),
  [ADR 0010](0010-on-host-operations-agent.md) (a read tier that cannot write),
  [install_hetzner.md §14](../install_hetzner.md#14-the-http-api-optional),
  [duk.md](../duk.md#without-a-tunnel-the-http-api)

## Context

From anywhere but the host, `duk` reaches the warehouse through an SSH tunnel to
the PostgreSQL socket (ADR 0008). The SSH key is the only secret and the Postgres
role the only authorization, and that is as good as this gets.

It also requires SSH. Three places `duk` is wanted cannot provide it:

- **A sandboxed or hosted agent.** A cloud session can make HTTPS requests to an
  allowlisted host and nothing else: no outbound port 22, no long-lived tunnel
  process, and no private key it should be handed. This is how such a session
  already reads Sharadar: the host is allowed and the key is supplied to the
  request, never to the session.
- **A machine that is not set up for it.** The tunnel needs an OS account on the
  host, a key, a `~/.ssh/config` stanza and a service to keep the forward alive
  ([duk.md](../duk.md#from-claude-code-on-a-laptop)). That is right for the
  machine one works on daily and heavy for the second one.
- **Anything that is not a full computer.** A phone, a notebook service, a CI job.

ADR 0008 considered a service on the host and rejected it: *"it needs a port, a
certificate, and an authorization system of its own — a second one, next to the
Postgres roles that already work"*. All three are still true, and this ADR
accepts them. What has changed is not the cost but that there are now readers the
tunnel cannot serve at any cost.

So the question is how small the second authorization system can be, and how
little the service can be trusted with.

## Decision

### 1. The API serves `duk`'s reads, by name, and nothing else

`duk.datasource.db` is already the whole of the warehouse's read surface: 22
functions that take a connection and return frames, rows or dicts, reading only
`mart` and `ref` (ADR 0009). The API is those functions, callable over HTTPS:

```
POST /v1/reads/price_history
Authorization: Bearer ffn_3f9c0a1b22d4_…
{"args": {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": null, "adjusted": true}}
```

The service (`fafnir-api`, on the host) finds the function in
`duk.datasource.db.SERVED_READS`, runs it against PostgreSQL over the local
socket, and returns what it returned. There is one more authenticated route,
`GET /v1/reads` (the names served, the server's version, the key's name), and an
unauthenticated `GET /v1/health`.

On the client, a read marked `@_served` checks what it was given as `dsn`. A
libpq DSN runs the read locally, as before. An `ApiAddress` sends the same call to
the host (`duk.datasource.remote`; the HTTP request is made by
`fafnir.sources.warehouse`, since `duk` keeps no HTTP code of its own,
[ADR 0017](0017-vendor-access-is-one-library-not-a-distribution-per-vendor.md) §2).
The CLI does not know which happened: every
`ds_db.x(dsn=cfg.dsn, …)` call site is unchanged, and `-S db` still means "the
warehouse".

Three things follow from serving functions rather than tables:

- **One implementation.** The resolution ladder, the adjusted/raw choice, the
  `limit` rule and the resampling run on the host, in the code the DSN path runs.
  The two transports cannot disagree about a series, because only one of them
  computes it. `test_api_integration.py` runs every served read both ways against
  one seeded warehouse and compares the results value for value and type for
  type, and runs `duk` commands both ways and compares the output.
- **No query language.** A request names a read and gives its arguments. It
  cannot name a table, a column, or a predicate the read does not already take.
- **The surface cannot drift.** Decorating a read is the whole of serving it, and
  the server's list is the client's list. A test fails if a public function in
  `duk.datasource.db` is not decorated.

**Why not the shape of Sharadar's API**, which is what prompted this
(`/datatables/SHARADAR/SEP?ticker=…&date.gte=…`): a table API moves the logic to
the client. `duk` would resolve a ticker in one request and read prices in a
second, reimplementing the ladder against whatever the tables expose, and a
generic filter grammar is a query language with a different spelling: the thing
ADR 0008 kept out of the MCP surface for good reason. What is kept from
Sharadar's design is the part that was actually wanted: an HTTPS endpoint and a
key.

### 2. The wire carries types, not text

`ph --exact` prints `Decimal` values as the warehouse stores them; a frame has a
`DatetimeIndex` and an `int64` volume column. JSON has neither. A client that read
a price through a float or a date through a string would print something other
than the DSN path prints, and the difference would be in the last digit, where it
is not noticed.

`duk.datasource.wire` therefore tags what JSON cannot say (`{"$dec":
"189.8400"}`, `{"$date": …}`, `{"$frame": {columns, dtypes, index, attrs}}`) and
decodes each to the value that was encoded. A type it does not carry is refused,
not passed through `str`. The format is `duk`'s own, versioned by the `/v1` in the
path. It is documented in the module and is not a general-purpose API for other
clients: one that wants the warehouse without `duk` can call it, but will have to
read the tags.

### 3. A key is a random secret, stored as its hash

```
ffn_<key_id: 12 hex>_<secret: 32 random bytes, base64url>
```

- `fafnir api key create NAME` prints the key once. `ops.api_key` (migration
  0046) stores its SHA-256, its `key_id`, its name, who issued it and when, an
  optional expiry, and a revocation stamp. The key itself is in no table and no
  backup.
- A plain SHA-256, not a password hash: the secret is 256 random bits, so there
  is nothing to slow down, and the check runs on every request.
- The server finds the row by `key_id` and compares digests in constant time. An
  unknown `key_id` and a wrong secret get the same answer.
- `fafnir api key revoke NAME` stamps the row; it is never deleted, so a name in
  an access log can always be traced. A running server honours a revocation
  within 30 seconds, the lifetime of its key cache.
- The key travels in `Authorization: Bearer`. A key in the query string, which is
  how Sharadar takes one, is not accepted: a URL is written to proxy logs, access
  logs and shell history. `duk` refuses to send a key over plain HTTP to anything
  but a loopback address.

That is the entire second authorization system: one table, one comparison. A key
is all-or-nothing over the served reads. There are no scopes, because there is
nothing to scope: every served read is one `fafnir_app` may already run.

### 4. The Postgres role is still the boundary

The key decides who may ask. What can be done once asked is decided where it
always has been:

- The service connects as a login role that is a member of `fafnir_app`: `SELECT`
  on `mart` and `ref`, plus `SELECT` on `ops.api_key` and nothing else in `ops`.
  If every check in the service were wrong at once, that is still all it could
  read, and it could write nothing.
- `fafnir-api` **refuses to start** as a role that can write, and `fafnir-api
  --check` reports the role, its timeout and its privileges. This is
  `fafnir-mcp`'s rule (ADR 0010), for the same reason: every route reads, so a
  misconfigured DSN would never fail on its own.
- Its DSN comes from `--dsn` or `FAFNIR_API_DSN` only. Not `~/.fafnirrc` and not
  `FAFNIR_DSN`, which on the host name `fafnir_ingest`.
- The role's `statement_timeout` bounds a request; a query that hits it is
  answered `504`, not left running.
- `ops.api_key` is revoked from `fafnir_ops`, which 0021's default privileges
  would otherwise have given it. The ops tier is what an agent reads with, and who
  holds a key is nothing the DQ queue needs.

**Attribution.** ADR 0008 gives each person a Postgres role so that
`pg_stat_activity` names them. The API has one role for every key, so it puts the
key's name in `application_name` (`fafnir-api:rob-laptop`) and in its own access
log (`key=rob-laptop read=price_history status=200 ms=14`). That answers "whose
query is this" and "what did this key read". It does not give per-key
`pg_stat_statements`, timeouts or connection limits, which per-person roles do:
see the alternatives.

### 5. It listens on loopback; the proxy holds TLS

`fafnir-api` speaks plain HTTP on `127.0.0.1:8787`. A reverse proxy (Caddy, in
the install guide) terminates TLS on 443 and forwards to it. The service has no
certificate, no private key and no public port, and PostgreSQL stays on
`listen_addresses = 'localhost'` exactly as before. The firewall gains one rule:
443, to the proxy.

The application is standard-library WSGI (`fafnir_api.app`), so it is testable
with no server and has no framework to keep patched. `waitress` serves it and is
the `api` extra's only dependency; a client needs no extra at all.

### 6. Configuration: an address in place of a DSN

```toml
# ~/.dukrc
[api]
fafnir_url = "https://fafnir.example.com"
fafnir_key = "ffn_…"            # or FAFNIR_API_KEY in the environment
```

The environment beats the file, and at each level a DSN beats an API address:
`FAFNIR_DSN`, then `FAFNIR_API_URL`, then `[database].dsn`, then
`[api].fafnir_url`. A machine that can open a database connection uses it.

## What this does not do

- **Vendor sources.** `-S fmp` and `-S sharadar` never touched the warehouse and
  are unchanged.
- **The MCP server.** `fafnir-mcp` still takes a DSN. Its `read` profile is these
  same reads and could be given an address later; its `ops` profile and
  `sql_read` are host-only by design (ADR 0010) and must never be reachable
  through a key.
- **Writes.** There are none. Every change to the warehouse goes through the
  `fafnir` CLI on the host.
- **Row caps.** A read returns over the API what it returns over a DSN. Capping
  it would make the two transports disagree, which is the one thing §1 exists to
  prevent. The bound is the role's `statement_timeout` and the per-key rate limit
  (600 requests a minute by default).

## Alternatives considered

**A Sharadar-style table API.** §1.

**PostgreSQL on a public port, over TLS, with a password per person.** The
"key" is then a Postgres password and there is no service to write. ADR 0008
already rejected it, and the reasons are stronger here: it exposes the database's
own protocol and every pre-auth bug in it to the internet, and it does not serve
the reader this ADR is for, since a sandbox that allows HTTPS to one host does
not allow port 5432.

**A VPN (Tailscale / WireGuard).** No code, no public port, and it removes the
tunnel-keeping chore for any machine that can join. It is the better answer for a
second laptop, and nothing here prevents using it. It does not reach a session
that cannot install a network interface, and it is not an API key.

**One Postgres role per key.** The service would connect as the key's own role
(a `pg_ident` map from its OS user to each), restoring ADR 0008's attribution in
full: per-key `pg_stat_statements`, timeouts and connection limits. The cost is a
`CREATE ROLE`, a `pg_ident.conf` line and a reload for every key, by a superuser,
which turns "issue a key" from one command into a deployment change. Not taken
now. `ops.api_key` can gain a `db_role` column when the first key needs its own
budget; nothing about the wire or the client changes.

**OAuth, or signed tokens (JWT).** They buy delegation and expiry without a
lookup. There is nobody to delegate to, and the lookup is one indexed row,
cached. A revocable random key is what a single-operator service needs, and is
what the reader's side (an environment variable, a secrets manager, a proxy that
injects a header) already knows how to hold.

**A web framework (FastAPI / Starlette).** Three routes and a JSON body do not
need one, and each dependency on a public service is a dependency to patch. The
`mcp` extra already pulls Starlette in, but only on hosts that serve an agent.

**Arrow or Parquet on the wire.** Faster for a large frame, and typed. But half
of what the reads return is not a frame (nested dicts of `Decimal` and `date`),
`pyarrow` is a large dependency for a CLI that defers `pandas` to save 0.3s, and
the largest frame `duk` asks for is one security's history: a few hundred
kilobytes of JSON, compressed by the proxy.

## Consequences

- **The host has a public HTTPS port.** It needs a DNS name, a certificate (the
  proxy renews it) and patching of the proxy and of `waitress`. Before this, the
  only thing listening to the internet was `sshd`.
- **A key is a bearer secret on someone's disk.** Whoever copies it reads the
  warehouse until it is revoked. An SSH key never leaves its machine and can be
  held in an agent; this can do neither. Keep keys per device, name them for the
  device, give anything unattended an expiry, and prefer the tunnel where it
  works.
- **Publishing is now a property of `duk.datasource.db`.** A read added there
  and decorated is on the API at the next deploy. That is the same rule `mart`
  already has (ADR 0009: a view there is a grant to every reader), one layer up.
- **A new type needs the wire to carry it.** A read that starts returning, say, a
  `timedelta` fails over the API with a `WireError` until `wire.py` learns it.
  The integration test has a case for every served read so that this fails in CI.
- **Client and host can be different versions.** A `duk` newer than the host may
  ask for a read the host does not have; it is told so in one line (*the host's
  fafnir is older than this duk*). Upgrade the host first.
- **Unauthenticated requests are not rate-limited by the service.** The per-key
  limit applies after a key is accepted. A stream of bad keys costs one indexed
  lookup per distinct `key_id` and nothing else, and a guess at a 256-bit secret
  does not succeed; a host that wants to drop such traffic earlier does it at the
  proxy or with `fail2ban` on the access log.
- **ADR 0008 is not withdrawn.** Its tunnel is still how a person's daily machine
  should read, still the only way to the MCP server from a laptop, and still the
  only path with no secret at rest.
