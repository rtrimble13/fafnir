"""
Scheduled data-quality checks. Each check writes to ``ops.data_quality_flag``
rather than failing the load, so anomalies surface for review instead of silently
corrupting research data.

  * gaps      -- trading-calendar days with no price row (per active security)
  * outliers  -- implausible close-to-close moves not explained by a split
  * freshness -- securities whose latest price lags the market's latest date

These are deliberately set-based SQL so they scale across the universe.

Every check runs on a schedule over the same data, so each one flags a standing
condition *once* rather than once per pass: the insert skips any (security_id,
check_name, record_key) that is already sitting unresolved in the queue. Without
that, a security with 40 missing days would contribute 40 rows a night forever
and the open-DQ count `fafnir status` reports would grow without bound while the
number of real problems stayed flat. A condition with a different record_key -- a
new gap date, a later stale date -- is a different occurrence and is still
recorded. See ``repository.add_dq_flag_once``, which is the same rule for the
row-at-a-time callers.
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
    min and max loaded date. Returns the number of new flags written -- a gap
    already open in the queue is not flagged again.
    """
    limit_clause = f"LIMIT {int(limit_securities)}" if limit_securities else ""
    row = db.fetchone(
        f"""
        WITH bounds AS (
            SELECT security_id, min(trade_date) AS dmin, max(trade_date) AS dmax
            FROM core.daily_price GROUP BY security_id {limit_clause}
        ),
        detected AS (
            SELECT b.security_id,
                   jsonb_build_object('trade_date', c.trade_date::text) AS record_key
            FROM bounds b
            JOIN ref.trading_calendar c
              ON c.exchange_code = %s AND c.is_open
             AND c.trade_date BETWEEN b.dmin AND b.dmax
            LEFT JOIN core.daily_price p
              ON p.security_id = b.security_id AND p.trade_date = c.trade_date
            WHERE p.security_id IS NULL
        ),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT d.security_id, 'core.daily_price', d.record_key, 'gap', 'warn',
                   jsonb_build_object('exchange', %s::text), now()
            FROM detected d
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'gap'
                  AND f.security_id = d.security_id
                  AND f.record_key = d.record_key
                  AND f.resolved_at IS NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """,
        (exchange_code, exchange_code),
    )
    logger.info(
        "gap check: %d missing days, %d newly flagged", row["detected"], row["flagged"]
    )
    return int(row["flagged"])


def check_outliers(db: Database, threshold: float = DEFAULT_OUTLIER_THRESHOLD) -> int:
    """Flag close-to-close moves exceeding ``threshold`` not explained by a split.

    Returns the number of new flags written; a move already open in the queue is
    not flagged again on the next pass over the same bars.
    """
    row = db.fetchone(
        """
        WITH moves AS (
            SELECT security_id, trade_date, close,
                   lag(close) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_close
            FROM core.daily_price
        ),
        detected AS (
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
        ),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT d.security_id, 'core.daily_price',
                   jsonb_build_object('trade_date', d.trade_date::text),
                   'outlier', 'warn',
                   jsonb_build_object('move', d.move::float8,
                                      'close', d.close::float8,
                                      'prev_close', d.prev_close::float8),
                   now()
            FROM detected d
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'outlier'
                  AND f.security_id = d.security_id
                  AND f.record_key = jsonb_build_object('trade_date', d.trade_date::text)
                  AND f.resolved_at IS NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """,
        (threshold,),
    )
    logger.info(
        "outlier check: %d moves over threshold (%.0f%%), %d newly flagged",
        row["detected"],
        threshold * 100,
        row["flagged"],
    )
    return int(row["flagged"])


def check_freshness(db: Database, exchange_code: str = "NASDAQ") -> int:
    """Flag actively-trading securities whose latest price lags the market latest.

    Keyed on the security's own last loaded date, so a security that stays stale
    is flagged once; one that takes a bar and then falls behind again is a new
    occurrence and is flagged again.
    """
    row = db.fetchone(
        """
        WITH market_latest AS (
            SELECT max(trade_date) AS d FROM core.daily_price
        ),
        detected AS (
            SELECT s.security_id, max(p.trade_date) AS last_date, ml.d AS market_date
            FROM core.security s
            JOIN core.daily_price p ON p.security_id = s.security_id
            CROSS JOIN market_latest ml
            WHERE s.is_actively_trading
            GROUP BY s.security_id, ml.d
            HAVING max(p.trade_date) < ml.d
        ),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT d.security_id, 'core.daily_price',
                   jsonb_build_object('last_date', d.last_date::text),
                   'stale', 'warn',
                   jsonb_build_object('market_date', d.market_date::text),
                   now()
            FROM detected d
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'stale'
                  AND f.security_id = d.security_id
                  AND f.record_key = jsonb_build_object('last_date', d.last_date::text)
                  AND f.resolved_at IS NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """,
    )
    logger.info(
        "freshness check: %d stale securities, %d newly flagged",
        row["detected"],
        row["flagged"],
    )
    return int(row["flagged"])


def run_all(
    db: Database,
    exchange_code: str = "NASDAQ",
    outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
) -> dict:
    """Run every check and return a summary dict of NEW flag counts.

    A run over data whose problems are all already in the queue reports zeros --
    that is the check working, not the check finding nothing. The standing totals
    are in ops.data_quality_flag (and `fafnir status`); the per-check detected
    counts are logged.
    """
    return {
        "gaps": check_gaps(db, exchange_code),
        "outliers": check_outliers(db, outlier_threshold),
        "stale": check_freshness(db, exchange_code),
    }
