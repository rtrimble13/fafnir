"""
The tool implementations, free of any MCP SDK import.

Every function here takes plain arguments and returns a plain dict, so the whole
surface -- argument validation, row caps, truncation, error wording -- is testable
without an SDK installed and without a database for most of it. :mod:`fafnir_mcp.server`
is the adapter that registers these; it holds no logic of its own.

Reads reuse ``duk.datasource.db`` wherever one exists there, exactly as
``extending.md`` prescribes and ADR 0008 repeats: *"reuse fafnir.db.repository read
functions or the duk.datasource.db adapters rather than re-implementing SQL."* An
agent and a person asking for the same series must get the same rows, and the only
way to guarantee that is for it to be the same query.

The ops-profile tools have no ``duk`` counterpart -- ``duk`` reads ``mart`` and
these read ``ops`` and ``landing`` -- so their SQL is here, parameterized, with the
relation names fixed in the source rather than assembled from arguments.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fafnir_mcp.errors import ToolError, connection_error
from fafnir_mcp.results import (
    DEFAULT_MAX_ROWS,
    SQL_MAX_ROWS,
    clamp_limit,
    envelope,
    jsonable,
)
from fafnir_mcp.sqlguard import validate_select

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _connect(dsn: str, *, read_only: bool = False):
    """Open a psycopg connection, translating failure into a ToolError.

    One connection per call, matching ``duk.datasource.db``. A pool would save a
    few milliseconds over a Unix socket and cost a lifecycle to get wrong: a
    connection held across calls can sit in an aborted transaction, which is
    exactly what ``idle_in_transaction_session_timeout`` on the role exists to
    reap. Cheap and stateless is the better trade here.
    """
    if not dsn:
        raise ToolError(
            "no DSN configured. Set FAFNIR_DSN in the MCP server's environment "
            "(see doc/agent.md), or [database].dsn in ~/.fafnirrc."
        )
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover
        raise ToolError(
            "the fafnir MCP server requires psycopg. Install with: "
            "pip install 'fafnir[mcp]'"
        ) from exc
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a structured message
        raise connection_error(exc, dsn) from None
    if read_only:
        # The second of the two independent write barriers (ADR 0010). The first is
        # the role itself, which holds no write privilege; this one also catches the
        # statements that write while looking like reads -- SELECT ... INTO, a
        # data-modifying CTE -- which no lexer should be trusted to spot.
        conn.read_only = True
    return conn


def _parse_date(value: Optional[str], label: str) -> Optional[dt.date]:
    """ISO in, ``date`` out. ADR 0008: dates in, dates out, ISO."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(
            f"{label} must be an ISO date (YYYY-MM-DD), got {value!r}"
        ) from None


def _require_symbol(symbol: str) -> str:
    if not symbol or not str(symbol).strip():
        raise ToolError("symbol is required")
    return str(symbol).strip().upper()


def _resolve_or_raise(dsn: str, symbol: str) -> int:
    """Resolve a ticker to a security_id through duk's ladder, or say so plainly."""
    from duk.datasource.db import _connect as _duk_connect
    from duk.datasource.db import _resolve_security_id

    symbol = _require_symbol(symbol)
    try:
        with _duk_connect(dsn) as conn, conn.cursor() as cur:
            sec_id = _resolve_security_id(cur, symbol)
    except Exception as exc:  # noqa: BLE001
        raise connection_error(exc, dsn) from None
    if sec_id is None:
        raise ToolError(
            f"no security matches '{symbol}'. The resolver tries the live ticker, "
            f"then the primary symbol, then a ticker the security used to trade "
            f"under -- so this symbol is in none of them. Check the spelling, or "
            f"use screen_securities to find it by name."
        )
    return sec_id


def _last_trade_date(dsn: str, security_id: int) -> Optional[dt.date]:
    """The most recent bar the warehouse holds for a security, or None.

    Read from the raw view: the adjusted view derives from it and spans the same
    dates, so one query answers for both series.
    """
    rows = _query(
        dsn,
        "SELECT max(trade_date) AS last_date FROM mart.v_daily_price_raw "
        "WHERE security_id = %s",
        (security_id,),
    )
    return rows[0]["last_date"] if rows else None


def _dataframe_rows(df) -> list[dict]:
    """A duk DataFrame as row dicts, without the two pandas artifacts.

    ``reset_index`` is the obvious way to turn a duk frame into records and gets
    both of these wrong, so neither is theoretical:

    * **An unnamed index becomes a column called ``index``.** duk's price frames
      are date-indexed and reset cleanly to a ``date`` column, but ``screen`` and
      the ``list_*`` frames carry a default RangeIndex -- which arrives at an agent
      as ``"index": 0``, a field that means nothing and invites being cited.
      Reset only when the index is named, i.e. when it carries information.
    * **A daily bar's date becomes a midnight timestamp.** ``price_history`` builds
      its index with ``pd.to_datetime``, so a trade date serialises as
      ``2024-01-02T00:00:00``. That is not what ADR 0008 means by ISO dates, and
      the false precision is worse than ugly: a bar is a session, not an instant,
      and an agent doing date arithmetic on it across a timezone would be wrong.
      Coerced back to ``date`` here rather than in :func:`jsonable`, which must not
      assume every midnight timestamp is really a date.
    """
    if df is None or getattr(df, "empty", True):
        return []

    frame = df
    if frame.index.name is not None:
        frame = frame.reset_index()
        if hasattr(frame[frame.columns[0]], "dt"):
            first = frame.columns[0]
            try:
                frame[first] = frame[first].dt.date
            except (AttributeError, TypeError):  # pragma: no cover -- not datetimes
                pass

    return [
        {str(k): jsonable(v) for k, v in record.items()}
        for record in frame.to_dict(orient="records")
    ]


# ---------------------------------------------------------------------------
# read profile -- mart and ref only, backed by duk.datasource.db
# ---------------------------------------------------------------------------


def resolve_symbol(*, dsn: str, symbol: str) -> dict:
    """Resolve a ticker to a security, reporting a rename when that is the hit."""
    from duk.datasource.db import resolve_company

    symbol = _require_symbol(symbol)
    try:
        candidates = resolve_company(dsn=dsn, query=symbol)
    except Exception as exc:  # noqa: BLE001
        raise connection_error(exc, dsn) from None
    if not candidates:
        raise ToolError(f"no security matches '{symbol}'")
    return envelope(candidates, matched=symbol)


def price_history(
    *,
    dsn: str,
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    adjusted: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Daily OHLCV for one security, raw (as traded) or split/dividend adjusted.

    ``adjusted`` is a required decision rather than a default, and the returned
    envelope repeats it: a raw series shows a split as a real jump (ADR 0001,
    ADR 0004), and an agent that mistakes one for the other reports a 4:1 split as
    a 75% crash. Saying which series this is, in the payload, is cheap insurance.
    """
    from duk.datasource.db import price_history as _duk_price_history

    symbol = _require_symbol(symbol)
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start and end and start > end:
        raise ToolError(f"start_date {start} is after end_date {end}")

    # Every argument is validated before the database is touched. Cheap, but also
    # the difference between "limit must be at least 1" and a connection error from
    # a resolve that should never have been attempted -- and an agent reading the
    # second one will go looking at the warehouse instead of at its own call.
    requested = clamp_limit(limit, DEFAULT_MAX_ROWS) if limit is not None else None

    # Resolve first, so an unknown ticker is an ERROR that explains the ladder
    # rather than an empty result set. duk returns an empty frame for a symbol it
    # cannot resolve -- right for a CLI, where the user can see they typed ABCD and
    # there is no ABCD, and wrong here: "rows: []" reads as "the warehouse holds no
    # bars for this security", which is a different and much more alarming claim.
    # The resolved id is reused below rather than resolved twice.
    sec_id = _resolve_or_raise(dsn, symbol)

    # `limit` is handed to duk rather than applied to the result, so it means the
    # MOST RECENT n bars. Capping the envelope instead would return the EARLIEST n,
    # which for a price series is not a smaller answer but a different and usually
    # useless one: "the last 10 bars of AAPL" would come back as ten days from 1985.
    # ...and when no end_date is given, the window is anchored to the security's
    # OWN last bar rather than to today. This is a deliberate divergence from
    # `duk -S db ph -n`, which defaults end_date to today because it was designed
    # against a live API where today is always the last bar.
    #
    # Against a warehouse that is not true, and the cases where it is false are
    # exactly the ones being investigated: a delisted security whose last bar is
    # from 2019, or a `stale` flag on a security that stopped updating three weeks
    # ago. duk returns an empty frame for both -- correct by its own contract and
    # useless here, since "no rows" reads as "no data held" when the truth is "no
    # data in the last fifteen calendar days". An agent triaging staleness needs
    # the final bars, which is what makes this the right default for this surface.
    #
    # Parity with duk on an explicit date range is untouched, and is what the
    # integration test asserts (ADR 0008 implementation note 3).
    if requested is not None and end is None:
        anchor = _last_trade_date(dsn, sec_id)
        if anchor is None:
            return envelope(
                [],
                symbol=symbol,
                series="adjusted" if adjusted else "raw",
                note="the warehouse holds no bars for this security",
            )
        end = anchor

    try:
        df = _duk_price_history(
            dsn=dsn,
            symbol=symbol,
            start_date=start.isoformat() if start else None,
            end_date=end.isoformat() if end else None,
            frequency="day",
            limit=requested,
            fields=None,
            adjusted=bool(adjusted),
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, dsn) from None

    rows = _dataframe_rows(df)
    return envelope(
        rows,
        max_rows=DEFAULT_MAX_ROWS,
        symbol=symbol,
        series="adjusted" if adjusted else "raw",
        note=(
            "Split/dividend adjusted, point-in-time stable."
            if adjusted
            else "UNADJUSTED, as traded -- a split shows as a real jump."
        ),
    )


def screen_securities(
    *,
    dsn: str,
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
    """Screen securities from ``mart.security_latest``.

    Two properties of that relation an agent has to know, so they are in the
    envelope rather than only in the docs: it is a MATERIALIZED view refreshed by
    ``fafnir db refresh-marts``, so it lags the last load; and delisted securities
    are retained by design (no survivorship bias), so a screen without
    ``is_actively_trading`` includes companies that stopped trading years ago.
    """
    from duk.datasource.db import screen

    cap = clamp_limit(limit, DEFAULT_MAX_ROWS)
    try:
        df = screen(
            dsn=dsn,
            sector=sector,
            industry=industry,
            exchange=exchange,
            country=country,
            marketCapMoreThan=market_cap_more_than,
            marketCapLowerThan=market_cap_less_than,
            isEtf=is_etf,
            isFund=is_fund,
            isActivelyTrading=is_actively_trading,
            limit=cap + 1,
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, dsn) from None

    return envelope(
        _dataframe_rows(df),
        max_rows=cap,
        source="mart.security_latest",
        note=(
            "Refresh-lagged (materialized; refreshed by `fafnir db refresh-marts`). "
            "Delisted securities are retained -- filter is_actively_trading to "
            "exclude them."
        ),
    )


def list_sectors(*, dsn: str) -> dict:
    from duk.datasource.db import list_sectors as _duk

    try:
        return envelope(_dataframe_rows(_duk(dsn=dsn)))
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, dsn) from None


def list_industries(*, dsn: str) -> dict:
    from duk.datasource.db import list_industries as _duk

    try:
        return envelope(_dataframe_rows(_duk(dsn=dsn)))
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, dsn) from None


def security_profile(*, dsn: str, symbol: str) -> dict:
    """The full company summary duk assembles for ``duk ls <ticker>``.

    Profile, coverage, corporate-action summary, last raw bar and open DQ flags in
    one call, because those are the five things needed to answer "what does the
    warehouse hold on this, and can I trust it?" and five round trips to ask it is
    four too many.
    """
    from duk.datasource.db import company_summary

    sec_id = _resolve_or_raise(dsn, symbol)
    try:
        summary = company_summary(dsn=dsn, security_id=sec_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, dsn) from None

    # adjusted_prices is a DataFrame of up to five years of bars -- duk computes
    # trailing statistics from it. Returning it here would blow the context budget
    # for a profile lookup; an agent that wants the series calls price_history.
    summary.pop("adjusted_prices", None)
    return {
        "profile": jsonable(summary.get("profile")),
        "coverage": jsonable(summary.get("coverage")),
        "actions": jsonable(summary.get("actions")),
        "last_bar": jsonable(summary.get("last_bar")),
        "open_dq_flags": jsonable(summary.get("dq_flags") or []),
        "fundamentals": jsonable(summary.get("fundamentals")),
        "note": (
            "open_dq_flags is the mart window: counts, keys and dates, never "
            "`detail`. Use dq_queue (ops profile) for detail and resolution history."
        ),
    }


def dq_summary(*, dsn: str, symbol: str) -> dict:
    """Open data-quality flags for one security, from the mart window.

    ADR 0008's reason for putting this on the read surface at all: *"a model
    reasoning over a price series should be able to see that the series carries two
    open gap flags, rather than treating every bar as equally sound."*
    """
    sec_id = _resolve_or_raise(dsn, symbol)
    sql = (
        "SELECT dq_flag_id, check_name, severity, table_name, record_key, "
        "detected_at FROM mart.v_security_dq_open WHERE security_id = %s "
        "ORDER BY check_name, detected_at"
    )
    rows = _query(dsn, sql, (sec_id,))
    by_check: dict[str, int] = {}
    for row in rows:
        by_check[row["check_name"]] = by_check.get(row["check_name"], 0) + 1
    return envelope(
        rows,
        symbol=_require_symbol(symbol),
        security_id=sec_id,
        open_by_check=by_check,
        note=(
            "Open flags only, without `detail`. price_* checks repeat by design; "
            "every other check is one row per open condition."
        ),
    )


# ---------------------------------------------------------------------------
# ops profile -- the operational record (migration 0021, ADR 0010)
# ---------------------------------------------------------------------------


def dq_queue(
    *,
    dsn: str,
    check_name: Optional[str] = None,
    severity: Optional[str] = None,
    symbol: Optional[str] = None,
    state: str = "open",
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """The DQ queue **with** ``detail`` and resolution history.

    This is the tool the ops tier exists for: ``mart.v_security_dq_open`` withholds
    ``detail``, resolved rows, ``resolved_by`` and ``resolution_note`` by design
    (ADR 0009), and triage needs all four -- the measured move, what was decided
    about this condition last time, and by whom.

    ``check_name`` accepts a trailing ``*`` to glob a family (``price_*``), matching
    ``fafnir dq list``, so a person and an agent narrow the queue the same way.
    """
    if state not in ("open", "resolved", "all"):
        raise ToolError("state must be one of: open, resolved, all")

    clauses: list[str] = ["1=1"]
    params: list[Any] = []

    if check_name:
        pattern = str(check_name).strip()
        if pattern.endswith("*"):
            clauses.append("f.check_name LIKE %s")
            params.append(pattern[:-1] + "%")
        else:
            clauses.append("f.check_name = %s")
            params.append(pattern)
    if severity:
        if severity not in ("info", "warn", "error"):
            raise ToolError("severity must be one of: info, warn, error")
        clauses.append("f.severity = %s")
        params.append(severity)
    if symbol:
        clauses.append("f.security_id = %s")
        params.append(_resolve_or_raise(dsn, symbol))
    if state == "open":
        clauses.append("f.resolved_at IS NULL")
    elif state == "resolved":
        clauses.append("f.resolved_at IS NOT NULL")
    since_date = _parse_date(since, "since")
    if since_date:
        clauses.append("f.detected_at >= %s")
        params.append(since_date)

    cap = clamp_limit(limit, DEFAULT_MAX_ROWS)
    params.append(cap + 1)  # one extra, so `truncated` can be honest

    sql = (
        "SELECT f.dq_flag_id, f.check_name, f.severity, f.security_id, "
        "       s.primary_symbol AS symbol, f.table_name, f.record_key, f.detail, "
        "       f.ingestion_run_id, f.detected_at, f.resolved_at, "
        "       f.resolved_by, f.resolution_note "
        "  FROM ops.data_quality_flag f "
        "  LEFT JOIN core.security s ON s.security_id = f.security_id "
        f" WHERE {' AND '.join(clauses)} "
        " ORDER BY f.detected_at DESC, f.dq_flag_id DESC LIMIT %s"
    )
    return envelope(
        _query(dsn, sql, tuple(params)),
        max_rows=cap,
        state=state,
        note=(
            "Resolving is a judgement, not a repair: closing a flag frees its slot "
            "in ux_dq_flag_open_condition, so if the defect is still in the data "
            "the next `fafnir dq run` flags it again. Repair first."
        ),
    )


#: Checks a proactive sweep may never close, whatever the evidence looks like.
#:
#: This is a floor, not a judgement. It exists in code because a sweep works a
#: queue in bulk, and bulk is exactly where "this one looks like the others" stops
#: being reasoning. Each entry is a playbook line, not a preference:
#:
#: * ``price_scale_collapse``     -- the bar STORED, flattened. Resolving deletes
#:                                   the only record of the lost high/low.
#: * ``corporate_action_drift``   -- the data is already repaired; the flag is
#:                                   evidence about the market-wide sweep, and
#:                                   closing it silently discards that evidence.
#: * ``symbol_change_conflict``   -- `dq resolve` changes nothing here. The nightly
#:                                   sweep re-detects it; the terminal state lives
#:                                   in core.symbol_change via merge-rename or
#:                                   dismiss-rename.
#: * ``price_price_out_of_range`` -- a real quote NUMERIC(20,6) cannot hold.
#: * ``price_subresolution_price``-- below the 5e-7 quantize cliff. Same verdict.
#:
#: The first three are the skill's standing rule 5 verbatim; the last two are the
#: playbooks' "report, do not resolve". ``test_never_auto_matches_the_skill``
#: parses SKILL.md and asserts the two lists agree, so this cannot drift into
#: being a second, quieter policy.
NEVER_AUTO_RESOLVE = frozenset(
    {
        "price_scale_collapse",
        "corporate_action_drift",
        "symbol_change_conflict",
        "price_price_out_of_range",
        "price_subresolution_price",
    }
)


def dq_triage(
    *,
    dsn: str,
    check_name: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Open flags with the corroborating evidence a *proactive* sweep needs.

    ``dq_queue`` answers "what is open"; this answers "what else is true about it",
    which is the difference between triage and guessing when the queue is being
    worked in bulk rather than one flag at a time.

    Three columns carry that difference, and each one is a playbook step that would
    otherwise be a hand-written correlation query per check:

    ``cohort_size``
        How many distinct securities carry this same ``check_name`` on this same
        ``record_key->>'trade_date'``. This is the single most important number in
        the whole sweep: **one security with a gap is a market fact; two hundred
        securities with a gap on the same date is a missed load.** Resolving the
        second case flag-by-flag is the wrong answer to one problem, and it is the
        mistake a bulk sweep is most likely to make quickly.

    ``prior_resolutions``
        How many times this ``(check_name, security_id)`` pair has already been
        closed. A condition resolved three times and back again is not a market
        fact being re-observed -- it is a defect nobody has repaired, and closing
        it a fourth time is the queue churn ADR 0010 names as the likeliest quiet
        damage. Non-zero here means read ``last_resolution_note`` before deciding.

    ``never_auto_resolve``
        Whether this check is on :data:`NEVER_AUTO_RESOLVE`. Surfaced per row so it
        is in front of the decision at the moment it is made, rather than something
        to have remembered from the skill.

    Plus ``is_actively_trading``/``delisted_date``/``is_fund``, which are what the
    ``stale`` playbook's three-way split turns on.

    Reads only. Nothing here resolves anything: effects go through
    ``fafnir dq resolve`` under ``sudo -u fafnir``, where the CLI's guards already
    live (ADR 0010 §4).
    """
    # The CTE already restricts to open flags, so the outer filter starts empty.
    clauses: list[str] = ["1=1"]
    params: list[Any] = []

    if check_name:
        pattern = str(check_name).strip()
        if pattern.endswith("*"):
            clauses.append("f.check_name LIKE %s")
            params.append(pattern[:-1] + "%")
        else:
            clauses.append("f.check_name = %s")
            params.append(pattern)
    since_date = _parse_date(since, "since")
    if since_date:
        clauses.append("f.detected_at >= %s")
        params.append(since_date)

    cap = clamp_limit(limit, DEFAULT_MAX_ROWS)
    params.append(cap + 1)

    # cohort is aggregated over the WHOLE open queue, in its own CTE, then joined
    # back -- not computed as a window over the returned page. A cohort counted
    # after LIMIT shrinks with the page size, which would make a missed load look
    # like a market fact at limit=20: the exact error this column exists to prevent.
    #
    # It is a separate CTE rather than `count(DISTINCT ...) OVER (...)` because
    # Postgres does not implement DISTINCT for window functions.
    sql = (
        "WITH open_flags AS ("
        "  SELECT f.dq_flag_id, f.check_name, f.severity, f.security_id,"
        "         f.record_key, f.detail, f.ingestion_run_id, f.detected_at,"
        "         (f.record_key->>'trade_date') AS trade_date"
        "    FROM ops.data_quality_flag f"
        "   WHERE f.resolved_at IS NULL"
        "), cohort AS ("
        "  SELECT check_name, (record_key->>'trade_date') AS trade_date,"
        "         count(*) AS cohort_flags,"
        "         count(DISTINCT security_id) AS cohort_size"
        "    FROM ops.data_quality_flag"
        "   WHERE resolved_at IS NULL"
        "   GROUP BY 1, 2"
        "), prior AS ("
        "  SELECT check_name, security_id, count(*) AS prior_resolutions,"
        "         max(resolved_at) AS last_resolved_at,"
        "         (array_agg(resolution_note ORDER BY resolved_at DESC))[1]"
        "             AS last_resolution_note,"
        "         (array_agg(resolved_by ORDER BY resolved_at DESC))[1]"
        "             AS last_resolved_by"
        "    FROM ops.data_quality_flag"
        "   WHERE resolved_at IS NOT NULL"
        "   GROUP BY check_name, security_id"
        ") "
        "SELECT f.dq_flag_id, f.check_name, f.severity, f.security_id,"
        "       s.primary_symbol AS symbol, f.record_key, f.detail,"
        "       f.ingestion_run_id, f.detected_at, f.trade_date,"
        "       c.cohort_size, c.cohort_flags,"
        "       coalesce(p.prior_resolutions, 0) AS prior_resolutions,"
        "       p.last_resolved_at, p.last_resolved_by, p.last_resolution_note,"
        "       s.is_actively_trading, s.delisted_date, s.is_fund,"
        "       (f.check_name = ANY(%s)) AS never_auto_resolve"
        "  FROM open_flags f"
        "  LEFT JOIN core.security s ON s.security_id = f.security_id"
        # IS NOT DISTINCT FROM, not =: checks whose record_key carries no
        # trade_date (symbol_change_conflict, adjustment_failed) have a NULL here,
        # and `NULL = NULL` would drop their cohort row and null the count.
        "  LEFT JOIN cohort c ON c.check_name = f.check_name"
        "                    AND c.trade_date IS NOT DISTINCT FROM f.trade_date"
        "  LEFT JOIN prior p ON p.check_name = f.check_name"
        "                   AND p.security_id = f.security_id"
        f" WHERE {' AND '.join(clauses)} "
        " ORDER BY c.cohort_size DESC NULLS LAST, f.check_name,"
        "          f.detected_at DESC, f.dq_flag_id DESC LIMIT %s"
    )
    rows = _query(dsn, sql, (sorted(NEVER_AUTO_RESOLVE), *params))
    return envelope(
        rows,
        max_rows=cap,
        never_auto_resolve=sorted(NEVER_AUTO_RESOLVE),
        note=(
            "cohort_size is the decisive number: one security is a market fact, "
            "many securities sharing a date is one missed load and must be "
            "repaired, not resolved. prior_resolutions > 0 means this condition "
            "was closed before and came back -- read last_resolution_note before "
            "closing it again. Nothing here resolves anything; effects go through "
            "`sudo -u fafnir fafnir dq resolve`."
        ),
    )


def dq_totals(*, dsn: str, state: str = "open") -> dict:
    """Counts per check and severity -- the shape of the queue before its contents.

    ``price_*`` is counted separately because those flags repeat by design (one row
    per detection, so a persistently bad bar accumulates them), while every other
    check is one row per open condition. Mixing them makes a stuck symbol look like
    a spreading problem.
    """
    if state not in ("open", "resolved", "all"):
        raise ToolError("state must be one of: open, resolved, all")
    where = {
        "open": "WHERE resolved_at IS NULL",
        "resolved": "WHERE resolved_at IS NOT NULL",
        "all": "",
    }[state]
    rows = _query(
        dsn,
        f"SELECT check_name, severity, count(*) AS flags, "
        f"       count(DISTINCT security_id) AS securities, "
        f"       min(detected_at) AS first_detected, "
        f"       max(detected_at) AS last_detected "
        f"  FROM ops.data_quality_flag {where} "
        f" GROUP BY check_name, severity ORDER BY count(*) DESC",
        (),
    )
    repeating = sum(
        r["flags"] for r in rows if str(r["check_name"]).startswith("price_")
    )
    distinct = sum(
        r["flags"] for r in rows if not str(r["check_name"]).startswith("price_")
    )
    return envelope(
        rows,
        state=state,
        distinct_condition_flags=distinct,
        repeating_price_flags=repeating,
        note=(
            "price_* flags repeat per detection by design; the rest are one row "
            "per open condition. distinct_condition_flags is the count of problems."
        ),
    )


def ingestion_runs(
    *,
    dsn: str,
    status: Optional[str] = None,
    endpoint: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """The lineage log: one row per load, with timings, rows and bytes.

    The two questions this answers that nothing else can: which step dominated last
    night's window, and what failed. ``bytes_downloaded`` is also the FMP bandwidth
    meter -- the 50 GB/month budget is summed from this column.
    """
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        if status not in ("started", "success", "partial", "failed"):
            raise ToolError("status must be one of: started, success, partial, failed")
        clauses.append("status = %s")
        params.append(status)
    if endpoint:
        clauses.append("endpoint = %s")
        params.append(str(endpoint))
    since_date = _parse_date(since, "since")
    if since_date:
        clauses.append("started_at >= %s")
        params.append(since_date)

    cap = clamp_limit(limit, DEFAULT_MAX_ROWS)
    params.append(cap + 1)
    sql = (
        "SELECT ingestion_run_id, source, endpoint, params, window_from, window_to, "
        "       symbols_requested, rows_inserted, rows_updated, rows_quarantined, "
        "       bytes_downloaded, status, error_message, started_at, finished_at, "
        "       (finished_at - started_at) AS duration "
        "  FROM ops.ingestion_run "
        f" WHERE {' AND '.join(clauses)} "
        " ORDER BY started_at DESC LIMIT %s"
    )
    return envelope(_query(dsn, sql, tuple(params)), max_rows=cap)


def watermarks(
    *, dsn: str, symbol: Optional[str] = None, limit: Optional[int] = None
) -> dict:
    """Per source/endpoint/security incremental high-water marks.

    Worth reading during triage for a reason that is not obvious: a persistently
    quarantined bar HOLDS the price watermark (up to ``MAX_QUARANTINE_HOLDS = 5``
    runs) before the loader steps past it. A symbol accumulating ``price_*`` flags
    is therefore usually also a symbol that has stopped advancing, and the
    watermark is where that shows.
    """
    clauses = ["1=1"]
    params: list[Any] = []
    if symbol:
        clauses.append("w.security_id = %s")
        params.append(_resolve_or_raise(dsn, symbol))
    cap = clamp_limit(limit, DEFAULT_MAX_ROWS)
    params.append(cap + 1)
    sql = (
        "SELECT w.source, w.endpoint, w.security_id, s.primary_symbol AS symbol, "
        "       w.last_loaded_date, w.last_run_at, w.updated_at "
        "  FROM ops.load_watermark w "
        "  LEFT JOIN core.security s ON s.security_id = w.security_id "
        f" WHERE {' AND '.join(clauses)} "
        " ORDER BY w.updated_at DESC LIMIT %s"
    )
    return envelope(_query(dsn, sql, tuple(params)), max_rows=cap)


def landing_payload(*, dsn: str, endpoint: str, symbol: Optional[str] = None) -> dict:
    """The most recent raw vendor payload for an endpoint (and symbol).

    **Exactly one row, never a scan.** ``landing.fmp_raw.payload`` is unbounded
    vendor JSON; an unfiltered read of that table is the single worst thing on this
    surface for both context and I/O, so the tool cannot express one.

    This is ground truth when a field's meaning surprises you -- "what did FMP
    actually send for this bar?" is the question that separates a loader bug from a
    feed change.
    """
    if not endpoint or not str(endpoint).strip():
        raise ToolError(
            "endpoint is required -- landing_payload returns one payload, not a "
            "scan of the landing table. Use ingestion_runs to find the endpoint "
            "names in play."
        )
    clauses = ["endpoint = %s"]
    params: list[Any] = [str(endpoint).strip()]
    if symbol:
        clauses.append("symbol = %s")
        params.append(_require_symbol(symbol))
    rows = _query(
        dsn,
        "SELECT raw_id, ingestion_run_id, endpoint, params, symbol, http_status, "
        "       payload_hash, bytes, fetched_at, payload "
        f"  FROM landing.fmp_raw WHERE {' AND '.join(clauses)} "
        "ORDER BY fetched_at DESC LIMIT 1",
        tuple(params),
    )
    if not rows:
        raise ToolError(
            f"no landing payload for endpoint={endpoint!r}"
            + (f" symbol={symbol!r}" if symbol else "")
            + ". Check the endpoint name against ingestion_runs."
        )
    return {"payload": jsonable(rows[0])}


def schema_state(*, dsn: str) -> dict:
    """Applied migrations -- is this host running the schema the repo expects?

    An operations question with an operations answer, which is why ``meta`` is in
    the ops tier's grants at all.
    """
    return envelope(
        _query(
            dsn,
            "SELECT version, name, checksum, applied_at FROM meta.schema_migration "
            "ORDER BY version",
            (),
        )
    )


def sql_read(*, dsn: str, sql: str, limit: Optional[int] = None) -> dict:
    """Run an arbitrary read-only ``SELECT``. Ops profile only.

    ADR 0010 argues this exception and states its boundary: ops profile, on-host, a
    read-only role, a read-only transaction with a statement allowlist, and a row
    cap. All five hold here -- :func:`fafnir_mcp.sqlguard.validate_select` is the
    allowlist, ``read_only=True`` is the transaction, ``SQL_MAX_ROWS`` is the cap,
    and the role is the deployment's. Drop any one and this is no longer the tool
    that was argued for.

    It exists because investigation is not a fixed set of tool signatures: "which
    securities have three or more open gap flags on dates when most of the universe
    also has no bar?" is the query that separates a holiday from a broken loader,
    and no parameterized tool asks it.
    """
    statement = validate_select(sql)
    cap = clamp_limit(limit, SQL_MAX_ROWS)

    conn = _connect(dsn, read_only=True)
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute(statement)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(_sql_failure(exc)) from None
            if cur.description is None:
                raise ToolError(
                    "the statement returned no result set. sql_read runs queries; "
                    "changes go through the fafnir CLI (ADR 0010)."
                )
            rows = cur.fetchmany(cap + 1)
            columns = [d.name for d in cur.description]
    finally:
        conn.close()

    return envelope(rows, max_rows=cap, columns=columns, statement=statement)


def _sql_failure(exc: Exception) -> str:
    """Explain a rejected statement in terms of the rule that rejected it."""
    text = str(exc).strip()
    lowered = text.lower()
    if "read-only transaction" in lowered:
        return (
            "refused: that statement writes, and sql_read runs in a read-only "
            "transaction. Changes to the warehouse go through the fafnir CLI "
            "(`fafnir dq resolve`, `fafnir ingest`, `fafnir adjust`) -- see "
            f"ADR 0010.\n  postgres said: {text}"
        )
    if "permission denied" in lowered:
        return (
            "permission denied. The ops tier reads core, mart, ref, ops, landing "
            "and meta, and holds no write privilege of any kind (migration 0021). "
            f"If this is a relation it should reach, that is a grants bug.\n"
            f"  postgres said: {text}"
        )
    if "statement timeout" in lowered or "canceling statement" in lowered:
        return (
            "the query exceeded the role's statement_timeout. Narrow it -- add a "
            "date range, a security_id, or an aggregate -- rather than asking for "
            f"more time.\n  postgres said: {text}"
        )
    return f"the query failed.\n  postgres said: {text}"


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _query(dsn: str, sql: str, params: tuple) -> list[dict]:
    """Run one parameterized read and return row dicts."""
    conn = _connect(dsn, read_only=True)
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute(sql, params)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(_sql_failure(exc)) from None
            return list(cur.fetchall())
    finally:
        conn.close()


def _wrap(exc: Exception, dsn: str) -> Exception:
    """Pass a ToolError through; turn anything else into a structured one.

    duk raises ``DataSourceError`` for a bad argument and psycopg errors for
    everything else, and both reach an agent as text -- so the distinction worth
    preserving is "your request was wrong" versus "the warehouse could not be
    reached", not the exception class.
    """
    if isinstance(exc, ToolError):
        return exc
    name = type(exc).__name__
    if name == "DataSourceError":
        return ToolError(str(exc))
    if "Operational" in name or "connection" in str(exc).lower():
        return connection_error(exc, dsn)
    return ToolError(_sql_failure(exc))
