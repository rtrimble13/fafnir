"""check_freshness's threshold, pinned without a database.

The integration tests in test_declared_universe.py show the check against real
bars. These pin the arithmetic and the binding, which is where a moved constant or
a reordered placeholder would go wrong silently: the query still runs, it just
judges against the wrong date.
"""

from __future__ import annotations

from fafnir.dq import checks


class _CaptureDB:
    """Records the one statement check_freshness sends, and returns no rows."""

    def __init__(self):
        self.sql = None
        self.params = None

    def fetchone(self, sql, params=()):
        self.sql, self.params = sql, params
        return {"detected": 0, "flagged": 0}


def test_one_late_session_is_not_stale_and_two_are():
    # One missed session is FMP posting a thin name's bar a night late: 128 of the
    # 173 stale flags still open on 2026-09-10 were exactly that.
    assert checks.stale_sessions_required(False) == 2


def test_a_nav_gets_its_publishing_lag_on_top():
    assert checks.stale_sessions_required(True) == (
        checks.stale_sessions_required(False) + checks.NAV_LAG_TRADING_DAYS
    )


def test_the_check_binds_every_placeholder_it_writes():
    db = _CaptureDB()
    checks.check_freshness(db, "NASDAQ")

    assert db.sql.count("%s") == len(db.params)
    assert tuple(db.params[:5]) == checks.freshness_cutoff_params("NASDAQ")
    assert db.params[5] == list(checks.NAV_LAGGING_ASSET_TYPES)


def test_a_fund_is_nav_priced_whatever_its_asset_type():
    """All 5,097 production funds are stored asset_type 'equity' with is_fund."""
    db = _CaptureDB()
    checks.check_freshness(db)

    assert "s.is_fund" in checks.NAV_PRICED_PREDICATE
    assert checks.NAV_PRICED_PREDICATE in db.sql
    # Grouped on, or the HAVING that reads it would not compile.
    assert "s.is_fund" in db.sql.split("GROUP BY", 1)[1].split("HAVING", 1)[0]
