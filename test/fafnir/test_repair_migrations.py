"""The repair migrations 0015 and 0016 (needs FAFNIR_TEST_DSN).

0014 stopped new duplicate DQ flags being written and the upsert_symbol_xref fix
stopped new duplicate open ticker periods, but neither touched what a warehouse
that had already been running nightly was carrying. Both invariants are also only
as strong as every current call site until the schema holds them.

These tests run the shipped migration files against a database seeded with exactly
the corruption the two bugs produced, so what is verified is the SQL that will run
on the real warehouse -- not a reimplementation of it.
"""

from __future__ import annotations

import datetime as dt
import os

import psycopg
import pytest

from fafnir.db import repository as repo
from fafnir.db.migrate import find_sql_dir

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

XREF_INDEX = "core.ux_symbol_xref_open"
FLAG_INDEX = "ops.ux_dq_flag_open_condition"


def _run_migration(db, stem: str) -> None:
    """Execute a shipped .up.sql exactly as the migration runner would."""
    path = next(find_sql_dir().glob(f"migrations/{stem}*.up.sql"))
    db.execute_script(path.read_text())


@pytest.fixture()
def unconstrained(db):
    """Drop the two unique indexes so the corruption can be seeded at all.

    The test body restores one of them by running the migration that creates it,
    which is the point: the repair has to succeed on a table the constraint would
    reject. Teardown restores BOTH -- the other one, and either one when the test
    fails before it gets that far. The database is shared by the whole session
    (`migrated_dsn`), so an index left dropped here is a constraint silently
    missing from every test that runs after this file.
    """
    db.execute(f"DROP INDEX IF EXISTS {XREF_INDEX}")
    db.execute(f"DROP INDEX IF EXISTS {FLAG_INDEX}")
    try:
        yield db
    finally:
        _run_migration(db, "0015")
        _run_migration(db, "0016")


def _mk_security(db, symbol, name="Test Co"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code="NASDAQ",
    )


def _xref(db):
    return [
        (r["symbol"], r["valid_from"], r["valid_to"], r["security_id"])
        for r in db.fetchall(
            "SELECT symbol, valid_from, valid_to, security_id FROM core.symbol_xref"
            " ORDER BY symbol, valid_from"
        )
    ]


def _open_period(db, symbol, security_id, valid_from, is_primary=True):
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, is_primary)"
        " VALUES (%s,%s,%s,%s)",
        (security_id, symbol, valid_from, is_primary),
    )


def _flag(db, security_id, check_name, record_key=None, resolved=False):
    import json

    db.execute(
        """
        INSERT INTO ops.data_quality_flag
            (security_id, check_name, severity, record_key, resolved_at)
        VALUES (%s, %s, 'warn', %s::jsonb, CASE WHEN %s THEN now() END)
        """,
        (
            security_id,
            check_name,
            json.dumps(record_key) if record_key else None,
            resolved,
        ),
    )


def _open_flags(db):
    return [(r["check_name"], r["record_key"], r["n"]) for r in db.fetchall("""
            SELECT check_name, record_key, count(*) AS n
              FROM ops.data_quality_flag WHERE resolved_at IS NULL
             GROUP BY 1, 2 ORDER BY 1, 2
            """)]


# ---------------------------------------------------------------------------
# 0015 -- one open ticker period
# ---------------------------------------------------------------------------


def test_0015_deletes_the_spurious_period_the_rename_bug_left(unconstrained):
    # Exactly what one rename plus one nightly security-master run used to leave:
    # the correct period at the change date, and a duplicate claiming 1900.
    db = unconstrained
    sid = _mk_security(db, "META")
    _open_period(db, "META", sid, dt.date(1900, 1, 1))
    _open_period(db, "META", sid, dt.date(2024, 6, 10))

    _run_migration(db, "0015")

    assert _xref(db) == [("META", dt.date(2024, 6, 10), None, sid)]


def test_0015_closes_rather_than_deletes_a_different_securitys_claim(unconstrained):
    # Not this bug's signature, and not ours to delete: a security_id is an
    # identity and the row records that it held the ticker. Bounded, not dropped.
    db = unconstrained
    first = _mk_security(db, "AAA", name="First")
    second = _mk_security(db, "BBB", name="Second")
    _open_period(db, "DUP", first, dt.date(2019, 1, 1))
    _open_period(db, "DUP", second, dt.date(2022, 3, 4))

    _run_migration(db, "0015")

    assert _xref(db) == [
        ("DUP", dt.date(2019, 1, 1), dt.date(2022, 3, 3), first),
        ("DUP", dt.date(2022, 3, 4), None, second),
    ]
    # Resolution is unchanged -- it already answered with the later period.
    assert repo.resolve_security_id(db, "DUP") == second


def test_0015_keeps_the_period_resolution_actually_answers_with(unconstrained):
    # The survivor has to be chosen the way the resolver chooses, which is
    # (is_primary DESC, valid_from DESC) -- not valid_from alone. Ranking on the
    # date would keep the later non-primary period and silently move the ticker to
    # a different security_id, which is the one thing this migration promises not
    # to do.
    db = unconstrained
    primary = _mk_security(db, "AAA", name="Primary holder")
    other = _mk_security(db, "BBB", name="Other")
    _open_period(db, "DUP", primary, dt.date(2020, 1, 1), is_primary=True)
    _open_period(db, "DUP", other, dt.date(2024, 6, 10), is_primary=False)
    before = repo.resolve_security_id(db, "DUP")

    _run_migration(db, "0015")

    assert before == primary, "precondition: is_primary wins over the later date"
    assert repo.resolve_security_id(db, "DUP") == before
    assert db.fetchall(
        "SELECT security_id FROM core.symbol_xref WHERE symbol='DUP'"
        " AND valid_to IS NULL"
    ) == [{"security_id": primary}]


def test_0015_closes_three_open_periods_without_overlapping_them(unconstrained):
    # Each superseded period closes the day before the NEXT one starts, not the day
    # before the survivor: closing them all against the survivor would leave
    # 1900-2023 and 2020-2023 both claiming 2020-2023.
    db = unconstrained
    a = _mk_security(db, "AAA", name="A")
    b = _mk_security(db, "BBB", name="B")
    c = _mk_security(db, "CCC", name="C")
    _open_period(db, "DUP", a, dt.date(1900, 1, 1))
    _open_period(db, "DUP", b, dt.date(2020, 1, 1))
    _open_period(db, "DUP", c, dt.date(2024, 6, 10))

    _run_migration(db, "0015")

    periods = _xref(db)
    assert periods == [
        ("DUP", dt.date(1900, 1, 1), dt.date(2019, 12, 31), a),
        ("DUP", dt.date(2020, 1, 1), dt.date(2024, 6, 9), b),
        ("DUP", dt.date(2024, 6, 10), None, c),
    ]
    # Stated as the invariant rather than just the dates: no two periods for one
    # ticker may cover the same day.
    assert db.fetchval("""
            SELECT count(*) FROM core.symbol_xref x JOIN core.symbol_xref y
              ON x.symbol = y.symbol AND x.valid_from < y.valid_from
             WHERE COALESCE(x.valid_to, 'infinity'::date) >= y.valid_from
            """) == 0


def test_0015_leaves_a_healthy_warehouse_alone(unconstrained):
    db = unconstrained
    sid = _mk_security(db, "AAA")
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to)"
        " VALUES (%s,'OLD','1900-01-01','2019-12-31')",
        (sid,),
    )
    _open_period(db, "FINE", sid, dt.date(2020, 1, 1))
    before = _xref(db)

    _run_migration(db, "0015")

    assert _xref(db) == before


def test_0015_then_refuses_a_second_open_period(unconstrained):
    db = unconstrained
    sid = _mk_security(db, "META")
    _open_period(db, "META", sid, dt.date(2024, 6, 10))

    _run_migration(db, "0015")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _open_period(db, "META", sid, dt.date(1900, 1, 1))


# ---------------------------------------------------------------------------
# 0016 -- one unresolved problem, one unresolved row
# ---------------------------------------------------------------------------


def test_0016_collapses_duplicates_and_keeps_the_earliest(unconstrained):
    db = unconstrained
    sid = _mk_security(db, "AAA")
    for _ in range(3):
        _flag(db, sid, "gap", {"trade_date": "2024-06-05"})
    _flag(db, sid, "gap", {"trade_date": "2024-06-06"})
    for _ in range(2):
        _flag(db, sid, "adjustment_failed")  # keyless: dedupes per security
    first_seen = db.fetchval(
        "SELECT min(detected_at) FROM ops.data_quality_flag WHERE check_name='gap'"
    )

    _run_migration(db, "0016")

    assert _open_flags(db) == [
        ("adjustment_failed", None, 1),
        ("gap", {"trade_date": "2024-06-05"}, 1),
        ("gap", {"trade_date": "2024-06-06"}, 1),
    ]
    # The earliest survives: detected_at is when the problem was FIRST seen, which
    # is the one fact a triage queue must not lose to a collapse.
    assert (
        db.fetchval(
            "SELECT detected_at FROM ops.data_quality_flag WHERE check_name='gap'"
            ' AND record_key = \'{"trade_date":"2024-06-05"}\''
        )
        == first_seen
    )


def test_0016_leaves_price_quarantines_repeating(unconstrained):
    # Their repetition IS the counter: count_price_quarantines reads it to decide
    # when a persistently-bad bar stops holding the ingestion watermark. Collapsing
    # them would reset it to 1 and hold the watermark behind that bar forever.
    db = unconstrained
    sid = _mk_security(db, "AAA")
    for _ in range(4):
        _flag(db, sid, "price_bad_ohlc", {"date": "2024-06-05"})

    _run_migration(db, "0016")

    assert repo.count_price_quarantines(db, sid, "2024-06-05") == 4
    # ...and a fifth is still allowed to land afterwards.
    _flag(db, sid, "price_bad_ohlc", {"date": "2024-06-05"})
    assert repo.count_price_quarantines(db, sid, "2024-06-05") == 5


def test_0016_leaves_resolved_flags_alone(unconstrained):
    # A resolved flag is a closed record, not a duplicate of the open one.
    db = unconstrained
    sid = _mk_security(db, "AAA")
    _flag(db, sid, "gap", {"trade_date": "2024-06-05"}, resolved=True)
    _flag(db, sid, "gap", {"trade_date": "2024-06-05"})

    _run_migration(db, "0016")

    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == 2


def test_0016_then_refuses_a_second_open_flag_for_one_condition(unconstrained):
    db = unconstrained
    sid = _mk_security(db, "AAA")
    _flag(db, sid, "symbol_change_conflict", {"old": "FB", "new": "META"})

    _run_migration(db, "0016")

    with pytest.raises(psycopg.errors.UniqueViolation):
        # Same condition, key written with its fields in the other order: jsonb
        # equality is semantic, so the constraint sees one condition, as the
        # write-path probe does.
        _flag(db, sid, "symbol_change_conflict", {"new": "META", "old": "FB"})


def test_0016_still_admits_a_new_occurrence_and_a_recurrence(unconstrained):
    db = unconstrained
    sid = _mk_security(db, "AAA")
    _flag(db, sid, "gap", {"trade_date": "2024-06-05"})
    _run_migration(db, "0016")

    _flag(db, sid, "gap", {"trade_date": "2024-07-01"})  # a different gap date
    db.execute("UPDATE ops.data_quality_flag SET resolved_at = now()")
    _flag(db, sid, "gap", {"trade_date": "2024-06-05"})  # resolved, then recurred

    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE resolved_at IS NULL"
        )
        == 1
    )
    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == 3


# ---------------------------------------------------------------------------
# The one runtime path that merges two securities' flags
# ---------------------------------------------------------------------------


def test_folding_a_stub_drops_the_flags_the_survivor_already_carries(db):
    # Folding is the one moment two securities' flags become one security's, so it
    # is the one place a repoint can land a second open flag on a condition that
    # already has one -- an inflated queue before 0016, a UniqueViolation that
    # would abort the rename sweep after it.
    survivor = _mk_security(db, "META", name="Meta")
    stub = _mk_security(db, "META2", name="Meta stub")
    for sid in (survivor, stub):
        _flag(db, sid, "security_beta_out_of_range", {"symbol": "META"})
    _flag(db, stub, "gap", {"trade_date": "2024-06-05"})  # only the stub has this

    assert repo.fold_empty_security(db, victim_id=stub, survivor_id=survivor)

    assert _open_flags(db) == [
        ("gap", {"trade_date": "2024-06-05"}, 1),
        ("security_beta_out_of_range", {"symbol": "META"}, 1),
    ]
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s", (stub,)
        )
        == 0
    )


def test_folding_a_stub_keeps_both_securities_price_quarantines(db):
    # A stub CAN carry price_* flags -- a quarantined bar writes a flag but no price
    # row, so security_has_history still calls the row empty -- and those must
    # survive the fold intact or the watermark hold is silently shortened.
    survivor = _mk_security(db, "META", name="Meta")
    stub = _mk_security(db, "META2", name="Meta stub")
    for sid in (survivor, stub):
        for _ in range(2):
            _flag(db, sid, "price_bad_ohlc", {"date": "2024-06-05"})

    assert repo.fold_empty_security(db, victim_id=stub, survivor_id=survivor)

    assert repo.count_price_quarantines(db, survivor, "2024-06-05") == 4
