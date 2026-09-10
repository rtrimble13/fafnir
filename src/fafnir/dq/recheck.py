"""Close the flags whose condition is no longer true.

A DQ flag records that something *was* wrong. Nothing in the queue records that it
has since been put right: the checks only ever add (``add_dq_flag_once`` skips a
condition already open, and never retracts one), so a flag outlives the defect it
describes. After a repair the queue is left asserting things that are not so, and
the only way to correct it has been to reconstruct the repaired set by hand and
feed it back through ``dq resolve`` as a filter.

That reconstruction is where the danger is. It is done in a different language
from the check, against a different reading of the same rule, so the filter can be
subtly wider than the repair -- and a filter that is wider than the repair closes
flags whose condition still holds. Every such filter is also single-use.

This module removes the reconstruction. For each supported check it re-evaluates
that check's *own* predicate against the flags it wrote, and reports the ones the
predicate no longer selects. Resolving those is not a judgement about whether the
data is acceptable; it is the observation that the recorded condition is absent
from the warehouse now.

What is deliberately not here
-----------------------------
``price_*`` never appears. Those flags are quarantine records for bars that were
never stored, so there is nothing to re-evaluate, and their repeats are
load-bearing -- ``count_price_quarantines`` counts them to decide when a
persistently bad bar has held a watermark long enough.

:data:`~fafnir_mcp.tools.NEVER_AUTO_RESOLVE` never appears either, and
:func:`_assert_scope_is_safe` fails the import if it ever does. Those checks are
measurements or unrepresentable values rather than conditions that clear --
`corporate_action_drift` describes data that has *already* been repaired and is
telling you the sweep cannot be trusted, and closing it silently discards that.

A check absent from :data:`RECHECKABLE` is not an oversight to be fixed by adding
it. It is absent because its predicate is a judgement about vendor data
(`dividend_exceeds_price`, `split_invalid`, `security_company_name_drift`) rather
than a function of warehouse state, and re-running it would return exactly what it
returned the first time.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

from fafnir.db.connection import Database
from fafnir.dq.checks import (
    DEFAULT_OUTLIER_THRESHOLD,
    FRESHNESS_CUTOFF_CTES,
    GAP_MIN_SESSION_DENSITY,
    GAP_MIN_SESSIONS_FOR_DENSITY,
    NAV_LAGGING_ASSET_TYPES,
    NAV_PRICED_PREDICATE,
    stale_sessions_required,
)
from fafnir.logging_config import get_logger

logger = get_logger("dq")


class RecheckResult(NamedTuple):
    """What one check's re-evaluation found."""

    check_name: str
    open_flags: int
    stale_flag_ids: Sequence[int]
    reason: str

    @property
    def stale(self) -> int:
        return len(self.stale_flag_ids)


# Each entry is the NEGATION of what the named check emits, so a flag is listed
# only when re-running that check today would not write it. The reason is what
# lands in the resolution note, and it names the predicate rather than the repair
# -- the repair is whatever the operator did, and the note should say what was
# *observed*, not what is assumed to have caused it.
_GAP_SQL = """
WITH f AS (
    SELECT dq_flag_id, security_id, (record_key->>'trade_date')::date AS d
      FROM ops.data_quality_flag
     WHERE check_name = 'gap' AND resolved_at IS NULL
),
bounds AS (
    SELECT security_id, min(trade_date) AS dmin, max(trade_date) AS dmax,
           count(*) AS bars
      FROM core.daily_price
     WHERE security_id IN (SELECT DISTINCT security_id FROM f)
     GROUP BY security_id
),
cov AS (
    SELECT b.*,
           (SELECT count(*) FROM ref.trading_calendar c
             WHERE c.exchange_code = %s AND c.is_open
               AND c.trade_date BETWEEN b.dmin AND b.dmax) AS sessions
      FROM bounds b
)
SELECT f.dq_flag_id
  FROM f
  LEFT JOIN cov ON cov.security_id = f.security_id
 WHERE
       -- the bar arrived
       EXISTS (SELECT 1 FROM core.daily_price p
                WHERE p.security_id = f.security_id AND p.trade_date = f.d)
       -- or the calendar no longer calls that day a session (migration 0022 is
       -- the case that produced 69,230 of these at once)
    OR NOT EXISTS (SELECT 1 FROM ref.trading_calendar c
                    WHERE c.exchange_code = %s AND c.is_open
                      AND c.trade_date = f.d)
       -- or the security lost every bar, so it has no window to have a gap in
    OR cov.security_id IS NULL
       -- or the day now falls outside the security's own window, which is the
       -- only range check_gaps ever looks at
    OR f.d < cov.dmin OR f.d > cov.dmax
       -- or the security is no longer dense enough to be checked per session, so
       -- check_gaps would write sparse_coverage for it instead
    OR (cov.sessions >= %s
        AND cov.bars::numeric / nullif(cov.sessions, 0) < %s)
"""

_DIVIDEND_SQL = """
SELECT f.dq_flag_id
  FROM ops.data_quality_flag f
 WHERE f.check_name = 'dividend_no_prior_close' AND f.resolved_at IS NULL
   AND EXISTS (SELECT 1 FROM core.daily_price p
                WHERE p.security_id = f.security_id
                  AND p.trade_date < (f.record_key->>'ex_date')::date)
"""

_OUTLIER_SQL = """
WITH f AS (
    SELECT dq_flag_id, security_id, (record_key->>'trade_date')::date AS d
      FROM ops.data_quality_flag
     WHERE check_name = 'outlier' AND resolved_at IS NULL
),
m AS (
    SELECT f.*,
           (SELECT p.close FROM core.daily_price p
             WHERE p.security_id = f.security_id AND p.trade_date = f.d) AS close,
           (SELECT p.close FROM core.daily_price p
             WHERE p.security_id = f.security_id AND p.trade_date < f.d
             ORDER BY p.trade_date DESC LIMIT 1) AS prev_close
      FROM f
)
SELECT m.dq_flag_id
  FROM m
 WHERE m.close IS NULL OR m.prev_close IS NULL OR m.prev_close <= 0
       -- the move is no longer over the threshold, because a bar on either side
       -- was corrected or backfilled
    OR abs(m.close - m.prev_close) / m.prev_close <= %s
       -- or the split that explains it has since been loaded, which is the
       -- playbook's repair for this check
    OR EXISTS (SELECT 1 FROM core.corporate_action ca
                WHERE ca.security_id = m.security_id
                  AND ca.action_type = 'split' AND ca.ex_date = m.d)
"""

# The reference dates are check_freshness's own CTEs, verbatim, so the negation
# cannot drift from the check when the threshold moves.
_STALE_SQL = (
    "WITH "
    + FRESHNESS_CUTOFF_CTES
    + """
SELECT f.dq_flag_id
  FROM ops.data_quality_flag f
  LEFT JOIN core.security s ON s.security_id = f.security_id
 CROSS JOIN cutoff co
 CROSS JOIN LATERAL (
     SELECT max(p.trade_date) AS d FROM core.daily_price p
      WHERE p.security_id = f.security_id) lb
 WHERE f.check_name = 'stale' AND f.resolved_at IS NULL
   AND (
        -- the security stopped trading, so check_freshness no longer considers it
        NOT COALESCE(s.is_actively_trading, FALSE)
        -- or it has taken a bar since. The flag is keyed on the last_date it had
        -- when written, so a later bar ends THAT occurrence: a security that falls
        -- behind again is a new record_key and a new flag.
     OR COALESCE(lb.d, DATE '1900-01-01') > (f.record_key->>'last_date')::date
        -- or it is no longer far enough behind the market to be called stale: the
        -- flag was written under an earlier, one-session threshold, or it is a fund
        -- that has only now been recognised as NAV-priced
     OR COALESCE(lb.d, DATE '1900-01-01') >= CASE
            WHEN """
    + NAV_PRICED_PREDICATE
    + """ THEN co.nav_d
            ELSE co.listed_d
        END
   )
"""
)

_SPARSE_SQL = """
WITH f AS (
    SELECT dq_flag_id, security_id
      FROM ops.data_quality_flag
     WHERE check_name = 'sparse_coverage' AND resolved_at IS NULL
),
b AS (
    SELECT f.dq_flag_id, f.security_id, min(p.trade_date) AS dmin,
           max(p.trade_date) AS dmax, count(*) AS bars
      FROM f JOIN core.daily_price p USING (security_id)
     GROUP BY f.dq_flag_id, f.security_id
)
SELECT b.dq_flag_id
  FROM b
 CROSS JOIN LATERAL (
     SELECT count(*) AS sessions FROM ref.trading_calendar c
      WHERE c.exchange_code = %s AND c.is_open
        AND c.trade_date BETWEEN b.dmin AND b.dmax) s
 -- The security trades densely enough to be checked per session again, so
 -- check_gaps no longer describes it this way. Without this the flag outlives the
 -- classification: a security that crosses back over the line keeps an open flag
 -- asserting a density it no longer has, while also accruing gap flags.
 WHERE s.sessions < %s
    OR b.bars::numeric / nullif(s.sessions, 0) >= %s
"""

_CLASSIFICATION_SQL = """
SELECT f.dq_flag_id
  FROM ops.data_quality_flag f
 WHERE f.check_name = 'security_missing_classification' AND f.resolved_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM core.security s
                    WHERE s.security_id = f.security_id
                      AND s.is_actively_trading AND s.delisted_date IS NULL
                      AND (s.sector_id IS NULL OR s.industry_id IS NULL))
"""


class _Rule(NamedTuple):
    sql: str
    # Which settings the SQL takes, in the order its placeholders appear. Named
    # rather than positional in the table so a reordered query cannot silently
    # bind a density where an exchange belongs.
    params: tuple[str, ...]
    reason: str


RECHECKABLE: dict[str, _Rule] = {
    "gap": _Rule(
        _GAP_SQL,
        ("exch", "exch", "min_sessions", "min_density"),
        "a bar now exists for that session, or the calendar no longer calls it a "
        "session, or the security is no longer checked per session",
    ),
    "dividend_no_prior_close": _Rule(
        _DIVIDEND_SQL,
        (),
        "a prior raw close now exists on this security, so the dividend factor can "
        "be valued",
    ),
    "outlier": _Rule(
        _OUTLIER_SQL,
        ("threshold",),
        "the close-to-close move is no longer over the threshold, or a split "
        "explaining it has since been loaded",
    ),
    "stale": _Rule(
        _STALE_SQL,
        ("exch", "exch", "stale_listed", "exch", "stale_nav", "nav_types"),
        "the security has taken a bar later than the last_date this flag records, "
        "or is no longer far enough behind the market to be stale, or is no longer "
        "actively trading",
    ),
    "sparse_coverage": _Rule(
        _SPARSE_SQL,
        ("exch", "min_sessions", "min_density"),
        "the security now holds bars for enough of its own sessions to be checked "
        "per session again",
    ),
    "security_missing_classification": _Rule(
        _CLASSIFICATION_SQL,
        (),
        "the security now carries a sector and industry, or is no longer a listed "
        "security",
    ),
}


def _assert_scope_is_safe() -> None:
    """Fail loudly at import if this module ever grows a check it must not touch.

    The two exclusions are not stylistic. A ``price_*`` flag describes a bar that
    was never stored, so re-evaluating it against stored data would find the
    condition "gone" for every one of them; NEVER_AUTO_RESOLVE checks are
    measurements whose value is the record itself. Either would be closed in bulk
    by a command whose whole promise is that it only closes what it verified.
    """
    from fafnir_mcp.tools import NEVER_AUTO_RESOLVE

    overlap = set(RECHECKABLE) & set(NEVER_AUTO_RESOLVE)
    if overlap:
        raise AssertionError(f"recheck must not touch NEVER_AUTO_RESOLVE: {overlap}")
    price = {c for c in RECHECKABLE if c.startswith("price_")}
    if price:
        raise AssertionError(f"recheck must not touch price_* quarantines: {price}")


_assert_scope_is_safe()


def recheck(
    db: Database,
    *,
    checks: Optional[Sequence[str]] = None,
    exchange_code: str = "NASDAQ",
    outlier_threshold: float = DEFAULT_OUTLIER_THRESHOLD,
    min_density: float = GAP_MIN_SESSION_DENSITY,
    min_sessions: int = GAP_MIN_SESSIONS_FOR_DENSITY,
) -> list[RecheckResult]:
    """Re-evaluate each supported check against its own open flags.

    Returns one result per check, in a stable order, including checks with nothing
    stale -- "0 of 39,475 no longer hold" is an answer, and a caller that only sees
    the non-empty rows cannot tell it apart from a check that was skipped.

    Reads only. Closing what this finds is :func:`fafnir.db.repository.resolve_dq_flags`.
    """
    wanted = list(checks) if checks else list(RECHECKABLE)
    unknown = [c for c in wanted if c not in RECHECKABLE]
    if unknown:
        raise ValueError(
            f"not re-evaluable: {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(RECHECKABLE))}"
        )

    settings = {
        "exch": exchange_code,
        "threshold": outlier_threshold,
        "min_density": min_density,
        "min_sessions": min_sessions,
        # Not caller settings: check_freshness takes none, so its negation must
        # not either, or a recheck could judge against a threshold the check never
        # used.
        "stale_listed": stale_sessions_required(False),
        "stale_nav": stale_sessions_required(True),
        "nav_types": list(NAV_LAGGING_ASSET_TYPES),
    }
    out: list[RecheckResult] = []
    for name in wanted:
        rule = RECHECKABLE[name]
        sql, reason = rule.sql, rule.reason
        params = tuple(settings[k] for k in rule.params)
        open_flags = int(
            db.fetchval(
                """
                SELECT count(*) FROM ops.data_quality_flag
                 WHERE check_name = %s AND resolved_at IS NULL
                """,
                (name,),
            )
            or 0
        )
        ids = [int(r["dq_flag_id"]) for r in db.fetchall(sql, params)]
        logger.info(
            "recheck %s: %d of %d open flags no longer hold", name, len(ids), open_flags
        )
        out.append(RecheckResult(name, open_flags, ids, reason))
    return out
