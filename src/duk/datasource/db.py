"""
Database data source: the fafnir PostgreSQL warehouse.

Reads the ``mart`` schema -- and only ``mart`` -- returning the same DataFrame
contracts as the live (FMP) path, so the CLI output and the pure compute modules
behave identically regardless of source. psycopg is imported lazily so duk remains
usable in live mode without it installed.

``mart`` is the whole of it by design: ADR 0008 makes every per-person and per-agent
role a member of ``fafnir_app``, which holds SELECT on ``mart`` and ``ref`` and
nothing else. A read added here against ``core`` or ``ops`` will work for whoever
writes it (loaders run as ``fafnir_ingest``) and fail for every laptop and MCP
client -- which is exactly how ``ph`` was broken for the ``fafnir_app`` path before
migration 0020. Add a ``mart`` view instead.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from duk.datasource.base import DataSourceError, shape_price_dataframe
from duk.date_utils import get_api_date_range


def _parse_date(value: Optional[str | date], label: str) -> Optional[date]:
    """Coerce a ``YYYY-MM-DD`` CLI string to a ``date``.

    ``get_api_date_range`` does date arithmetic (start + limit * frequency), so it
    must never see a raw string. The live path parses in
    ``fmp_api.get_price_history``; the db path parses here so both sources agree.
    """
    if value is None or isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataSourceError(
            f"Invalid {label} '{value}': expected YYYY-MM-DD"
        ) from exc


def _connect(dsn: str):
    if not dsn:
        raise DataSourceError(
            "db source selected but no DSN configured. Set FAFNIR_DSN or "
            "[database].dsn in ~/.dukrc, or use --source live."
        )
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover
        raise DataSourceError(
            "db source requires psycopg. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg.connect(dsn, row_factory=dict_row)


# The resolution ladder. Its PREDICATES AND ORDERING must stay identical to
# fafnir.db.repository.resolve_security_id (XREF_RESOLVE_SQL / PRIMARY_RESOLVE_SQL /
# HISTORICAL_XREF_RESOLVE_SQL) so the read path resolves a ticker to the same
# security_id the loader used. Duplicated (not imported) to keep duk's db datasource
# free of a hard fafnir/psycopg import at module load.
#
# The RELATIONS deliberately differ. fafnir runs as fafnir_ingest and reads core;
# duk reads the `mart` seam, because ADR 0008 makes every per-person and per-agent
# role a member of fafnir_app, which has no USAGE on core at all. mart.v_symbol_lookup
# is a passthrough of core.symbol_xref and mart.v_security_profile exposes
# core.security's primary_symbol/source/delisted_date under the alias `symbol`, so
# the rows and their order are the same -- asserted by an integration test rather
# than trusted (test_db_company_summary.py).
_XREF_RESOLVE_SQL = (
    "SELECT security_id FROM mart.v_symbol_lookup "
    "WHERE symbol = %s AND valid_to IS NULL "
    "ORDER BY is_primary DESC, valid_from DESC LIMIT 1"
)
_PRIMARY_RESOLVE_SQL = (
    "SELECT security_id FROM mart.v_security_profile WHERE symbol = %s "
    "ORDER BY (source = %s) DESC, (delisted_date IS NULL) DESC, security_id ASC "
    "LIMIT 1"
)
# A ticker the security used to trade under, before a rename moved it. Last, so a
# live owner of a reused ticker and a delisted issuer both win over it.
_HISTORICAL_XREF_RESOLVE_SQL = (
    "SELECT security_id FROM mart.v_symbol_lookup "
    "WHERE symbol = %s AND valid_to IS NOT NULL "
    "ORDER BY valid_to DESC, valid_from DESC LIMIT 1"
)


def _resolve_security_id(cur, symbol: str, source: str = "fmp") -> Optional[int]:
    cur.execute(_XREF_RESOLVE_SQL, (symbol,))
    row = cur.fetchone()
    if row:
        return int(row["security_id"])
    cur.execute(_PRIMARY_RESOLVE_SQL, (symbol, source))
    row = cur.fetchone()
    if row:
        return int(row["security_id"])
    cur.execute(_HISTORICAL_XREF_RESOLVE_SQL, (symbol,))
    row = cur.fetchone()
    return int(row["security_id"]) if row else None


def price_history(
    *,
    dsn: str,
    symbol: str,
    start_date: Optional[str],
    end_date: Optional[str],
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[list[str]] = None,
    adjusted: bool = False,
) -> pd.DataFrame:
    """Return a date-indexed OHLCV DataFrame from fafnir, shaped like the live path."""
    symbol = symbol.upper()
    start, end = get_api_date_range(
        _parse_date(start_date, "start date"),
        _parse_date(end_date, "end date"),
        limit,
        frequency,
    )

    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _resolve_security_id(cur, symbol)
        if sec_id is None:
            return pd.DataFrame()
        relation = (
            "mart.v_daily_price_adjusted" if adjusted else "mart.v_daily_price_raw"
        )
        clauses = ["security_id = %s"]
        params: list[Any] = [sec_id]
        if start is not None:
            clauses.append("trade_date >= %s")
            params.append(start)
        if end is not None:
            clauses.append("trade_date <= %s")
            params.append(end)
        cur.execute(
            f"SELECT trade_date AS date, open, high, low, close, volume "
            f"FROM {relation} WHERE {' AND '.join(clauses)} ORDER BY trade_date ASC",
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    # Prices: exact decimals -> float for downstream numeric ops (matches live).
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = df[col].astype(float)
    # Volume stays integer to match the live path's int64 dtype (the adjusted view
    # rounds to whole shares as unconstrained numeric, so coerce Decimal/int ->
    # int64). Adjusted volume can legitimately exceed int64 -- a deep forward-split
    # history back-adjusts volume by the cumulative split ratio -- so fall back to
    # exact Python ints (object dtype) rather than overflow-crashing.
    if "volume" in df.columns:
        try:
            df["volume"] = df["volume"].astype("int64")
        except (OverflowError, TypeError):
            df["volume"] = df["volume"].map(int)
    return shape_price_dataframe(
        df,
        frequency=frequency,
        limit=limit,
        fields=fields,
        start_date=start_date,
        end_date=end_date,
    )


def screen(
    *,
    dsn: str,
    sector: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    exchange: Optional[str] = None,
    country: Optional[str] = None,
    marketCapMoreThan: Optional[float] = None,
    marketCapLowerThan: Optional[float] = None,
    priceMoreThan: Optional[float] = None,
    priceLowerThan: Optional[float] = None,
    betaMoreThan: Optional[float] = None,
    betaLowerThan: Optional[float] = None,
    volumeMoreThan: Optional[int] = None,
    volumeLowerThan: Optional[int] = None,
    isEtf: Optional[bool] = None,
    isFund: Optional[bool] = None,
    isActivelyTrading: Optional[bool] = None,
    limit: Optional[int] = None,
    **_ignored,
) -> pd.DataFrame:
    """Screen securities from mart.security_latest. Mirrors the live screener columns."""
    clauses: list[str] = ["1=1"]
    params: list[Any] = []

    def add(cond: str, value):
        clauses.append(cond)
        params.append(value)

    if sector:
        add("sector_name = ANY(%s)", list(sector))
    if industry:
        add("industry_name = ANY(%s)", list(industry))
    if exchange:
        add("exchange_code = %s", exchange)
    if country:
        add("country = %s", country)
    if marketCapMoreThan is not None:
        add("market_cap_usd > %s", marketCapMoreThan)
    if marketCapLowerThan is not None:
        add("market_cap_usd < %s", marketCapLowerThan)
    if priceMoreThan is not None:
        add("last_close > %s", priceMoreThan)
    if priceLowerThan is not None:
        add("last_close < %s", priceLowerThan)
    if betaMoreThan is not None:
        add("beta > %s", betaMoreThan)
    if betaLowerThan is not None:
        add("beta < %s", betaLowerThan)
    if volumeMoreThan is not None:
        add("last_volume > %s", volumeMoreThan)
    if volumeLowerThan is not None:
        add("last_volume < %s", volumeLowerThan)
    if isEtf is not None:
        add("is_etf = %s", isEtf)
    if isFund is not None:
        add("is_fund = %s", isFund)
    if isActivelyTrading is not None:
        add("is_actively_trading = %s", isActivelyTrading)

    sql = (
        'SELECT symbol, company_name AS "companyName", market_cap_usd AS "marketCap", '
        "sector_name AS sector, industry_name AS industry, beta, last_close AS price, "
        "last_volume AS volume, exchange_code AS exchange, country "
        f"FROM mart.security_latest WHERE {' AND '.join(clauses)} ORDER BY symbol"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pd.DataFrame(rows)


def list_sectors(*, dsn: str) -> pd.DataFrame:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sector_id, sector_name FROM ref.sector ORDER BY sector_name"
        )
        return pd.DataFrame(cur.fetchall())


def list_industries(*, dsn: str) -> pd.DataFrame:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT industry_id, industry_name FROM ref.industry ORDER BY industry_name"
        )
        return pd.DataFrame(cur.fetchall())


def list_actively_trading(*, dsn: str, limit: Optional[int] = None) -> pd.DataFrame:
    sql = (
        "SELECT symbol, company_name AS name FROM mart.security_latest "
        "WHERE is_actively_trading ORDER BY symbol"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall())


# ---------------------------------------------------------------------------
# Company summary (`duk ls <query>`)
# ---------------------------------------------------------------------------

# Name search is the LAST rung of the resolution ladder, after the three ticker
# queries above. A ticker is an exact, unambiguous intent and must never lose to a
# substring name match -- "CAT" is Caterpillar, not every company with "cat" in its
# name.
#
# Plain ILIKE, no trigram index: core.security is ~21k rows, so this is a sub-10ms
# sequential scan, and pg_trgm needs a superuser to install -- which the migrator
# deliberately is not. Revisit if the universe grows an order of magnitude.
#
# Ordering: exact match, then prefix match, then alphabetical. `lower(x) = lower(y)`
# rather than ILIKE for the exact rung, since a name may legitimately contain the
# LIKE metacharacters % and _.
_NAME_SEARCH_SQL = """
    SELECT security_id, symbol, company_name, exchange_code, exchange_name,
           is_actively_trading, delisted_date
      FROM mart.v_security_profile
     WHERE company_name ILIKE %(pattern)s OR lower(company_name) = lower(%(query)s)
     ORDER BY (lower(company_name) = lower(%(query)s)) DESC,
              (company_name ILIKE %(prefix)s) DESC,
              company_name ASC
     LIMIT %(limit)s
"""

_PROFILE_BY_ID_SQL = """
    SELECT * FROM mart.v_security_profile WHERE security_id = %s
"""

# The ticker a security used to trade under, when THAT is what the user typed.
# Reported so a summary reached through a rename says so, rather than silently
# answering about a different-looking company.
_FORMER_TICKER_SQL = """
    SELECT symbol, valid_to FROM mart.v_symbol_lookup
     WHERE security_id = %s AND symbol = %s AND valid_to IS NOT NULL
     ORDER BY valid_to DESC LIMIT 1
"""

# Candidates shown when a name matches more than one company. 20 is a screenful;
# past that the query is too vague to disambiguate by reading anyway.
NAME_CANDIDATE_LIMIT = 20

# How far back the adjusted series is pulled for the return statistics. Five years
# covers every trailing window reported (the longest is 1Y, plus headroom for
# annualised volatility) without dragging 8,000 rows across the wire for a
# thirty-year name.
_STATS_LOOKBACK_DAYS = 5 * 366


def resolve_company(*, dsn: str, query: str) -> list[dict]:
    """Resolve a ticker or company name to candidate securities.

    Returns [] for no match, one dict for an unambiguous match, several for an
    ambiguous name. A ticker hit always returns exactly one candidate -- the
    ladder is a precedence, not a search.
    """
    query = (query or "").strip()
    if not query:
        return []

    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _resolve_security_id(cur, query.upper())
        if sec_id is not None:
            cur.execute(_PROFILE_BY_ID_SQL, (sec_id,))
            row = cur.fetchone()
            if row is not None:
                row = dict(row)
                cur.execute(_FORMER_TICKER_SQL, (sec_id, query.upper()))
                former = cur.fetchone()
                # Only when the *typed* ticker is the retired one. Resolving AAPL
                # to a security that also once traded as APPL is not a rename hit.
                row["matched_former_symbol"] = former["symbol"] if former else None
                row["matched_former_valid_to"] = former["valid_to"] if former else None
                return [row]

        cur.execute(
            _NAME_SEARCH_SQL,
            {
                "query": query,
                "pattern": f"%{query}%",
                "prefix": f"{query}%",
                "limit": NAME_CANDIDATE_LIMIT,
            },
        )
        return [dict(r) for r in cur.fetchall()]


def _fundamentals(cur, security_id: int) -> Optional[dict]:
    """The fundamentals row, when that milestone has landed.

    A capability probe rather than a stub: fundamentals are not in the warehouse
    yet, and when they arrive as `mart.v_security_fundamentals_latest` this starts
    reporting them with no duk release. `to_regclass` returns NULL rather than
    raising for an absent relation, which is what makes the probe cheap.
    """
    cur.execute("SELECT to_regclass('mart.v_security_fundamentals_latest') AS rel")
    row = cur.fetchone()
    if row is None or row["rel"] is None:
        return None
    cur.execute(
        "SELECT * FROM mart.v_security_fundamentals_latest WHERE security_id = %s",
        (security_id,),
    )
    found = cur.fetchone()
    return dict(found) if found else None


def company_summary(*, dsn: str, security_id: int) -> dict:
    """Assemble the raw facts behind `duk ls <company>`.

    Returns plain dicts, dates and Decimals -- no formatting and no click. The
    derived statistics (trailing returns, volatility, drawdown) are computed by
    :mod:`duk.company_summary` from ``adjusted_prices``, because those formulas
    already live in ``duk.return_utils`` and are already tested there.
    """
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_PROFILE_BY_ID_SQL, (security_id,))
        profile = cur.fetchone()
        if profile is None:
            raise DataSourceError(f"No security with security_id {security_id}")

        cur.execute(
            "SELECT * FROM mart.v_security_price_coverage WHERE security_id = %s",
            (security_id,),
        )
        coverage = cur.fetchone()

        cur.execute(
            "SELECT * FROM mart.v_security_action_summary WHERE security_id = %s",
            (security_id,),
        )
        actions = cur.fetchone()

        # Last bar comes from the raw series: `last_close` is what the security
        # actually traded at, not a back-adjusted figure.
        cur.execute(
            "SELECT trade_date, close, volume FROM mart.v_daily_price_raw "
            "WHERE security_id = %s ORDER BY trade_date DESC LIMIT 1",
            (security_id,),
        )
        last_bar = cur.fetchone()

        cur.execute(
            "SELECT check_name, severity, record_key, detected_at "
            "FROM mart.v_security_dq_open WHERE security_id = %s "
            "ORDER BY check_name, detected_at",
            (security_id,),
        )
        dq_flags = [dict(r) for r in cur.fetchall()]

        fundamentals = _fundamentals(cur, security_id)

    adjusted = pd.DataFrame()
    if last_bar is not None:
        start = last_bar["trade_date"] - timedelta(days=_STATS_LOOKBACK_DAYS)
        adjusted = price_history(
            dsn=dsn,
            symbol=profile["symbol"],
            start_date=start.isoformat(),
            end_date=last_bar["trade_date"].isoformat(),
            frequency="day",
            limit=None,
            fields=["close"],
            adjusted=True,
        )

    return {
        "profile": dict(profile),
        "coverage": dict(coverage) if coverage else None,
        "actions": dict(actions) if actions else None,
        "last_bar": dict(last_bar) if last_bar else None,
        "dq_flags": dq_flags,
        "fundamentals": fundamentals,
        "adjusted_prices": adjusted,
    }
