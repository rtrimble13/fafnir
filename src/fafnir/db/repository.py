"""
Parameterized data-access functions over the fafnir schema.

Writes are key-based upserts (``ON CONFLICT ... DO UPDATE``) so every load is
idempotent. Reads return plain dicts/lists; the duk ``db`` datasource shapes
them into the DataFrame contracts the CLI expects.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from fafnir.db.connection import Database

# ---------------------------------------------------------------------------
# Reference dimensions
# ---------------------------------------------------------------------------


def ensure_exchange(
    db: Database, code: str, name: str | None = None, country: str | None = None
) -> None:
    if not code:
        return
    db.execute(
        """
        INSERT INTO ref.exchange (exchange_code, exchange_name, country)
        VALUES (%s, %s, %s)
        ON CONFLICT (exchange_code) DO NOTHING
        """,
        (code, name, country),
    )


def get_or_create_sector(db: Database, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    db.execute(
        "INSERT INTO ref.sector (sector_name) VALUES (%s) ON CONFLICT (sector_name) DO NOTHING",
        (name,),
    )
    return db.fetchval(
        "SELECT sector_id FROM ref.sector WHERE sector_name = %s", (name,)
    )


def get_or_create_industry(db: Database, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    db.execute(
        "INSERT INTO ref.industry (industry_name) VALUES (%s) "
        "ON CONFLICT (industry_name) DO NOTHING",
        (name,),
    )
    return db.fetchval(
        "SELECT industry_id FROM ref.industry WHERE industry_name = %s", (name,)
    )


# ---------------------------------------------------------------------------
# Security master
# ---------------------------------------------------------------------------


def upsert_security(
    db: Database,
    *,
    primary_symbol: str,
    company_name: Optional[str],
    asset_type: str = "equity",
    exchange_code: Optional[str] = None,
    sector_id: Optional[int] = None,
    industry_id: Optional[int] = None,
    currency: str = "USD",
    country: Optional[str] = None,
    is_actively_trading: bool = True,
    is_etf: bool = False,
    is_fund: bool = False,
    ipo_date: Optional[date] = None,
    delisted_date: Optional[date] = None,
    cik: Optional[str] = None,
    isin: Optional[str] = None,
    cusip: Optional[str] = None,
    source: str = "fmp",
) -> int:
    """Insert/update a security by its (source, primary_symbol, exchange) soft key.

    The conflict arbiter is 0009's *partial* index, which covers only rows with
    ``delisted_date IS NULL``. A delisted security is therefore invisible here: a
    reused ticker inserts a new row and mints a new security_id rather than
    overwriting the dead issuer's identity and price history.

    Returns the security_id.
    """
    row = db.fetchone(
        """
        INSERT INTO core.security
            (primary_symbol, company_name, asset_type, exchange_code, sector_id,
             industry_id, currency, country, is_actively_trading, is_etf, is_fund,
             ipo_date, delisted_date, cik, isin, cusip, source, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (source, primary_symbol, COALESCE(exchange_code, ''))
            WHERE delisted_date IS NULL
        DO UPDATE SET
            company_name        = EXCLUDED.company_name,
            asset_type          = EXCLUDED.asset_type,
            sector_id           = EXCLUDED.sector_id,
            industry_id         = EXCLUDED.industry_id,
            currency            = EXCLUDED.currency,
            country             = EXCLUDED.country,
            is_actively_trading = EXCLUDED.is_actively_trading,
            is_etf              = EXCLUDED.is_etf,
            is_fund             = EXCLUDED.is_fund,
            ipo_date            = COALESCE(EXCLUDED.ipo_date, core.security.ipo_date),
            delisted_date       = EXCLUDED.delisted_date,
            cik                 = COALESCE(EXCLUDED.cik, core.security.cik),
            isin                = COALESCE(EXCLUDED.isin, core.security.isin),
            cusip               = COALESCE(EXCLUDED.cusip, core.security.cusip),
            updated_at          = now()
        RETURNING security_id
        """,
        (
            primary_symbol,
            company_name,
            asset_type,
            exchange_code,
            sector_id,
            industry_id,
            currency,
            country,
            is_actively_trading,
            is_etf,
            is_fund,
            ipo_date,
            delisted_date,
            cik,
            isin,
            cusip,
            source,
        ),
    )
    return int(row["security_id"])


def upsert_symbol_xref(
    db: Database,
    *,
    security_id: int,
    symbol: str,
    valid_from: date | str | None = None,
    is_primary: bool = True,
    source: str = "fmp",
) -> None:
    """Map a ticker to a security_id for a validity period.

    ``valid_from=None`` means "start after any period this ticker has already
    served". A reused ticker therefore opens a *new* period instead of hijacking
    the dead issuer's row -- which, since XREF_RESOLVE_SQL only reads open
    periods, is what keeps a delisted company's price history addressable and
    stops the new issuer from inheriting it.
    """
    db.execute(
        """
        INSERT INTO core.symbol_xref (security_id, symbol, valid_from, is_primary, source)
        VALUES (
            %s, %s,
            COALESCE(
                %s::date,
                (SELECT max(valid_to) + 1 FROM core.symbol_xref
                  WHERE symbol = %s AND valid_to IS NOT NULL),
                '1900-01-01'::date
            ),
            %s, %s
        )
        ON CONFLICT (symbol, valid_from) DO UPDATE SET
            security_id = EXCLUDED.security_id,
            is_primary  = EXCLUDED.is_primary
        WHERE core.symbol_xref.valid_to IS NULL
        """,
        (security_id, symbol, valid_from, symbol, is_primary, source),
    )


def mark_delisted(db: Database, *, security_id: int, delisted_date: date) -> bool:
    """Flip a listed security to delisted and close its open ticker period.

    One-way and idempotent: a row that already carries a ``delisted_date`` is
    left untouched, so re-running the loader can never rewrite a delisting or
    resurrect a dead issuer. Returns True only when this call did the delisting.
    """
    row = db.fetchone(
        """
        UPDATE core.security
           SET is_actively_trading = FALSE,
               delisted_date       = %s,
               updated_at          = now()
         WHERE security_id = %s AND delisted_date IS NULL
        RETURNING security_id
        """,
        (delisted_date, security_id),
    )
    if row is None:
        return False
    db.execute(
        """
        UPDATE core.symbol_xref
           SET valid_to = %s
         WHERE security_id = %s AND valid_to IS NULL AND valid_from <= %s
        """,
        (delisted_date, security_id, delisted_date),
    )
    return True


def upsert_company_profile(
    db: Database,
    *,
    security_id: int,
    description: Optional[str],
    ceo: Optional[str],
    full_time_employees: Optional[int],
    website: Optional[str],
    beta: Optional[float],
    market_cap_usd: Optional[float],
    last_dividend: Optional[float],
    price_range: Optional[str],
    image_url: Optional[str],
    source: str = "fmp",
) -> None:
    db.execute(
        """
        INSERT INTO core.company_profile
            (security_id, description, ceo, full_time_employees, website, beta,
             market_cap_usd, last_dividend, price_range, image_url, loaded_at, source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), %s)
        ON CONFLICT (security_id) DO UPDATE SET
            description = EXCLUDED.description,
            ceo = EXCLUDED.ceo,
            full_time_employees = EXCLUDED.full_time_employees,
            website = EXCLUDED.website,
            beta = EXCLUDED.beta,
            market_cap_usd = EXCLUDED.market_cap_usd,
            last_dividend = EXCLUDED.last_dividend,
            price_range = EXCLUDED.price_range,
            image_url = EXCLUDED.image_url,
            loaded_at = now()
        """,
        (
            security_id,
            description,
            ceo,
            full_time_employees,
            website,
            beta,
            market_cap_usd,
            last_dividend,
            price_range,
            image_url,
            source,
        ),
    )


# Canonical security-id resolution. The duk db datasource
# (duk/datasource/db.py::_resolve_security_id) MUST use these identical queries so
# the loader and the read path always resolve the same ticker to the same id.
XREF_RESOLVE_SQL = (
    "SELECT security_id FROM core.symbol_xref "
    "WHERE symbol = %s AND valid_to IS NULL "
    "ORDER BY is_primary DESC, valid_from DESC LIMIT 1"
)
# Deterministic fallback: prefer the given source, then lowest id. Ordering (not a
# hard source filter) so a symbol that only exists under another source still
# resolves — and resolves identically in both code paths.
PRIMARY_RESOLVE_SQL = (
    "SELECT security_id FROM core.security WHERE primary_symbol = %s "
    "ORDER BY (source = %s) DESC, (delisted_date IS NULL) DESC, security_id ASC "
    "LIMIT 1"
)


def resolve_security_id(
    db: Database, symbol: str, source: str = "fmp"
) -> Optional[int]:
    """Resolve a ticker to a security_id via the current xref, falling back to primary_symbol."""
    val = db.fetchval(XREF_RESOLVE_SQL, (symbol,))
    if val is not None:
        return int(val)
    val = db.fetchval(PRIMARY_RESOLVE_SQL, (symbol, source))
    return int(val) if val is not None else None


# ---------------------------------------------------------------------------
# Daily prices
# ---------------------------------------------------------------------------


def upsert_daily_prices(
    db: Database,
    rows: Sequence[dict],
    *,
    ingestion_run_id: Optional[int] = None,
    source: str = "fmp",
) -> int:
    """Bulk upsert raw OHLCV rows.

    Each row dict must have: security_id, trade_date, open, high, low, close,
    volume, and optionally vwap. Returns number of rows written.
    """
    if not rows:
        return 0
    params = [
        (
            r["security_id"],
            r["trade_date"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r.get("volume", 0),
            r.get("vwap"),
            source,
            ingestion_run_id,
        )
        for r in rows
    ]
    return db.executemany(
        """
        INSERT INTO core.daily_price
            (security_id, trade_date, open, high, low, close, volume, vwap,
             source, ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (security_id, trade_date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
            source = EXCLUDED.source, ingestion_run_id = EXCLUDED.ingestion_run_id,
            loaded_at = now()
        """,
        params,
    )


def max_price_date(db: Database, security_id: int) -> Optional[date]:
    return db.fetchval(
        "SELECT max(trade_date) FROM core.daily_price WHERE security_id = %s",
        (security_id,),
    )


# ---------------------------------------------------------------------------
# Corporate actions & adjustment factors
# ---------------------------------------------------------------------------


def upsert_corporate_action(
    db: Database,
    *,
    security_id: int,
    action_type: str,
    ex_date: date,
    split_numerator: Optional[float] = None,
    split_denominator: Optional[float] = None,
    dividend_amount: Optional[float] = None,
    currency: str = "USD",
    record_date: Optional[date] = None,
    payment_date: Optional[date] = None,
    declaration_date: Optional[date] = None,
    ingestion_run_id: Optional[int] = None,
    source: str = "fmp",
) -> None:
    db.execute(
        """
        INSERT INTO core.corporate_action
            (security_id, action_type, ex_date, record_date, payment_date,
             declaration_date, split_numerator, split_denominator, dividend_amount,
             currency, source, ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (security_id, action_type, ex_date) DO UPDATE SET
            record_date = EXCLUDED.record_date,
            payment_date = EXCLUDED.payment_date,
            declaration_date = EXCLUDED.declaration_date,
            split_numerator = EXCLUDED.split_numerator,
            split_denominator = EXCLUDED.split_denominator,
            dividend_amount = EXCLUDED.dividend_amount,
            currency = EXCLUDED.currency,
            loaded_at = now()
        """,
        (
            security_id,
            action_type,
            ex_date,
            record_date,
            payment_date,
            declaration_date,
            split_numerator,
            split_denominator,
            dividend_amount,
            currency,
            source,
            ingestion_run_id,
        ),
    )


def corporate_actions_for(db: Database, security_id: int) -> list[dict]:
    return db.fetchall(
        """
        SELECT action_type, ex_date, split_numerator, split_denominator, dividend_amount
        FROM core.corporate_action
        WHERE security_id = %s
        ORDER BY ex_date ASC
        """,
        (security_id,),
    )


def close_on_or_before(db: Database, security_id: int, d: date) -> Optional[float]:
    """Raw close on the latest trade_date <= d (used to value dividends for adjustment)."""
    return db.fetchval(
        """
        SELECT close FROM core.daily_price
        WHERE security_id = %s AND trade_date < %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (security_id, d),
    )


def replace_adjustment_factors(
    db: Database, security_id: int, factors: Sequence[dict]
) -> int:
    """Replace all adjustment factors for a security. factors: effective_date,
    cumulative_price_factor, cumulative_volume_factor."""
    db.execute(
        "DELETE FROM core.adjustment_factor WHERE security_id = %s", (security_id,)
    )
    if not factors:
        return 0
    params = [
        (
            security_id,
            f["effective_date"],
            f["cumulative_price_factor"],
            f["cumulative_volume_factor"],
        )
        for f in factors
    ]
    return db.executemany(
        """
        INSERT INTO core.adjustment_factor
            (security_id, effective_date, cumulative_price_factor,
             cumulative_volume_factor, computed_at)
        VALUES (%s,%s,%s,%s, now())
        """,
        params,
    )


def securities_with_actions(db: Database) -> list[int]:
    rows = db.fetchall(
        "SELECT DISTINCT security_id FROM core.corporate_action ORDER BY security_id"
    )
    return [int(r["security_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Watermarks & lineage
# ---------------------------------------------------------------------------


def get_watermark(
    db: Database, source: str, endpoint: str, security_id: int = 0
) -> Optional[date]:
    return db.fetchval(
        """
        SELECT last_loaded_date FROM ops.load_watermark
        WHERE source = %s AND endpoint = %s AND security_id = %s
        """,
        (source, endpoint, security_id),
    )


def set_watermark(
    db: Database,
    source: str,
    endpoint: str,
    last_loaded_date: date,
    security_id: int = 0,
) -> None:
    db.execute(
        """
        INSERT INTO ops.load_watermark
            (source, endpoint, security_id, last_loaded_date, last_run_at, updated_at)
        VALUES (%s,%s,%s,%s, now(), now())
        ON CONFLICT (source, endpoint, security_id) DO UPDATE SET
            last_loaded_date = GREATEST(
                ops.load_watermark.last_loaded_date, EXCLUDED.last_loaded_date),
            last_run_at = now(), updated_at = now()
        """,
        (source, endpoint, security_id, last_loaded_date),
    )


def land_payload(
    db: Database,
    *,
    endpoint: str,
    params: dict,
    symbol: Optional[str],
    http_status: Optional[int],
    payload: Any,
    payload_hash: str,
    nbytes: int,
    ingestion_run_id: Optional[int],
) -> None:
    import json

    db.execute(
        """
        INSERT INTO landing.fmp_raw
            (ingestion_run_id, endpoint, params, symbol, http_status, payload,
             payload_hash, bytes, fetched_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
        """,
        (
            ingestion_run_id,
            endpoint,
            json.dumps(params),
            symbol,
            http_status,
            json.dumps(payload),
            payload_hash,
            nbytes,
        ),
    )


def add_dq_flag(
    db: Database,
    *,
    check_name: str,
    severity: str = "warn",
    security_id: Optional[int] = None,
    table_name: Optional[str] = None,
    record_key: Optional[dict] = None,
    detail: Optional[dict] = None,
    ingestion_run_id: Optional[int] = None,
) -> None:
    import json

    db.execute(
        """
        INSERT INTO ops.data_quality_flag
            (ingestion_run_id, security_id, table_name, record_key, check_name,
             severity, detail, detected_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
        """,
        (
            ingestion_run_id,
            security_id,
            table_name,
            json.dumps(record_key) if record_key else None,
            check_name,
            severity,
            json.dumps(detail) if detail else None,
        ),
    )


def count_price_quarantines(db: Database, security_id: int, date_iso: str) -> int:
    """How many times a given trade_date has been quarantined for this security
    (price_* checks). Used to bound the watermark hold on a persistently-bad bar."""
    return int(
        db.fetchval(
            """
            SELECT count(*) FROM ops.data_quality_flag
            WHERE security_id = %s
              AND check_name LIKE 'price\\_%%'
              AND record_key->>'date' = %s
            """,
            (security_id, date_iso),
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Read API (used by duk db datasource and `fafnir status`)
# ---------------------------------------------------------------------------


def read_price_history(
    db: Database,
    symbol: str,
    start_date: Optional[date],
    end_date: Optional[date],
    adjusted: bool,
) -> list[dict]:
    """Return raw or adjusted OHLCV rows for a symbol, ascending by date."""
    security_id = resolve_security_id(db, symbol)
    if security_id is None:
        return []
    relation = "mart.v_daily_price_adjusted" if adjusted else "core.daily_price"
    clauses = ["security_id = %s"]
    params: list[Any] = [security_id]
    if start_date is not None:
        clauses.append("trade_date >= %s")
        params.append(start_date)
    if end_date is not None:
        clauses.append("trade_date <= %s")
        params.append(end_date)
    where = " AND ".join(clauses)
    return db.fetchall(
        f"""
        SELECT trade_date AS date, open, high, low, close, volume
        FROM {relation}
        WHERE {where}
        ORDER BY trade_date ASC
        """,
        params,
    )


def read_security_count(db: Database) -> dict:
    return db.fetchone("""
        SELECT
            count(*)                                   AS securities,
            count(*) FILTER (WHERE is_actively_trading) AS active,
            count(*) FILTER (WHERE delisted_date IS NOT NULL) AS delisted
        FROM core.security
        """) or {}
