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


# ---------------------------------------------------------------------------
# `security dedupe` -- folding re-minted rows back onto the history
# ---------------------------------------------------------------------------


def _mint_duplicate(db, symbol, *, delisted=None, name=None):
    """A second core.security row for a ticker, the way the master used to mint it.

    Direct INSERT on purpose: `upsert_security` is the code path that stopped doing
    this, so it cannot be used to reproduce what it used to produce.

    The caller must leave at most one row of the pair listed: 0012's
    ux_security_active_source_symbol is UNIQUE (source, primary_symbol) WHERE
    delisted_date IS NULL, so two *active* rows for one ticker is a state the
    schema has refused since before this bug existed. The re-mint chain is
    therefore delisted-with-the-bars first, then the fresh active shell -- which is
    why `_retire` runs on the keeper in every test below.
    """
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = db.fetchval(
        """
        INSERT INTO core.security
            (primary_symbol, company_name, asset_type, exchange_code, delisted_date)
        VALUES (%s, %s, 'equity', 'NASDAQ', %s)
        RETURNING security_id
        """,
        (symbol, name or f"{symbol} Inc", delisted),
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return int(sid)


def _retire(db, sid, when=dt.date(2023, 5, 1)):
    """Delist a row and close its open ticker period, the way `mark_delisted` does.

    This is the step that makes room for the re-mint: while the row is listed the
    unique index refuses a second one.
    """
    repo.mark_delisted(db, security_id=sid, delisted_date=when)


def _periods(db, sid, symbol):
    """Every xref period this security holds for the ticker, oldest first."""
    return [
        r["valid_to"]
        for r in db.fetchall(
            """
            SELECT valid_to FROM core.symbol_xref
             WHERE security_id = %s AND symbol = %s
             ORDER BY valid_from
            """,
            (sid, symbol),
        )
    ]


def _security_ids(db, symbol):
    return {
        int(r["security_id"])
        for r in db.fetchall(
            "SELECT security_id FROM core.security WHERE primary_symbol = %s",
            (symbol,),
        )
    }


def test_survivor_is_the_row_holding_the_bars(db):
    keeper = _mk_security(db, "DDUP")
    _bars(db, keeper, 5)
    _retire(db, keeper)
    shell = _mint_duplicate(db, "DDUP")

    groups = repo.duplicate_symbol_groups(db, symbol="DDUP")

    assert len(groups) == 1
    assert groups[0].survivor_id == keeper
    assert [v.security_id for v in groups[0].victims] == [shell]
    assert groups[0].blocker is None


def test_a_group_with_two_history_holders_is_refused(db):
    """Ticker reuse (0009) says two rows is correct -- never fold that silently."""
    a = _mk_security(db, "DREU")
    _bars(db, a, 5)
    _retire(db, a)
    b = _mint_duplicate(db, "DREU")
    _bars(db, b, 5, close=7.0)

    group = repo.duplicate_symbol_groups(db, symbol="DREU")[0]

    assert group.survivor_id is None
    assert "hold bars" in group.blocker
    assert group.victims == []


def test_a_group_with_no_history_holder_is_refused(db):
    a = _mk_security(db, "DNOH")
    _retire(db, a)
    _mint_duplicate(db, "DNOH")

    group = repo.duplicate_symbol_groups(db, symbol="DNOH")[0]

    assert group.survivor_id is None
    assert "no row holds bars" in group.blocker
    assert a in _security_ids(db, "DNOH")


def test_dedupe_deletes_the_shell_and_keeps_the_history(db):
    keeper = _mk_security(db, "DFOLD")
    _bars(db, keeper, 5)
    _retire(db, keeper)
    _mint_duplicate(db, "DFOLD")

    result = _run(db, cli.security_dedupe, ["--symbol", "DFOLD", "--by", "t", "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _security_ids(db, "DFOLD") == {keeper}
    assert len(_bar_dates(db, keeper)) == 5


def test_dedupe_dry_run_changes_nothing(db):
    keeper = _mk_security(db, "DDRY")
    _bars(db, keeper, 5)
    _retire(db, keeper)
    shell = _mint_duplicate(db, "DDRY")

    result = _run(db, cli.security_dedupe, ["--symbol", "DDRY", "--dry-run"])

    assert result.exit_code == 0, _text(result)
    assert "Dry run" in _text(result)
    assert _security_ids(db, "DDRY") == {keeper, shell}


def test_dedupe_takes_the_merge_path_for_a_shell_carrying_actions(db):
    """A shell that accumulated duplicate actions is not `fold`-able.

    `security_has_history` counts corporate actions, so `fold_empty_security`
    refuses it; the guarded merge is what moves it. Without that branch the
    commonest shell on this warehouse -- 2,289 of them -- would be skipped.
    """
    keeper = _mk_security(db, "DACT")
    _bars(db, keeper, 5)
    _retire(db, keeper)
    shell = _mint_duplicate(db, "DACT")
    repo.upsert_corporate_action(
        db,
        security_id=shell,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 4),
        dividend_amount=0.25,
    )
    assert repo.security_has_history(db, shell)

    result = _run(db, cli.security_dedupe, ["--symbol", "DACT", "--by", "t", "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _security_ids(db, "DACT") == {keeper}
    moved = db.fetchval(
        "SELECT count(*) FROM core.corporate_action WHERE security_id = %s", (keeper,)
    )
    assert int(moved) == 1


def test_dedupe_reopens_the_survivors_xref_period(db):
    """Each mint closed the previous period; deleting the shells must not leave the
    ticker resolving only as a former symbol.

    Asserted on the xref row rather than through `active_security_for_symbol`,
    which falls back to `core.security.primary_symbol` when no period is open and
    so answers `keeper` either way -- it cannot see whether the period re-opened.
    """
    keeper = _mk_security(db, "DXRF")
    _bars(db, keeper, 5)
    # Close the keeper's period first: while it is open, `upsert_symbol_xref`
    # re-points that same row at the new security rather than opening a second
    # one, and the mint would leave the keeper holding no period at all.
    db.execute(
        "UPDATE core.symbol_xref SET valid_to = %s WHERE security_id = %s",
        (dt.date(2024, 1, 1), keeper),
    )
    shell = _mint_duplicate(db, "DXRF", delisted=dt.date(2024, 1, 2))
    assert _periods(db, keeper, "DXRF") == [dt.date(2024, 1, 1)]

    result = _run(db, cli.security_dedupe, ["--symbol", "DXRF", "--by", "t", "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _security_ids(db, "DXRF") == {keeper}
    assert shell not in _security_ids(db, "DXRF")
    assert _periods(db, keeper, "DXRF") == [None]
    assert repo.active_security_for_symbol(db, "DXRF") == keeper


def test_dedupe_leaves_a_delisted_survivors_period_closed(db):
    """A closed period on a delisted row is what it means -- do not re-open it."""
    keeper = _mk_security(db, "DDEL")
    _bars(db, keeper, 5)
    _retire(db, keeper, dt.date(2024, 6, 10))
    _mint_duplicate(db, "DDEL")

    _run(db, cli.security_dedupe, ["--symbol", "DDEL", "--by", "t", "--yes"])

    assert _security_ids(db, "DDEL") == {keeper}
    assert _periods(db, keeper, "DDEL") == [dt.date(2024, 6, 10)]
    assert repo.active_security_for_symbol(db, "DDEL") is None


def _open_identity_flags(db):
    """The tickers carrying an open `security_duplicate_identity` flag."""
    return {r["record_key"]["symbol"] for r in db.fetchall("""
            SELECT record_key FROM ops.data_quality_flag
             WHERE check_name = 'security_duplicate_identity'
               AND resolved_at IS NULL
            """)}


def _identity_flag(db, security_id, symbol):
    db.execute(
        """
        INSERT INTO ops.data_quality_flag
            (security_id, table_name, record_key, check_name, severity, detail,
             detected_at)
        VALUES (%s, 'core.security', jsonb_build_object('symbol', %s::text),
                'security_duplicate_identity', 'warn', '{}'::jsonb, now())
        """,
        (security_id, symbol),
    )


def _remint(db, symbol, *, name=None):
    """The shape this command exists for: the bar-holder delisted, a fresh shell."""
    keeper = _mk_security(db, symbol, name=name)
    _bars(db, keeper, 5)
    _retire(db, keeper)
    _mint_duplicate(db, symbol, name=name)
    return keeper


def test_dedupe_closes_only_the_flags_for_tickers_it_actually_repaired(db):
    """A bare `--check` filter would close the whole queue.

    `resolve_dq_flags` refuses a filter that narrows nothing, but a check name
    narrows it enough to pass that guard while still selecting every duplicated
    ticker in the warehouse -- including the ones `--symbol` never looked at and
    the ones reported as needing review. Their duplicates are still there.
    """
    repaired = _remint(db, "DSCPA")
    _identity_flag(db, repaired, "DSCPA")

    untouched = _remint(db, "DSCPB")
    _identity_flag(db, untouched, "DSCPB")

    reviewed = _mk_security(db, "DSCPC")
    _bars(db, reviewed, 5)
    _retire(db, reviewed)
    _bars(db, _mint_duplicate(db, "DSCPC"), 5, close=7.0)  # two bar-holders: skipped
    _identity_flag(db, reviewed, "DSCPC")

    assert _open_identity_flags(db) == {"DSCPA", "DSCPB", "DSCPC"}

    result = _run(db, cli.security_dedupe, ["--symbol", "DSCPA", "--by", "t", "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _security_ids(db, "DSCPA") == {repaired}
    assert _open_identity_flags(db) == {"DSCPB", "DSCPC"}


def test_a_group_whose_rows_name_different_companies_is_refused(db):
    """Ticker reuse: a new issuer on a dead ticker, before its first bars land.

    Structurally identical to a re-mint -- one delisted row with the history, one
    empty listed row -- so the bars rule alone would fold a legitimately new
    security into a dead company's identity, which is the fork 0009 mints a
    separate security_id to avoid. The name is the only thing that separates them,
    which is why `security_duplicate_identity` reports distinct_company_names.
    """
    dead = _mk_security(db, "DNAME", name="Old Issuer Inc")
    _bars(db, dead, 5)
    _retire(db, dead)
    newcomer = _mint_duplicate(db, "DNAME", name="Wholly Different Corp")

    group = repo.duplicate_symbol_groups(db, symbol="DNAME")[0]

    assert group.survivor_id is None
    assert "distinct company names" in group.blocker
    assert group.victims == []

    result = _run(db, cli.security_dedupe, ["--symbol", "DNAME", "--by", "t", "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _security_ids(db, "DNAME") == {dead, newcomer}


# ---------------------------------------------------------------------------
# `security merge` -- the pair neither sibling command can reach
# ---------------------------------------------------------------------------


def _sec_ids(db, *symbols):
    return {
        int(r["security_id"])
        for r in db.fetchall(
            "SELECT security_id FROM core.security WHERE primary_symbol = ANY(%s)",
            (list(symbols),),
        )
    }


def test_merge_folds_a_duplicate_that_spans_two_tickers(db):
    """The MAPP/MATR shape: a rename re-minted on the OLD ticker after it applied.

    `dedupe` groups by primary_symbol so it never pairs these, and `merge-rename`
    needs the old ticker live. This is the case that had no command.
    """
    survivor = _mk_security(db, "NEWT", cusip="41151J836")
    _bars(db, survivor, 6)
    victim = _mint_duplicate(db, "OLDT", delisted=dt.date(2024, 6, 30))
    _bars(db, victim, 4)

    result = _run(db, cli.security_merge, [str(victim), str(survivor), "--yes"])

    assert result.exit_code == 0, _text(result)
    assert _sec_ids(db, "OLDT") == set()
    assert _sec_ids(db, "NEWT") == {survivor}
    assert len(_bar_dates(db, survivor)) == 6


def test_merge_refuses_a_row_against_itself(db):
    sid = _mk_security(db, "SELF")
    result = _run(db, cli.security_merge, [str(sid), str(sid), "--yes"])
    assert result.exit_code != 0
    assert "same security" in _text(result)


def test_merge_refuses_an_unknown_id(db):
    sid = _mk_security(db, "KNOWN")
    result = _run(db, cli.security_merge, ["999999999", str(sid), "--yes"])
    assert result.exit_code != 0
    assert "No such security" in _text(result)


def test_merge_dry_run_changes_nothing(db):
    survivor = _mk_security(db, "DRYA")
    _bars(db, survivor, 5)
    victim = _mint_duplicate(db, "DRYB")
    _bars(db, victim, 5)

    result = _run(db, cli.security_merge, [str(victim), str(survivor), "--dry-run"])

    assert result.exit_code == 0, _text(result)
    assert "Dry run" in _text(result)
    assert _sec_ids(db, "DRYA", "DRYB") == {survivor, victim}


def test_merge_refuses_disagreeing_ohlc_without_force(db):
    survivor = _mk_security(db, "DISA")
    _bars(db, survivor, 5, close=100.0)
    victim = _mint_duplicate(db, "DISB")
    _bars(db, victim, 5, close=7.0)

    result = _run(db, cli.security_merge, [str(victim), str(survivor), "--yes"])

    assert result.exit_code != 0
    assert "BLOCKER" in _text(result)
    assert victim in _sec_ids(db, "DISB")


def test_merge_force_overrides_the_guard(db):
    survivor = _mk_security(db, "FRCA")
    _bars(db, survivor, 5, close=100.0)
    victim = _mint_duplicate(db, "FRCB")
    _bars(db, victim, 5, close=7.0)

    result = _run(
        db, cli.security_merge, [str(victim), str(survivor), "--yes", "--force"]
    )

    assert result.exit_code == 0, _text(result)
    assert _sec_ids(db, "FRCB") == set()


def test_merge_warns_when_an_identifier_only_the_victim_has_will_be_lost(db):
    """compare_securities is silent here: it only reports a mismatch where BOTH
    sides carry the field, so a victim-only CUSIP is invisible to the guard and
    goes with the deleted row."""
    survivor = _mk_security(db, "LOSA")
    _bars(db, survivor, 5)
    victim = _mint_duplicate(db, "LOSB")
    _bars(db, victim, 5)
    db.execute(
        "UPDATE core.security SET cusip = %s WHERE security_id = %s",
        ("999999999", victim),
    )

    result = _run(db, cli.security_merge, [str(victim), str(survivor), "--dry-run"])

    assert result.exit_code == 0, _text(result)
    assert "WARNING" in _text(result)
    assert "999999999" in _text(result)


def test_merge_closes_the_identity_flag_only_when_the_ticker_is_single(db):
    keeper = _mk_security(db, "TRIO")
    _bars(db, keeper, 5)
    second = _mint_duplicate(db, "TRIO")
    _bars(db, second, 5)
    third = _mint_duplicate(db, "TRIO")
    repo.add_dq_flag_once(
        db,
        check_name="security_duplicate_identity",
        security_id=keeper,
        record_key={"symbol": "TRIO"},
    )

    def _flag_open():
        return bool(
            repo.open_dq_flag_ids_for_record(
                db,
                check_name="security_duplicate_identity",
                record_key={"symbol": "TRIO"},
            )
        )

    # Two of three folded: the ticker still has more than one row, so the
    # condition the flag describes is still true and it must stay open.
    result = _run(db, cli.security_merge, [str(second), str(keeper), "--yes"])
    assert result.exit_code == 0, _text(result)
    assert _flag_open()
    assert "still has 2 rows" in _text(result)

    # The last duplicate goes: now it is single, and the flag may close.
    result = _run(db, cli.security_merge, [str(third), str(keeper), "--yes"])
    assert result.exit_code == 0, _text(result)
    assert not _flag_open()
