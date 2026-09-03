"""
Do our error messages actually reach the caller?

ADR 0008 requires errors to be *"structured messages, not tracebacks"*, and every
`ToolError` in this package is written to be read by a model — the SQL guard
explains where changes go, the resolver failure explains the ticker ladder, the
connection diagnostic names the tunnel.

None of that reached the client, and no unit test could have noticed. The MCP SDK
sorts exceptions from a tool body into two classes: its own ``ToolError`` is a
failure you anticipated and the message is delivered, while **anything else** is a
crash — the client gets ``"Error executing tool <name>"`` and the real text stays
in the server's log. ``fafnir_mcp.errors.ToolError`` is deliberately not the SDK's
class (so :mod:`fafnir_mcp.tools` needs no SDK), so it landed in the second bucket
and every diagnostic in the package was replaced by five generic words.

:func:`fafnir_mcp.server._surfaced` translates it. These tests exercise the real
registered tool through the real ``call_tool`` path, because that is the only place
the defect was visible.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="the mcp SDK is an optional dependency")

# Never connected to: every case below fails argument validation first, so no
# tool in this module reaches the database.
DSN = "dbname=fafnir user=nobody"


def _call(tool: str, **arguments) -> str:
    """Invoke a registered tool through the SDK and return what the caller sees."""
    from mcp.server.mcpserver.exceptions import ToolError as SdkToolError

    from fafnir_mcp.server import build_server

    server = build_server(dsn=DSN, profile="ops")
    try:
        asyncio.run(server.call_tool(tool, arguments))
    except SdkToolError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- the wrong bucket; report which
        raise AssertionError(
            f"{tool} raised {type(exc).__name__}, which the SDK treats as a crash "
            f"and whose message is withheld from the client: {exc}"
        ) from exc
    raise AssertionError(f"{tool} did not fail as expected")


# (tool, arguments, a phrase the caller must actually see)
SURFACED = [
    (
        "sql_read",
        {"sql": "DELETE FROM core.daily_price"},
        "Start with SELECT or WITH",
    ),
    (
        "sql_read",
        {"sql": "SELECT 1; DROP TABLE core.security"},
        "exactly one statement",
    ),
    ("sql_read", {"sql": ""}, "non-empty SELECT"),
    ("dq_queue", {"state": "bogus"}, "state must be one of"),
    ("dq_totals", {"state": "bogus"}, "state must be one of"),
    ("ingestion_runs", {"status": "bogus"}, "status must be one of"),
    (
        "landing_payload",
        {"endpoint": ""},
        "endpoint is required",
    ),
    (
        "price_history",
        {"symbol": "AAPL", "start_date": "not-a-date"},
        "ISO date (YYYY-MM-DD)",
    ),
    ("price_history", {"symbol": ""}, "symbol is required"),
    ("price_history", {"symbol": "AAPL", "limit": 0}, "at least 1"),
    ("resolve_symbol", {"symbol": "   "}, "symbol is required"),
]


@pytest.mark.parametrize("tool,arguments,phrase", SURFACED)
def test_the_caller_sees_the_real_message(tool, arguments, phrase):
    assert phrase in _call(tool, **arguments)


def test_a_refusal_says_where_changes_go():
    """The rule is taught at the moment it is broken, or not at all.

    An agent that gets "refused" learns nothing; one that gets "changes go through
    the fafnir CLI" does not try again through this tool.
    """
    message = _call("sql_read", sql="UPDATE core.security SET company_name = 'x'")
    assert "fafnir" in message
    assert "CLI" in message


def test_an_unreachable_warehouse_is_diagnosed_not_dumped():
    """ADR 0008: a failed connection should name the failure that really happens.

    This one does reach the network layer -- a port nothing listens on -- so it
    exercises the connection path rather than argument validation.
    """
    from mcp.server.mcpserver.exceptions import ToolError as SdkToolError

    from fafnir_mcp.server import build_server

    server = build_server(
        dsn="host=127.0.0.1 port=1 dbname=fafnir user=nobody", profile="ops"
    )
    with pytest.raises(SdkToolError) as exc:
        asyncio.run(server.call_tool("schema_state", {}))
    message = str(exc.value)
    assert "tunnel is not up" in message or "could not reach the warehouse" in message
    assert "Traceback" not in message


def test_the_wrapper_preserves_the_tool_schema():
    """`@server.tool()` derives the JSON schema from the signature, so the wrapper
    must not hide it -- an all-`kwargs` schema would let any argument through."""
    from fafnir_mcp.server import build_server

    server = build_server(dsn=DSN, profile="ops")
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    properties = tools["price_history"].input_schema.get("properties", {})
    assert {"symbol", "adjusted", "start_date", "end_date", "limit"} <= set(properties)
    assert "kwargs" not in properties
    assert "args" not in properties
