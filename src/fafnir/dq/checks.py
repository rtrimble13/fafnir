"""
Scheduled data-quality checks. Each check writes to ``ops.data_quality_flag``
rather than failing the load, so anomalies surface for review instead of silently
corrupting research data.

  * gaps      -- trading-calendar days with no price row (per active security),
                for securities that trade densely enough for a missing day to mean
                something; the rest get one `sparse_coverage` flag instead
  * outliers  -- implausible close-to-close moves not explained by a split
  * freshness -- securities whose latest price lags the market's latest date
  * identity  -- one ticker carrying more than one security row
  * profile   -- actively-traded securities with no sector/industry

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

A condition that has been ACCEPTED (0024) is skipped as well, and that is a
different statement from a resolution. Resolving is judged against the data, so it
frees the slot and a condition still present is written again on the next pass --
which is what makes the queue trustworthy. Acceptance says the condition is real,
permanent and has no repair: a vendor that has no bars for a security's first
decade will not produce them tomorrow. Each guard below therefore carries two
NOT EXISTS probes rather than one widened predicate, so both stay matched to a
partial index (ix_dq_flag_open_condition, ix_dq_flag_accepted_condition).
"""

from __future__ import annotations

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("dq")

DEFAULT_OUTLIER_THRESHOLD = 0.5  # 50% close-to-close move flags for review

# The share of its own calendar sessions a security must actually have a bar for
# before a missing session is evidence of anything.
#
# `gap` asks the exchange calendar what a session was and flags every one without a
# bar. That is the right question for a security that trades daily and the wrong one
# for a security that does not: MAIR's median daily volume is one share, ACOM holds
# bars for 26% of its sessions since 1998, and for names like these an absent bar is
# a fact about liquidity, not about the load. Flagging per session turned ~2,900 such
# securities into 599,808 rows and buried the ~29 that look genuinely broken.
#
# Below this density the security gets ONE `sparse_coverage` flag carrying the
# numbers, and no per-session gap flags. Above it, nothing changes. 0.80 is chosen
# to sit clear of both populations rather than between them: of the 200 securities
# with the most gap flags, 171 fall below it and 29 above, and the ones above are
# the ones whose missing days look like real holes. It is deliberately not a tuning
# knob for queue size -- moving it down hides broken securities, and moving it up
# starts flagging thin ones per session again.
GAP_MIN_SESSION_DENSITY = 0.80

# Sessions a security's window must span before its density means anything.
#
# Density over a handful of sessions is noise: a security with two bars and one
# missing day scores 0.67 and would be called sparse on the strength of a single
# absence. Below this many sessions the security is treated as dense and flagged
# per session as before -- the check declines to guess rather than guessing wrong.
# On this warehouse the guard costs almost nothing: of the 642 securities under the
# density threshold, 42 span fewer than 60 sessions and they hold 584 gap flags
# between them.
GAP_MIN_SESSIONS_FOR_DENSITY = 60

# Asset types whose price is published after the equity close, and how many trading
# days behind the market they are allowed to sit before that counts as stale.
#
# An open-end fund strikes NAV at 4pm ET and the vendor posts it that evening, so a
# nightly run timed for equities routinely finds a fund one day behind. Without this
# allowance check_freshness flags every fund every night on a record_key (its own
# last_date) that changes daily -- so add_dq_flag_once cannot dedupe it, and the
# queue grows by one row per fund per night forever. That unbounded growth is the
# exact failure the once-per-occurrence rule exists to prevent, reintroduced by a
# security whose price is simply published later. Delaying the whole nightly job for
# a handful of symbols would be the more expensive fix.
NAV_LAGGING_ASSET_TYPES = ("fund",)
NAV_LAG_TRADING_DAYS = 1


def check_gaps(
    db: Database,
    exchange_code: str = "NASDAQ",
    limit_securities: int = 0,
    min_density: float = GAP_MIN_SESSION_DENSITY,
    min_sessions: int = GAP_MIN_SESSIONS_FOR_DENSITY,
) -> int:
    """Flag trading days (per the calendar) missing from core.daily_price.

    Only checks securities that have at least one price row, between their own
    min and max loaded date, and only those holding a bar for at least
    ``min_density`` of the sessions in that window -- see
    :data:`GAP_MIN_SESSION_DENSITY`. A security below that line gets a single
    ``sparse_coverage`` flag instead of one flag per session it did not trade.
    A window shorter than ``min_sessions`` is too short for density to mean
    anything, so those securities stay on the per-session path.

    Returns the number of new flags written, of both kinds -- a condition already
    open in the queue is not flagged again.
    """
    limit_clause = f"LIMIT {int(limit_securities)}" if limit_securities else ""
    row = db.fetchone(
        f"""
        WITH bounds AS (
            SELECT security_id, min(trade_date) AS dmin, max(trade_date) AS dmax,
                   count(*) AS bars
            FROM core.daily_price GROUP BY security_id {limit_clause}
        ),
        coverage AS (
            SELECT b.security_id, b.dmin, b.dmax, b.bars,
                   count(c.trade_date) AS sessions
            FROM bounds b
            JOIN ref.trading_calendar c
              ON c.exchange_code = %s AND c.is_open
             AND c.trade_date BETWEEN b.dmin AND b.dmax
            GROUP BY 1, 2, 3, 4
        ),
        classified AS (
            SELECT *, bars::numeric / nullif(sessions, 0) AS density FROM coverage
        ),
        dense AS (
            SELECT * FROM classified
             WHERE sessions < %s OR density >= %s
        ),
        sparse AS (
            SELECT * FROM classified
             WHERE sessions >= %s AND density < %s
        ),
        detected AS (
            SELECT d.security_id,
                   jsonb_build_object('trade_date', c.trade_date::text) AS record_key
            FROM dense d
            JOIN ref.trading_calendar c
              ON c.exchange_code = %s AND c.is_open
             AND c.trade_date BETWEEN d.dmin AND d.dmax
            LEFT JOIN core.daily_price p
              ON p.security_id = d.security_id AND p.trade_date = c.trade_date
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
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'gap'
                  AND f.security_id = d.security_id
                  AND f.record_key = d.record_key
                  AND f.accepted_at IS NOT NULL
            )
            RETURNING 1
        ),
        -- One row per security, keyed on an empty record_key so the condition
        -- dedupes for the life of the security. The window moves every night as
        -- new bars land; keying on it would defeat add_dq_flag_once and grow the
        -- queue by one row per sparse security per night, which is the failure
        -- NAV_LAG_TRADING_DAYS exists to prevent elsewhere. The moving numbers
        -- live in `detail`, which does not participate in the dedupe.
        written_sparse AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT s.security_id, 'core.daily_price', '{{}}'::jsonb,
                   'sparse_coverage', 'info',
                   jsonb_build_object('bars', s.bars,
                                      'sessions', s.sessions,
                                      'density', round(s.density, 4)::float8,
                                      'from', s.dmin::text,
                                      'to', s.dmax::text,
                                      'exchange', %s::text),
                   now()
            FROM sparse s
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'sparse_coverage'
                  AND f.security_id = s.security_id
                  AND f.resolved_at IS NULL
            )
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'sparse_coverage'
                  AND f.security_id = s.security_id
                  AND f.accepted_at IS NOT NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected)       AS detected,
               (SELECT count(*) FROM written)        AS flagged,
               (SELECT count(*) FROM sparse)         AS sparse_securities,
               (SELECT count(*) FROM written_sparse) AS sparse_flagged
        """,
        (
            exchange_code,
            min_sessions,
            min_density,
            min_sessions,
            min_density,
            exchange_code,
            exchange_code,
            exchange_code,
        ),
    )
    logger.info(
        "gap check: %d missing days, %d newly flagged; "
        "%d securities below %.0f%% session density, %d newly flagged sparse",
        row["detected"],
        row["flagged"],
        row["sparse_securities"],
        min_density * 100,
        row["sparse_flagged"],
    )
    return int(row["flagged"]) + int(row["sparse_flagged"])


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
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'outlier'
                  AND f.security_id = d.security_id
                  AND f.record_key = jsonb_build_object('trade_date', d.trade_date::text)
                  AND f.accepted_at IS NOT NULL
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

    "The market's latest date" is the newest date carrying a bar *that is an open
    session on the calendar*. Some securities -- money-market funds especially --
    publish a price on days the market is shut, and one such bar is enough to move
    an unrestricted max(trade_date) onto a weekend and make every other security
    in the universe look a session behind.

    Securities priced at a NAV struck after the equity close get
    :data:`NAV_LAG_TRADING_DAYS` of slack, measured in trading days off the
    calendar rather than calendar days -- a Monday-morning run must not treat the
    weekend as three days of lateness. See :data:`NAV_LAGGING_ASSET_TYPES`.
    """
    row = db.fetchone(
        """
        WITH market_latest AS (
            -- Restricted to open sessions on the venue calendar. A money-market
            -- fund strikes a NAV seven days a week, so an unrestricted
            -- max(trade_date) lands on a Saturday whenever one of them is loaded
            -- -- and then every security whose last bar is Friday's is "behind
            -- the market" and the whole universe is flagged stale at once.
            SELECT max(p.trade_date) AS d
              FROM core.daily_price p
              JOIN ref.trading_calendar c
                ON c.trade_date = p.trade_date
               AND c.exchange_code = %s AND c.is_open
        ),
        allowance AS (
            -- The oldest last_date a NAV-priced security may carry and still be
            -- considered current: NAV_LAG_TRADING_DAYS open sessions back from the
            -- market's latest date.
            SELECT COALESCE(
                (SELECT min(c.trade_date)
                   FROM (SELECT trade_date
                           FROM ref.trading_calendar
                          WHERE exchange_code = %s AND is_open
                            AND trade_date <= (SELECT d FROM market_latest)
                          ORDER BY trade_date DESC
                          LIMIT %s) c),
                (SELECT d FROM market_latest)
            ) AS d
        ),
        detected AS (
            SELECT s.security_id, max(p.trade_date) AS last_date, ml.d AS market_date
            FROM core.security s
            JOIN core.daily_price p ON p.security_id = s.security_id
            CROSS JOIN market_latest ml
            CROSS JOIN allowance al
            WHERE s.is_actively_trading
            GROUP BY s.security_id, s.asset_type, ml.d, al.d
            HAVING max(p.trade_date) < CASE
                       WHEN s.asset_type = ANY(%s::text[]) THEN al.d
                       ELSE ml.d
                   END
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
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'stale'
                  AND f.security_id = d.security_id
                  AND f.record_key = jsonb_build_object('last_date', d.last_date::text)
                  AND f.accepted_at IS NOT NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """,
        (
            exchange_code,
            exchange_code,
            NAV_LAG_TRADING_DAYS + 1,
            list(NAV_LAGGING_ASSET_TYPES),
        ),
    )
    logger.info(
        "freshness check: %d stale securities, %d newly flagged",
        row["detected"],
        row["flagged"],
    )
    return int(row["flagged"])


def check_duplicate_identity(db: Database) -> int:
    """Flag tickers carried by more than one row in ``core.security``.

    A ticker names one issuer at a time, so two rows under one symbol means the
    company's identity has forked: its bars sit on one security_id while the ticker
    resolves to another. The read path cannot detect this -- it asks for one
    security and gets one -- so nothing downstream ever complains, which is how
    4,321 forked tickers accumulated over three weeks without a single failed run.

    Keyed on the symbol rather than on a security_id, and flagged against the row
    holding the *oldest* identity, so re-forking the same ticker tomorrow does not
    open a second flag for the same condition. `detail` carries the competing ids
    and how many of them have no bars, which is what says whether this is a repair
    (shells to delete) or a genuine reuse to leave alone.

    Repair, then resolve: deleting the shells and re-pointing ``core.symbol_xref``
    is what closes this, not the resolve.

    Shaped to match ``var/fafnir-fixes-2026-09-06/survey-duplicate-securities.sql``,
    which measured this against production: a correlated EXISTS per security row
    against ``core.daily_price`` (partitioned on trade_date, ~150M rows, so a probe
    by security_id alone cannot prune and hits every partition) exceeded the read
    role's statement_timeout. One pass building the distinct set, joined, is the
    form that finishes -- and the forked groups are resolved first so the bar
    lookup covers ~18k rows rather than the whole master.
    """
    row = db.fetchone("""
        WITH groups AS (
            SELECT primary_symbol
              FROM core.security
             GROUP BY primary_symbol
            HAVING count(*) > 1
        ),
        withbars AS (
            SELECT DISTINCT security_id FROM core.daily_price
        ),
        forked AS (
            SELECT s.primary_symbol,
                   min(s.security_id) AS anchor_id,
                   count(*)           AS row_count,
                   count(*) FILTER (WHERE b.security_id IS NULL)
                       AS rows_without_bars,
                   count(DISTINCT s.company_name) AS distinct_names
              FROM core.security s
              JOIN groups g USING (primary_symbol)
              LEFT JOIN withbars b ON b.security_id = s.security_id
             GROUP BY s.primary_symbol
        ),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT f.anchor_id, 'core.security',
                   jsonb_build_object('symbol', f.primary_symbol),
                   'security_duplicate_identity', 'warn',
                   jsonb_build_object(
                       'row_count', f.row_count,
                       'rows_without_bars', f.rows_without_bars,
                       'distinct_company_names', f.distinct_names
                   ),
                   now()
            FROM forked f
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag d
                WHERE d.check_name = 'security_duplicate_identity'
                  AND d.record_key = jsonb_build_object('symbol', f.primary_symbol)
                  AND d.resolved_at IS NULL
            )
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag d
                WHERE d.check_name = 'security_duplicate_identity'
                  AND d.record_key = jsonb_build_object('symbol', f.primary_symbol)
                  AND d.accepted_at IS NOT NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM forked)  AS detected,
               (SELECT count(*) FROM written) AS flagged
        """)
    logger.info(
        "identity check: %d forked tickers, %d newly flagged",
        row["detected"],
        row["flagged"],
    )
    return int(row["flagged"])


def check_missing_classification(db: Database) -> int:
    """Flag actively-traded securities with no sector or industry.

    Cheap, and it is the check whose absence let an eight-day outage read as a
    feature request. The classification is a screener field on every universe
    load, so a listed security without one means either the vendor omitted it or
    something in the write path dropped it -- and the second case is silent in
    every other signal the warehouse produces.

    Actively-traded only. Delisted rows legitimately predate the classification
    and cannot be refreshed: the universe load no longer sees them, and enriching
    them through `upsert_security` would insert rather than update.
    """
    row = db.fetchone("""
        WITH detected AS (
            SELECT security_id, primary_symbol
              FROM core.security
             WHERE is_actively_trading
               AND delisted_date IS NULL
               AND (sector_id IS NULL OR industry_id IS NULL)
        ),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT d.security_id, 'core.security',
                   jsonb_build_object('symbol', d.primary_symbol),
                   'security_missing_classification', 'info',
                   '{}'::jsonb,
                   now()
            FROM detected d
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'security_missing_classification'
                  AND f.security_id = d.security_id
                  AND f.resolved_at IS NULL
            )
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag f
                WHERE f.check_name = 'security_missing_classification'
                  AND f.security_id = d.security_id
                  AND f.accepted_at IS NOT NULL
            )
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """)
    logger.info(
        "classification check: %d unclassified securities, %d newly flagged",
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
        "duplicate_identity": check_duplicate_identity(db),
        "missing_classification": check_missing_classification(db),
    }
