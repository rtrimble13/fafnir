"""Repairing the two rename conflicts a loader will not touch (needs FAFNIR_TEST_DSN).

`ingest symbol-changes` applies what it safely can and records the rest as
`conflict`, which it retries every night. Two kinds never clear on their own:

  * the rename is real, but a security-master load that ran before the sweep minted
    the new ticker as a second security and then filled it with bars -- two rows,
    one company, and `fold_empty_security` refuses because neither is empty;
  * the rename is not real, and both tickers stay live, so no ordering of the sweep
    will ever free the target.

`fafnir security merge-rename` and `fafnir security dismiss-rename` are the two
answers. These tests pin the properties that make them safe to point at production:
a merge proves identity from the vendor's identifiers before deleting anything and
refuses when the overlap disagrees, a dismissal reaches a terminal status so the
sweep stops, and both leave the DQ queue closed rather than re-flagging tonight.

Real database, not fakes: every guard here is a SQL predicate, a partitioned-table
insert or a partial unique index.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

CHANGE_DATE = dt.date(2024, 6, 10)


class _Cfg:
    def __init__(self, dsn):
        self.dsn = dsn


def _run(db, command, args, **kwargs):
    """Invoke a `fafnir security` subcommand against the test database.

    The subcommand is invoked directly rather than through `cli.main`, whose group
    callback would rebuild the config from ~/.fafnirrc and point this at whatever
    warehouse the machine running the suite happens to have.
    """
    return CliRunner().invoke(
        command, list(args), obj={"config": _Cfg(db.dsn)}, **kwargs
    )


def _text(result) -> str:
    try:
        return result.output + (result.stderr or "")
    except ValueError:
        return result.output


def _mk_security(db, symbol, *, cusip=None, isin=None, cik=None, name=None):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name or f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
        cusip=cusip,
        isin=isin,
        cik=cik,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _bars(db, sid, days, *, close=100.0):
    """`days` consecutive sessions from 2024-06-03, at a flat price."""
    start = dt.date(2024, 6, 3)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": start + dt.timedelta(days=i),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            }
            for i in range(days)
        ],
    )


def _bar_dates(db, sid):
    return [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM core.daily_price WHERE security_id = %s "
            "ORDER BY trade_date",
            (sid,),
        )
    ]


@pytest.fixture()
def duplicated(db):
    """The production shape: GREE renamed to VIP, but VIP was minted separately.

    The survivor holds the long history under the old ticker; the duplicate holds a
    shorter run under the new one, overlapping it -- the vendor serves a renamed
    ticker's full continuous series, so the duplicate arrives with bars predating
    the rename. Identity matches, because it is one company.
    """
    survivor = _mk_security(
        db, "GREE", cusip="39531G308", isin="US39531G3083", cik="0001844971"
    )
    victim = _mk_security(
        db, "VIP", cusip="39531G308", isin="US39531G3083", cik="0001844971"
    )
    _bars(db, survivor, 10)
    _bars(db, victim, 14)  # 10 shared sessions, 4 the survivor does not have
    repo.record_symbol_change(
        db,
        old_symbol="GREE",
        new_symbol="VIP",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_CONFLICT,
        security_id=survivor,
    )
    repo.add_dq_flag_once(
        db,
        check_name="symbol_change_conflict",
        severity="error",
        security_id=survivor,
        record_key={"old_symbol": "GREE", "new_symbol": "VIP"},
    )
    return survivor, victim


# ---------------------------------------------------------------------------
# compare_securities -- the evidence, before anything is touched
# ---------------------------------------------------------------------------


def test_a_matching_pair_has_no_blockers(db, duplicated):
    survivor, victim = duplicated
    plan = repo.compare_securities(db, survivor_id=survivor, victim_id=victim)
    assert plan.blockers == []
    assert plan.shared_days == 10
    assert plan.victim_only_bars == 4
    assert plan.disagreeing_days == 0


def test_a_differing_cusip_blocks_the_merge(db):
    """The ODVWZ case: same registrant, different instrument.

    CIK identifies the SEC filer and survives a rename, so a warrant and its common
    stock share one. CUSIP is the instrument, which is the grain a merge works at --
    without this guard the two would be merged on the strength of the CIK alone.
    """
    survivor = _mk_security(db, "ODVWZ", cusip="68828E239", cik="0001431852")
    victim = _mk_security(db, "OGGWZ", cusip="68827X113", cik="0001431852")
    _bars(db, survivor, 5)
    _bars(db, victim, 5)
    plan = repo.compare_securities(db, survivor_id=survivor, victim_id=victim)
    assert any("cusip differs" in b for b in plan.blockers)


def test_a_missing_identifier_is_not_a_mismatch(db):
    """FMP leaves cik empty on most ETFs; refusing on that would block real merges."""
    survivor = _mk_security(db, "TUGN", cusip="53656F169", cik="0001683471")
    victim = _mk_security(db, "SEPQ", cusip="53656F169", cik=None)
    _bars(db, survivor, 5)
    _bars(db, victim, 5)
    plan = repo.compare_securities(db, survivor_id=survivor, victim_id=victim)
    assert plan.blockers == []


def test_disagreeing_overlap_blocks_the_merge(db):
    """One side is discarded on the overlap, so the two copies have to agree first."""
    survivor = _mk_security(db, "AAA", cusip="X")
    victim = _mk_security(db, "BBB", cusip="X")
    _bars(db, survivor, 5, close=100.0)
    _bars(db, victim, 5, close=101.0)
    plan = repo.compare_securities(db, survivor_id=survivor, victim_id=victim)
    assert plan.disagreeing_days == 5
    assert any("disagree on OHLC" in b for b in plan.blockers)
    assert plan.disagreement_sample, "the operator needs to see which sessions"


def test_volume_alone_does_not_block(db):
    """A restated volume across a rename costs no price accuracy -- report, not refuse."""
    survivor = _mk_security(db, "AAA", cusip="X")
    victim = _mk_security(db, "BBB", cusip="X")
    _bars(db, survivor, 3)
    _bars(db, victim, 3)
    db.execute(
        "UPDATE core.daily_price SET volume = 999 WHERE security_id = %s", (victim,)
    )
    plan = repo.compare_securities(db, survivor_id=survivor, victim_id=victim)
    assert plan.blockers == []
    assert plan.volume_only_disagreements == 3


# ---------------------------------------------------------------------------
# merge_security -- what actually moves
# ---------------------------------------------------------------------------


def test_merge_keeps_the_union_of_the_bars(db, duplicated):
    survivor, victim = duplicated
    before = set(_bar_dates(db, survivor)) | set(_bar_dates(db, victim))
    repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert set(_bar_dates(db, survivor)) == before
    assert _bar_dates(db, victim) == []


def test_merge_deletes_the_victim_and_keeps_the_survivors_id(db, duplicated):
    survivor, victim = duplicated
    repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.security WHERE security_id = %s", (victim,)
        )
        == 0
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.security WHERE security_id = %s", (survivor,)
        )
        == 1
    )


def test_merge_refuses_a_mismatched_pair_and_changes_nothing(db):
    survivor = _mk_security(db, "ODVWZ", cusip="68828E239")
    victim = _mk_security(db, "OGGWZ", cusip="68827X113")
    _bars(db, survivor, 5)
    _bars(db, victim, 5)
    with pytest.raises(repo.MergeRefused) as excinfo:
        repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert excinfo.value.plan.identity_mismatches
    assert len(_bar_dates(db, victim)) == 5, "a refused merge must not move a row"


def test_force_overrides_the_guard(db):
    """The escape hatch exists, but only as a deliberate second decision."""
    survivor = _mk_security(db, "ODVWZ", cusip="68828E239")
    victim = _mk_security(db, "OGGWZ", cusip="68827X113")
    _bars(db, survivor, 5)
    _bars(db, victim, 5)
    repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.security WHERE security_id = %s", (victim,)
        )
        == 0
    )


def test_merge_drops_the_victims_duplicate_open_flags(db, duplicated):
    """ux_dq_flag_open_condition (0016) makes a naive repoint abort the whole merge."""
    survivor, victim = duplicated
    for sid in (survivor, victim):
        repo.add_dq_flag_once(
            db,
            check_name="gap",
            security_id=sid,
            record_key={"trade_date": "2024-06-04"},
        )
    repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE check_name = 'gap' "
            "AND security_id = %s AND resolved_at IS NULL",
            (survivor,),
        )
        == 1
    )


def test_merge_drops_colliding_actions_rather_than_aborting(db, duplicated):
    """core.corporate_action carries UNIQUE (security_id, action_type, ex_date)."""
    survivor, victim = duplicated
    for sid in (survivor, victim):
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="dividend",
            ex_date=dt.date(2024, 6, 5),
            dividend_amount=0.25,
        )
    report = repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert report.actions_dropped == 1
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.corporate_action WHERE security_id = %s",
            (survivor,),
        )
        == 1
    )


def test_merge_takes_the_later_watermark(db, duplicated):
    """The duplicate is the row the daily load has been feeding, so it is ahead."""
    survivor, victim = duplicated
    repo.set_watermark(db, "fmp", "prices", dt.date(2024, 6, 12), security_id=survivor)
    repo.set_watermark(db, "fmp", "prices", dt.date(2024, 6, 16), security_id=victim)
    repo.merge_security(db, victim_id=victim, survivor_id=survivor)
    assert repo.get_watermark(db, "fmp", "prices", survivor) == dt.date(2024, 6, 16)


def test_merge_refuses_to_eat_itself(db, duplicated):
    survivor, _ = duplicated
    with pytest.raises(ValueError):
        repo.compare_securities(db, survivor_id=survivor, victim_id=survivor)


# ---------------------------------------------------------------------------
# fafnir security merge-rename
# ---------------------------------------------------------------------------


def test_merge_rename_moves_the_ticker_and_closes_the_flag(db, duplicated):
    survivor, victim = duplicated
    result = _run(db, cli.security_merge_rename, ["GREE", "VIP", "--yes"])
    assert result.exit_code == 0, _text(result)

    assert repo.active_security_for_symbol(db, "VIP") == survivor
    assert (
        db.fetchval("SELECT status FROM core.symbol_change WHERE old_symbol = 'GREE'")
        == repo.CHANGE_APPLIED
    )
    open_flags = repo.list_dq_flags(
        db, repo.DqFilter(checks=("symbol_change_conflict",)), limit=10
    )
    assert open_flags == [], "the conflict has to leave the queue, not just the audit"


def test_merge_rename_dry_run_changes_nothing(db, duplicated):
    survivor, victim = duplicated
    result = _run(db, cli.security_merge_rename, ["GREE", "VIP", "--dry-run"])
    assert result.exit_code == 0, _text(result)
    assert "Dry run" in _text(result)
    assert repo.active_security_for_symbol(db, "VIP") == victim
    assert len(_bar_dates(db, victim)) == 14


def test_merge_rename_refuses_a_mismatched_pair(db):
    survivor = _mk_security(db, "ODVWZ", cusip="68828E239")
    victim = _mk_security(db, "OGGWZ", cusip="68827X113")
    _bars(db, survivor, 5)
    _bars(db, victim, 5)
    repo.record_symbol_change(
        db,
        old_symbol="ODVWZ",
        new_symbol="OGGWZ",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_CONFLICT,
        security_id=survivor,
    )
    result = _run(db, cli.security_merge_rename, ["ODVWZ", "OGGWZ", "--yes"])
    assert result.exit_code != 0
    assert "BLOCKER" in _text(result)
    assert len(_bar_dates(db, victim)) == 5


def test_merge_rename_sends_a_free_ticker_back_to_the_sweep(db):
    """No duplicate means no conflict; inventing a merge with one side helps nobody."""
    _mk_security(db, "GREE")
    result = _run(db, cli.security_merge_rename, ["GREE", "VIP", "--yes"])
    assert result.exit_code != 0
    assert "ingest symbol-changes" in _text(result)


def test_merge_rename_defaults_the_date_from_the_recorded_conflict(db, duplicated):
    survivor, _ = duplicated
    result = _run(db, cli.security_merge_rename, ["GREE", "VIP", "--yes"])
    assert result.exit_code == 0, _text(result)
    closed = db.fetchone(
        "SELECT valid_to FROM core.symbol_xref WHERE symbol = 'GREE' "
        "AND security_id = %s",
        (survivor,),
    )
    assert closed["valid_to"] == CHANGE_DATE - dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# fafnir security dismiss-rename
# ---------------------------------------------------------------------------


@pytest.fixture()
def bogus_rename(db):
    """The VBX/USSX shape: two live securities, a rename that never happened."""
    vbx = _mk_security(db, "VBX")
    ussx = _mk_security(db, "USSX")
    _bars(db, vbx, 5)
    _bars(db, ussx, 5)
    repo.record_symbol_change(
        db,
        old_symbol="VBX",
        new_symbol="USSX",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_CONFLICT,
        security_id=vbx,
    )
    repo.add_dq_flag_once(
        db,
        check_name="symbol_change_conflict",
        severity="error",
        security_id=vbx,
        record_key={"old_symbol": "VBX", "new_symbol": "USSX"},
    )
    return vbx, ussx


def test_dismiss_reaches_a_terminal_status(db, bogus_rename):
    result = _run(
        db,
        cli.security_dismiss_rename,
        ["VBX", "USSX", "-m", "feed emitted both ways", "--yes"],
    )
    assert result.exit_code == 0, _text(result)
    status = db.fetchval(
        "SELECT status FROM core.symbol_change WHERE old_symbol = 'VBX'"
    )
    assert status == repo.CHANGE_DISMISSED
    assert status in repo.TERMINAL_CHANGE_STATUSES, "or the sweep keeps retrying it"


def test_dismiss_keeps_the_reasoning_on_the_row(db, bogus_rename):
    _run(
        db,
        cli.security_dismiss_rename,
        ["VBX", "USSX", "-m", "both still trading", "--by", "ada", "--yes"],
    )
    detail = db.fetchval(
        "SELECT detail FROM core.symbol_change WHERE old_symbol = 'VBX'"
    )
    assert detail["dismissed_note"] == "both still trading"
    assert detail["dismissed_by"] == "ada"
    assert detail["dismissed_at"]


def test_dismiss_leaves_the_review_queue(db, bogus_rename):
    _run(db, cli.security_dismiss_rename, ["VBX", "USSX", "-m", "bad row", "--yes"])
    assert repo.count_unapplied_symbol_changes(db) == 0
    assert (
        repo.list_dq_flags(
            db, repo.DqFilter(checks=("symbol_change_conflict",)), limit=10
        )
        == []
    )


def test_dismiss_changes_no_price_data(db, bogus_rename):
    vbx, ussx = bogus_rename
    _run(db, cli.security_dismiss_rename, ["VBX", "USSX", "-m", "bad row", "--yes"])
    assert len(_bar_dates(db, vbx)) == 5
    assert len(_bar_dates(db, ussx)) == 5
    assert repo.active_security_for_symbol(db, "USSX") == ussx


def test_dismiss_cannot_overwrite_an_applied_decision(db):
    """A dismissal is about the feed. It must never become a way to unmake a rename."""
    sid = _mk_security(db, "FB")
    repo.record_symbol_change(
        db,
        old_symbol="FB",
        new_symbol="META",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_APPLIED,
        security_id=sid,
    )
    rows = repo.dismiss_symbol_change(
        db, old_symbol="FB", new_symbol="META", note="n", dismissed_by="ada"
    )
    assert rows == []
    assert (
        db.fetchval("SELECT status FROM core.symbol_change WHERE old_symbol = 'FB'")
        == repo.CHANGE_APPLIED
    )


def test_dismiss_reports_an_unknown_pair_rather_than_succeeding_quietly(db):
    result = _run(db, cli.security_dismiss_rename, ["NOPE", "NADA", "-m", "x", "--yes"])
    assert result.exit_code != 0
    assert "No recorded rename" in _text(result)


def test_re_dismissing_is_a_no_op_that_says_so(db, bogus_rename):
    args = ["VBX", "USSX", "-m", "bad row", "--yes"]
    assert _run(db, cli.security_dismiss_rename, args).exit_code == 0
    again = _run(db, cli.security_dismiss_rename, args)
    assert again.exit_code == 0, _text(again)
    assert "already terminal" in _text(again)
