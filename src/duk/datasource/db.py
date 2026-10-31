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

from duk import identifiers
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

# The security that traded under the ticker on a date (#80): AS_OF_RESOLVE_SQL,
# on the mart seam. Not a rung of the ladder above -- a caller who named a date is
# answered for that date or not at all.
_AS_OF_RESOLVE_SQL = (
    "SELECT x.security_id FROM mart.v_symbol_lookup x "
    "WHERE x.symbol = %s AND x.valid_from <= %s "
    "AND (x.valid_to IS NULL OR x.valid_to >= %s) "
    "AND (x.valid_from > DATE '1900-01-01' OR EXISTS ("
    "SELECT 1 FROM mart.v_daily_price_raw p "
    "WHERE p.security_id = x.security_id AND p.trade_date <= %s)) "
    "ORDER BY x.valid_from DESC, x.is_primary DESC, x.security_id ASC LIMIT 1"
)


def _resolve_security_id(
    cur, symbol: str, source: str = "fmp", as_of: Optional[date] = None
) -> Optional[int]:
    if as_of is not None:
        cur.execute(_AS_OF_RESOLVE_SQL, (symbol, as_of, as_of, as_of))
        row = cur.fetchone()
        return int(row["security_id"]) if row else None
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


# ---------------------------------------------------------------------------
# Identifier resolution (`cik:` / `isin:` / `cusip:`)
# ---------------------------------------------------------------------------

# Deliberately NOT a rung of the ticker ladder above. That ladder is a precedence
# duplicated from fafnir.db.repository.resolve_security_id and asserted to match it
# (test_mart_read_seam.py); an identifier reaches a security by a different key and
# with a different failure mode (a CIK is an ISSUER, so it can legitimately match
# several securities), so it resolves separately and returns candidates rather than
# one id.
#
# All three predicates compare NORMALISED forms on both sides, because the vendor's
# spelling and the user's differ in ways that carry no information: the SEC writes
# CIK 0000051143 and people type 51143, and an ISIN or CUSIP is pasted in whatever
# case and grouping it was copied from. duk.identifiers normalises the typed value;
# these expressions normalise the stored one. Migration 0023 indexes exactly these
# expressions, so they must not drift from it.
_IDENT_COLUMNS = (
    "security_id, symbol, company_name, exchange_code, exchange_name, "
    "is_actively_trading, delisted_date"
)
# Candidate order, when there is more than one: listed before delisted, then by
# ticker. It orders the did-you-mean table; it never picks a winner.
_IDENT_ORDER = (
    " ORDER BY (delisted_date IS NULL) DESC, is_actively_trading DESC, symbol ASC"
)

_IDENT_BY_ID_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile WHERE security_id = %s"
)
_CIK_RESOLVE_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile "
    "WHERE ltrim(btrim(cik), '0') = %s" + _IDENT_ORDER
)
_ISIN_RESOLVE_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile "
    "WHERE upper(btrim(isin)) = %s" + _IDENT_ORDER
)
_CUSIP_RESOLVE_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile "
    "WHERE upper(btrim(cusip)) = %s" + _IDENT_ORDER
)
# An 8-character CUSIP is the same issue without its check digit; match on the
# stored value's first eight.
_CUSIP8_RESOLVE_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile "
    "WHERE left(upper(btrim(cusip)), 8) = %s" + _IDENT_ORDER
)
# Second rung for both CUSIP and ISIN: a North American ISIN is literally
# `country || CUSIP || check digit`, so each identifier still finds the security
# when the vendor filled in only the other column. Not a guess -- the two encode
# the same issue -- and only reached when the direct match found nothing.
_CUSIP_VIA_ISIN_SQL = (
    f"SELECT {_IDENT_COLUMNS} FROM mart.v_security_profile "
    "WHERE left(upper(btrim(isin)), 2) IN ('US', 'CA') "
    "AND substring(upper(btrim(isin)) from 3 for %s) = %s" + _IDENT_ORDER
)


def _rows(cur, sql: str, params: tuple) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _resolve_by_identifier(cur, ident: identifiers.Identifier) -> list[dict]:
    """Candidate securities carrying an identifier. Empty, one, or several.

    Several is a real answer, not a failure of the query: one CIK is one SEC filer,
    and a filer can have several listed classes (GOOG and GOOGL share one). The
    caller shows a did-you-mean rather than picking, for the same reason the name
    search does -- answering confidently about the wrong share class is the failure
    that cannot be spotted from the output.
    """
    if ident.scheme == identifiers.TICKER_AT:
        sec_id = _resolve_security_id(cur, ident.value, as_of=ident.as_of)
        if sec_id is None:
            return []
        return _rows(cur, _IDENT_BY_ID_SQL, (sec_id,))
    if ident.scheme == identifiers.SECURITY_ID:
        return _rows(cur, _IDENT_BY_ID_SQL, (int(ident.value),))
    if ident.scheme == identifiers.CIK:
        return _rows(cur, _CIK_RESOLVE_SQL, (ident.value,))
    if ident.scheme == identifiers.ISIN:
        found = _rows(cur, _ISIN_RESOLVE_SQL, (ident.value,))
        if found:
            return found
        cusip = identifiers.cusip_from_isin(ident.value)
        return _rows(cur, _CUSIP_RESOLVE_SQL, (cusip,)) if cusip else []
    if ident.scheme == identifiers.CUSIP:
        sql = _CUSIP_RESOLVE_SQL if len(ident.value) == 9 else _CUSIP8_RESOLVE_SQL
        found = _rows(cur, sql, (ident.value,))
        if found:
            return found
        return _rows(cur, _CUSIP_VIA_ISIN_SQL, (len(ident.value), ident.value))
    raise DataSourceError(  # pragma: no cover -- tickers never reach here
        f"{ident.label} is not resolvable as an identifier"
    )


# Every security that has traded under a ticker, newest first, with the last day of
# its period under it (today for an open one): the candidates a bare ticker could
# mean when the ticker was reused (#80).
_TICKER_PERIODS_SQL = """
    SELECT p.security_id, p.symbol, p.company_name, p.exchange_code,
           p.exchange_name, p.is_actively_trading, p.delisted_date,
           max(COALESCE(x.valid_to, CURRENT_DATE)) AS last_day
      FROM mart.v_symbol_lookup x
      JOIN mart.v_security_profile p ON p.security_id = x.security_id
     WHERE x.symbol = %s
     GROUP BY p.security_id, p.symbol, p.company_name, p.exchange_code,
              p.exchange_name, p.is_actively_trading, p.delisted_date
     ORDER BY last_day DESC, p.security_id
"""


def ticker_periods(*, dsn: str, symbol: str) -> list[dict]:
    """The securities that have traded under ``symbol``, newest first."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        return _rows(cur, _TICKER_PERIODS_SQL, (symbol.upper(),))


def resolve_identifier(*, dsn: str, identifier: identifiers.Identifier) -> list[dict]:
    """`_resolve_by_identifier` with its own connection, for callers outside a cursor."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        return _resolve_by_identifier(cur, identifier)


def price_history(
    *,
    dsn: str,
    symbol: Optional[str] = None,
    start_date: Optional[str],
    end_date: Optional[str],
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[list[str]] = None,
    adjusted: bool = False,
    security_id: Optional[int] = None,
    exact: bool = False,
) -> pd.DataFrame:
    """Return a date-indexed OHLCV DataFrame from fafnir, shaped like the live path.

    With ``exact`` the prices stay the ``Decimal`` values the warehouse stores
    (object columns) instead of becoming floats, for a caller that must not
    read a price through one (`ph --exact`). Daily frequency only: resampling
    is arithmetic, and the float path is the one that does it.

    Give ``symbol`` to resolve a ticker through the ladder, or ``security_id`` when
    the caller already holds the identity -- which is not merely a saved query. A
    caller that resolved by CIK/ISIN/CUSIP, or that is reporting on a security it
    already has in hand, must not round-trip through ``primary_symbol``: a reused
    ticker resolves to whoever owns it NOW, so the round trip can hand back a
    different security's prices under the right-looking name.
    """
    if security_id is None and not symbol:
        raise DataSourceError("price_history needs either a symbol or a security_id")
    start, end = get_api_date_range(
        _parse_date(start_date, "start date"),
        _parse_date(end_date, "end date"),
        limit,
        frequency,
    )

    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = security_id
        if sec_id is None:
            sec_id = _resolve_security_id(cur, symbol.upper())
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
    if exact:
        if frequency.lower() != "day":
            raise DataSourceError(
                "exact prices are daily bars; frequency must be 'day'"
            )
        return shape_price_dataframe(
            df,
            frequency="day",
            limit=limit,
            fields=fields,
            start_date=start_date,
            end_date=end_date,
        )
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


#: Screen filters the warehouse cannot honour. ``mart.security_latest`` carries no
#: dividend column, and a filter dropped silently answers a different question
#: with a confident list, so each is refused by name instead.
UNSUPPORTED_SCREEN_FILTERS = {
    "dividendMoreThan": "--dividend",
    "dividendLowerThan": "--dividend",
}


def _norm_name(value: str) -> str:
    """A sector or industry name as compared: case and spacing carry no meaning."""
    return " ".join(str(value).split()).lower()


def _screen_clauses(
    *,
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
    includeDelisted: bool = False,
    **ignored,
) -> tuple[list[str], list[Any]]:
    """The WHERE clauses of a screen over ``mart.security_latest``, and their
    parameters. Shared by :func:`screen` and the counted lists, so a count is the
    number of rows the same screen returns.

    Text filters compare case- and space-insensitively: ``energy``, ``Energy``
    and ``ENERGY`` are one sector, ``nyse`` one exchange.
    """
    for key, flag in UNSUPPORTED_SCREEN_FILTERS.items():
        if ignored.get(key) is not None:
            raise DataSourceError(
                f"{flag} cannot be screened in the warehouse: mart.security_latest "
                "carries no dividend. Use -S fmp, or screen without it."
            )
    clauses: list[str] = ["1=1"]
    params: list[Any] = []

    def add(cond: str, value):
        clauses.append(cond)
        params.append(value)

    norm = "lower(regexp_replace(btrim({col}), '\\s+', ' ', 'g'))"
    if sector:
        add(
            f"{norm.format(col='sector_name')} = ANY(%s)",
            [_norm_name(v) for v in sector],
        )
    if industry:
        add(
            f"{norm.format(col='industry_name')} = ANY(%s)",
            [_norm_name(v) for v in industry],
        )
    if exchange:
        add("upper(btrim(exchange_code)) = %s", exchange.strip().upper())
    if country:
        add("upper(btrim(country)) = %s", country.strip().upper())
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
    elif not includeDelisted:
        clauses.append("is_actively_trading")
    return clauses, params


def screen(*, dsn: str, limit: Optional[int] = None, **filters) -> pd.DataFrame:
    """Screen securities from mart.security_latest. Mirrors the live screener columns.

    Actively trading securities only, unless ``includeDelisted`` or an explicit
    ``isActivelyTrading``: the warehouse keeps every security that stopped trading,
    the dead issuers minted from Sharadar among them (#79), and a screen without
    the filter returns several times the rows of the market as it trades now.

    Sector, industry, exchange and country match case-insensitively. A dividend
    filter is refused (:data:`UNSUPPORTED_SCREEN_FILTERS`), not ignored.
    """
    clauses, params = _screen_clauses(**filters)
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


def sector_names(*, dsn: str) -> list[str]:
    """Every sector name the warehouse knows (``ref.sector``), alphabetically."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT sector_name FROM ref.sector ORDER BY sector_name")
        return [r["sector_name"] for r in cur.fetchall()]


def industry_names(*, dsn: str) -> list[str]:
    """Every industry name the warehouse knows (``ref.industry``), alphabetically."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT industry_name FROM ref.industry ORDER BY industry_name")
        return [r["industry_name"] for r in cur.fetchall()]


# Counted over the same rows a screen with these filters returns (the screen's
# WHERE), from the refresh-lagged snapshot. Every sector in ref.sector is listed,
# with 0 where no security falls in it -- unless an industry filter scopes the
# list, when only the sectors holding that industry are.
_SECTOR_COUNT_SQL = """
    WITH counted AS (
        SELECT sector_name, count(*) AS n
          FROM mart.security_latest
         WHERE {where}
         GROUP BY sector_name
    )
    SELECT s.sector_id, s.sector_name, COALESCE(c.n, 0)::bigint AS security_count
      FROM ref.sector s
      LEFT JOIN counted c ON c.sector_name = s.sector_name
     WHERE %s IS FALSE OR c.n IS NOT NULL
     ORDER BY s.sector_name
"""
# ref.industry carries no sector: an industry's sector is the one its securities
# are classified in. One that spans two sectors is listed once under each, with
# its count in each; one holding no security has no sector to name (NULL, and 0).
_INDUSTRY_COUNT_SQL = """
    WITH counted AS (
        SELECT industry_name, sector_name, count(*) AS n
          FROM mart.security_latest
         WHERE {where} AND industry_name IS NOT NULL
         GROUP BY industry_name, sector_name
    )
    SELECT i.industry_id, i.industry_name, c.sector_name,
           COALESCE(c.n, 0)::bigint AS security_count
      FROM ref.industry i
      LEFT JOIN counted c ON c.industry_name = i.industry_name
     WHERE %s IS FALSE OR c.n IS NOT NULL
     ORDER BY i.industry_name, c.sector_name NULLS LAST
"""
_UNCLASSIFIED_SQL = (
    "SELECT count(*) AS n FROM mart.security_latest WHERE {where} AND {column} IS NULL"
)


def _counted(
    dsn: str, sql: str, column: str, scoped: bool, filters: dict
) -> pd.DataFrame:
    clauses, params = _screen_clauses(**filters)
    where = " AND ".join(clauses)
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql.format(where=where), [*params, scoped])
        frame = pd.DataFrame(cur.fetchall())
        cur.execute(_UNCLASSIFIED_SQL.format(where=where, column=column), params)
        unclassified = int(cur.fetchone()["n"])
    # Securities the screen matched that carry no sector (industry): not a row,
    # since there is no name to list them under, but not dropped from the count
    # either -- the table's footer says how many.
    frame.attrs["unclassified"] = unclassified
    return frame


def list_sectors(*, dsn: str, **filters) -> pd.DataFrame:
    """Sectors with ``security_count``: how many securities a screen with
    ``filters`` returns in each (actively trading by default, as :func:`screen`).

    ``frame.attrs["unclassified"]`` is how many of the screened securities have no
    sector at all.
    """
    scoped = bool(filters.get("industry"))
    return _counted(dsn, _SECTOR_COUNT_SQL, "sector_name", scoped, filters)


def list_industries(*, dsn: str, **filters) -> pd.DataFrame:
    """Industries with the ``sector_name`` their securities are classified in and
    ``security_count``, scoped by ``filters`` as :func:`list_sectors` is. With a
    sector filter, only the industries holding a security in it."""
    scoped = bool(filters.get("sector"))
    return _counted(dsn, _INDUSTRY_COUNT_SQL, "industry_name", scoped, filters)


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
    """Resolve a ticker, an identifier or a company name to candidate securities.

    Returns [] for no match, one dict for an unambiguous match, several for an
    ambiguous name or an identifier shared by more than one security. A ticker hit
    always returns exactly one candidate -- the ladder is a precedence, not a
    search.

    ``cik:``/``isin:``/``cusip:`` short-circuits the ladder entirely: the user named
    a key, so neither the ticker rungs nor the name search below can improve on the
    answer, and running them would let ``cik:51143`` fall through to a name search
    for the string "cik:51143".

    Raises :class:`duk.identifiers.IdentifierError` for a malformed identifier.
    """
    query = (query or "").strip()
    if not query:
        return []
    ident = identifiers.parse(query)

    with _connect(dsn) as conn, conn.cursor() as cur:
        if not ident.is_ticker:
            return _resolve_by_identifier(cur, ident)

        sec_id = _resolve_security_id(cur, ident.value)
        if sec_id is not None:
            cur.execute(_PROFILE_BY_ID_SQL, (sec_id,))
            row = cur.fetchone()
            if row is not None:
                row = dict(row)
                cur.execute(_FORMER_TICKER_SQL, (sec_id, ident.value))
                former = cur.fetchone()
                # Only when the *typed* ticker is the retired one. Resolving AAPL
                # to a security that also once traded as APPL is not a rename hit.
                row["matched_former_symbol"] = former["symbol"] if former else None
                row["matched_former_valid_to"] = former["valid_to"] if former else None
                return [row]

        if ident.explicit:
            # `ticker:`/`symbol:` is an assertion about what the argument IS. A name
            # search on it would answer a question the user explicitly did not ask.
            return []

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
            security_id=security_id,
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


# ---------------------------------------------------------------------------
# Fundamentals (`duk fs` / `duk fa`, #107)
# ---------------------------------------------------------------------------

#: `--statement` and the mart view of every version of that statement (0036).
STATEMENT_VIEWS = {
    "income": "mart.v_income_statement_versions",
    "balance": "mart.v_balance_sheet_versions",
    "cash": "mart.v_cash_flow_statement_versions",
}
STATEMENT_PERIODS = ("annual", "quarter")

# A version in force at D, the rule each view's comment states and
# mart.fn_statements_as_of applies: current on the load timeline, known at D on the
# knowledge timeline. That function answers each security's latest period only; a
# statement is every period, so it is read here from the view by the same rule.
_STATEMENT_SQL = """
    SELECT * FROM {view}
     WHERE security_id = %(sid)s AND period_type = %(period)s
       AND superseded_at IS NULL AND known_from <= %(as_of)s
       AND (known_to IS NULL OR %(as_of)s < known_to)
     ORDER BY fiscal_date DESC
"""
# What a point-in-time read leaves out: what the caller already named, and the later
# dates of both timelines. A version's known_to is when it stopped being in force,
# which seen from an --as-of date is the future (ADR 0018).
_STATEMENT_HIDDEN = frozenset(
    {"security_id", "period_type", "known_to", "loaded_at", "superseded_at"}
)

# Decision 5 (plan fundamentals-sf1.md): SF1 files an issuer's statements under its
# primary share class only. A class holding none of its own is answered with the
# one other class under its CIK that does hold them, and never with a guess among
# several.
_HOLDS_STATEMENTS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM mart.v_income_statement_versions "
    "WHERE security_id = %s) AS held"
)
_ISSUER_CLASS_SQL = """
    SELECT p.security_id, p.symbol, p.cik
      FROM mart.v_security_profile me
      JOIN mart.v_security_profile p
        ON ltrim(btrim(p.cik), '0') = ltrim(btrim(me.cik), '0')
       AND p.security_id <> me.security_id
     WHERE me.security_id = %s
       AND NULLIF(ltrim(btrim(me.cik), '0'), '') IS NOT NULL
       AND EXISTS (SELECT 1 FROM mart.v_income_statement_versions v
                    WHERE v.security_id = p.security_id)
"""

# The factor row (0037, 0038). Without a date, the latest snapshot; with one, the
# panel's month-end on or before it. Both are materialized: unrefreshed, PostgreSQL
# refuses the read with SQLSTATE 55000.
_LATEST_FACTORS_SQL = (
    "SELECT * FROM mart.security_fundamentals_latest WHERE security_id = %s"
)
_LATEST_AS_OF_SQL = "SELECT max(as_of_date) AS d FROM mart.security_fundamentals_latest"
_PANEL_MONTH_END_SQL = (
    "SELECT max(as_of_date) AS d FROM mart.fundamental_panel WHERE as_of_date <= %s"
)
_PANEL_ROW_SQL = (
    "SELECT * FROM mart.fundamental_panel WHERE security_id = %s AND as_of_date = %s"
)
_NOT_POPULATED = "55000"


def _sec_id(cur, symbol: Optional[str], security_id: Optional[int]) -> Optional[int]:
    if security_id is not None:
        return security_id
    if not symbol:
        raise DataSourceError("a symbol or a security_id is required")
    return _resolve_security_id(cur, symbol.upper())


def _statements_of(cur, security_id: int) -> Optional[dict]:
    """The issuer's class whose statements stand for ``security_id``'s, or None
    when it holds its own (or no single other class does)."""
    cur.execute(_HOLDS_STATEMENTS_SQL, (security_id,))
    if cur.fetchone()["held"]:
        return None
    found = _rows(cur, _ISSUER_CLASS_SQL, (security_id,))
    return found[0] if len(found) == 1 else None


def statements(
    *,
    dsn: str,
    symbol: Optional[str] = None,
    security_id: Optional[int] = None,
    statement: str = "income",
    period: str = "annual",
    as_of: Optional[str | date] = None,
    limit: Optional[int] = None,
) -> dict:
    """One statement of one security, every period as known at ``as_of``.

    Each period is at its version in force on ``as_of`` (default today): a period
    filed later is absent, and a restatement after ``as_of`` does not show. Values
    stay the exact decimals the warehouse stores; share counts are in the share
    terms of each version's ``known_from`` (ADR 0019).

    Returns ``security_id`` (None: no such security), ``as_of``, ``statements_of``
    (the issuer's class read instead, decision 5, else None) and ``rows``, oldest
    first; ``limit`` keeps the latest periods.
    """
    if statement not in STATEMENT_VIEWS:
        raise DataSourceError(
            f"Unknown statement '{statement}': expected one of "
            f"{', '.join(STATEMENT_VIEWS)}"
        )
    if period not in STATEMENT_PERIODS:
        raise DataSourceError(f"Unknown period '{period}': expected annual or quarter")
    if limit is not None and limit < 1:
        raise DataSourceError("limit must be at least 1")
    day = _parse_date(as_of, "as-of date")

    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _sec_id(cur, symbol, security_id)
        cur.execute("SELECT COALESCE(%s::date, current_date) AS d", (day,))
        day = cur.fetchone()["d"]
        out = {"security_id": sec_id, "as_of": day, "statements_of": None, "rows": []}
        if sec_id is None:
            return out
        issuer = _statements_of(cur, sec_id)
        sql = _STATEMENT_SQL.format(view=STATEMENT_VIEWS[statement])
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(
            sql,
            {
                "sid": issuer["security_id"] if issuer else sec_id,
                "period": period,
                "as_of": day,
            },
        )
        rows = [
            {k: v for k, v in r.items() if k not in _STATEMENT_HIDDEN}
            for r in cur.fetchall()
        ]
    rows.reverse()
    out.update(statements_of=issuer, rows=rows)
    return out


def factor_row(
    *,
    dsn: str,
    symbol: Optional[str] = None,
    security_id: Optional[int] = None,
    as_of: Optional[str | date] = None,
) -> dict:
    """The factor panel's row for one security.

    Without ``as_of``, ``mart.security_fundamentals_latest``: the factors as of the
    last `fafnir db refresh-marts`. With it, ``mart.fundamental_panel`` at the last
    month-end on or before ``as_of``, every input as known then. A secondary class
    is answered with its issuer's class, as :func:`statements` is.

    Returns ``security_id``, ``source``, ``as_of_date`` (the snapshot's or the
    month-end's; None before the panel's first), ``statements_of`` and ``row``
    (None: the security did not trade in the week to that date).
    """
    day = _parse_date(as_of, "as-of date")
    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _sec_id(cur, symbol, security_id)
        source = (
            "mart.security_fundamentals_latest"
            if day is None
            else "mart.fundamental_panel"
        )
        out = {
            "security_id": sec_id,
            "source": source,
            "as_of_date": None,
            "statements_of": None,
            "row": None,
        }
        if sec_id is None:
            return out
        issuer = _statements_of(cur, sec_id)
        read_id = issuer["security_id"] if issuer else sec_id
        try:
            if day is None:
                cur.execute(_LATEST_AS_OF_SQL)
                when = cur.fetchone()["d"]
                cur.execute(_LATEST_FACTORS_SQL, (read_id,))
            else:
                cur.execute(_PANEL_MONTH_END_SQL, (day,))
                when = cur.fetchone()["d"]
                cur.execute(_PANEL_ROW_SQL, (read_id, when))
            row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 -- one case is translated
            if getattr(exc, "sqlstate", None) == _NOT_POPULATED:
                raise DataSourceError(
                    f"{source} has not been refreshed yet: run "
                    "`fafnir db refresh-marts`"
                ) from exc
            raise
    out.update(
        as_of_date=when,
        statements_of=issuer,
        row=dict(row) if row else None,
    )
    return out


# ---------------------------------------------------------------------------
# Ticker history, corporate actions, trading calendar (`duk th` / `ca` / `tc`)
#
# The reads a client keeping its own books needs besides prices: which security
# a ticker named on each day, the splits and dividends at event grain, and the
# venue's sessions. portable reads all three through these commands (its ADR
# 0032), so they return the warehouse's rows as they are -- no resolution ladder,
# no fallback, no rounding -- and leave the policy to the caller.
# ---------------------------------------------------------------------------

# Each period during which a ticker named a security, with the security it named
# as it is described now. `valid_to` is the period's last day (NULL: still open);
# a period starting 1900-01-01 is the "first seen" fallback for a ticker whose
# start fafnir never learned. Periods overlap only where the xref records both.
_TICKER_HISTORY_SQL = """
    SELECT x.symbol, x.security_id, x.valid_from, x.valid_to, x.is_primary,
           p.symbol AS primary_symbol, p.company_name, p.asset_type,
           p.exchange_code, p.currency, p.country, p.cik, p.isin, p.cusip,
           p.is_actively_trading, p.delisted_date
      FROM mart.v_symbol_lookup x
      JOIN mart.v_security_profile p ON p.security_id = x.security_id
     WHERE {who}
       AND (%(end)s::date IS NULL OR x.valid_from <= %(end)s::date)
       AND (%(start)s::date IS NULL OR x.valid_to IS NULL
            OR x.valid_to >= %(start)s::date)
     ORDER BY x.valid_from, x.security_id, x.symbol
"""


def ticker_history(
    *,
    dsn: str,
    symbol: Optional[str] = None,
    security_ids: Optional[list[int]] = None,
    start_date: Optional[str | date] = None,
    end_date: Optional[str | date] = None,
) -> list[dict]:
    """The periods of a ticker's history, or of the given securities' tickers.

    With ``symbol``, every security the ticker has named (matched in any case);
    with ``security_ids``, every ticker those securities have held. ``start_date``
    / ``end_date`` keep the periods that overlap the range. Oldest first.
    """
    if (symbol is None) == (security_ids is None):
        raise DataSourceError("ticker_history needs a symbol or security_ids")
    start = _parse_date(start_date, "start date")
    end = _parse_date(end_date, "end date")
    if start and end and end < start:
        raise DataSourceError(f"the range ends ({end}) before it starts ({start})")
    if symbol is not None:
        who, value = "upper(x.symbol) = %(who)s", symbol.strip().upper()
    else:
        who, value = "x.security_id = ANY(%(who)s)", [int(i) for i in security_ids]
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            _TICKER_HISTORY_SQL.format(who=who),
            {"who": value, "start": start, "end": end},
        )
        return [dict(r) for r in cur.fetchall()]


_CORPORATE_ACTIONS_SQL = """
    SELECT ex_date, action_type, split_numerator, split_denominator,
           dividend_amount, currency, record_date, payment_date, declaration_date,
           corporate_action_id, security_id, source
      FROM mart.v_corporate_action
     WHERE security_id = %(sid)s
       AND (%(start)s::date IS NULL OR ex_date >= %(start)s::date)
       AND (%(end)s::date IS NULL OR ex_date <= %(end)s::date)
       AND (%(kind)s::text IS NULL OR action_type = %(kind)s::text)
     ORDER BY ex_date, action_type, corporate_action_id
"""
ACTION_TYPES = ("split", "dividend")


def corporate_actions(
    *,
    dsn: str,
    symbol: Optional[str] = None,
    security_id: Optional[int] = None,
    start_date: Optional[str | date] = None,
    end_date: Optional[str | date] = None,
    action_type: Optional[str] = None,
) -> dict:
    """One security's splits and cash dividends, one row per event, by ex-date.

    Amounts and ratios stay the ``NUMERIC`` the warehouse stores (``Decimal``).
    Returns ``security_id`` (None: no such security) and ``rows``.

    Raises DataSourceError naming fafnir migration 0033 when the warehouse has no
    ``mart.v_corporate_action`` yet.
    """
    if action_type is not None and action_type not in ACTION_TYPES:
        raise DataSourceError(
            f"Unknown action type '{action_type}': expected split or dividend"
        )
    start = _parse_date(start_date, "start date")
    end = _parse_date(end_date, "end date")
    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _sec_id(cur, symbol, security_id)
        out: dict = {"security_id": sec_id, "rows": []}
        if sec_id is None:
            return out
        try:
            cur.execute(
                _CORPORATE_ACTIONS_SQL,
                {"sid": sec_id, "start": start, "end": end, "kind": action_type},
            )
        except Exception as exc:  # noqa: BLE001 -- one case is translated
            if getattr(exc, "sqlstate", None) == "42P01":
                raise DataSourceError(
                    "this warehouse has no mart.v_corporate_action: it arrives with "
                    "fafnir migration 0033 (`fafnir db migrate` on the host)"
                ) from exc
            raise
        out["rows"] = [dict(r) for r in cur.fetchall()]
    return out


# Every venue with a calendar, and the span it holds. fafnir seeds open days only;
# a closure no rule predicts is a row with is_open false (migration 0022), so a day
# with no row inside the span is a closed day and outside it is unknown.
_CALENDAR_SUMMARY_SQL = """
    SELECT c.exchange_code, x.exchange_name,
           min(c.trade_date) AS first_date, max(c.trade_date) AS last_date,
           count(*) FILTER (WHERE c.is_open) AS open_days,
           count(*) FILTER (WHERE NOT c.is_open) AS closed_days
      FROM ref.trading_calendar c
      LEFT JOIN ref.exchange x ON x.exchange_code = c.exchange_code
     GROUP BY c.exchange_code, x.exchange_name
     ORDER BY c.exchange_code
"""
_CALENDAR_DAYS_SQL = """
    SELECT trade_date AS date, is_open, session_note
      FROM ref.trading_calendar
     WHERE upper(exchange_code) = %(exchange)s
       AND (%(start)s::date IS NULL OR trade_date >= %(start)s::date)
       AND (%(end)s::date IS NULL OR trade_date <= %(end)s::date)
     ORDER BY trade_date
"""


def calendars(*, dsn: str) -> list[dict]:
    """Each venue's calendar: its span, and how many open and closed days it marks."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_CALENDAR_SUMMARY_SQL)
        return [dict(r) for r in cur.fetchall()]


def calendar_days(
    *,
    dsn: str,
    exchange: str,
    start_date: Optional[str | date] = None,
    end_date: Optional[str | date] = None,
) -> list[dict]:
    """``exchange``'s calendar rows from ``start_date`` to ``end_date``: each day it
    marks, open or (an ad-hoc closure) not. No fallback to another venue."""
    start = _parse_date(start_date, "start date")
    end = _parse_date(end_date, "end date")
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            _CALENDAR_DAYS_SQL,
            {"exchange": exchange.strip().upper(), "start": start, "end": end},
        )
        return [dict(r) for r in cur.fetchall()]
