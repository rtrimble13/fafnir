"""
Database data source: the fafnir PostgreSQL warehouse.

Reads from the ``mart``/``core`` schemas and returns the same DataFrame contracts
as the live (FMP) path, so the CLI output and the pure compute modules behave
identically regardless of source. psycopg is imported lazily so duk remains
usable in live mode without it installed.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from duk.datasource.base import DataSourceError, shape_price_dataframe
from duk.date_utils import get_api_date_range


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


# These two queries MUST stay identical to fafnir.db.repository.resolve_security_id
# (XREF_RESOLVE_SQL / PRIMARY_RESOLVE_SQL) so the read path resolves a ticker to the
# same security_id the loader used. Duplicated (not imported) to keep duk's db
# datasource free of a hard fafnir/psycopg import at module load.
_XREF_RESOLVE_SQL = (
    "SELECT security_id FROM core.symbol_xref "
    "WHERE symbol = %s AND valid_to IS NULL "
    "ORDER BY is_primary DESC, valid_from DESC LIMIT 1"
)
_PRIMARY_RESOLVE_SQL = (
    "SELECT security_id FROM core.security WHERE primary_symbol = %s "
    "ORDER BY (source = %s) DESC, security_id ASC LIMIT 1"
)


def _resolve_security_id(cur, symbol: str, source: str = "fmp") -> Optional[int]:
    cur.execute(_XREF_RESOLVE_SQL, (symbol,))
    row = cur.fetchone()
    if row:
        return int(row["security_id"])
    cur.execute(_PRIMARY_RESOLVE_SQL, (symbol, source))
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
    start, end = get_api_date_range(start_date, end_date, limit, frequency)

    with _connect(dsn) as conn, conn.cursor() as cur:
        sec_id = _resolve_security_id(cur, symbol)
        if sec_id is None:
            return pd.DataFrame()
        relation = "mart.v_daily_price_adjusted" if adjusted else "core.daily_price"
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
    # Volume stays integer to match the live path's int64 dtype (the adjusted
    # view returns numeric(38,0), so coerce Decimal/int -> int64).
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype("int64")
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
