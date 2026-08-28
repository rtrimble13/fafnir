"""One unresolved problem is one unresolved flag (needs FAFNIR_TEST_DSN).

`repository.add_dq_flag` is an unguarded INSERT, and the checks that call it are
re-run on a schedule over the same data: `fafnir adjust` recomputes every security
with actions every night and `fafnir dq run` re-scans the whole universe. So one
standing problem used to produce one new unresolved flag per run, forever -- a
security with 40 missing days contributing 40 rows a night -- while `fafnir status`
reported count(*) WHERE resolved_at IS NULL as the number an operator should
triage. The queue degraded into a log.

The repeat is load-bearing in exactly one place: `count_price_quarantines` counts
the price_* flags for a (security_id, date) so `daily_price` can stop holding the
watermark behind a permanently-bad bar after MAX_QUARANTINE_HOLDS. That is why the
fix is a sibling helper (`add_dq_flag_once`) opted into per call site rather than a
change to `add_dq_flag`, and these tests pin both halves of that split.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import adjustments

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_security(db, symbol, *, name="Test Co"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _bars(db, sid, closes: dict):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            }
            for day, close in closes.items()
        ],
    )


def _open_flags(db, check_name: str | None = None) -> int:
    sql = "SELECT count(*) FROM ops.data_quality_flag WHERE resolved_at IS NULL"
    params: tuple = ()
    if check_name is not None:
        sql += " AND check_name = %s"
        params = (check_name,)
    return int(db.fetchval(sql, params) or 0)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def test_a_standing_condition_is_flagged_once_however_often_it_is_detected(db):
    sid = _mk_security(db, "AAA")

    first = repo.add_dq_flag_once(
        db,
        check_name="dividend_exceeds_price",
        security_id=sid,
        record_key={"ex_date": "2024-06-10"},
    )
    again = [
        repo.add_dq_flag_once(
            db,
            check_name="dividend_exceeds_price",
            security_id=sid,
            record_key={"ex_date": "2024-06-10"},
        )
        for _ in range(5)
    ]

    assert first is True
    assert again == [False] * 5
    assert _open_flags(db) == 1


def test_a_different_record_key_is_a_different_occurrence(db):
    # A new gap date or a different ex-date is a new problem, not a repeat of the
    # one already in the queue, and must still reach the operator.
    sid = _mk_security(db, "AAA")

    for day in ("2024-06-10", "2024-06-11", "2024-06-12"):
        assert repo.add_dq_flag_once(
            db, check_name="gap", security_id=sid, record_key={"trade_date": day}
        )

    assert _open_flags(db) == 3


def test_the_same_condition_on_another_security_is_its_own_flag(db):
    a = _mk_security(db, "AAA")
    b = _mk_security(db, "BBB")

    assert repo.add_dq_flag_once(db, check_name="adjustment_failed", security_id=a)
    assert repo.add_dq_flag_once(db, check_name="adjustment_failed", security_id=b)
    assert not repo.add_dq_flag_once(db, check_name="adjustment_failed", security_id=a)

    assert _open_flags(db) == 2


def test_a_keyless_flag_dedupes_per_security(db):
    # adjustment_failed carries no record_key: NULL has to match NULL here, or a
    # security that fails every night is flagged every night.
    sid = _mk_security(db, "AAA")

    repo.add_dq_flag_once(
        db, check_name="adjustment_failed", severity="error", security_id=sid
    )
    repo.add_dq_flag_once(
        db,
        check_name="adjustment_failed",
        severity="error",
        security_id=sid,
        detail={"error": "ValueError: a different message on the next run"},
    )

    assert _open_flags(db) == 1


def test_key_order_does_not_make_a_second_flag(db):
    # Matching is jsonb equality, not string equality: the same key written with
    # its fields in another order is the same condition.
    sid = _mk_security(db, "AAA")

    assert repo.add_dq_flag_once(
        db,
        check_name="symbol_change_conflict",
        security_id=sid,
        record_key={"old_symbol": "FB", "new_symbol": "META"},
    )
    assert not repo.add_dq_flag_once(
        db,
        check_name="symbol_change_conflict",
        security_id=sid,
        record_key={"new_symbol": "META", "old_symbol": "FB"},
    )

    assert _open_flags(db) == 1


def test_resolving_a_flag_lets_the_condition_be_raised_again(db):
    # Only *unresolved* flags suppress. A problem someone marked handled that then
    # comes back is news again.
    sid = _mk_security(db, "AAA")
    repo.add_dq_flag_once(
        db, check_name="gap", security_id=sid, record_key={"trade_date": "2024-06-10"}
    )
    db.execute("UPDATE ops.data_quality_flag SET resolved_at = now()")

    assert repo.add_dq_flag_once(
        db, check_name="gap", security_id=sid, record_key={"trade_date": "2024-06-10"}
    )
    assert _open_flags(db) == 1
    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == 2


def test_price_quarantines_still_repeat_so_the_watermark_can_be_released(db):
    # The one place the repeat is the signal rather than noise: daily_price stays on
    # add_dq_flag because count_price_quarantines counts these rows to decide when a
    # persistently-bad bar has held the watermark long enough. Deduplicating them
    # would freeze this counter at 1 and hold the watermark behind that bar forever.
    sid = _mk_security(db, "AAA")

    for _ in range(3):
        repo.add_dq_flag(
            db,
            check_name="price_non_positive_close",
            security_id=sid,
            record_key={"symbol": "AAA", "date": "2024-06-10"},
        )

    assert repo.count_price_quarantines(db, sid, "2024-06-10") == 3


# ---------------------------------------------------------------------------
# `fafnir dq run` over unchanged data
# ---------------------------------------------------------------------------


def _universe_with_every_problem(db):
    """One security with a gap and an outlier, one that has gone stale."""
    gappy = _mk_security(db, "GAP")
    _bars(
        db,
        gappy,
        {
            dt.date(2024, 6, 3): 100,
            dt.date(2024, 6, 4): 100,
            # 2024-06-05 and 06-06 are open sessions with no bar -> two gaps
            dt.date(2024, 6, 7): 100,
            dt.date(2024, 6, 10): 300,  # +200% with no split -> outlier
        },
    )
    stale = _mk_security(db, "OLD")
    _bars(db, stale, {dt.date(2024, 6, 3): 50})
    return gappy, stale


def test_dq_run_twice_over_unchanged_data_leaves_the_open_count_unchanged(db):
    _universe_with_every_problem(db)

    first = checks.run_all(db)
    after_first = _open_flags(db)
    second = checks.run_all(db)

    # The first pass finds the problems; the second finds the same ones and writes
    # nothing, because they are all still sitting in the queue.
    assert first == {"gaps": 2, "outliers": 1, "stale": 1}
    assert second == {"gaps": 0, "outliers": 0, "stale": 0}
    assert _open_flags(db) == after_first == 4


def test_dq_run_still_records_a_problem_that_is_new(db):
    gappy, stale = _universe_with_every_problem(db)
    checks.run_all(db)

    # The stale security takes one bar and immediately falls behind again: a
    # different last_date, so a different occurrence.
    _bars(db, stale, {dt.date(2024, 6, 4): 50})
    # And the gappy one gains a bar past a session it never loaded.
    _bars(db, gappy, {dt.date(2024, 6, 12): 300})

    second = checks.run_all(db)

    assert second["stale"] == 1
    assert second["gaps"] == 1  # 2024-06-11, newly inside the security's range
    assert _open_flags(db, "gap") == 3
    assert _open_flags(db, "stale") == 2


# ---------------------------------------------------------------------------
# `fafnir adjust` over unchanged data
# ---------------------------------------------------------------------------


def test_adjust_twice_over_unchanged_data_leaves_the_open_count_unchanged(db):
    # `fafnir adjust` recomputes every security with actions on every run, so its
    # flags are re-raised nightly by design.
    sid = _mk_security(db, "AAA")
    _bars(db, sid, {dt.date(2024, 6, 10): 10})
    # A dividend before any price: no prior close to value it against.
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 3),
        dividend_amount=0.25,
    )
    # And one worth more than the close it follows.
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 12),
        dividend_amount=99.0,
    )

    adjustments.adjust_all(db)
    after_first = _open_flags(db)
    for _ in range(3):
        adjustments.adjust_all(db)

    assert after_first == 2
    assert _open_flags(db, "dividend_no_prior_close") == 1
    assert _open_flags(db, "dividend_exceeds_price") == 1
    assert _open_flags(db) == after_first


def test_adjust_flags_a_new_ex_date_it_has_not_seen(db):
    sid = _mk_security(db, "AAA")
    _bars(db, sid, {dt.date(2024, 6, 10): 10})
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 12),
        dividend_amount=99.0,
    )
    adjustments.adjust_all(db)

    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 13),
        dividend_amount=99.0,
    )
    adjustments.adjust_all(db)

    assert _open_flags(db, "dividend_exceeds_price") == 2
