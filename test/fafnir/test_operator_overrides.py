"""Operator corrections to corporate actions and bars, and the loaders honouring them.

The feature is one contrast, and the first two tests are it: delete a vendor row by
hand and the next load writes it straight back; delete it with `fafnir actions
delete` and the next load sets it aside. Everything else guards the edges -- the
record kept, the dry run that changes nothing, the refusals, and the undo.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import corporate_actions as ca
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration

AS_OF = dt.date(2024, 12, 31)


class _FakeFMP:
    """Canned splits, dividends and bars; no network."""

    bytes_downloaded = 0
    request_count = 0

    def __init__(self, splits=None, dividends=None, bars=None):
        self._splits = splits or []
        self._dividends = dividends or []
        self._bars = bars or []

    def splits(self, symbol):
        return list(self._splits)

    def dividends(self, symbol):
        return list(self._dividends)

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return list(self._bars)


def _mk(db, symbol="OVRD"):
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


def _bar(d, close, volume=1000):
    return {
        "date": d,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _store_bars(db, sid, closes: dict):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date.fromisoformat(d),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000,
            }
            for d, c in closes.items()
        ],
    )


def _split(db, sid, ex, num, den, source="fmp"):
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date.fromisoformat(ex),
        split_numerator=num,
        split_denominator=den,
        source=source,
    )
    return int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action "
            "WHERE security_id=%s AND action_type='split' AND ex_date=%s",
            (sid, ex),
        )
    )


def _splits(db, sid):
    return [
        (str(r["ex_date"]), r["source"])
        for r in db.fetchall(
            "SELECT ex_date, source FROM core.corporate_action "
            "WHERE security_id=%s AND action_type='split' ORDER BY ex_date",
            (sid,),
        )
    ]


def _pull_actions(db, fmp, symbol, sid):
    result = ca.ActionsResult()
    with RunLog(db, source="fmp", endpoint=ca.ENDPOINT, params={}) as run:
        ca.load_symbol_actions(
            db, fmp, symbol, sid, run=run, as_of=AS_OF, result=result
        )
    return result


def _run(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


@pytest.fixture()
def cli_db(db, monkeypatch):
    """Point the CLI at the test database."""
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    return db


# ---------------------------------------------------------------------------
# The contrast
# ---------------------------------------------------------------------------

DUPLICATE_FEED = [
    {"date": "2024-06-05", "numerator": 4, "denominator": 1},
    {"date": "2024-06-06", "numerator": 4, "denominator": 1},
]


def test_a_hand_deleted_duplicate_split_comes_back_on_the_next_pull(db):
    """KEEX, 2026-09: the feed carried a real 4:1 split and a pre-announced copy of it
    a day later. Deleting the copy in SQL does not survive the loader."""
    sid = _mk(db, "KEEX")
    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "KEEX", sid)
    db.execute(
        "DELETE FROM core.corporate_action WHERE security_id=%s AND ex_date='2024-06-06'",
        (sid,),
    )

    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "KEEX", sid)

    assert _splits(db, sid) == [("2024-06-05", "fmp"), ("2024-06-06", "fmp")]


def test_a_command_deleted_duplicate_split_stays_deleted(cli_db):
    db = cli_db
    sid = _mk(db, "KEEX")
    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "KEEX", sid)
    dup = int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action "
            "WHERE security_id=%s AND ex_date='2024-06-06'",
            (sid,),
        )
    )

    out = _run("actions", "delete", str(dup), "-m", "pre-announced duplicate", "--yes")
    assert out.exit_code == 0, out.output
    result = _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "KEEX", sid)

    assert _splits(db, sid) == [("2024-06-05", "fmp")]
    assert result.suppressed == 1


# ---------------------------------------------------------------------------
# actions delete
# ---------------------------------------------------------------------------


def test_delete_keeps_the_row_it_removed_and_who_removed_it(cli_db):
    db = cli_db
    sid = _mk(db)
    aid = _split(db, sid, "2024-06-06", 4, 1)

    out = _run(
        "actions", "delete", str(aid), "-m", "duplicate", "--by", "claude", "--yes"
    )
    assert out.exit_code == 0, out.output

    (o,) = repo.list_operator_overrides(db, security_id=sid)
    assert (o["target"], o["action_type"], str(o["key_date"]), o["operation"]) == (
        "corporate_action",
        "split",
        "2024-06-06",
        "delete",
    )
    assert (o["note"], o["created_by"]) == ("duplicate", "claude")
    assert o["detail"]["row"]["corporate_action_id"] == aid
    assert o["detail"]["row"]["split_numerator"] == "4.000000"


def test_delete_recomputes_the_factors(cli_db):
    db = cli_db
    sid = _mk(db)
    _split(db, sid, "2024-06-05", 4, 1)
    dup = _split(db, sid, "2024-06-06", 4, 1)
    from fafnir.ingest import adjustments

    adjustments.compute_for_security(db, sid)
    assert db.fetchval(
        "SELECT min(cumulative_price_factor) FROM core.adjustment_factor "
        "WHERE security_id=%s",
        (sid,),
    ) == pytest.approx(0.0625)

    assert _run("actions", "delete", str(dup), "-m", "dup", "--yes").exit_code == 0

    assert db.fetchval(
        "SELECT min(cumulative_price_factor) FROM core.adjustment_factor "
        "WHERE security_id=%s",
        (sid,),
    ) == pytest.approx(0.25)


def test_delete_dry_run_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db)
    aid = _split(db, sid, "2024-06-06", 4, 1)

    out = _run("actions", "delete", str(aid), "-m", "dup", "--dry-run")

    assert out.exit_code == 0, out.output
    assert "Dry run" in out.output
    assert _splits(db, sid) == [("2024-06-06", "fmp")]
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_delete_of_an_unknown_id_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db)
    aid = _split(db, sid, "2024-06-06", 4, 1)

    out = CliRunner().invoke(
        cli.main, ["actions", "delete", str(aid), "999999", "-m", "x", "--yes"]
    )

    assert out.exit_code != 0
    assert "No corporate action 999999" in out.output
    assert _splits(db, sid) == [("2024-06-06", "fmp")]


def test_delete_refuses_an_operator_row_and_points_at_revoke(cli_db):
    db = cli_db
    sid = _mk(db)
    assert (
        _run(
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            "2024-06-05",
            "--split",
            "3:1",
            "-m",
            "missing split",
            "--yes",
        ).exit_code
        == 0
    )
    aid = int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action WHERE security_id=%s",
            (sid,),
        )
    )

    out = CliRunner().invoke(
        cli.main, ["actions", "delete", str(aid), "-m", "x", "--yes"]
    )

    assert out.exit_code != 0
    assert "fafnir override revoke" in out.output
    assert _splits(db, sid) == [("2024-06-05", "operator")]


# ---------------------------------------------------------------------------
# actions add
# ---------------------------------------------------------------------------


def test_an_added_split_is_not_overwritten_or_withdrawn_by_the_feed(cli_db):
    db = cli_db
    sid = _mk(db, "OSCX")
    out = _run(
        "actions",
        "add",
        "--symbol",
        "OSCX",
        "--ex-date",
        "2024-06-05",
        "--split",
        "1:3",
        "-m",
        "unreported reverse split",
        "--yes",
    )
    assert out.exit_code == 0, out.output

    # The feed later reports the same date with a different ratio.
    _pull_actions(
        db,
        _FakeFMP(splits=[{"date": "2024-06-05", "numerator": 1, "denominator": 2}]),
        "OSCX",
        sid,
    )

    row = db.fetchone(
        "SELECT split_numerator, split_denominator, source FROM core.corporate_action "
        "WHERE security_id=%s",
        (sid,),
    )
    assert (float(row["split_numerator"]), float(row["split_denominator"])) == (1, 3)
    assert row["source"] == "operator"


def test_add_refuses_a_key_that_already_has_a_row(cli_db):
    db = cli_db
    sid = _mk(db)
    _split(db, sid, "2024-06-05", 2, 1)

    out = CliRunner().invoke(
        cli.main,
        [
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            "2024-06-05",
            "--split",
            "3:1",
            "-m",
            "x",
            "--yes",
        ],
    )

    assert out.exit_code != 0
    assert "Delete it first" in out.output
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_add_refuses_a_future_ex_date(cli_db):
    db = cli_db
    sid = _mk(db)
    future = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    out = CliRunner().invoke(
        cli.main,
        [
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            future,
            "--dividend",
            "0.5",
            "-m",
            "x",
            "--yes",
        ],
    )

    assert out.exit_code != 0
    assert "in the future" in out.output
    assert _splits(db, sid) == []


def test_add_dry_run_shows_the_factor_change_and_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db)
    _store_bars(db, sid, {"2024-06-04": 30.0, "2024-06-05": 10.0})

    out = _run(
        "actions",
        "add",
        "--security-id",
        str(sid),
        "--ex-date",
        "2024-06-05",
        "--split",
        "3:1",
        "-m",
        "x",
        "--dry-run",
    )

    assert out.exit_code == 0, out.output
    assert "a 3:1 split implies x0.3333" in out.output
    assert "no factors -> 1 factor" in out.output
    assert _splits(db, sid) == []
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_add_requires_exactly_one_kind(cli_db):
    sid = _mk(cli_db)
    out = CliRunner().invoke(
        cli.main,
        [
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            "2024-06-05",
            "-m",
            "x",
            "--yes",
        ],
    )
    assert out.exit_code != 0
    assert "exactly one of --split or --dividend" in out.output


# ---------------------------------------------------------------------------
# actions redate
# ---------------------------------------------------------------------------


def test_redate_moves_the_action_and_keeps_the_feeds_date_from_returning(cli_db):
    db = cli_db
    sid = _mk(db, "LNOK")
    feed = _FakeFMP(splits=[{"date": "2024-06-10", "numerator": 2, "denominator": 1}])
    _pull_actions(db, feed, "LNOK", sid)
    aid = int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action WHERE security_id=%s",
            (sid,),
        )
    )

    out = _run(
        "actions",
        "redate",
        str(aid),
        "--ex-date",
        "2024-06-05",
        "-m",
        "price halves on 06-05",
        "--yes",
    )
    assert out.exit_code == 0, out.output
    _pull_actions(db, feed, "LNOK", sid)

    assert _splits(db, sid) == [("2024-06-05", "operator")]
    delete, add = repo.list_operator_overrides(db, security_id=sid)
    assert (delete["operation"], str(delete["key_date"])) == ("delete", "2024-06-10")
    assert (add["operation"], str(add["key_date"])) == ("add", "2024-06-05")
    assert delete["detail"]["redated_to_override"] == add["override_id"]
    assert add["detail"]["redated_from_override"] == delete["override_id"]


def test_redate_onto_an_existing_action_is_refused_without_writing_half(cli_db):
    db = cli_db
    sid = _mk(db)
    _split(db, sid, "2024-06-05", 2, 1)
    later = _split(db, sid, "2024-06-10", 2, 1)

    out = CliRunner().invoke(
        cli.main,
        [
            "actions",
            "redate",
            str(later),
            "--ex-date",
            "2024-06-05",
            "-m",
            "x",
            "--yes",
        ],
    )

    assert out.exit_code != 0
    assert "delete" in out.output
    assert _splits(db, sid) == [("2024-06-05", "fmp"), ("2024-06-10", "fmp")]
    assert repo.list_operator_overrides(db, include_revoked=True) == []


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def test_the_reconciliation_leaves_operator_edits_alone(cli_db):
    db = cli_db
    sid = _mk(db, "RECN")
    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "RECN", sid)
    dup = int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action "
            "WHERE security_id=%s AND ex_date='2024-06-06'",
            (sid,),
        )
    )
    assert _run("actions", "delete", str(dup), "-m", "dup", "--yes").exit_code == 0
    assert (
        _run(
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            "2024-03-01",
            "--dividend",
            "0.25",
            "-m",
            "missing dividend",
            "--yes",
        ).exit_code
        == 0
    )

    result = ca.ActionsResult()
    with RunLog(db, source="fmp", endpoint=ca.ENDPOINT, params={}) as run:
        ca.reconcile(
            db,
            _FakeFMP(splits=DUPLICATE_FEED),
            [{"security_id": sid, "symbol": "RECN"}],
            run=run,
            as_of=AS_OF,
            settle_days=7,
            result=result,
        )

    assert result.drift == 0
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name='corporate_action_drift'"
        )
        == 0
    )
    assert _splits(db, sid) == [("2024-06-05", "fmp")]


# ---------------------------------------------------------------------------
# prices delete
# ---------------------------------------------------------------------------


def test_a_deleted_bar_is_not_re_inserted_by_the_overlap(cli_db):
    db = cli_db
    sid = _mk(db, "SPIK")
    bars = [_bar("2024-06-03", 10), _bar("2024-06-04", 1430), _bar("2024-06-05", 10)]
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(db, _FakeFMP(bars=bars), "SPIK", run=run)

    out = _run(
        "prices",
        "delete",
        "--symbol",
        "SPIK",
        "--date",
        "2024-06-04",
        "-m",
        "spike: 1430 between two closes of 10",
        "--yes",
    )
    assert out.exit_code == 0, out.output
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(
            db,
            _FakeFMP(bars=bars),
            "SPIK",
            run=run,
            start_date=dt.date(2024, 6, 1),
        )

    dates = [
        str(r["trade_date"])
        for r in db.fetchall(
            "SELECT trade_date FROM core.daily_price WHERE security_id=%s "
            "ORDER BY trade_date",
            (sid,),
        )
    ]
    assert dates == ["2024-06-03", "2024-06-05"]
    (o,) = repo.list_operator_overrides(db, security_id=sid)
    assert o["detail"]["row"]["close"] == "1430.000000"


def test_non_session_deletes_only_the_bars_on_closed_days(cli_db):
    db = cli_db
    sid = _mk(db, "WKND")
    # 2024-06-08 and -09 are a weekend; the loader would set them aside today, so
    # store them the way a pre-fix load did.
    _store_bars(
        db,
        sid,
        {
            "2024-06-07": 10.0,
            "2024-06-08": 0.09,
            "2024-06-09": 0.09,
            "2024-06-10": 10.0,
        },
    )

    dry = _run(
        "prices", "delete", "--symbol", "WKND", "--non-session", "-m", "x", "--dry-run"
    )
    assert "2 bars would be deleted" in dry.output, dry.output
    out = _run(
        "prices",
        "delete",
        "--symbol",
        "WKND",
        "--non-session",
        "-m",
        "weekend prints of another instrument",
        "--yes",
    )
    assert out.exit_code == 0, out.output

    dates = [
        str(r["trade_date"])
        for r in db.fetchall(
            "SELECT trade_date FROM core.daily_price WHERE security_id=%s "
            "ORDER BY trade_date",
            (sid,),
        )
    ]
    assert dates == ["2024-06-07", "2024-06-10"]


def test_prices_delete_skips_a_date_with_no_bar(cli_db):
    db = cli_db
    sid = _mk(db, "NOBR")
    _store_bars(db, sid, {"2024-06-03": 10.0})

    out = _run(
        "prices",
        "delete",
        "--symbol",
        "NOBR",
        "--date",
        "2024-06-04",
        "-m",
        "x",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert "No stored bar on 2024-06-04" in out.output
    assert repo.list_operator_overrides(db, include_revoked=True) == []


# ---------------------------------------------------------------------------
# override revoke
# ---------------------------------------------------------------------------


def test_revoking_a_delete_lets_the_feed_bring_the_row_back(cli_db):
    db = cli_db
    sid = _mk(db, "UNDO")
    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "UNDO", sid)
    dup = int(
        db.fetchval(
            "SELECT corporate_action_id FROM core.corporate_action "
            "WHERE security_id=%s AND ex_date='2024-06-06'",
            (sid,),
        )
    )
    assert _run("actions", "delete", str(dup), "-m", "dup", "--yes").exit_code == 0
    (o,) = repo.list_operator_overrides(db, security_id=sid)

    out = _run("override", "revoke", str(o["override_id"]), "-m", "was real", "--yes")
    assert out.exit_code == 0, out.output
    _pull_actions(db, _FakeFMP(splits=DUPLICATE_FEED), "UNDO", sid)

    assert _splits(db, sid) == [("2024-06-05", "fmp"), ("2024-06-06", "fmp")]
    (kept,) = repo.list_operator_overrides(db, security_id=sid, include_revoked=True)
    assert kept["revoked_note"] == "was real"
    assert repo.list_operator_overrides(db, security_id=sid) == []


def test_revoking_an_add_removes_the_operator_row(cli_db):
    db = cli_db
    sid = _mk(db)
    assert (
        _run(
            "actions",
            "add",
            "--security-id",
            str(sid),
            "--ex-date",
            "2024-06-05",
            "--split",
            "3:1",
            "-m",
            "x",
            "--yes",
        ).exit_code
        == 0
    )
    (o,) = repo.list_operator_overrides(db, security_id=sid)

    out = _run("override", "revoke", str(o["override_id"]), "-m", "wrong", "--yes")

    assert out.exit_code == 0, out.output
    assert _splits(db, sid) == []


def test_revoking_twice_is_refused(cli_db):
    db = cli_db
    sid = _mk(db)
    aid = _split(db, sid, "2024-06-06", 4, 1)
    assert _run("actions", "delete", str(aid), "-m", "dup", "--yes").exit_code == 0
    (o,) = repo.list_operator_overrides(db, security_id=sid)
    oid = str(o["override_id"])
    assert _run("override", "revoke", oid, "-m", "x", "--yes").exit_code == 0

    out = CliRunner().invoke(cli.main, ["override", "revoke", oid, "-m", "x", "--yes"])

    assert out.exit_code != 0
    assert "already revoked" in out.output


def test_override_list_json_names_the_change(cli_db):
    db = cli_db
    sid = _mk(db, "LIST")
    aid = _split(db, sid, "2024-06-06", 4, 1)
    assert _run("actions", "delete", str(aid), "-m", "dup", "--yes").exit_code == 0

    out = _run("override", "list", "--symbol", "LIST", "--json")

    (row,) = json.loads(out.output)
    assert (row["primary_symbol"], row["operation"], row["note"]) == (
        "LIST",
        "delete",
        "dup",
    )


# ---------------------------------------------------------------------------
# Constraints and merges
# ---------------------------------------------------------------------------


def test_a_blank_note_is_refused_by_the_table(db):
    sid = _mk(db)
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO ops.operator_override "
            "(security_id, target, action_type, key_date, operation, note, created_by) "
            "VALUES (%s, 'corporate_action', 'split', '2024-06-05', 'delete', ' ', 'x')",
            (sid,),
        )


def test_a_bar_can_only_be_deleted_not_added(db):
    sid = _mk(db)
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO ops.operator_override "
            "(security_id, target, key_date, operation, note, created_by) "
            "VALUES (%s, 'daily_price', '2024-06-05', 'add', 'n', 'x')",
            (sid,),
        )


def test_a_merge_carries_the_victims_overrides_to_the_survivor(cli_db):
    db = cli_db
    survivor = _mk(db, "SURV")
    victim = repo.upsert_security(
        db,
        primary_symbol="VICT",
        company_name="SURV Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    aid = _split(db, victim, "2024-06-06", 4, 1)
    assert _run("actions", "delete", str(aid), "-m", "dup", "--yes").exit_code == 0

    repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)

    (o,) = repo.list_operator_overrides(db)
    assert o["security_id"] == survivor
    assert (survivor, "split", dt.date(2024, 6, 6)) in repo.suppressed_action_keys(db)
