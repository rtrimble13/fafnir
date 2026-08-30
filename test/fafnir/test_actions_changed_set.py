"""The upsert reports whether it actually wrote anything (needs FAFNIR_TEST_DSN).

`fafnir adjust --changed` recomputes the securities a corporate-actions run touched
rather than every security that has ever had an action. That only works if "touched"
means *changed*: an unconditional ON CONFLICT DO UPDATE rewrites every row it sees,
so a nightly re-load would name the whole universe as changed and the incremental
path would be a full recompute wearing a different flag.

The mechanism is a WHERE on the DO UPDATE, and these pin both halves of it -- the
no-op that must not be counted, and the real amendment that must be.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

EX = dt.date(2026, 8, 15)


def _mk_security(db, symbol="KO"):
    repo.ensure_exchange(db, "NYSE", "New York Stock Exchange", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name="Test Co",
        asset_type="equity",
        exchange_code="NYSE",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _run_id(db, endpoint="corporate-actions"):
    return int(
        db.fetchval(
            """
            INSERT INTO ops.ingestion_run (source, endpoint, status, started_at)
            VALUES ('fmp', %s, 'started', now())
            RETURNING ingestion_run_id
            """,
            (endpoint,),
        )
    )


def _dividend(db, sid, amount, *, run_id=None, **kw):
    return repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=EX,
        dividend_amount=amount,
        ingestion_run_id=run_id,
        **kw,
    )


@pytest.mark.integration
def test_a_new_action_is_reported_as_changed(db):
    sid = _mk_security(db)
    assert _dividend(db, sid, 0.48) is True


@pytest.mark.integration
def test_re_loading_the_same_action_is_not_a_change(db):
    """The case that decides whether the incremental path is worth anything.

    Every night's re-load sees the same history; if that counted as a change, the
    changed-set would be the whole universe.
    """
    sid = _mk_security(db)
    _dividend(db, sid, 0.48)

    assert _dividend(db, sid, 0.48) is False
    assert _dividend(db, sid, 0.48) is False


@pytest.mark.integration
def test_the_same_amount_at_a_different_scale_is_not_a_change(db):
    """0.48 and 0.480000 are one dividend.

    The column is NUMERIC(20, 6) and the feed's value arrives as a float, so a
    comparison done on text or on the raw parameter would call this an amendment
    every single night.
    """
    sid = _mk_security(db)
    _dividend(db, sid, 0.48)

    assert _dividend(db, sid, 0.4800000) is False


@pytest.mark.integration
def test_an_amended_amount_is_a_change(db):
    sid = _mk_security(db)
    _dividend(db, sid, 0.48)

    assert _dividend(db, sid, 0.52) is True
    assert db.fetchval(
        "SELECT dividend_amount FROM core.corporate_action WHERE security_id = %s",
        (sid,),
    ) == Decimal("0.52")


@pytest.mark.integration
def test_an_amended_payment_date_is_a_change(db):
    """A date moving matters even when the money does not: it is still a restatement."""
    sid = _mk_security(db)
    _dividend(db, sid, 0.48, payment_date=dt.date(2026, 9, 1))

    assert _dividend(db, sid, 0.48, payment_date=dt.date(2026, 9, 8)) is True


@pytest.mark.integration
def test_a_no_op_load_leaves_lineage_alone(db):
    """loaded_at / ingestion_run_id name the run that last CHANGED the row.

    If an unchanged re-load bumped them, `securities_changed_by_run` would return
    every security the run merely looked at.
    """
    sid = _mk_security(db)
    first = _run_id(db)
    _dividend(db, sid, 0.48, run_id=first)
    before = db.fetchone(
        "SELECT ingestion_run_id, loaded_at FROM core.corporate_action "
        "WHERE security_id = %s",
        (sid,),
    )

    second = _run_id(db)
    assert _dividend(db, sid, 0.48, run_id=second) is False

    after = db.fetchone(
        "SELECT ingestion_run_id, loaded_at FROM core.corporate_action "
        "WHERE security_id = %s",
        (sid,),
    )
    assert after["ingestion_run_id"] == before["ingestion_run_id"] == first
    assert after["loaded_at"] == before["loaded_at"]


@pytest.mark.integration
def test_a_real_change_restamps_lineage_to_the_new_run(db):
    sid = _mk_security(db)
    first = _run_id(db)
    _dividend(db, sid, 0.48, run_id=first)

    second = _run_id(db)
    assert _dividend(db, sid, 0.52, run_id=second) is True

    assert repo.securities_changed_by_run(db, second) == [sid]
    assert repo.securities_changed_by_run(db, first) == []


@pytest.mark.integration
def test_the_changed_set_names_only_the_securities_that_moved(db):
    """What `fafnir adjust --changed` actually recomputes."""
    moved = _mk_security(db, "KO")
    still = _mk_security(db, "JNJ")
    run_one = _run_id(db)
    _dividend(db, moved, 0.48, run_id=run_one)
    _dividend(db, still, 1.19, run_id=run_one)

    run_two = _run_id(db)
    _dividend(db, moved, 0.52, run_id=run_two)  # amended
    _dividend(db, still, 1.19, run_id=run_two)  # unchanged

    assert repo.securities_changed_by_run(db, run_two) == [moved]


@pytest.mark.integration
def test_latest_run_id_finds_the_most_recent_actions_run(db):
    _run_id(db, "prices")
    first = _run_id(db, "corporate-actions")
    second = _run_id(db, "corporate-actions")
    _run_id(db, "prices")

    assert second > first
    assert repo.latest_run_id(db, "fmp", "corporate-actions") == second


# ---------------------------------------------------------------------------
# The watermark queries the sweep depends on
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_security_with_no_actions_watermark_is_offered_for_a_first_load(db):
    sid = _mk_security(db)

    pending = repo.securities_without_actions_watermark(db, "corporate-actions")

    assert [p["security_id"] for p in pending] == [sid]


@pytest.mark.integration
def test_a_watermarked_security_is_not_offered_again(db):
    sid = _mk_security(db)
    repo.set_watermark(db, "fmp", "corporate-actions", dt.date(2026, 8, 30), sid)

    assert repo.securities_without_actions_watermark(db, "corporate-actions") == []


@pytest.mark.integration
def test_the_whole_endpoint_watermark_is_independent_of_every_security(db):
    """security_id = 0 is the sweep's own row, and migration 0004 already allows it.

    It must not collide with any security's watermark on the same source, and it
    must survive alongside them -- that is the entire reason ADR 0007 needs no
    migration.
    """
    sid = _mk_security(db)
    repo.set_watermark(db, "fmp", "corporate-actions", dt.date(2026, 8, 20), sid)
    repo.set_watermark(db, "fmp", "corporate-actions-calendar", dt.date(2026, 8, 30), 0)

    assert repo.get_watermark(db, "fmp", "corporate-actions", sid) == dt.date(
        2026, 8, 20
    )
    assert repo.get_watermark(db, "fmp", "corporate-actions-calendar", 0) == dt.date(
        2026, 8, 30
    )


@pytest.mark.integration
def test_the_reconciliation_slice_covers_the_universe_exactly_once(db):
    """Every security is checked once per cycle -- the bound a random sample cannot give."""
    ids = {_mk_security(db, f"SYM{i}") for i in range(12)}

    seen: list[int] = []
    for bucket in range(5):
        seen += [
            s["security_id"]
            for s in repo.actions_reconciliation_slice(db, buckets=5, bucket=bucket)
        ]

    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


@pytest.mark.integration
def test_a_delisted_security_is_left_out_of_the_nightly_universe(db):
    """It can never have another corporate action, so re-polling it is pure waste."""
    live = _mk_security(db, "LIVE")
    gone = _mk_security(db, "GONE")
    db.execute(
        "UPDATE core.security SET is_actively_trading = FALSE, "
        "delisted_date = %s WHERE security_id = %s",
        (dt.date(2026, 1, 5), gone),
    )

    assert [s["security_id"] for s in repo.universe_securities(db)] == [live]
    assert sorted(
        s["security_id"] for s in repo.universe_securities(db, include_inactive=True)
    ) == sorted([live, gone])
