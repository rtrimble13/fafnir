"""
The MCP adapter: registers :mod:`fafnir_mcp.tools` with an MCP server by profile.

This file holds no logic. Every rule that matters -- argument validation, row caps,
the SQL allowlist, error wording -- lives in the modules it imports, so a change of
SDK is confined here and the surface stays testable without one.

**Profiles are enforced by registration, not by a check at call time.** The
ops-only tools, ``sql_read`` above all, are not registered under ``--profile read``:
they do not exist on that surface rather than existing and refusing. ADR 0010 asks
for exactly that, and it is the difference between a boundary and a reminder.

The one piece of real work here is :func:`_surfaced`, which translates our
:class:`fafnir_mcp.errors.ToolError` into the SDK's own error type so the message
actually reaches the caller. See its docstring -- without it, every carefully
worded diagnostic in this package is replaced by "Error executing tool <name>".
"""

from __future__ import annotations

import functools
from typing import Callable, Optional

from fafnir_mcp import DEFAULT_PROFILE, PROFILES, __version__
from fafnir_mcp import tools as T
from fafnir_mcp.errors import ToolError

INSTRUCTIONS = """\
The fafnir financial market-data warehouse.

Two facts that make answers wrong if forgotten:

* Prices come in two series. `price_history(adjusted=false)` is RAW, as traded --
  a 4:1 split shows as a 75% drop because that is what the tape says.
  `adjusted=true` is the split/dividend-adjusted series. Always say which you used.
* `screen_securities` reads a materialized view refreshed by
  `fafnir db refresh-marts`, so it lags the last load, and delisted securities are
  retained on purpose (no survivorship bias). Filter `is_actively_trading` unless
  you want them.

Before reasoning over a series, check `dq_summary` for that security: open flags
mean the warehouse itself is unsure about some of those bars.

Nothing here writes. Changes to the warehouse go through the `fafnir` CLI.
"""


def _surfaced(fn: Callable) -> Callable:
    """Re-raise our ToolError as the MCP SDK's, so its message reaches the caller.

    This is not plumbing; it is the difference between the error surface ADR 0008
    specifies and no error surface at all. The SDK sorts exceptions from a tool
    body into two classes: its own ``ToolError`` is *"a failure you anticipated"``
    and the client receives the message, while **anything else** is treated as a
    crash -- the client gets only ``"Error executing tool <name>"`` and the real
    text stays in the server's log, where an agent cannot see it.

    ``fafnir_mcp.errors.ToolError`` is deliberately not the SDK's class, because
    :mod:`fafnir_mcp.tools` imports no SDK (which is what makes the validation and
    error wording unit-testable without one). So the translation has to happen
    here, and it has to happen for every tool -- measured, not assumed: before this
    existed, "no security matches 'NOPE'. The resolver tries the live ticker, then
    ..." and every refusal from the SQL guard reached the model as five generic
    words.

    ``functools.wraps`` matters too: ``@server.tool()`` builds the tool's JSON
    schema by inspecting the signature, and ``inspect.signature`` follows
    ``__wrapped__``, so the schema is still derived from the real parameters.
    """
    from mcp.server.mcpserver.exceptions import ToolError as SdkToolError

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            raise SdkToolError(str(exc)) from exc

    return wrapper


def build_server(*, dsn: str, profile: str = DEFAULT_PROFILE):
    """Construct an MCP server exposing the tools for ``profile``."""
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}"
        )
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "the fafnir MCP server requires the mcp SDK.\n"
            "  Install with: pip install 'fafnir[mcp]'"
        ) from exc

    server = MCPServer(
        name="fafnir",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    # -- read profile: mart + ref, via duk.datasource.db -------------------
    @server.tool()
    @_surfaced
    def resolve_symbol(symbol: str) -> dict:
        """Resolve a ticker or company name to a security in the warehouse.

        Tries the live ticker, then the primary symbol, then a ticker the security
        used to trade under before a rename -- so a former ticker resolves, and
        says that it did.
        """
        return T.resolve_symbol(dsn=dsn, symbol=symbol)

    @server.tool()
    @_surfaced
    def price_history(
        symbol: str,
        adjusted: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Daily OHLCV for one security. Dates are ISO (YYYY-MM-DD).

        adjusted=False returns the RAW series as traded, where a split is a real
        jump. adjusted=True returns the split/dividend-adjusted series. Pick
        deliberately and report which you used.
        """
        return T.price_history(
            dsn=dsn,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjusted=adjusted,
            limit=limit,
        )

    @server.tool()
    @_surfaced
    def screen_securities(
        sector: Optional[list[str]] = None,
        industry: Optional[list[str]] = None,
        exchange: Optional[str] = None,
        country: Optional[str] = None,
        market_cap_more_than: Optional[float] = None,
        market_cap_less_than: Optional[float] = None,
        is_etf: Optional[bool] = None,
        is_fund: Optional[bool] = None,
        is_actively_trading: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Screen securities by sector, industry, venue, country or market cap.

        Reads a refresh-lagged materialized view that retains delisted securities.
        """
        return T.screen_securities(
            dsn=dsn,
            sector=sector,
            industry=industry,
            exchange=exchange,
            country=country,
            market_cap_more_than=market_cap_more_than,
            market_cap_less_than=market_cap_less_than,
            is_etf=is_etf,
            is_fund=is_fund,
            is_actively_trading=is_actively_trading,
            limit=limit,
        )

    @server.tool()
    @_surfaced
    def list_sectors() -> dict:
        """Every sector name the warehouse classifies securities into."""
        return T.list_sectors(dsn=dsn)

    @server.tool()
    @_surfaced
    def list_industries() -> dict:
        """Every industry name the warehouse classifies securities into."""
        return T.list_industries(dsn=dsn)

    @server.tool()
    @_surfaced
    def security_profile(symbol: str) -> dict:
        """Everything the warehouse holds on one security, in one call.

        Profile, price coverage, corporate-action summary, last raw bar and open
        data-quality flags -- the five things needed to answer "what is here, and
        can I trust it?".
        """
        return T.security_profile(dsn=dsn, symbol=symbol)

    @server.tool()
    @_surfaced
    def dq_summary(symbol: str) -> dict:
        """Open data-quality flags for one security.

        Call this before reasoning over a price series: open flags mean the
        warehouse itself is unsure about some of those bars.
        """
        return T.dq_summary(dsn=dsn, symbol=symbol)

    if profile == "ops":
        _register_ops_tools(server, dsn)

    return server


def _register_ops_tools(server, dsn: str) -> None:
    """The operational record and free-form reads. Ops profile only (ADR 0010)."""

    @server.tool()
    @_surfaced
    def dq_queue(
        check_name: Optional[str] = None,
        severity: Optional[str] = None,
        symbol: Optional[str] = None,
        state: str = "open",
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """The data-quality queue WITH detail and resolution history.

        check_name accepts a trailing * to glob a family ('price_*'), like
        `fafnir dq list`. state is open (default), resolved, or all -- read
        'resolved' to see what was decided about this condition before, and by whom.
        """
        return T.dq_queue(
            dsn=dsn,
            check_name=check_name,
            severity=severity,
            symbol=symbol,
            state=state,
            since=since,
            limit=limit,
        )

    @server.tool()
    @_surfaced
    def dq_totals(state: str = "open") -> dict:
        """Flag counts per check and severity -- the shape of the queue.

        Separates price_* (which repeat per detection by design) from every other
        check (one row per open condition), because mixing them makes one stuck
        symbol look like a spreading problem.
        """
        return T.dq_totals(dsn=dsn, state=state)

    @server.tool()
    @_surfaced
    def ingestion_runs(
        status: Optional[str] = None,
        endpoint: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """The load lineage log: timings, row counts, bytes, failures.

        Answers which step dominated the nightly window, what failed, and how much
        FMP bandwidth has been spent (sum bytes_downloaded against 50 GB/month).
        """
        return T.ingestion_runs(
            dsn=dsn, status=status, endpoint=endpoint, since=since, limit=limit
        )

    @server.tool()
    @_surfaced
    def watermarks(symbol: Optional[str] = None, limit: Optional[int] = None) -> dict:
        """Incremental high-water marks per source/endpoint/security.

        A persistently quarantined bar holds a symbol's price watermark for up to
        five runs, so a symbol accumulating price_* flags has usually also stopped
        advancing -- this is where that shows.
        """
        return T.watermarks(dsn=dsn, symbol=symbol, limit=limit)

    @server.tool()
    @_surfaced
    def landing_payload(endpoint: str, symbol: Optional[str] = None) -> dict:
        """The most recent RAW vendor payload for an endpoint (and symbol).

        Ground truth when a field's meaning surprises you: what the vendor actually
        sent, which is what separates a loader bug from a feed change. Returns
        exactly one payload -- endpoint is required.
        """
        return T.landing_payload(dsn=dsn, endpoint=endpoint, symbol=symbol)

    @server.tool()
    @_surfaced
    def schema_state() -> dict:
        """Applied migrations: is this host running the schema the repo expects?"""
        return T.schema_state(dsn=dsn)

    @server.tool()
    @_surfaced
    def sql_read(sql: str, limit: Optional[int] = None) -> dict:
        """Run an arbitrary read-only SELECT against the warehouse.

        Single statement, must begin with SELECT or WITH, runs in a read-only
        transaction as a role with no write privilege, capped at 500 rows. Use it
        for questions no named tool asks -- correlating flags across securities and
        dates is the main one. Changes go through the fafnir CLI, never here.
        """
        return T.sql_read(dsn=dsn, sql=sql, limit=limit)


__all__ = ["build_server", "INSTRUCTIONS", "ToolError"]
