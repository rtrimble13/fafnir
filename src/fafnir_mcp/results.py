"""
Result shaping: row caps, the ``truncated`` flag, and JSON-safe coercion.

ADR 0008: *"Every result is row-capped with an explicit ``truncated`` flag, both
because context is finite and because an unbounded result is the cheapest denial
of service against the tunnel."*

The flag is the load-bearing half. A silently truncated result is worse than a
refused one: an agent that cannot tell 200 rows from "the first 200 of 40,000"
will reason over the head of a series and report it as the whole.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Iterable, Optional

#: Row cap for the ordinary tools. Generous enough for a decade of daily bars
#: (~2,520) to come back whole, which is the common case that must not truncate.
DEFAULT_MAX_ROWS = 5000

#: Row cap for ``sql_read``. Much tighter on purpose: an arbitrary query is the one
#: place a cross join can be written by accident, and an exploratory aggregate that
#: needs more than 500 rows to answer is a query that should have aggregated.
SQL_MAX_ROWS = 500


def clamp_limit(value: Optional[int], ceiling: int, *, label: str = "limit") -> int:
    """Coerce a caller-supplied limit into 1..ceiling.

    A limit above the ceiling is clamped rather than refused: the caller gets the
    ceiling's worth of rows and a ``truncated`` flag saying there were more, which
    is more useful than an error telling it to ask again for less.
    """
    if value is None:
        return ceiling
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        from fafnir_mcp.errors import ToolError

        raise ToolError(f"{label} must be a whole number, got {value!r}") from None
    if parsed < 1:
        from fafnir_mcp.errors import ToolError

        raise ToolError(f"{label} must be at least 1, got {parsed}")
    return min(parsed, ceiling)


def envelope(
    rows: Iterable[dict],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    **extra: Any,
) -> dict:
    """Wrap rows in the standard result shape.

    ``truncated`` is True only when rows were actually dropped, so an agent can
    trust that False means it is holding the complete answer. ``row_count`` is
    what is being returned, not what matched -- counting the whole match would mean
    running the query twice, and the flag already carries the fact that matters.
    """
    materialized = list(rows)
    truncated = len(materialized) > max_rows
    if truncated:
        materialized = materialized[:max_rows]
    return {
        "rows": [jsonable(row) for row in materialized],
        "row_count": len(materialized),
        "truncated": truncated,
        **extra,
    }


def jsonable(value: Any) -> Any:
    """Coerce warehouse types into something JSON-serializable.

    Four cases matter here, and one of them is a correctness decision rather than a
    formatting one:

    * ``date`` / ``datetime`` -> ISO strings. ADR 0008: *"Dates in, dates out, ISO;
      no locale-dependent formatting."*
    * ``Decimal`` -> **str**, not float. Money in this warehouse is exact
      ``NUMERIC`` by construction (ADR 0001), and rendering it as a float here
      would undo that at the last step -- 0.1 + 0.2 arriving at an agent as
      0.30000000000000004 is the kind of thing that gets reported as a data defect.
      A string round-trips exactly and is unambiguous to read.
    * ``memoryview`` / ``bytes`` -> a short marker. Nothing on this surface should
      be returning binary; saying so is better than a base64 wall.
    * containers -> recursed, so JSONB columns (``record_key``, ``detail``,
      ``params``, ``payload``) come through with their nested dates intact.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes, not rendered>"
    return str(value)
