"""
Structured errors for the MCP surface.

ADR 0008: *"Errors are structured messages, not tracebacks -- and a failed
connection says 'the SSH tunnel to the warehouse is not up', since that is the
failure that will actually happen."*

Two things follow. A tool raises :class:`ToolError` with a sentence an agent can
act on, and a connection failure is translated into whichever of the three
failures it actually is, because the psycopg message alone does not distinguish
them well enough to be useful.
"""

from __future__ import annotations


class ToolError(Exception):
    """A tool failed in a way the caller should be told about, in words.

    Raised for bad arguments, unknown symbols, and refused statements. The message
    is the whole payload: it is what reaches the model, so it says what was wrong
    and, where there is one, what to do instead.
    """


def connection_error(exc: Exception, dsn: str) -> ToolError:
    """Translate a connection failure into the diagnosis that is usually right.

    The raw psycopg text ("connection failed: ... No such file or directory") sends
    a reader looking at the wrong layer. Three failures account for nearly all of
    them, and each has a different fix:

    * the SSH tunnel is down -- a laptop client whose forward died (ADR 0008);
    * PostgreSQL is not running, or not listening on the socket -- on-host;
    * the role cannot authenticate -- ``pg_hba.conf`` / ``pg_ident.conf`` ordering,
      which install §3.6 warns about and which people hit every time.

    The DSN is echoed with no password: it is the single most useful fact for
    telling these apart, and a DSN under this deployment carries no secret anyway
    (ADR 0008: the SSH key is the only credential). It is scrubbed regardless,
    because that guarantee is a property of the deployment, not of this function.
    """
    text = str(exc)
    lowered = text.lower()
    safe = scrub_dsn(dsn)

    if "authentication failed" in lowered or "pg_hba" in lowered:
        hint = (
            "the role cannot authenticate. Check that the peer/ident rule for this "
            "role sits ABOVE the generic 'local all all peer' line in pg_hba.conf, "
            "and that pg_ident.conf maps this OS user to this role "
            "(install_hetzner.md §3.6)."
        )
    elif "does not exist" in lowered and "role" in lowered:
        hint = (
            "the database role does not exist. Per-agent roles are a deployment "
            "fact, not a migration -- see doc/agent.md."
        )
    elif _looks_like_tcp(safe):
        hint = (
            "could not reach the warehouse. If this DSN points at a forwarded "
            "port, the SSH tunnel is not up: run `ssh -fN fafnir` (or check "
            "`ssh -O check fafnir`) and retry."
        )
    else:
        hint = (
            "could not reach the warehouse on the local socket. Check that "
            "PostgreSQL is running (`systemctl status postgresql@16-main`) and "
            "that the socket directory in the DSN is the one it listens on."
        )

    return ToolError(f"{hint}\n  dsn: {safe}\n  postgres said: {text.strip()}")


def _looks_like_tcp(dsn: str) -> bool:
    """True when the DSN names a TCP host rather than a Unix socket directory.

    A socket DSN either omits ``host`` entirely or gives a path (leading ``/``).
    Anything else is a hostname or address, which under ADR 0008 means a tunnel.
    """
    for token in dsn.split():
        if token.startswith("host="):
            value = token[len("host=") :]
            return bool(value) and not value.startswith("/")
    return False


def scrub_dsn(dsn: str) -> str:
    """The DSN with any ``password=`` token replaced, for safe echoing."""
    return " ".join(
        "password=***" if token.startswith("password=") else token
        for token in (dsn or "").split()
    )
