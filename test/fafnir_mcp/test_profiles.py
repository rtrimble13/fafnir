"""
Which tools exist on which profile.

ADR 0010 permits ``sql_read`` in the ops profile and forbids it in the read
profile, and is specific about the mechanism: it is *"registered only under
--profile ops, by not existing at all under --profile read rather than by a check
at call time"*. That is the difference between a boundary and a reminder, and it is
invisible in review -- both profiles are built by the same function, and the read
profile looks complete either way.

So this asserts the actual registered tool set. Skipped when the MCP SDK is not
installed, since it is an optional dependency the warehouse does not need.
"""

from __future__ import annotations

import asyncio

import pytest

from fafnir_mcp import PROFILES

pytest.importorskip("mcp", reason="the mcp SDK is an optional dependency")

DSN = "dbname=fafnir user=nobody"  # never connected to; build_server does not dial

# Available to any read role. duk's own surface, plus the DQ window ADR 0008 puts
# on it so an agent can ask whether a series is trustworthy.
READ_TOOLS = {
    "resolve_symbol",
    "price_history",
    "screen_securities",
    "list_sectors",
    "list_industries",
    "security_profile",
    "dq_summary",
}

# The operational record, and free-form reads. These need fafnir_ops (0021).
OPS_ONLY_TOOLS = {
    "dq_queue",
    "dq_triage",
    "dq_totals",
    "ingestion_runs",
    "watermarks",
    "landing_payload",
    "schema_state",
    "sql_read",
}


def _tool_names(profile: str) -> set[str]:
    """The registered tool names for a profile.

    ``list_tools`` is async, driven here with ``asyncio.run`` from a sync test
    rather than through an async pytest plugin: the plugin would be a third
    optional dependency (installed today only because the MCP SDK happens to pull
    anyio in), and a missing async plugin does not skip an async test -- it passes
    it, vacuously, as a coroutine nobody awaited. A boundary test that silently
    stops testing is worse than no boundary test.
    """
    from fafnir_mcp.server import build_server

    server = build_server(dsn=DSN, profile=profile)
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_read_profile_exposes_the_read_surface():
    assert _tool_names("read") == READ_TOOLS


def test_ops_profile_is_a_superset_of_read():
    """Each profile is a superset of the one before it, as the docs promise."""
    assert _tool_names("ops") == READ_TOOLS | OPS_ONLY_TOOLS


@pytest.mark.parametrize("tool", sorted(OPS_ONLY_TOOLS))
def test_ops_tools_are_absent_from_the_read_profile(tool):
    """Absent, not present-and-refusing.

    `sql_read` is the one that matters: ADR 0008's no-free-form-SQL rule still
    holds for anything reachable from a laptop, and this is what keeps it holding.
    """
    assert tool not in _tool_names("read")


def test_an_unknown_profile_is_refused():
    from fafnir_mcp.server import build_server

    with pytest.raises(ValueError):
        build_server(dsn=DSN, profile="admin")


def test_every_declared_profile_builds():
    """PROFILES is the public list; nothing in it may be unbuildable."""
    from fafnir_mcp.server import build_server

    for profile in PROFILES:
        assert build_server(dsn=DSN, profile=profile) is not None


def test_every_tool_has_a_description():
    """A tool schema is a usability boundary (ADR 0008) -- an undescribed tool is
    one a model will use wrongly or not at all."""
    from fafnir_mcp.server import build_server

    server = build_server(dsn=DSN, profile="ops")
    undescribed = [
        tool.name
        for tool in asyncio.run(server.list_tools())
        if not (tool.description or "").strip()
    ]
    assert not undescribed, undescribed
