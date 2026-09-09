"""Accepting a condition stops the check asking about it again.

The whole of this feature is one contrast, and the first two tests are it: resolve
a condition whose defect is still in the data and the next run writes it back;
accept it and the next run does not. Everything else here guards the edges of that.
"""

from __future__ import annotations

import datetime as dt

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import checks

pytestmark = pytest.mark.integration


def _mk(db, symbol="ACPT"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _bars_with_a_hole(db, sid):
    """2024-06-03, -04 and -06; 2024-06-05 is an open session and is missing."""
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            }
            for d in (dt.date(2024, 6, 3), dt.date(2024, 6, 4), dt.date(2024, 6, 6))
        ],
    )


def _open_gap_ids(db, sid):
    return [
        int(r["dq_flag_id"])
        for r in db.fetchall(
            "SELECT dq_flag_id FROM ops.data_quality_flag WHERE check_name='gap' "
            "AND security_id = %s AND resolved_at IS NULL",
            (sid,),
        )
    ]


def _all_gap_rows(db, sid):
    return int(
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE check_name='gap' "
            "AND security_id = %s",
            (sid,),
        )
    )


# ---------------------------------------------------------------------------
# The contrast this exists for
# ---------------------------------------------------------------------------


def test_a_resolved_condition_comes_back(db):
    """Not a bug -- the documented behaviour, and the reason accept is needed.

    Resolution is judged against the data. The defect is still there, so the check
    is right to write it again.
    """
    sid = _mk(db, "ACRES")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (first,) = _open_gap_ids(db, sid)

    repo.resolve_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(first,)),
        note="tidying up",
        resolved_by="tester",
    )
    assert _open_gap_ids(db, sid) == []

    checks.check_gaps(db, exchange_code="NASDAQ")
    reopened = _open_gap_ids(db, sid)
    assert len(reopened) == 1
    assert reopened[0] != first  # a new row, not the old one


def test_an_accepted_condition_does_not(db):
    sid = _mk(db, "ACACC")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (first,) = _open_gap_ids(db, sid)

    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(first,)),
        note="vendor has no bar for this session and never will",
        accepted_by="tester",
    )
    assert _open_gap_ids(db, sid) == []

    checks.check_gaps(db, exchange_code="NASDAQ")
    assert _open_gap_ids(db, sid) == []
    assert _all_gap_rows(db, sid) == 1  # nothing new was written


def test_acceptance_also_blocks_the_row_at_a_time_path(db):
    """`add_dq_flag_once` is a separate probe from the set-based checks.

    It has its own guard, matched to its own partial index, so it needs its own
    test -- a fix applied only to fafnir.dq.checks would leave every loader-side
    caller re-writing accepted conditions.
    """
    sid = _mk(db, "ACROW")
    key = {"trade_date": "2024-06-05"}
    repo.add_dq_flag_once(
        db, check_name="gap", security_id=sid, record_key=key, severity="warn"
    )
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="permanent",
        accepted_by="tester",
    )

    wrote = repo.add_dq_flag_once(
        db, check_name="gap", security_id=sid, record_key=key, severity="warn"
    )
    assert wrote is False
    assert _all_gap_rows(db, sid) == 1


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_accept_refuses_without_a_note(db):
    sid = _mk(db, "ACNON")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)

    for empty in ("", "   "):
        with pytest.raises(ValueError, match="needs a note"):
            repo.accept_dq_flags(
                db,
                repo.DqFilter(state="open", flag_ids=(flag,)),
                note=empty,
                accepted_by="tester",
            )
    assert _open_gap_ids(db, sid) == [flag]


def test_accept_refuses_an_unnarrowed_filter(db):
    with pytest.raises(ValueError, match="refusing to accept the whole queue"):
        repo.accept_dq_flags(
            db, repo.DqFilter(state="open"), note="everything", accepted_by="tester"
        )


def test_an_accepted_row_is_also_resolved(db):
    """0024's CHECK. Everything written before this migration reads
    `resolved_at IS NULL` as "in the queue" and must stay correct."""
    sid = _mk(db, "ACRSV")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="permanent",
        accepted_by="tester",
    )
    row = db.fetchone(
        "SELECT resolved_at, accepted_at, accepted_by, accepted_note "
        "FROM ops.data_quality_flag WHERE dq_flag_id = %s",
        (flag,),
    )
    assert row["resolved_at"] is not None
    assert row["accepted_at"] is not None
    assert row["accepted_by"] == "tester"
    assert row["accepted_note"] == "permanent"


def test_the_check_constraint_refuses_acceptance_without_resolution(db):
    sid = _mk(db, "ACCK")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    with pytest.raises(Exception):
        db.execute(
            "UPDATE ops.data_quality_flag SET accepted_at = now() "
            "WHERE dq_flag_id = %s",
            (flag,),
        )


# ---------------------------------------------------------------------------
# Visibility and undo
# ---------------------------------------------------------------------------


def _ids(db, **kwargs):
    return {
        int(r["dq_flag_id"])
        for r in repo.list_dq_flags(db, repo.DqFilter(**kwargs), limit=1000)
    }


def test_the_listing_carries_the_acceptance_provenance(db):
    """0024's down migration tells the operator to keep
    `dq list --state accepted --detail --json` before dropping the columns, because
    that is the only copy the decisions will have. That is only true if the listing
    actually carries them."""
    sid = _mk(db, "ACPROV")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="vendor has no bar for this session",
        accepted_by="tester",
    )

    (row,) = repo.list_dq_flags(db, repo.DqFilter(state="accepted"), limit=10)
    assert row["accepted_at"] is not None
    assert row["accepted_by"] == "tester"
    assert row["accepted_note"] == "vendor has no bar for this session"


def test_accepting_a_quarantine_flag_does_not_suppress_it(db):
    """The one family acceptance cannot suppress, pinned so the claim stays honest.

    The loader writes `price_<reason>` on a REJECTED bar through `add_dq_flag`,
    which has no dedupe probe by design: `count_price_quarantines` counts the
    repeats to decide when a persistently-bad bar has held the watermark long
    enough, so a probe there would freeze that counter behind the bar forever.
    Accepting clears the backlog; it does not stop the next read re-flagging it.
    """
    sid = _mk(db, "ACQTN")
    key = {"symbol": "ACQTN", "date": "2024-06-05"}
    repo.add_dq_flag(
        db,
        check_name="price_subresolution_price",
        severity="warn",
        security_id=sid,
        record_key=key,
    )
    open_ids = [
        int(r["dq_flag_id"])
        for r in db.fetchall(
            "SELECT dq_flag_id FROM ops.data_quality_flag WHERE security_id = %s "
            "AND check_name = 'price_subresolution_price' AND resolved_at IS NULL",
            (sid,),
        )
    ]
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=tuple(open_ids)),
        note="below the quantize cliff",
        accepted_by="tester",
    )

    # The next run re-reads the same bar and re-validates it.
    repo.add_dq_flag(
        db,
        check_name="price_subresolution_price",
        severity="warn",
        security_id=sid,
        record_key=key,
    )
    still_open = db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s "
        "AND check_name = 'price_subresolution_price' AND resolved_at IS NULL",
        (sid,),
    )
    assert still_open == 1, "the quarantine flag is expected back: see the docstring"
    # And the budget still sees every attempt, accepted or not -- which is the
    # reason the probe is not there.
    assert repo.count_price_quarantines(db, sid, "2024-06-05") == 2


def test_accepted_flags_are_listed_as_accepted_not_as_resolved(db):
    """Suppressed is not the same as invisible: an operator must be able to ask
    what has been agreed away, without it hiding among ordinary resolutions."""
    sid = _mk(db, "ACLIST")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="permanent",
        accepted_by="tester",
    )

    assert flag in _ids(db, state="accepted", checks=("gap",))
    assert flag not in _ids(db, state="resolved", checks=("gap",))
    assert flag not in _ids(db, state="open", checks=("gap",))
    assert flag in _ids(db, state="all", checks=("gap",))


def test_reopen_takes_the_acceptance_back(db):
    sid = _mk(db, "ACREOP")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="permanent",
        accepted_by="tester",
    )

    reopened, conflicted = repo.reopen_dq_flags(db, [flag])
    assert reopened == [flag]
    assert conflicted == []
    row = db.fetchone(
        "SELECT resolved_at, accepted_at, accepted_by, accepted_note "
        "FROM ops.data_quality_flag WHERE dq_flag_id = %s",
        (flag,),
    )
    assert row["accepted_at"] is None
    assert row["accepted_by"] is None
    assert row["accepted_note"] is None
    assert row["resolved_at"] is None


def test_a_reopened_condition_is_checked_again(db):
    """The undo has to restore the check's behaviour, not just the columns."""
    sid = _mk(db, "ACRECH")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)
    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="permanent",
        accepted_by="tester",
    )
    repo.reopen_dq_flags(db, [flag])

    checks.check_gaps(db, exchange_code="NASDAQ")
    assert _open_gap_ids(db, sid) == [flag]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, dsn):
        self.dsn = dsn


def _run(db, args):
    return CliRunner().invoke(cli.dq_accept, args, obj={"config": _Cfg(db.dsn)})


def test_every_dq_state_has_a_label():
    """`--state` takes its choices from DQ_STATES, and every listing renders
    `_dq_label`. A state in one and not the other is a KeyError on the command the
    feature tells operators to use -- no database needed to catch it."""
    for state in repo.DQ_STATES:
        assert cli._dq_label(repo.DqFilter(state=state))


def test_dq_list_renders_the_accepted_state(db):
    """`dq list --state accepted` is named in the command's own output, in its
    help, and in 0024's down migration as the way to keep the decisions. It has to
    render in text mode, not just under --json, which takes a different path."""
    sid = _mk(db, "ACRENDER")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)

    empty = CliRunner().invoke(
        cli.dq_list, ["--state", "accepted"], obj={"config": _Cfg(db.dsn)}
    )
    assert empty.exit_code == 0, empty.output

    repo.accept_dq_flags(
        db,
        repo.DqFilter(state="open", flag_ids=(flag,)),
        note="vendor has no bar for this session",
        accepted_by="tester",
    )
    for args in (["--state", "accepted"], ["--state", "accepted", "--detail"]):
        result = CliRunner().invoke(cli.dq_list, args, obj={"config": _Cfg(db.dsn)})
        assert result.exit_code == 0, result.output
        # The summary heads with "Accepted DQ flags"; the detail page closes with
        # "... (accepted)". Either way the state has to reach the output.
        assert "accepted" in result.output.lower()


def test_accept_dry_run_changes_nothing(db):
    sid = _mk(db, "ACDRY")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")
    (flag,) = _open_gap_ids(db, sid)

    result = _run(db, ["--check", "gap", "--symbol", "ACDRY", "-m", "x", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would be accepted" in result.output
    assert _open_gap_ids(db, sid) == [flag]


def test_accept_command_requires_a_note(db):
    result = _run(db, ["--check", "gap", "--symbol", "ACDRY", "--yes"])
    assert result.exit_code != 0


def test_accept_command_refuses_an_unfiltered_run(db):
    result = _run(db, ["-m", "everything", "--yes"])
    assert result.exit_code != 0
    assert "Refusing to accept the whole queue" in result.output


def test_accept_command_closes_and_suppresses(db):
    sid = _mk(db, "ACCMD")
    _bars_with_a_hole(db, sid)
    checks.check_gaps(db, exchange_code="NASDAQ")

    result = _run(
        db,
        [
            "--check",
            "gap",
            "--symbol",
            "ACCMD",
            "--by",
            "tester",
            "-m",
            "vendor has no bar for this session",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _open_gap_ids(db, sid) == []

    checks.check_gaps(db, exchange_code="NASDAQ")
    assert _open_gap_ids(db, sid) == []
