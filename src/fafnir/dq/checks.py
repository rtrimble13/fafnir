"""
Scheduled data-quality checks. Each check writes to ``ops.data_quality_flag``
rather than failing the load, so anomalies surface for review instead of silently
corrupting research data.

  * gaps      -- trading-calendar days with no price row (per active security)
  * outliers  -- implausible close-to-close moves not explained by a split
  * freshness -- securities whose latest price lags the market's latest date

These are deliberately set-based SQL so they scale across the universe.
"""

from __future__ import annotations

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("dq")

DEFAULT_OUTLIER_THRESHOLD = 0.5  # 50% close-to-close move flags for review


def check_gaps(
    db: Database, exchange_code: str = "NASDAQ", limit_securities: int = 0
) -> int:
    """Flag trading days (per the calendar) missing from core.daily_price.

    Only checks securities that have at least one price row, between their own
    min and max loaded date. Returns the number of new flags written.
    """
    limit_clause = f"LIMIT {int(limit_securities)}" if limit_securities else ""
    rows = db.fetchall(
        f"""
        WITH bounds AS (
            SELECT security_id, min(trade_date) AS dmin, max(trade_date) AS dmax
            FROM core.daily_price GROUP BY security_id {limit_clause}
        )
        SELECT b.security_id, c.trade_date
        FROM bounds b
        JOIN ref.trading_calendar c
          ON c.exchange_code = %s AND c.is_open
         AND c.trade_date BETWEEN b.dmin AND b.dmax
        LEFT JOIN core.daily_price p
          ON p.security_id = b.security_id AND p.trade_date = c.trade_date
        WHERE p.security_id IS NULL
        """,
        (exchange_code,),
    )
    for r in rows:
        db.execute(
            """
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity, detail, detected_at)
            VALUES (%s, 'core.daily_price', %s, 'gap', 'warn', %s, now())
            """,
            (
                r["security_id"],
                _json({"trade_date": str(r["trade_date"])}),
                _json({"exchange": exchange_code}),
            ),
        )
    logger.info("gap check: %d missing-day flags", len(rows))
    return len(rows)


def check_outliers(db: Database, threshold: float = DEFAULT_OUTLIER_THRESHOLD) -> int:
    """Flag close-to-close moves exceeding ``threshold`` not explained by a split."""
    rows = db.fetchall(
        """
        WITH moves AS (
            SELECT security_id, trade_date, close,
                   lag(close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_close
            FROM core.daily_price
        )
        SELECT m.security_id, m.trade_date, m.close, m.prev_close,
               abs(m.close - m.prev_close) / m.prev_close AS move
        FROM moves m
        WHERE m.prev_close IS NOT NULL AND m.prev_close > 0
          AND abs(m.close - m.prev_close) / m.prev_close > %s
          AND NOT EXISTS (
                SELECT 1 FROM core.corporate_action ca
                WHERE ca.security_id = m.security_id
                  AND ca.action_type = 'split'
                  AND ca.ex_date = m.trade_date)
        """,
        (threshold,),
    )
    for r in rows:
        db.execute(
            """
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity, detail, detected_at)
            VALUES (%s, 'core.daily_price', %s, 'outlier', 'warn', %s, now())
            """,
            (
                r["security_id"],
                _json({"trade_date": str(r["trade_date"])}),
                _json(
                    {
                        "move": float(r["move"]),
                        "close": float(r["close"]),
                        "prev_close": float(r["prev_close"]),
                    }
                ),
            ),
        )
    logger.info(
        "outlier check: %d flags (threshold=%.0f%%)", len(rows), threshold * 100
    )
    return len(rows)


def check_freshness(db: Database, exchange_code: str = "NASDAQ") -> int:
    """Flag actively-trading securities whose latest price lags the market latest."""
    rows = db.fetchall(
        """
        WITH market_latest AS (
            SELECT max(trade_date) AS d FROM core.daily_price
        )
        SELECT s.security_id, max(p.trade_date) AS last_date, ml.d AS market_date
        FROM core.security s
        JOIN core.daily_price p ON p.security_id = s.security_id
        CROSS JOIN market_latest ml
        WHERE s.is_actively_trading
        GROUP BY s.security_id, ml.d
        HAVING max(p.trade_date) < ml.d
        """,
    )
    for r in rows:
        db.execute(
            """
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity, detail, detected_at)
            VALUES (%s, 'core.daily_price', %s, 'stale', 'warn', %s, now())
            """,
            (
                r["security_id"],
                _json({"last_date": str(r["last_date"])}),
                _json({"market_date": str(r["market_date"])}),
            ),
        )
    logger.info("freshness check: %d stale securities", len(rows))
    return len(rows)


def run_all(
    db: Database,
    exchange_code: str = "NASDAQ",
    outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
) -> dict:
    """Run every check and return a summary dict of counts."""
    return {
        "gaps": check_gaps(db, exchange_code),
        "outliers": check_outliers(db, outlier_threshold),
        "stale": check_freshness(db, exchange_code),
    }


def _json(obj) -> str:
    import json

    return json.dumps(obj, default=str)
