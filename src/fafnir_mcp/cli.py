"""
``fafnir-mcp`` -- the console script.

Started as a child process of an AI client over stdio (ADR 0008 §3): it listens on
no port, terminates no TLS, and holds no secret. The DSN comes from the
environment, where the client's MCP configuration puts it.
"""

from __future__ import annotations

import argparse
import os
import sys

from fafnir_mcp import DEFAULT_PROFILE, PROFILES, __version__
from fafnir_mcp.errors import scrub_dsn


def _resolve_dsn(explicit: str | None) -> str:
    """DSN from ``--dsn`` or ``FAFNIR_DSN``. Explicit only -- no config fallback.

    ``fafnir.config`` is deliberately NOT consulted, and this is a security
    decision rather than a simplification. ``FafnirConfig.dsn`` never returns
    empty: with no config file at all it assembles one from its defaults, which
    are ``user=fafnir_ingest`` -- the write role. An MCP server that fell back to
    it would silently connect an agent as the identity that owns the write path,
    on any host where the environment variable was forgotten, and would look like
    it was working. ADR 0010 gives the agent no writable role; inheriting the
    loader's config is exactly how it would get one.

    So the DSN is explicit or absent, and absent is an error with instructions.
    """
    if explicit:
        return explicit.strip()
    return os.environ.get("FAFNIR_DSN", "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fafnir-mcp",
        description=(
            "MCP server over the fafnir warehouse. Reads only; every change goes "
            "through the fafnir CLI (ADR 0010)."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=os.environ.get("FAFNIR_MCP_PROFILE", DEFAULT_PROFILE),
        help=(
            "read: mart + ref, parameterized tools, safe to run from a laptop. "
            "ops: adds the operational record (DQ detail, runs, watermarks, "
            "landing payloads) and free-form read-only SQL. Requires a role in "
            "fafnir_ops; intended for an agent on the warehouse host. "
            "[default: %(default)s]"
        ),
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="libpq DSN [default: $FAFNIR_DSN, then ~/.fafnirrc]",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify the DSN connects and report the role and its privileges, then "
            "exit. Use this first -- a misconfigured DSN otherwise surfaces as a "
            "tool failure inside an agent conversation."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print(
            "fafnir-mcp: no DSN.\n"
            "  Set FAFNIR_DSN in the MCP server's environment, or pass --dsn.\n"
            "  ~/.fafnirrc is deliberately NOT used as a fallback: it points at "
            "the write role.\n"
            "  Example (on the warehouse host, ops profile):\n"
            '    FAFNIR_DSN="dbname=fafnir user=claude_ops '
            'application_name=fafnir-mcp"\n'
            "  See doc/agent.md.",
            file=sys.stderr,
        )
        return 2

    if args.check:
        return _check(dsn, args.profile)

    # Built BEFORE the privilege check, because the SDK is a hard prerequisite and
    # server.py imports it lazily -- so this call, not the import above, is what
    # fails on a host without the mcp extra. Do the check first and such a host
    # reports a connection warning before the actual cause, sending whoever reads
    # it to the wrong layer. Building registers tools and opens no connection, so
    # the reordering costs nothing.
    from fafnir_mcp.server import build_server

    server = build_server(dsn=dsn, profile=args.profile)

    if not _refuse_writable_role(dsn):
        return 1

    server.run("stdio")
    return 0


def _refuse_writable_role(dsn: str) -> bool:
    """Refuse to start as a role that can write. Returns False to abort.

    ADR 0010's whole tier argument rests on the agent's credential being unable to
    change data, and a deployment convention is not a mechanism -- one wrong DSN in
    one MCP config undoes it, silently, because every tool here reads and so
    nothing would ever fail. This is the one moment it can be checked once and
    cheaply.

    The split matters: a role that CAN write is a security failure and is fatal. A
    warehouse that cannot be reached is an availability failure and is NOT -- the
    server starts, and the first tool call returns the structured "the tunnel is
    not up" diagnosis ADR 0008 asks for, which is far more useful to whoever is
    reading an agent transcript than an MCP client reporting that a server died.
    """
    from fafnir_mcp.errors import ToolError
    from fafnir_mcp.tools import _connect

    try:
        conn = _connect(dsn, read_only=True)
    except ToolError as exc:
        print(f"fafnir-mcp: warning: {exc}", file=sys.stderr)
        return True  # availability, not authorization -- start and report per call
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            probe = _probe_writable(cur, "ops")
            if probe is None:
                return True
            relation, writable = probe
            if writable:
                cur.execute("SELECT current_user AS role")
                role = cur.fetchone()["role"]
                print(
                    f"fafnir-mcp: refusing to start.\n"
                    f"  Role {role!r} can INSERT into {relation}.\n"
                    f"  An agent must connect as a read-only role -- a member of "
                    f"fafnir_app (read profile) or fafnir_ops (ops profile), with "
                    f"default_transaction_read_only = on.\n"
                    f"  Mutations go through the fafnir CLI, never this server "
                    f"(ADR 0010). See doc/agent.md.",
                    file=sys.stderr,
                )
                return False
    except Exception as exc:  # noqa: BLE001 -- a failed probe must not block start
        print(
            f"fafnir-mcp: warning: could not verify role privileges: {exc}",
            file=sys.stderr,
        )
    finally:
        conn.close()
    return True


def _probe_writable(cur, profile: str):
    """Return (relation, can_write) for the first relation this role can resolve.

    Ordered most-privileged first so the answer is about the strongest thing the
    role reaches, not the weakest: for the ops profile that is a core fact table,
    and for the read profile a mart relation is all there is.

    Both statements are guarded, because ``to_regclass`` does NOT merely return
    NULL for a relation the role cannot see -- measured, not assumed: it returns
    NULL for a *nonexistent* relation but raises ``InsufficientPrivilege`` for one
    in a schema the role has no USAGE on, which is precisely the case this probe
    exists to survive. The connection is autocommit so a raised statement does not
    abort the ones after it.
    """
    candidates = ["core.daily_price", "mart.security_latest", "ref.exchange"]
    if profile == "read":
        candidates = ["mart.security_latest", "ref.exchange"]
    for relation in candidates:
        try:
            cur.execute("SELECT to_regclass(%s) AS rel", (relation,))
            if cur.fetchone()["rel"] is None:
                continue
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT') AS writable",
                (relation,),
            )
            return relation, bool(cur.fetchone()["writable"])
        except Exception:  # noqa: BLE001 -- unreachable means not writable either
            continue
    return None


def _check(dsn: str, profile: str) -> int:
    """Connect, and report who we are and what we can see.

    Deliberately more than a ping. The failure this catches is not "the database is
    down" -- that is obvious -- but "the agent is connected as the wrong role", which
    otherwise shows up much later as a permission error inside a conversation, or
    worse, does not show up at all because the role is *more* privileged than
    intended.
    """
    from fafnir_mcp.errors import ToolError
    from fafnir_mcp.tools import _connect

    print(f"profile: {profile}")
    print(f"dsn:     {scrub_dsn(dsn)}")
    try:
        conn = _connect(dsn, read_only=True)
        # The probes below deliberately run statements that may be refused, so each
        # must fail in isolation rather than aborting the transaction the rest share.
        conn.autocommit = True
    except ToolError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, current_database(), "
                "       current_setting('default_transaction_read_only') AS read_only, "
                "       current_setting('statement_timeout') AS statement_timeout"
            )
            row = cur.fetchone()
            print(f"role:    {row['current_user']}")
            print(f"db:      {row['current_database']}")
            print(f"read-only default: {row['read_only']}")
            print(f"statement_timeout: {row['statement_timeout']}")

            required = ["mart", "ref"]
            if profile == "ops":
                required += ["core", "ops", "landing", "meta"]
            print("\nschema access:")
            missing = []
            for schema in required:
                cur.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'USAGE') AS ok",
                    (schema,),
                )
                ok = bool(cur.fetchone()["ok"])
                print(f"  {schema:<9} {'yes' if ok else 'NO'}")
                if not ok:
                    missing.append(schema)

            if row["statement_timeout"] in ("0", "0ms"):
                # Not fatal -- the role works without it -- but ADR 0008 sets one
                # per agent role on purpose: an unbounded query is the cheapest
                # denial of service against the warehouse, and a model writes
                # unbounded queries by accident.
                print(
                    "  warning: no statement_timeout on this role. Set one "
                    "(ALTER ROLE ... SET statement_timeout = '30s') -- see "
                    "doc/agent.md."
                )

            # The check that matters most: this role must not be able to write. A
            # read tier that can write is not a tier (ADR 0010).
            #
            # Probed against a relation the role can actually RESOLVE.
            # has_table_privilege raises rather than returning false when the
            # schema is unreachable, so probing core from the read profile -- which
            # deliberately has no USAGE on core -- would crash the check instead of
            # passing it.
            # A missing schema IS the diagnosis; stop here rather than probing on.
            # Nothing a write probe could add changes the fix, and continuing means
            # every later statement is asking a role that has already been shown to
            # be the wrong one.
            if missing:
                print(
                    f"\nFAILED: no USAGE on {', '.join(missing)}. The {profile} "
                    f"profile needs a role that is a member of "
                    f"{'fafnir_ops (migration 0021)' if profile == 'ops' else 'fafnir_app'}"
                    f" -- see doc/agent.md.",
                    file=sys.stderr,
                )
                return 1

            probe = _probe_writable(cur, profile)
            if probe is None:
                print("\ncan write: could not probe (no readable relation found)")
                writable = False
            else:
                target, writable = probe
                print(f"\ncan write {target}: {'YES -- WRONG' if writable else 'no'}")
    finally:
        conn.close()

    if writable:
        print(
            "\nFAILED: this role can write. An agent must connect as a read-only "
            "role; mutations go through the fafnir CLI (ADR 0010).",
            file=sys.stderr,
        )
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
