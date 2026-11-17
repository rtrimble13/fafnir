"""
``fafnir-api`` -- serve the warehouse's HTTP API (ADR 0021), or check the role it
would serve as.

    fafnir-api --check          # who am I to PostgreSQL, and can I only read?
    fafnir-api                  # serve on 127.0.0.1:8787

The DSN comes from ``--dsn`` or ``FAFNIR_API_DSN`` and from nowhere else. Not from
``~/.fafnirrc``, and not from ``FAFNIR_DSN``: on the warehouse host both name
``fafnir_ingest``, the write role, and a service that fell back to either would
answer the internet as the identity that owns the write path, and would look like
it was working. This is ``fafnir-mcp``'s rule (ADR 0010), for the same reason.

It listens on loopback, in plain HTTP. The public port and TLS belong to the
reverse proxy in front of it (doc/install_hetzner.md §14).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from fafnir_api import __version__
from fafnir_api.app import DEFAULT_RATE

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
_LOOPBACK = ("127.0.0.1", "::1", "localhost")

# What the serving role must be able to read, and one relation it must not be
# able to write. `mart.security_latest` stands for the warehouse: a role that can
# INSERT there is not the read role, whatever else is true of it.
_PROBE_SQL = """
    SELECT current_user AS role,
           current_setting('default_transaction_read_only') AS read_only,
           current_setting('statement_timeout') AS statement_timeout,
           has_schema_privilege(current_user, 'mart', 'USAGE') AS mart,
           has_schema_privilege(current_user, 'ref', 'USAGE') AS ref,
           has_schema_privilege(current_user, 'ops', 'USAGE') AS ops
"""
_KEYS_READABLE_SQL = (
    "SELECT has_table_privilege(current_user, 'ops.api_key', 'SELECT') AS ok"
)
_WRITABLE_SQL = (
    "SELECT has_table_privilege(current_user, 'mart.security_latest', 'INSERT') "
    "OR has_table_privilege(current_user, 'ops.api_key', 'INSERT') AS writable"
)


def _resolve_dsn(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    return os.environ.get("FAFNIR_API_DSN", "").strip()


def probe(dsn: str) -> dict:
    """What the role behind ``dsn`` is and can do. Raises what psycopg raises when
    the database cannot be reached."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        found = dict(conn.execute(_PROBE_SQL).fetchone())
        found["keys_readable"] = False
        found["writable"] = False
        found["live_keys"] = None
        if found["ops"]:
            found["keys_readable"] = bool(
                conn.execute(_KEYS_READABLE_SQL).fetchone()["ok"]
            )
        if found["mart"] and found["ops"]:
            found["writable"] = bool(conn.execute(_WRITABLE_SQL).fetchone()["writable"])
        if found["keys_readable"]:
            found["live_keys"] = conn.execute(
                "SELECT count(*) AS n FROM ops.api_key WHERE revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())"
            ).fetchone()["n"]
    return found


def problems(found: dict) -> list[str]:
    """Why this role must not serve the API. Empty: it may."""
    out = []
    if found["writable"]:
        out.append(
            f"role {found['role']!r} can write. The API must serve as a role that "
            "reads mart and ref and writes nothing: a login role IN ROLE fafnir_app "
            "(doc/install_hetzner.md §14)"
        )
    for schema in ("mart", "ref"):
        if not found[schema]:
            out.append(f"role {found['role']!r} has no USAGE on {schema}")
    if not found["keys_readable"]:
        out.append(
            f"role {found['role']!r} cannot read ops.api_key, so no key can be "
            "checked: GRANT USAGE ON SCHEMA ops and SELECT ON ops.api_key to it "
            "(migration 0046; doc/install_hetzner.md §14)"
        )
    return out


def _check(dsn: str) -> int:
    try:
        found = probe(dsn)
    except Exception as exc:  # noqa: BLE001 -- reported, in one line
        print(f"FAILED: cannot connect: {exc}", file=sys.stderr)
        return 1
    print(f"role:              {found['role']}")
    print(f"read-only default: {found['read_only']}")
    print(f"statement_timeout: {found['statement_timeout']}")
    print(f"reads mart / ref:  {'yes' if found['mart'] and found['ref'] else 'NO'}")
    print(f"reads ops.api_key: {'yes' if found['keys_readable'] else 'NO'}")
    print(f"can write:         {'YES -- WRONG' if found['writable'] else 'no'}")
    if found["live_keys"] is not None:
        print(f"live keys:         {found['live_keys']}")
    if found["statement_timeout"] in ("0", "0ms"):
        print(
            "warning: no statement_timeout on this role. Set one (ALTER ROLE ... "
            "SET statement_timeout = '30s'): it is what bounds a request."
        )
    if found["read_only"] != "on":
        print(
            "warning: default_transaction_read_only is off for this role. Set it "
            "on: the grants already forbid writes, and this says so twice."
        )
    failed = problems(found)
    for line in failed:
        print(f"FAILED: {line}", file=sys.stderr)
    if failed:
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fafnir-api",
        description=(
            "HTTP API over the fafnir warehouse: duk's reads for a client with no "
            "database connection, authenticated by an API key (ADR 0021). Reads "
            "only. Keys are issued with `fafnir api key create`."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="libpq DSN of the read role [default: $FAFNIR_API_DSN]. "
        "~/.fafnirrc and $FAFNIR_DSN are deliberately not used: they name the "
        "write role.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FAFNIR_API_HOST", DEFAULT_HOST),
        help="Address to listen on [default: %(default)s]. Keep it loopback: this "
        "server speaks plain HTTP, and the reverse proxy in front of it holds TLS.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FAFNIR_API_PORT", DEFAULT_PORT)),
        help="Port to listen on [default: %(default)s]",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help="Requests served at once [default: %(default)s]. Each holds one "
        "database connection while it runs; keep it under the role's "
        "CONNECTION LIMIT.",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=int(os.environ.get("FAFNIR_API_RATE", DEFAULT_RATE)),
        help="Requests per key per minute; 0 for no limit [default: %(default)s]",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Connect, report the role and what it can do, and exit. Run this "
        "before the first start.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print(
            "fafnir-api: no DSN.\n"
            "  Set FAFNIR_API_DSN in the service's environment, or pass --dsn.\n"
            "  ~/.fafnirrc and FAFNIR_DSN are deliberately NOT used: they name the "
            "write role.\n"
            '  Example: FAFNIR_API_DSN="dbname=fafnir user=fafnir_api"\n'
            "  See doc/install_hetzner.md §14.",
            file=sys.stderr,
        )
        return 2
    if args.check:
        return _check(dsn)

    try:
        import waitress
    except ImportError:
        print(
            "fafnir-api: serving needs waitress. Install the api extra: "
            "pip install -e '.[api]'",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    # A role that can write, or that cannot check a key, is a misconfiguration and
    # is fatal. A warehouse that cannot be reached is an outage and is not: the
    # service starts, answers 503 until PostgreSQL is back, and needs no restart.
    try:
        failed = problems(probe(dsn))
    except Exception as exc:  # noqa: BLE001 -- availability, reported and survived
        print(
            f"fafnir-api: warning: cannot reach the warehouse yet: {exc}",
            file=sys.stderr,
        )
        failed = []
    if failed:
        print("fafnir-api: refusing to start.", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    if args.host not in _LOOPBACK:
        print(
            f"fafnir-api: warning: listening on {args.host}, not loopback. This "
            "server speaks plain HTTP: keys sent to it directly cross the network "
            "in the clear. Put the reverse proxy in front and listen on 127.0.0.1.",
            file=sys.stderr,
        )

    from fafnir_api.app import make_app
    from fafnir_api.keys import DatabaseKeyStore

    app = make_app(
        dsn=dsn, lookup=DatabaseKeyStore(dsn).lookup, rate_per_minute=args.rate
    )
    logging.getLogger("fafnir_api").info(
        "fafnir-api %s listening on %s:%d", __version__, args.host, args.port
    )
    waitress.serve(
        app, host=args.host, port=args.port, threads=args.threads, ident="fafnir-api"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
