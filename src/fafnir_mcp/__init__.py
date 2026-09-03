"""
The fafnir MCP server: agentic read access to the warehouse.

Two profiles, and the difference between them is a privilege decision, not a
convenience (ADR 0010):

``read``
    The ADR 0008 surface. Parameterized tools over ``mart`` and ``ref``, backed by
    ``duk.datasource.db`` so an agent and a person reading the same security get
    the same series. Connects as a member of ``fafnir_app``. Runs anywhere -- on
    the warehouse host, or from a laptop through the §11 tunnel.

``ops``
    Everything in ``read``, plus the operational record -- the DQ queue with
    ``detail`` and resolution history, ingestion runs, watermarks, landing
    payloads -- and ``sql_read``, an arbitrary read-only ``SELECT``. Connects as a
    member of ``fafnir_ops`` (migration 0021). Intended for an agent running ON the
    warehouse host, and nothing here writes.

The tool implementations live in :mod:`fafnir_mcp.tools` and import no MCP SDK at
all; :mod:`fafnir_mcp.server` is a thin adapter that registers them. That split is
deliberate -- it keeps the argument validation, row caps and error shaping unit
testable without the SDK installed, and confines an SDK version bump to one file.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: The profiles a server can be started with. Order is meaningful: each is a
#: superset of the one before it.
PROFILES = ("read", "ops")

DEFAULT_PROFILE = "read"

__all__ = ["PROFILES", "DEFAULT_PROFILE", "__version__"]
