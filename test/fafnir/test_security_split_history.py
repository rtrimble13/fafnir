"""Two issuers on one security row, and `fafnir security split-history`.

The vendor serves a reused ticker's whole history as one series, so a SPAC listed
in 2026 arrives on the same row as the company that used its ticker until 2019.
The first tests are the repair and its durability: the old issuer's bars move to a
row of their own, and the next load of the ticker's full history neither puts them
back on the live issuer nor feeds the split-off row. The rest guard the edges.
"""

from __future__ import annotations

import datetime as dt

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import corporate_actions as ca
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration

AS_OF = dt.date(2024, 12, 31)

# Sotheby's-shaped: an old issuer near 35, then a new listing near 10.
OLD = {"2024-01-02": 35, "2024-01-03": 36, "2024-01-04": 34, "2024-01-05": 35}
NEW = {"2024-06-03": 10, "2024-06-04": 10.1, "2024-06-05": 9.9}


class _FakeFMP:
    bytes_downloaded = 0
    request_count = 0

    def __init__(self, bars=None, splits=None, dividends=None):
        self._bars = bars or []
        self._splits = splits or []
        self._dividends = dividends or []

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return list(self._bars)

    def splits(self, symbol):
        return list(self._splits)

    def dividends(self, symbol):
        return list(self._dividends)


def _vendor_bars(closes: dict):
    return [
        {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1000}
        for d, c in closes.items()
    ]


def _mk(db, symbol="BID", name="Tribeca Strategic Acquisition Corp."):
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


def _load(db, symbol, closes, start=dt.date(2023, 12, 1)):
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        return load_symbol_prices(
            db, _FakeFMP(bars=_vendor_bars(closes)), symbol, run=run, start_date=start
        )


def _bars(db, sid):
    return {
        str(r["trade_date"]): float(r["close"])
        for r in db.fetchall(
            "SELECT trade_date, close FROM core.daily_price WHERE security_id=%s "
            "ORDER BY trade_date",
            (sid,),
        )
    }


def _actions(db, sid):
    return [
        (r["action_type"], str(r["ex_date"]), r["source"])
        for r in db.fetchall(
            "SELECT action_type, ex_date, source FROM core.corporate_action "
            "WHERE security_id=%s ORDER BY ex_date",
            (sid,),
        )
    ]


def _run(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


def _split_off(*extra):
    return _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-01-31",
        "--new-symbol",
        "BID",
        "--new-name",
        "Sotheby's",
        "-m",
        "Sotheby's to 2024-01-05; Tribeca SPAC lists 2024-06-03",
        "--by",
        "claude",
        *extra,
    )


def _dest(db, src):
    return int(
        db.fetchval(
            "SELECT security_id FROM core.security WHERE security_id <> %s", (src,)
        )
    )


@pytest.fixture()
def cli_db(db, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    return db


@pytest.fixture()
def two_issuers(cli_db):
    db = cli_db
    sid = _mk(db)
    _load(db, "BID", {**OLD, **NEW})
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 1, 4),
        dividend_amount=0.5,
    )
    return db, sid


# ---------------------------------------------------------------------------
# The repair and its durability
# ---------------------------------------------------------------------------


def test_the_old_issuer_moves_to_a_row_of_its_own(two_issuers):
    db, sid = two_issuers

    out = _split_off("--yes")
    assert out.exit_code == 0, out.output

    dest = _dest(db, sid)
    assert _bars(db, sid) == {k: float(v) for k, v in NEW.items()}
    assert _bars(db, dest) == {k: float(v) for k, v in OLD.items()}
    row = db.fetchone("SELECT * FROM core.security WHERE security_id=%s", (dest,))
    assert row["primary_symbol"] == "BID"
    assert row["company_name"] == "Sotheby's"
    assert row["source"] == "operator"
    assert row["is_actively_trading"] is False
    assert row["delisted_date"] == dt.date(2024, 1, 5)
    # The moved bars keep their lineage.
    assert db.fetchval(
        "SELECT count(*) FROM core.daily_price WHERE security_id=%s AND source='fmp'",
        (dest,),
    ) == len(OLD)
    # The dividend went with them, as an operator row the destination's feed cannot
    # withdraw; the source keeps its key suppressed.
    assert _actions(db, sid) == []
    assert _actions(db, dest) == [("dividend", "2024-01-04", "operator")]
    assert (sid, "dividend", dt.date(2024, 1, 4)) in repo.suppressed_action_keys(db)
    # A closed ticker period records which ticker the old issuer traded under.
    xref = db.fetchone("SELECT * FROM core.symbol_xref WHERE security_id=%s", (dest,))
    assert (xref["symbol"], xref["valid_from"], xref["valid_to"]) == (
        "BID",
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 5),
    )


def test_a_full_history_reload_does_not_undo_the_split(two_issuers):
    """The vendor goes on serving both issuers under BID."""
    db, sid = two_issuers
    assert _split_off("--yes").exit_code == 0
    dest = _dest(db, sid)

    _load(db, "BID", {**OLD, **NEW})
    result = ca.ActionsResult()
    with RunLog(db, source="fmp", endpoint=ca.ENDPOINT, params={}) as run:
        ca.load_symbol_actions(
            db,
            _FakeFMP(dividends=[{"date": "2024-01-04", "dividend": 0.5}]),
            "BID",
            sid,
            run=run,
            as_of=AS_OF,
            result=result,
        )

    assert _bars(db, sid) == {k: float(v) for k, v in NEW.items()}
    assert _bars(db, dest) == {k: float(v) for k, v in OLD.items()}
    assert _actions(db, sid) == []
    assert result.suppressed == 1


def test_the_ticker_still_resolves_to_the_live_issuer(two_issuers):
    db, sid = two_issuers
    assert _split_off("--yes").exit_code == 0

    assert repo.resolve_security_id(db, "BID") == sid
    assert repo.active_security_for_symbol(db, "BID") == sid


def test_no_vendor_loader_universe_reaches_the_split_off_row(two_issuers):
    db, sid = two_issuers
    assert _split_off("--yes").exit_code == 0
    dest = _dest(db, sid)

    for rows in (
        repo.universe_securities(db, include_inactive=True),
        repo.securities_without_actions_watermark(
            db, ca.ENDPOINT, include_inactive=True
        ),
        repo.securities_by_asset_type(db, ["equity"], include_inactive=True),
        repo.actions_reconciliation_slice(
            db, buckets=1, bucket=0, include_inactive=True
        ),
    ):
        assert dest not in {r["security_id"] for r in rows}


def test_a_pull_addressed_to_the_split_off_row_is_refused(cli_db):
    """A split-off row under a distinct ticker: an explicit pull must not feed it."""
    db = cli_db
    _mk(db)
    _load(db, "BID", {**OLD, **NEW})
    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-01-31",
        "--new-symbol",
        "BIDOLD",
        "--new-name",
        "Sotheby's",
        "-m",
        "old issuer",
        "--yes",
    )
    assert out.exit_code == 0, out.output
    dest = repo.resolve_security_id(db, "BIDOLD")

    assert _load(db, "BIDOLD", {"2024-02-01": 99}) == 0
    assert "2024-02-01" not in _bars(db, dest)


def test_dedupe_and_the_identity_check_do_not_see_a_duplicate(two_issuers):
    db, sid = two_issuers
    assert _split_off("--yes").exit_code == 0

    assert repo.duplicate_symbol_groups(db, symbol="BID") == []
    assert checks.check_duplicate_identity(db) == 0


# ---------------------------------------------------------------------------
# Plan, dry run, refusals
# ---------------------------------------------------------------------------


def test_the_dry_run_shows_the_boundary_and_changes_nothing(two_issuers):
    db, sid = two_issuers

    out = _split_off("--dry-run")

    assert out.exit_code == 0, out.output
    assert "2024-01-02..2024-01-05" in out.output
    assert "Moves 4 bars" in out.output
    assert "close 9.9" in out.output or "close 10" in out.output
    assert "Dry run: nothing changed." in out.output
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert db.fetchval("SELECT count(*) FROM ops.operator_override") == 0
    assert len(_bars(db, sid)) == len(OLD) + len(NEW)


def test_moving_every_bar_is_refused(two_issuers):
    db, sid = two_issuers
    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-12-31",
        "--new-symbol",
        "BID",
        "--new-name",
        "x",
        "-m",
        "n",
        "--yes",
    )
    assert out.exit_code != 0
    assert "security merge" in out.output
    assert db.fetchval("SELECT count(*) FROM core.security") == 1


def test_a_range_that_has_not_finished_is_refused(two_issuers):
    db, _ = two_issuers
    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        dt.date.today().isoformat(),
        "--new-symbol",
        "BID",
        "--new-name",
        "x",
        "-m",
        "n",
        "--yes",
    )
    assert out.exit_code != 0
    assert db.fetchval("SELECT count(*) FROM ops.operator_override") == 0


def test_splitting_the_same_range_twice_is_refused(two_issuers):
    db, _ = two_issuers
    assert _split_off("--yes").exit_code == 0
    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--from",
        "2024-01-01",
        "--to",
        "2024-01-31",
        "--new-symbol",
        "BID",
        "--new-name",
        "again",
        "-m",
        "n",
        "--yes",
    )
    assert out.exit_code != 0


# ---------------------------------------------------------------------------
# Into an existing security
# ---------------------------------------------------------------------------


def test_into_an_existing_security_refuses_a_disagreeing_session(two_issuers):
    db, sid = two_issuers
    qsi = _mk(db, "QSI", "Quantum-Si")
    _load(db, "QSI", {"2024-01-03": 99})

    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-01-31",
        "--into-security-id",
        str(qsi),
        "-m",
        "n",
        "--yes",
    )

    assert out.exit_code != 0
    assert "different prices" in out.output
    assert len(_bars(db, sid)) == len(OLD) + len(NEW)
    assert _bars(db, qsi) == {"2024-01-03": 99.0}


def test_into_an_existing_security_keeps_its_identical_sessions(two_issuers):
    db, sid = two_issuers
    qsi = _mk(db, "QSI", "Quantum-Si")
    _load(db, "QSI", {"2024-01-03": 36})

    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-01-31",
        "--into-security-id",
        str(qsi),
        "-m",
        "the SPAC period is already held on QSI",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert _bars(db, qsi) == {k: float(v) for k, v in OLD.items()}
    assert _bars(db, sid) == {k: float(v) for k, v in NEW.items()}
    assert db.fetchval("SELECT count(*) FROM core.security") == 2


# ---------------------------------------------------------------------------
# Bars an operator already deleted (--restore-deleted)
# ---------------------------------------------------------------------------


def test_restore_deleted_moves_bars_prices_delete_had_removed(two_issuers):
    """CAPA, 2026-09: the pre-launch bars were deleted before this command existed;
    their rows survive only in the overrides."""
    db, sid = two_issuers
    args = ["prices", "delete", "--symbol", "BID"]
    for d in OLD:
        args += ["--date", d]
    assert _run(*args, "-m", "pre-launch history", "--yes").exit_code == 0
    assert set(_bars(db, sid)) == set(NEW)

    out = _split_off("--restore-deleted", "--yes")

    assert out.exit_code == 0, out.output
    dest = _dest(db, sid)
    assert _bars(db, dest) == {k: float(v) for k, v in OLD.items()}
    # The original deletes stand: they are what keeps the vendor's copy off the
    # source, and the split did not create them.
    rows = db.fetchall(
        "SELECT revoked_at, detail FROM ops.operator_override "
        "WHERE security_id=%s AND target='daily_price'",
        (sid,),
    )
    assert len(rows) == len(OLD)
    assert all(r["revoked_at"] is None for r in rows)
    assert {r["detail"]["split"]["kind"] for r in rows} == {"restored"}
    _load(db, "BID", {**OLD, **NEW})
    assert set(_bars(db, sid)) == set(NEW)


# ---------------------------------------------------------------------------
# Flags and factors
# ---------------------------------------------------------------------------


def test_flags_about_a_moved_date_follow_the_rows(two_issuers):
    db, sid = two_issuers
    repo.add_dq_flag_once(
        db,
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2024-01-03"},
        check_name="outlier",
        severity="warn",
        detail={"move": 0.9},
    )
    repo.add_dq_flag_once(
        db,
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2024-06-03"},
        check_name="outlier",
        severity="warn",
        detail={"move": 0.7},
    )
    repo.add_dq_flag_once(
        db,
        security_id=sid,
        table_name="core.daily_price",
        record_key={},
        check_name="sparse_coverage",
        severity="info",
        detail={},
    )

    assert _split_off("--yes").exit_code == 0
    dest = _dest(db, sid)

    def keys(s):
        return sorted(
            (r["check_name"], (r["record_key"] or {}).get("trade_date"))
            for r in db.fetchall(
                "SELECT check_name, record_key FROM ops.data_quality_flag "
                "WHERE security_id=%s",
                (s,),
            )
        )

    assert keys(dest) == [("outlier", "2024-01-03")]
    assert keys(sid) == [("outlier", "2024-06-03"), ("sparse_coverage", None)]


def test_factors_follow_the_actions(two_issuers):
    db, sid = two_issuers
    from fafnir.ingest import adjustments

    adjustments.compute_for_security(db, sid)
    assert db.fetchval(
        "SELECT count(*) FROM core.adjustment_factor WHERE security_id=%s", (sid,)
    )

    assert _split_off("--yes").exit_code == 0
    dest = _dest(db, sid)

    assert (
        db.fetchval(
            "SELECT count(*) FROM core.adjustment_factor WHERE security_id=%s", (sid,)
        )
        == 0
    )
    assert db.fetchval(
        "SELECT count(*) FROM core.adjustment_factor WHERE security_id=%s", (dest,)
    )


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_puts_everything_back_and_deletes_the_empty_row(two_issuers):
    db, sid = two_issuers
    repo.add_dq_flag_once(
        db,
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2024-01-03"},
        check_name="outlier",
        severity="warn",
        detail={},
    )
    before_bars = _bars(db, sid)
    assert _split_off("--yes").exit_code == 0
    dest = _dest(db, sid)

    dry = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--into-security-id",
        str(dest),
        "--undo",
        "-m",
        "wrong boundary",
        "--dry-run",
    )
    assert dry.exit_code == 0, dry.output
    assert db.fetchval("SELECT count(*) FROM core.security") == 2

    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--into-security-id",
        str(dest),
        "--undo",
        "-m",
        "wrong boundary",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert _bars(db, sid) == before_bars
    assert _actions(db, sid) == [("dividend", "2024-01-04", "fmp")]
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.operator_override WHERE revoked_at IS NULL"
        )
        == 0
    )
    assert (
        db.fetchval(
            "SELECT security_id FROM ops.data_quality_flag WHERE check_name='outlier'"
        )
        == sid
    )
    assert repo.suppressed_price_dates(db, sid) == frozenset()


def test_undo_of_a_restore_leaves_the_original_deletes_standing(two_issuers):
    db, sid = two_issuers
    args = ["prices", "delete", "--symbol", "BID"]
    for d in OLD:
        args += ["--date", d]
    assert _run(*args, "-m", "pre-launch history", "--yes").exit_code == 0
    assert _split_off("--restore-deleted", "--yes").exit_code == 0
    dest = _dest(db, sid)

    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--into-security-id",
        str(dest),
        "--undo",
        "-m",
        "undo",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert set(_bars(db, sid)) == set(NEW)
    assert repo.suppressed_price_dates(db, sid) == frozenset(
        dt.date.fromisoformat(d) for d in OLD
    )
    assert all(
        "split" not in r["detail"]
        for r in db.fetchall(
            "SELECT detail FROM ops.operator_override WHERE target='daily_price'"
        )
    )


def test_undo_into_an_existing_security_leaves_its_own_sessions(two_issuers):
    db, sid = two_issuers
    qsi = _mk(db, "QSI", "Quantum-Si")
    _load(db, "QSI", {"2024-01-03": 36, "2024-07-01": 12})
    before = _bars(db, sid)
    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--to",
        "2024-01-31",
        "--into-security-id",
        str(qsi),
        "-m",
        "n",
        "--yes",
    )
    assert out.exit_code == 0, out.output

    out = _run(
        "security",
        "split-history",
        "--symbol",
        "BID",
        "--into-security-id",
        str(qsi),
        "--undo",
        "-m",
        "undo",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert _bars(db, sid) == before
    assert _bars(db, qsi) == {"2024-01-03": 36.0, "2024-07-01": 12.0}
    assert _actions(db, qsi) == []
    assert db.fetchval("SELECT count(*) FROM core.security") == 2
