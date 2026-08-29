"""The DQ queue is only as good as the triage it supports (needs FAFNIR_TEST_DSN).

`fafnir dq run` fills ops.data_quality_flag and `fafnir status` counts it, but for
a long time the only way to read a flag or close one was psql. These tests pin the
commands that replaced that, and the properties that make them safe to point at a
queue of 40,000 rows:

  * `list` and `resolve` select the same rows from the same options -- the whole
    workflow is "narrow with list, then re-run it as resolve", and a filter that
    meant something different under each would close flags nobody looked at;
  * an unfiltered or mistyped `resolve` closes nothing;
  * a resolution says who and why, and reopening it takes both back.
"""

from __future__ import annotations

import datetime as dt
import os
import types

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_security(db, symbol):
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


@pytest.fixture()
def queue(db):
    """A small queue with two securities, three checks and two severities."""
    aapl = _mk_security(db, "AAPL")
    msft = _mk_security(db, "MSFT")
    for day in ("2024-01-10", "2024-01-11"):
        repo.add_dq_flag_once(
            db,
            check_name="gap",
            security_id=aapl,
            table_name="core.daily_price",
            record_key={"trade_date": day},
            detail={"exchange": "NASDAQ"},
        )
    repo.add_dq_flag_once(
        db,
        check_name="gap",
        security_id=msft,
        record_key={"trade_date": "2024-02-01"},
    )
    repo.add_dq_flag_once(
        db,
        check_name="outlier",
        security_id=aapl,
        record_key={"trade_date": "2024-05-02"},
        detail={"move": 0.62},
    )
    repo.add_dq_flag_once(
        db, check_name="adjustment_failed", severity="error", security_id=msft
    )
    # price_* repeats by design; two rows, one condition.
    for _ in range(2):
        repo.add_dq_flag(
            db,
            check_name="price_missing_or_nonnumeric_ohlc",
            severity="error",
            security_id=msft,
            record_key={"date": "2024-08-01"},
        )
    return types.SimpleNamespace(db=db, aapl=aapl, msft=msft)


# The subcommands are invoked directly rather than through `cli.main`: the group
# callback rebuilds the config from ~/.fafnirrc and the environment, which would
# point these tests at whatever warehouse the machine running them happens to have.
_COMMANDS = {"list": cli.dq_list, "resolve": cli.dq_resolve, "reopen": cli.dq_reopen}


def _run(db, args, **kwargs):
    """Invoke a `fafnir dq` command against the test database."""
    return CliRunner().invoke(
        _COMMANDS[args[0]], list(args[1:]), obj={"config": _Cfg(db.dsn)}, **kwargs
    )


def _text(result) -> str:
    """Everything the command printed, wherever it sent it.

    Some of these messages are deliberately on stderr (the per-id "no such flag"
    lines), and whether CliRunner folds stderr into `output` has changed across
    click releases -- which the package only pins as >=8.0.
    """
    try:
        return result.output + (result.stderr or "")
    except ValueError:  # click<8.2: stderr was mixed into output already
        return result.output


class _Cfg:
    def __init__(self, dsn):
        self.dsn = dsn


def _open_ids(db, **kwargs):
    return {
        r["dq_flag_id"]
        for r in repo.list_dq_flags(db, repo.DqFilter(**kwargs), limit=1000)
    }


# ---------------------------------------------------------------------------
# Reading the queue
# ---------------------------------------------------------------------------


def test_the_summary_counts_problems_per_check_and_securities_once(queue):
    """Seven flags over five conditions: the summary has to add up both ways."""
    totals = repo.dq_flag_totals(queue.db)
    rows = {r["check_name"]: r for r in repo.summarize_dq_flags(queue.db)}

    assert totals["flags"] == 7
    # Two securities, not one row per (security, check): a security with a gap and
    # an outlier is still one security.
    assert totals["securities"] == 2
    assert totals["checks"] == 4
    assert rows["gap"]["flags"] == 3 and rows["gap"]["securities"] == 2
    assert rows["price_missing_or_nonnumeric_ohlc"]["flags"] == 2


def test_the_summary_leads_with_the_severity_that_wants_attention(queue):
    severities = [r["severity"] for r in repo.summarize_dq_flags(queue.db)]

    # Not alphabetical: 'error' < 'info' < 'warn' as text would put info above warn.
    assert severities == sorted(severities, key=lambda s: {"error": 0, "warn": 1}[s])


def test_a_check_glob_matches_the_family_and_nothing_else(queue):
    """`price_*` is the category the docs name; the literal _ must stay literal."""
    globbed = repo.dq_flag_totals(queue.db, repo.DqFilter(checks=("price_*",)))
    exact = repo.dq_flag_totals(queue.db, repo.DqFilter(checks=("gap",)))

    assert globbed["flags"] == 2
    assert exact["flags"] == 3
    assert (
        repo.dq_flag_totals(queue.db, repo.DqFilter(checks=("priceX*",)))["flags"] == 0
    )


def test_the_detail_view_carries_the_ticker_and_the_keys(queue):
    rows = repo.list_dq_flags(
        queue.db, repo.DqFilter(checks=("outlier",), security_id=queue.aapl)
    )

    assert len(rows) == 1
    assert rows[0]["primary_symbol"] == "AAPL"
    assert rows[0]["record_key"] == {"trade_date": "2024-05-02"}
    assert rows[0]["detail"] == {"move": 0.62}


def test_a_flag_with_no_security_still_lists(queue):
    """security_id is a soft reference; a universe-wide flag has none.

    An inner join here would drop it from the page while `fafnir status` went on
    counting it -- a problem invisible in the queue that reports it.
    """
    repo.add_dq_flag_once(queue.db, check_name="universe_stale", severity="error")

    rows = repo.list_dq_flags(queue.db, repo.DqFilter(checks=("universe_stale",)))

    assert len(rows) == 1 and rows[0]["primary_symbol"] is None


def test_until_includes_the_whole_of_its_day(db):
    """A date bound that dropped same-day flags would silently under-report."""
    sid = _mk_security(db, "AAA")
    repo.add_dq_flag_once(db, check_name="gap", security_id=sid, record_key={"d": 1})
    today = dt.date.today()

    assert repo.dq_flag_totals(db, repo.DqFilter(until=today))["flags"] == 1


def test_the_cli_summary_and_detail_render(queue):
    summary = _run(queue.db, ["list"])
    detail = _run(queue.db, ["list", "--detail", "--check", "gap"])

    assert summary.exit_code == 0, summary.output
    assert "Open DQ flags: 7" in _text(summary)
    # The price_* caveat is contextual: it appears because price_* is in the table.
    assert "price_* flags repeat" in _text(summary)
    assert detail.exit_code == 0, detail.output
    assert "AAPL" in _text(detail) and "trade_date=2024-01-10" in _text(detail)


def test_json_output_is_machine_readable(queue):
    import json

    result = _run(queue.db, ["list", "--json", "--check", "gap"])

    payload = json.loads(result.output)
    assert payload["totals"]["flags"] == 3
    assert {r["check_name"] for r in payload["rows"]} == {"gap"}


def test_a_malformed_date_is_a_usage_error_not_a_traceback(queue):
    """Bad input has to read as bad input. A strptime traceback out of the middle
    of a query tells an operator nothing about which option to fix."""
    result = _run(queue.db, ["list", "--since", "not-a-date"])

    assert result.exit_code != 0
    assert "'--since'" in _text(result)
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_paging_bounds_are_rejected_before_they_reach_postgres(queue):
    for args in (["list", "-d", "--limit", "0"], ["list", "-d", "--offset", "-1"]):
        result = _run(queue.db, args)

        assert result.exit_code != 0
        assert "is not in the range" in _text(result)


# ---------------------------------------------------------------------------
# list/resolve parity -- the property the workflow rests on
# ---------------------------------------------------------------------------


def test_resolve_closes_exactly_what_the_same_filter_lists(queue):
    """The workflow is "narrow with list, re-run as resolve"; the two must agree."""
    filt = repo.DqFilter(checks=("gap",), security_id=queue.aapl)
    listed = {r["dq_flag_id"] for r in repo.list_dq_flags(queue.db, filt, limit=1000)}

    closed = set(repo.resolve_dq_flags(queue.db, filt, note="backfilled"))

    assert listed == closed
    assert len(closed) == 2
    assert _open_ids(queue.db) & closed == set()


def test_an_unknown_symbol_resolves_nothing(queue):
    """The failure that matters: a typo falling through as "no filter" would close
    the entire queue under --yes."""
    result = _run(queue.db, ["resolve", "--symbol", "NOPE", "--yes"])

    assert result.exit_code != 0
    assert "Unknown symbol NOPE" in _text(result)
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_resolve_with_no_selection_refuses(queue):
    result = _run(queue.db, ["resolve"])

    assert result.exit_code != 0
    assert "Refusing to close the whole queue" in _text(result)
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_the_repository_refuses_an_unnarrowed_filter_too(queue):
    """The CLI guard is not the only caller; the rule belongs under it as well."""
    with pytest.raises(ValueError):
        repo.resolve_dq_flags(queue.db, repo.DqFilter(), note="all of it")

    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_ids_and_filters_together_are_refused(queue):
    ids = [str(i) for i in _open_ids(queue.db, checks=("gap",))]

    result = _run(queue.db, ["resolve", *ids, "--check", "outlier"])

    assert result.exit_code != 0
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_a_dry_run_changes_nothing(queue):
    result = _run(queue.db, ["resolve", "--check", "gap", "--dry-run"])

    assert result.exit_code == 0
    assert "3 flags would be closed" in _text(result)
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


def test_a_bulk_resolve_asks_before_closing(queue):
    """No --yes and no ids: the operator sees the count before it happens."""
    declined = _run(queue.db, ["resolve", "--check", "gap"], input="n\n")

    assert declined.exit_code != 0
    assert "Close 3 open flags" in _text(declined)
    assert repo.dq_flag_totals(queue.db)["flags"] == 7


# ---------------------------------------------------------------------------
# Resolution records a decision
# ---------------------------------------------------------------------------


def test_a_resolution_keeps_who_and_why(queue):
    ids = sorted(_open_ids(queue.db, checks=("outlier",)))

    result = _run(
        queue.db,
        ["resolve", *[str(i) for i in ids], "--note", "real move", "--by", "ada"],
    )

    assert result.exit_code == 0, result.output
    row = repo.list_dq_flags(queue.db, repo.DqFilter(state="resolved", flag_ids=ids))[0]
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "ada"
    assert row["resolution_note"] == "real move"


def test_resolving_twice_does_not_overwrite_the_first_decision(queue):
    """The second resolve is a no-op, not a re-attribution of someone else's call."""
    ids = sorted(_open_ids(queue.db, checks=("outlier",)))
    _run(queue.db, ["resolve", str(ids[0]), "--note", "real move", "--by", "ada"])

    again = _run(queue.db, ["resolve", str(ids[0]), "--note", "no idea", "--by", "bob"])

    row = repo.list_dq_flags(queue.db, repo.DqFilter(state="all", flag_ids=ids))[0]
    assert again.exit_code == 0
    assert "already resolved" in _text(again)
    assert row["resolved_by"] == "ada" and row["resolution_note"] == "real move"


def test_an_unknown_id_is_reported_not_swallowed(queue):
    result = _run(queue.db, ["resolve", "999999", "--note", "x"])

    assert result.exit_code == 0
    assert "no such flag" in _text(result)


def test_a_resolved_condition_is_flagged_again_when_it_is_still_there(queue):
    """Resolving is a judgement, not a repair -- the next run re-detects it.

    This is what frees the condition's slot in ux_dq_flag_open_condition, and it is
    the reason `resolve` is safe to use on a queue you have not fixed yet.
    """
    filt = repo.DqFilter(checks=("outlier",))
    repo.resolve_dq_flags(queue.db, filt, note="looked at it")

    reflagged = repo.add_dq_flag_once(
        queue.db,
        check_name="outlier",
        security_id=queue.aapl,
        record_key={"trade_date": "2024-05-02"},
    )

    assert reflagged is True
    assert repo.dq_flag_totals(queue.db, filt)["flags"] == 1


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


def test_reopen_puts_a_flag_back_and_drops_the_stale_note(queue):
    ids = sorted(_open_ids(queue.db, checks=("outlier",)))
    _run(queue.db, ["resolve", str(ids[0]), "--note", "wrong call", "--by", "ada"])

    result = _run(queue.db, ["reopen", str(ids[0])])

    row = repo.list_dq_flags(queue.db, repo.DqFilter(state="all", flag_ids=ids))[0]
    assert result.exit_code == 0
    assert row["resolved_at"] is None
    assert row["resolved_by"] is None and row["resolution_note"] is None


def test_reopen_reports_the_condition_that_is_already_back(queue):
    """One open row per condition. If the check re-flagged it, there is no slot."""
    ids = sorted(_open_ids(queue.db, checks=("outlier",)))
    repo.resolve_dq_flags(queue.db, repo.DqFilter(flag_ids=ids), note="looked at it")
    repo.add_dq_flag_once(
        queue.db,
        check_name="outlier",
        security_id=queue.aapl,
        record_key={"trade_date": "2024-05-02"},
    )

    result = _run(queue.db, ["reopen", str(ids[0])])

    assert result.exit_code == 0
    assert "already open on a newer flag" in _text(result)
    # And the failure did not take the rest of the command down with it.
    assert repo.dq_flag_totals(queue.db, repo.DqFilter(state="all"))["flags"] == 8


def test_one_conflict_does_not_block_the_other_ids(queue):
    """Each id gets its own savepoint, so a bad one costs only itself."""
    outlier = sorted(_open_ids(queue.db, checks=("outlier",)))
    gaps = sorted(_open_ids(queue.db, checks=("gap",), security_id=queue.aapl))
    repo.resolve_dq_flags(
        queue.db, repo.DqFilter(flag_ids=outlier + gaps), note="triage"
    )
    repo.add_dq_flag_once(
        queue.db,
        check_name="outlier",
        security_id=queue.aapl,
        record_key={"trade_date": "2024-05-02"},
    )

    reopened, conflicted = repo.reopen_dq_flags(queue.db, outlier + gaps)

    assert conflicted == outlier
    assert sorted(reopened) == gaps
