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
  * fundamentals -- the SF1 statements (#102): identities and ties, gaps and
                staleness, coverage, impossible or suspicious values, the TTM's
                quarterly series and the knowledge-interval chains. The loader
                writes the rest (restatements, corrections, estimated dates).

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
#
# The allowance is also granted to any security whose master row carries is_fund,
# unless its asset_type trades in sessions (SESSION_TRADED_ASSET_TYPES, below). The
# screener stores funds as 'equity' -- all 5,097 on this warehouse -- so an allowance
# keyed on asset_type alone never fired for any of them.
NAV_LAGGING_ASSET_TYPES = ("fund",)
NAV_LAG_TRADING_DAYS = 1

# Open sessions a security must be missing -- counting the market's latest -- before
# check_freshness calls it stale. NAV-priced securities get NAV_LAG_TRADING_DAYS on
# top of this.
#
# One was too few. FMP posts the bar for a thin warrant, unit or ETF a night late
# often enough that a one-session threshold turned the vendor's publishing lag into a
# queue: about 300 stale flags a night, 128 of the 173 still stuck on 2026-09-10 had
# missed exactly one session, and the 2026-09-09 sweep closed 15,391 that had simply
# taken the later bar. A queue that refills with the vendor's lag every night hides
# the few securities the loader has genuinely stopped reaching. Two is the smallest
# threshold the lag cannot meet on its own, and a security the nightly has really
# lost is two sessions behind one night later.
STALE_MIN_SESSIONS_BEHIND = 2


def stale_sessions_required(nav_priced: bool) -> int:
    """Open sessions a security must be missing before it counts as stale.

    Shared by :func:`check_freshness` and ``dq recheck`` through
    :func:`freshness_cutoff_params`, so the check and its negation cannot disagree
    about what "stale" means.
    """
    return STALE_MIN_SESSIONS_BEHIND + (NAV_LAG_TRADING_DAYS if nav_priced else 0)


# The reference dates check_freshness judges against, as CTEs to follow a WITH.
# Shared verbatim with ``dq recheck`` for the same reason as the constants above.
#
# ``cutoff`` holds, per class, the oldest last_date a security may carry and still be
# current: the Nth open session back from the market's latest date, N being
# :func:`stale_sessions_required`. Measured in sessions off the calendar rather than
# calendar days -- a Monday-morning run must not treat the weekend as lateness.
# Binds :func:`freshness_cutoff_params`.
FRESHNESS_CUTOFF_CTES = """
market_latest AS (
    -- Restricted to open sessions on the venue calendar. A money-market fund
    -- strikes a NAV seven days a week, so an unrestricted max(trade_date) lands on
    -- a Saturday whenever one of them is loaded -- and then every security whose
    -- last bar is Friday's is "behind the market" and the whole universe is
    -- flagged stale at once.
    SELECT max(p.trade_date) AS d
      FROM core.daily_price p
      JOIN ref.trading_calendar c
        ON c.trade_date = p.trade_date
       AND c.exchange_code = %s AND c.is_open
),
cutoff AS (
    SELECT COALESCE(
               (SELECT min(c.trade_date)
                  FROM (SELECT trade_date
                          FROM ref.trading_calendar
                         WHERE exchange_code = %s AND is_open
                           AND trade_date <= (SELECT d FROM market_latest)
                         ORDER BY trade_date DESC
                         LIMIT %s) c),
               (SELECT d FROM market_latest)
           ) AS listed_d,
           COALESCE(
               (SELECT min(c.trade_date)
                  FROM (SELECT trade_date
                          FROM ref.trading_calendar
                         WHERE exchange_code = %s AND is_open
                           AND trade_date <= (SELECT d FROM market_latest)
                         ORDER BY trade_date DESC
                         LIMIT %s) c),
               (SELECT d FROM market_latest)
           ) AS nav_d
)
"""

# Asset types that trade in exchange sessions even when the master also marks the
# row is_fund: an ETF prints a session close, not a NAV struck after it. Kept equal
# to fafnir.ingest.daily_price.SESSION_TRADED_ASSET_TYPES -- a test pins the two --
# so the loader and this check agree on which securities are NAV-priced.
SESSION_TRADED_ASSET_TYPES = ("etf",)

# Whether the security aliased ``s`` is priced at a NAV: a NAV asset type, or is_fund
# on anything that does not trade in sessions -- the loader's _is_nav_priced rule.
# COALESCE keeps a fund with no asset_type NAV-priced here as it is there. Binds the
# NAV asset types; the session-traded types are literals from the constant above.
NAV_PRICED_PREDICATE = (
    "(s.asset_type = ANY(%s::text[]) OR (s.is_fund AND COALESCE(s.asset_type, '') "
    "NOT IN (" + ", ".join(f"'{t}'" for t in SESSION_TRADED_ASSET_TYPES) + ")))"
)


def freshness_cutoff_params(exchange_code: str) -> tuple:
    """The values :data:`FRESHNESS_CUTOFF_CTES` binds, in placeholder order."""
    return (
        exchange_code,
        exchange_code,
        stale_sessions_required(False),
        exchange_code,
        stale_sessions_required(True),
    )


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

    A security is stale once it is missing :data:`STALE_MIN_SESSIONS_BEHIND` open
    sessions, counting the market's latest. Securities priced at a NAV struck after
    the equity close -- :data:`NAV_LAGGING_ASSET_TYPES`, or ``is_fund`` -- get
    :data:`NAV_LAG_TRADING_DAYS` more. The reference dates are
    :data:`FRESHNESS_CUTOFF_CTES`, which ``dq recheck`` negates.
    """
    row = db.fetchone(
        f"""
        WITH {FRESHNESS_CUTOFF_CTES},
        detected AS (
            SELECT s.security_id, max(p.trade_date) AS last_date, ml.d AS market_date
            FROM core.security s
            JOIN core.daily_price p ON p.security_id = s.security_id
            CROSS JOIN market_latest ml
            CROSS JOIN cutoff co
            WHERE s.is_actively_trading
            GROUP BY s.security_id, s.asset_type, s.is_fund,
                     ml.d, co.listed_d, co.nav_d
            HAVING max(p.trade_date) < CASE
                       WHEN {NAV_PRICED_PREDICATE} THEN co.nav_d
                       ELSE co.listed_d
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
        (*freshness_cutoff_params(exchange_code), list(NAV_LAGGING_ASSET_TYPES)),
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
             WHERE source = 'fmp'
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
             WHERE s.source = 'fmp'
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

    The classification is the one readers see (#109): the feeding vendor's, from
    ``core.v_security_classification``. A security Sharadar feeds is flagged when
    neither Sharadar's names nor core.security's give it a sector and industry.
    """
    row = db.fetchone("""
        WITH detected AS (
            SELECT s.security_id, s.primary_symbol
              FROM core.security s
              JOIN core.v_security_classification c ON c.security_id = s.security_id
             WHERE s.is_actively_trading
               AND s.delisted_date IS NULL
               AND (c.sector_id IS NULL OR c.industry_id IS NULL)
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


# ---------------------------------------------------------------------------
# Fundamentals (#102; plan doc/plans/fundamentals-sf1.md §6.5)
# ---------------------------------------------------------------------------
#
# Each check is a SELECT of the conditions it finds -- security_id, table_name,
# record_key, severity, detail -- written through _write_flags, which applies the
# same once-per-condition rule as every check above: no second row while one is
# open, none at all once one is accepted. A statement condition's record_key names
# the period, and the version where the check reads every version, so a restated
# version that still fails is a new condition and one that passes leaves the old
# flag to be resolved.
#
# "Current" is the load timeline's (superseded_at IS NULL): every knowledge version
# fafnir holds. "In force" is today's version of each period, the one a read as of
# today sees (ADR 0018).

STATEMENT_TABLES = (
    "core.income_statement",
    "core.balance_sheet",
    "core.cash_flow_statement",
)
CURRENT = "superseded_at IS NULL"
IN_FORCE = "superseded_at IS NULL AND known_to IS NULL"
#: A balance sheet balances within the larger of 0.5% of total assets and 1M of the
#: reporting currency (the build prompt's §6.4).
IDENTITY_SHARE, TIE_FLOOR = 0.005, 1_000_000
#: Cash: the period's change against the cash flow's, within 2% of the balance
#: (restricted cash, which SF1's balance leaves out, makes small gaps).
CASH_TIE_SHARE = 0.02
#: Consecutive periods: a quarter 8 to 18 weeks after the last (TTM contiguity,
#: 0036), a year 300 to 430 days.
QUARTER_GAP = (56, 126)
YEAR_GAP = (300, 430)
#: No period end in this many days while the security traded is a gap.
GAP_DAYS = 400
#: The panel's survivorship floor (decision 4): a leading gap counts from here.
COVERAGE_FLOOR = "1998-01-01"
#: An actively trading security whose latest period ended this long ago is stale.
FUNDAMENTALS_STALE_DAYS = 200
#: A filing this soon after its period end is implausible.
FILING_LAG_MIN_DAYS = 5
#: Four discrete quarters sum to the year; this many years' worth is a quarter
#: reported year to date.
YTD_RATIO = 1.5
#: Items that are never absent for a reporting company, by table: a zero between
#: two non-zero neighbours is suspect (probe item 6).
NEVER_ZERO = {
    "core.income_statement": ("revenue", "net_income", "weighted_average_shares"),
    "core.balance_sheet": (
        "total_assets",
        "total_liabilities",
        "total_equity_parent",
        "cash_and_equivalents",
        "shares_outstanding",
    ),
    "core.cash_flow_statement": ("net_cash_operating",),
}
#: Items that cannot be negative, by table.
NEVER_NEGATIVE = {
    "core.income_statement": (
        "revenue",
        "weighted_average_shares",
        "weighted_average_shares_diluted",
    ),
    "core.balance_sheet": ("total_assets", "shares_outstanding"),
}

_VERSION_KEY = (
    "jsonb_build_object('period_type', t.period_type, "
    "'fiscal_date', t.fiscal_date::text, 'version_no', t.version_no)"
)
_PERIOD_KEY = (
    "jsonb_build_object('period_type', t.period_type, "
    "'fiscal_date', t.fiscal_date::text)"
)
#: A check that reads more than one table names the statement in its record_key:
#: the open-condition index is (check_name, security_id, record_key), so one period
#: failing in two tables would otherwise be one condition.
_STATEMENT = {
    "core.income_statement": "income",
    "core.balance_sheet": "balance_sheet",
    "core.cash_flow_statement": "cash_flow",
}


def _keyed(key: str, table: str) -> str:
    """``key`` (a jsonb_build_object) with the table's statement added."""
    return key.replace(
        "jsonb_build_object(",
        f"jsonb_build_object('statement', '{_STATEMENT[table]}', ",
        1,
    )


def statements_exist(db: Database) -> bool:
    """Whether the statement tables are there: a database mid-migration has none."""
    return bool(
        db.fetchval(
            "SELECT bool_and(to_regclass(t) IS NOT NULL) FROM unnest(%s::text[]) t",
            (list(STATEMENT_TABLES),),
        )
    )


def _write_flags(
    db: Database, check: str, detected: str, params: tuple = (), *, what: str
) -> int:
    """Insert the conditions ``detected`` selects as ``check`` flags, once each.

    ``detected`` selects ``security_id, table_name, record_key, severity, detail``.
    Returns the number of new flags; the detected count is logged.
    """
    row = db.fetchone(
        f"""
        WITH detected AS ({detected}),
        written AS (
            INSERT INTO ops.data_quality_flag
                (security_id, table_name, record_key, check_name, severity,
                 detail, detected_at)
            SELECT d.security_id, d.table_name, d.record_key, %s, d.severity,
                   d.detail, now()
              FROM detected d
             WHERE NOT EXISTS (
                    SELECT 1 FROM ops.data_quality_flag f
                     WHERE f.check_name = %s
                       AND f.security_id IS NOT DISTINCT FROM d.security_id
                       AND f.record_key = d.record_key
                       AND f.resolved_at IS NULL)
               AND NOT EXISTS (
                    SELECT 1 FROM ops.data_quality_flag f
                     WHERE f.check_name = %s
                       AND f.security_id IS NOT DISTINCT FROM d.security_id
                       AND f.record_key = d.record_key
                       AND f.accepted_at IS NOT NULL)
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM detected) AS detected,
               (SELECT count(*) FROM written)  AS flagged
        """,
        (*params, check, check, check),
    )
    logger.info(
        "%s check: %d %s, %d newly flagged",
        check,
        row["detected"],
        what,
        row["flagged"],
    )
    return int(row["flagged"])


def check_balance_sheet_identity(db: Database) -> int:
    """Assets against liabilities and parent equity, per current version.

    SF1 carries no balance for noncontrolling interest, so where the period's income
    statement shows NCI income the gap is that interest: ``info`` there, ``error``
    elsewhere (plan §6.5).
    """
    return _write_flags(
        db,
        "balance_sheet_identity",
        f"""
        SELECT t.security_id, 'core.balance_sheet' AS table_name,
               {_VERSION_KEY} AS record_key,
               CASE WHEN nci.has THEN 'info' ELSE 'error' END AS severity,
               jsonb_build_object(
                   'total_assets', t.total_assets::float8,
                   'total_liabilities', t.total_liabilities::float8,
                   'total_equity_parent', t.total_equity_parent::float8,
                   'gap', x.gap::float8, 'tolerance', x.tol::float8,
                   'noncontrolling_income', nci.has) AS detail
          FROM core.balance_sheet t
          CROSS JOIN LATERAL (
                SELECT t.total_assets - t.total_liabilities - t.total_equity_parent
                           AS gap,
                       greatest(abs(t.total_assets) * {IDENTITY_SHARE},
                                {TIE_FLOOR}) AS tol) x
          CROSS JOIN LATERAL (
                SELECT EXISTS (
                    SELECT 1 FROM core.income_statement i
                     WHERE i.security_id = t.security_id
                       AND i.period_type = t.period_type
                       AND i.fiscal_date = t.fiscal_date
                       AND i.{CURRENT}
                       AND coalesce(i.net_income_noncontrolling, 0) <> 0) AS has) nci
         WHERE t.{CURRENT}
           AND t.total_assets IS NOT NULL AND t.total_liabilities IS NOT NULL
           AND t.total_equity_parent IS NOT NULL
           AND abs(x.gap) > x.tol
        """,
        what="balance sheets that do not balance",
    )


def check_cash_flow_tie(db: Database) -> int:
    """Operating, investing and financing cash flows and FX against the net change
    in cash, per current version (``warn``)."""
    return _write_flags(
        db,
        "cash_flow_tie",
        f"""
        SELECT t.security_id, 'core.cash_flow_statement' AS table_name,
               {_VERSION_KEY} AS record_key, 'warn' AS severity,
               jsonb_build_object(
                   'components', x.s::float8,
                   'net_change_in_cash', t.net_change_in_cash::float8,
                   'gap', (x.s - t.net_change_in_cash)::float8,
                   'tolerance', x.tol::float8) AS detail
          FROM core.cash_flow_statement t
          CROSS JOIN LATERAL (
                SELECT t.net_cash_operating + t.net_cash_investing
                       + t.net_cash_financing + coalesce(t.fx_effect_on_cash, 0) AS s,
                       greatest(greatest(abs(t.net_cash_operating),
                                         abs(t.net_cash_investing),
                                         abs(t.net_cash_financing)) * {IDENTITY_SHARE},
                                {TIE_FLOOR}) AS tol) x
         WHERE t.{CURRENT}
           AND t.net_cash_operating IS NOT NULL AND t.net_cash_investing IS NOT NULL
           AND t.net_cash_financing IS NOT NULL AND t.net_change_in_cash IS NOT NULL
           AND abs(x.s - t.net_change_in_cash) > x.tol
        """,
        what="cash flows that do not sum to the change in cash",
    )


def check_cash_balance_tie(db: Database) -> int:
    """The change in cash between consecutive balance sheets against the cash flow's
    net change, as in force today (``warn``)."""
    return _write_flags(
        db,
        "cash_balance_tie",
        f"""
        WITH b AS (
            SELECT security_id, period_type, fiscal_date,
                   cash_and_equivalents AS cash,
                   lag(fiscal_date) OVER w AS prev_fd,
                   lag(cash_and_equivalents) OVER w AS prev_cash
              FROM core.balance_sheet
             WHERE {IN_FORCE}
            WINDOW w AS (PARTITION BY security_id, period_type ORDER BY fiscal_date)
        )
        SELECT t.security_id, 'core.cash_flow_statement' AS table_name,
               {_PERIOD_KEY} AS record_key, 'warn' AS severity,
               jsonb_build_object(
                   'cash', b.cash::float8, 'previous_cash', b.prev_cash::float8,
                   'previous_fiscal_date', b.prev_fd::text,
                   'net_change_in_cash', t.net_change_in_cash::float8,
                   'gap', ((b.cash - b.prev_cash) - t.net_change_in_cash)::float8)
                   AS detail
          FROM b
          JOIN core.cash_flow_statement t
            ON t.security_id = b.security_id AND t.period_type = b.period_type
           AND t.fiscal_date = b.fiscal_date AND t.{IN_FORCE}
         WHERE b.cash IS NOT NULL AND b.prev_cash IS NOT NULL
           AND t.net_change_in_cash IS NOT NULL
           AND b.fiscal_date - b.prev_fd BETWEEN
               CASE b.period_type WHEN 'quarter' THEN {QUARTER_GAP[0]}
                                  ELSE {YEAR_GAP[0]} END
               AND CASE b.period_type WHEN 'quarter' THEN {QUARTER_GAP[1]}
                                      ELSE {YEAR_GAP[1]} END
           AND abs((b.cash - b.prev_cash) - t.net_change_in_cash)
               > greatest(greatest(abs(b.cash), abs(b.prev_cash)) * {CASH_TIE_SHARE},
                          {TIE_FLOOR})
        """,
        what="periods whose change in cash does not meet the cash flow",
    )


def check_fundamentals_gap(db: Database) -> int:
    """A security that traded through more than ``GAP_DAYS`` without a period end in
    force, between two of its periods or before its first (``info``)."""
    return _write_flags(
        db,
        "fundamentals_gap",
        f"""
        WITH p AS (
            SELECT security_id, fiscal_date,
                   lag(fiscal_date) OVER (PARTITION BY security_id
                                          ORDER BY fiscal_date) AS prev_fd
              FROM (SELECT DISTINCT security_id, fiscal_date
                      FROM core.income_statement WHERE {IN_FORCE}) d
        ),
        inner_gaps AS (
            SELECT p.security_id, p.prev_fd AS from_d, p.fiscal_date AS to_d
              FROM p
             WHERE p.fiscal_date - p.prev_fd > {GAP_DAYS}
               AND EXISTS (SELECT 1 FROM core.daily_price d
                            WHERE d.security_id = p.security_id
                              AND d.trade_date > p.prev_fd
                              AND d.trade_date < p.fiscal_date)
        ),
        leading_gaps AS (
            SELECT p.security_id, ft.d AS from_d, p.fiscal_date AS to_d
              FROM p
              CROSS JOIN LATERAL (
                    SELECT greatest(min(d.trade_date), DATE '{COVERAGE_FLOOR}') AS d
                      FROM core.daily_price d
                     WHERE d.security_id = p.security_id) ft
             WHERE p.prev_fd IS NULL AND p.fiscal_date - ft.d > {GAP_DAYS}
        )
        SELECT g.security_id, 'core.income_statement' AS table_name,
               jsonb_build_object('from', g.from_d::text, 'to', g.to_d::text)
                   AS record_key,
               'info' AS severity,
               jsonb_build_object('days', g.to_d - g.from_d) AS detail
          FROM (SELECT * FROM inner_gaps UNION ALL SELECT * FROM leading_gaps) g
        """,
        what="stretches traded without a statement",
    )


def check_fundamentals_stale(db: Database) -> int:
    """An actively trading security whose latest period in force ended more than
    ``FUNDAMENTALS_STALE_DAYS`` ago (``warn``). A later filing is a new occurrence;
    ``dq recheck`` closes the old one."""
    return _write_flags(
        db,
        "fundamentals_stale",
        f"""
        SELECT l.security_id, 'core.income_statement' AS table_name,
               jsonb_build_object('latest_fiscal_date', l.fd::text) AS record_key,
               'warn' AS severity,
               jsonb_build_object('days', current_date - l.fd) AS detail
          FROM (SELECT security_id, max(fiscal_date) AS fd
                  FROM core.income_statement WHERE {IN_FORCE}
                 GROUP BY security_id) l
          JOIN core.security s ON s.security_id = l.security_id
         WHERE s.is_actively_trading AND s.delisted_date IS NULL
           AND current_date - l.fd > {FUNDAMENTALS_STALE_DAYS}
        """,
        what="trading securities with stale statements",
    )


def check_fundamentals_currency_mismatch(db: Database) -> int:
    """Statements in force reported in a currency other than the security's trading
    currency (``info``): the panel's price ratios are NULL for them."""
    return _write_flags(
        db,
        "fundamentals_currency_mismatch",
        f"""
        SELECT t.security_id, 'core.income_statement' AS table_name,
               jsonb_build_object('reported_currency', t.reported_currency)
                   AS record_key,
               'info' AS severity,
               jsonb_build_object('trading_currency', coalesce(s.currency, 'USD'),
                                  'periods', count(*)) AS detail
          FROM core.income_statement t
          JOIN core.security s ON s.security_id = t.security_id
         WHERE t.{IN_FORCE} AND t.reported_currency IS NOT NULL
           AND t.reported_currency <> coalesce(s.currency, 'USD')
         GROUP BY t.security_id, t.reported_currency, s.currency
        """,
        what="securities reporting in another currency",
    )


def check_fundamentals_unmapped_fields(db: Database) -> int:
    """SF1 columns the map does not know, landed in ``extra.unmapped``: one flag per
    column name, for no security (``info``). The taxonomy's growth signal."""
    return _write_flags(
        db,
        "fundamentals_unmapped_fields",
        f"""
        SELECT NULL::bigint AS security_id, 'core.income_statement' AS table_name,
               jsonb_build_object('field', k.key) AS record_key,
               'info' AS severity,
               jsonb_build_object('rows', count(*),
                                  'securities', count(DISTINCT t.security_id))
                   AS detail
          FROM core.income_statement t
          CROSS JOIN LATERAL jsonb_object_keys(t.extra -> 'unmapped') AS k(key)
         WHERE t.{CURRENT} AND jsonb_typeof(t.extra -> 'unmapped') = 'object'
         GROUP BY k.key
        """,
        what="unmapped SF1 columns",
    )


def _contiguous(a: str, b: str, da: str, db_: str) -> str:
    lo, hi = QUARTER_GAP
    return f"({a} - {b} = 1 AND {da} - {db_} BETWEEN {lo} AND {hi})"


def check_fundamentals_ttm_incomplete(db: Database) -> int:
    """Quarters in force whose trailing four are not contiguous, past a security's
    first three: a missing or mislabelled quarter. One flag per security and fiscal
    year, listing the quarters (``info``)."""
    c3 = " AND ".join(
        (
            _contiguous("qn", "qn1", "fiscal_date", "d1"),
            _contiguous("qn1", "qn2", "d1", "d2"),
            _contiguous("qn2", "qn3", "d2", "d3"),
        )
    )
    return _write_flags(
        db,
        "fundamentals_ttm_incomplete",
        f"""
        WITH q AS (
            SELECT security_id, fiscal_date, fiscal_year, fiscal_period,
                   fiscal_year * 4 + substr(fiscal_period, 2)::int AS qn
              FROM core.income_statement
             WHERE period_type = 'quarter' AND {IN_FORCE}
        ),
        w AS (
            SELECT q.*, row_number() OVER w AS rn,
                   lag(qn, 1) OVER w AS qn1, lag(fiscal_date, 1) OVER w AS d1,
                   lag(qn, 2) OVER w AS qn2, lag(fiscal_date, 2) OVER w AS d2,
                   lag(qn, 3) OVER w AS qn3, lag(fiscal_date, 3) OVER w AS d3
              FROM q
            WINDOW w AS (PARTITION BY security_id ORDER BY fiscal_date)
        )
        SELECT security_id, 'core.income_statement' AS table_name,
               jsonb_build_object('fiscal_year', fiscal_year) AS record_key,
               'info' AS severity,
               jsonb_build_object('quarters',
                                  jsonb_agg(fiscal_period ORDER BY fiscal_date))
                   AS detail
          FROM w
         WHERE rn >= 4 AND NOT coalesce({c3}, false)
         GROUP BY security_id, fiscal_year
        """,
        what="security-years with an incomplete TTM",
    )


def check_fundamentals_negative_impossible(db: Database) -> int:
    """A negative total assets, revenue or share count in a current version
    (``error``)."""
    parts = []
    for table, items in NEVER_NEGATIVE.items():
        cond = " OR ".join(f"t.{c} < 0" for c in items)
        found = ", ".join(
            f"'{c}', CASE WHEN t.{c} < 0 THEN t.{c}::float8 END" for c in items
        )
        parts.append(f"""
        SELECT t.security_id, '{table}' AS table_name,
               {_keyed(_VERSION_KEY, table)} AS record_key,
               'error' AS severity, jsonb_strip_nulls(jsonb_build_object({found}))
                   AS detail
          FROM {table} t
         WHERE t.{CURRENT} AND ({cond})""")
    return _write_flags(
        db,
        "fundamentals_negative_impossible",
        "\n        UNION ALL".join(parts),
        what="versions with an impossible negative",
    )


def check_fundamentals_coverage(db: Database) -> int:
    """An actively trading security SF1 has statements for (its master's
    ``first_quarter``) with none in force here (``info``). Logs coverage by sector:
    the share of such securities with a statement, by the sector readers see
    (#109)."""
    eligible = f"""
        SELECT DISTINCT s.security_id, c.sector_id,
               EXISTS (SELECT 1 FROM core.income_statement t
                        WHERE t.security_id = s.security_id AND t.{IN_FORCE})
                   AS covered
          FROM core.security s
          JOIN core.v_security_classification c ON c.security_id = s.security_id
          JOIN core.security_vendor_xref x
            ON x.security_id = s.security_id AND x.vendor = 'sharadar'
           AND x.valid_to IS NULL
          JOIN core.vendor_security v
            ON v.vendor = 'sharadar' AND v.vendor_table = 'fundamentals'
           AND v.vendor_key = x.vendor_key AND v.first_quarter IS NOT NULL
         WHERE s.is_actively_trading AND s.delisted_date IS NULL
    """
    by_sector = db.fetchall(f"""
        SELECT coalesce(r.sector_name, '(none)') AS sector, count(*) AS n,
               count(*) FILTER (WHERE e.covered) AS covered
          FROM ({eligible}) e
          LEFT JOIN ref.sector r ON r.sector_id = e.sector_id
         GROUP BY 1 ORDER BY 1
        """)
    for r in by_sector:
        logger.info(
            "fundamentals coverage: %s %d of %d (%.1f%%)",
            r["sector"],
            r["covered"],
            r["n"],
            100.0 * r["covered"] / r["n"],
        )
    return _write_flags(
        db,
        "fundamentals_coverage",
        f"""
        SELECT e.security_id, 'core.income_statement' AS table_name,
               '{{}}'::jsonb AS record_key, 'info' AS severity,
               '{{}}'::jsonb AS detail
          FROM ({eligible}) e
         WHERE NOT e.covered
        """,
        what="SF1-covered securities without statements",
    )


def check_fundamentals_suspicious_zero(db: Database) -> int:
    """A zero, in force, between two in-force neighbours that report the item: SF1's
    zero for an absent value where the company does report it (``info``)."""
    parts = []
    for table, items in NEVER_ZERO.items():
        lags = ", ".join(
            f"lag({c}) OVER w AS {c}_prev, lead({c}) OVER w AS {c}_next" for c in items
        )
        hits = " OR ".join(
            f"({c} = 0 AND {c}_prev <> 0 AND {c}_next <> 0)" for c in items
        )
        listed = ", ".join(
            f"CASE WHEN {c} = 0 AND {c}_prev <> 0 AND {c}_next <> 0 " f"THEN '{c}' END"
            for c in items
        )
        parts.append(f"""
        SELECT t.security_id, '{table}' AS table_name,
               {_keyed(_PERIOD_KEY, table)} AS record_key,
               'info' AS severity,
               jsonb_build_object('items', to_jsonb(array_remove(ARRAY[{listed}],
                                                                 NULL))) AS detail
          FROM (SELECT *, {lags}
                  FROM {table} WHERE {IN_FORCE}
                WINDOW w AS (PARTITION BY security_id, period_type
                             ORDER BY fiscal_date)) t
         WHERE {hits}""")
    return _write_flags(
        db,
        "fundamentals_suspicious_zero",
        "\n        UNION ALL".join(parts),
        what="periods with a suspicious zero",
    )


def check_fundamentals_implausible_filing_lag(db: Database) -> int:
    """A filed version known less than ``FILING_LAG_MIN_DAYS`` after its period
    ended (``warn``): a filing date that is probably the period's. Read on the income
    statement, which every SF1 row carries."""
    return _write_flags(
        db,
        "fundamentals_implausible_filing_lag",
        f"""
        SELECT t.security_id, 'core.income_statement' AS table_name,
               {_VERSION_KEY} AS record_key, 'warn' AS severity,
               jsonb_build_object('known_from', t.known_from::text,
                                  'days', t.known_from - t.fiscal_date) AS detail
          FROM core.income_statement t
         WHERE t.{CURRENT} AND t.knowledge_source = 'filed'
           AND t.known_from - t.fiscal_date < {FILING_LAG_MIN_DAYS}
        """,
        what="filings implausibly close to their period end",
    )


def check_fundamentals_ytd_suspect(db: Database) -> int:
    """A fiscal year whose four quarters in force sum to more than ``YTD_RATIO``
    times the year: quarters reported year to date, not discretely (``warn``).
    Revenue, and operating cash flow, which 10-Qs report year to date.

    A fiscal year with two annual periods in force (a change of year end can label a
    transition period with the year it shares) is not judged: its quarters cannot be
    told apart by the label, and two annual rows would be two flags on one key."""
    parts = []
    for table, item in (
        ("core.income_statement", "revenue"),
        ("core.cash_flow_statement", "net_cash_operating"),
    ):
        parts.append(f"""
        SELECT q.security_id, '{table}' AS table_name,
               jsonb_build_object('fiscal_year', q.fiscal_year, 'item', '{item}')
                   AS record_key,
               'warn' AS severity,
               jsonb_build_object('quarters', q.s::float8, 'year', a.v::float8)
                   AS detail
          FROM (SELECT security_id, fiscal_year, count({item}) AS n,
                       sum({item}) AS s
                  FROM {table} WHERE period_type = 'quarter' AND {IN_FORCE}
                 GROUP BY security_id, fiscal_year) q
          JOIN (SELECT security_id, fiscal_year, min({item}) AS v
                  FROM {table} WHERE period_type = 'annual' AND {IN_FORCE}
                 GROUP BY security_id, fiscal_year
                HAVING count(*) = 1) a
            ON a.security_id = q.security_id AND a.fiscal_year = q.fiscal_year
         WHERE q.n = 4 AND a.v <> 0 AND abs(q.s) > {YTD_RATIO} * abs(a.v)""")
    return _write_flags(
        db,
        "fundamentals_ytd_suspect",
        "\n        UNION ALL".join(parts),
        what="years whose quarters look year to date",
    )


def check_fundamentals_version_chain(db: Database) -> int:
    """A period whose current versions do not chain: each ``known_to`` the next
    ``known_from``, only the last open (ADR 0018; ``error``). The loader keeps this;
    a break means a write went around it."""
    parts = []
    for table in STATEMENT_TABLES:
        parts.append(f"""
        SELECT security_id, '{table}' AS table_name,
               jsonb_build_object('statement', '{_STATEMENT[table]}',
                                  'period_type', period_type,
                                  'fiscal_date', fiscal_date::text) AS record_key,
               'error' AS severity,
               jsonb_build_object('versions', jsonb_agg(
                   jsonb_build_object('version_no', version_no,
                                      'known_from', known_from::text,
                                      'known_to', known_to::text)
                   ORDER BY known_from, version_no)) AS detail
          FROM (SELECT security_id, period_type, fiscal_date, version_no,
                       known_from, known_to,
                       lead(known_from) OVER w AS next_from
                  FROM {table} WHERE {CURRENT}
                WINDOW w AS (PARTITION BY security_id, period_type, fiscal_date
                             ORDER BY known_from, version_no)) v
         GROUP BY security_id, period_type, fiscal_date
        HAVING bool_or(CASE WHEN next_from IS NULL THEN known_to IS NOT NULL
                            ELSE known_to IS DISTINCT FROM next_from END)""")
    return _write_flags(
        db,
        "fundamentals_version_chain",
        "\n        UNION ALL".join(parts),
        what="periods whose versions do not chain",
    )


#: The fundamentals checks, in the order ``run_all`` runs them: its keys.
FUNDAMENTALS_CHECKS = {
    "balance_sheet_identity": check_balance_sheet_identity,
    "cash_flow_tie": check_cash_flow_tie,
    "cash_balance_tie": check_cash_balance_tie,
    "fundamentals_gap": check_fundamentals_gap,
    "fundamentals_stale": check_fundamentals_stale,
    "fundamentals_currency_mismatch": check_fundamentals_currency_mismatch,
    "fundamentals_unmapped_fields": check_fundamentals_unmapped_fields,
    "fundamentals_ttm_incomplete": check_fundamentals_ttm_incomplete,
    "fundamentals_negative_impossible": check_fundamentals_negative_impossible,
    "fundamentals_coverage": check_fundamentals_coverage,
    "fundamentals_suspicious_zero": check_fundamentals_suspicious_zero,
    "fundamentals_implausible_filing_lag": check_fundamentals_implausible_filing_lag,
    "fundamentals_ytd_suspect": check_fundamentals_ytd_suspect,
    "fundamentals_version_chain": check_fundamentals_version_chain,
}


def run_all(
    db: Database,
    exchange_code: str = "NASDAQ",
    outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
    include_fundamentals: bool = True,
) -> dict:
    """Run every check and return a summary dict of NEW flag counts.

    A run over data whose problems are all already in the queue reports zeros --
    that is the check working, not the check finding nothing. The standing totals
    are in ops.data_quality_flag (and `fafnir status`); the per-check detected
    counts are logged.

    The price and security checks' five keys come first and never change (#102);
    the fundamentals checks' keys follow, one per check. They are 0 without
    ``include_fundamentals`` or without the statement tables (a database
    mid-migration), and then nothing runs against them.
    """
    out = {
        "gaps": check_gaps(db, exchange_code),
        "outliers": check_outliers(db, outlier_threshold),
        "stale": check_freshness(db, exchange_code),
        "duplicate_identity": check_duplicate_identity(db),
        "missing_classification": check_missing_classification(db),
    }
    run = include_fundamentals and statements_exist(db)
    if include_fundamentals and not run:
        logger.info("fundamentals checks skipped: no statement tables")
    for key, check in FUNDAMENTALS_CHECKS.items():
        out[key] = check(db) if run else 0
    return out
