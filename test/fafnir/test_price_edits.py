"""`fafnir prices shift|rescale`: re-date or re-scale bars, and keep them corrected.

The feature is the same contrast 0025's corrections are built on. FVI and WLL carry
their whole history one day early and EQC a decade at 1/20 scale, in the vendor's
payload as well as the warehouse, so a re-fetch changes nothing and a hand-written
UPDATE is overwritten by the next load. The first tests are that contrast; the rest
guard the record kept, the refusals, the dry run and the undo.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration


class _FakeFMP:
    bytes_downloaded = 0
    request_count = 0

    def __init__(self, bars):
        self._bars = bars

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return list(self._bars)


def _mk(db, symbol="EDIT"):
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


def _vbar(d, o, h, lo, c, volume=1000, vwap=None):
    bar = {"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": volume}
    if vwap is not None:
        bar["vwap"] = vwap
    return bar


def _load(db, symbol, bars, start="2024-01-01"):
    stats: dict = {}
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(
            db,
            _FakeFMP(bars),
            symbol,
            run=run,
            start_date=dt.date.fromisoformat(start),
            stats=stats,
        )
    return stats


def _bars(db, sid):
    return {
        str(r["trade_date"]): r
        for r in db.fetchall(
            "SELECT trade_date, open, high, low, close, volume, vwap, source "
            "FROM core.daily_price WHERE security_id=%s ORDER BY trade_date",
            (sid,),
        )
    }


def _closes(db, sid):
    return {d: float(r["close"]) for d, r in _bars(db, sid).items()}


def _run(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args], catch_exceptions=False)


def _fails(*args):
    return CliRunner().invoke(cli.main, [str(a) for a in args])


@pytest.fixture()
def cli_db(db, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    return db


# A week of real sessions, Mon 2024-06-03 .. Fri 06-07, as the vendor dates them:
# one calendar day early, so Sun 06-02 .. Thu 06-06 and no Friday.
EARLY = [
    _vbar("2024-06-02", 10, 11, 9, 10.5),
    _vbar("2024-06-03", 10.5, 12, 10, 11),
    _vbar("2024-06-04", 11, 12, 10.5, 11.5),
    _vbar("2024-06-05", 11.5, 13, 11, 12),
    _vbar("2024-06-06", 12, 13, 11.5, 12.5),
]


def _store_early(db, sid):
    """Store EARLY as a load before the non-session set-aside would have."""
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date.fromisoformat(b["date"]),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
            for b in EARLY
        ],
    )


def _shift_week(sid, *extra):
    return _run(
        "prices",
        "shift",
        "--security-id",
        sid,
        "--from",
        "2024-06-02",
        "--to",
        "2024-06-06",
        "--days",
        1,
        "-m",
        "history one day early: Sunday bars, no Fridays",
        *extra,
    )


# ---------------------------------------------------------------------------
# The contrast
# ---------------------------------------------------------------------------
def test_a_hand_rescaled_bar_is_overwritten_by_the_next_load(db):
    sid = _mk(db, "EQC")
    bars = [_vbar("2024-06-03", 1, 1, 1, 1), _vbar("2024-06-04", 20, 20, 20, 20)]
    _load(db, "EQC", bars)
    db.execute(
        "UPDATE core.daily_price SET open=20, high=20, low=20, close=20 "
        "WHERE security_id=%s AND trade_date='2024-06-03'",
        (sid,),
    )

    _load(db, "EQC", bars)

    assert _closes(db, sid)["2024-06-03"] == 1.0


def test_a_command_rescaled_bar_survives_the_next_load(cli_db):
    db = cli_db
    sid = _mk(db, "EQC")
    bars = [_vbar("2024-06-03", 1, 1, 1, 1), _vbar("2024-06-04", 20, 20, 20, 20)]
    _load(db, "EQC", bars)

    out = _run(
        "prices",
        "rescale",
        "--symbol",
        "EQC",
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-03",
        "--factor",
        20,
        "-m",
        "pre-change bars at 1/20",
        "--yes",
    )
    assert out.exit_code == 0, out.output
    stats = _load(db, "EQC", bars)

    assert _closes(db, sid) == {"2024-06-03": 20.0, "2024-06-04": 20.0}
    assert _bars(db, sid)["2024-06-03"]["source"] == "operator"
    assert stats["suppressed"] == 1


def test_a_shifted_history_survives_a_reload_of_the_misdated_payload(cli_db):
    db = cli_db
    sid = _mk(db, "WLL")
    _store_early(db, sid)

    out = _shift_week(sid, "--yes")
    assert out.exit_code == 0, out.output
    stats = _load(db, "WLL", EARLY, start="2024-06-01")

    assert list(_bars(db, sid)) == [
        "2024-06-03",
        "2024-06-04",
        "2024-06-05",
        "2024-06-06",
        "2024-06-07",
    ]
    assert _closes(db, sid)["2024-06-07"] == 12.5
    assert {r["source"] for r in _bars(db, sid).values()} == {"operator"}
    # The Sunday bar is non-session; the four weekday ones land on edited dates.
    assert stats["suppressed"] == 4
    assert stats["non_session"] == 1


# ---------------------------------------------------------------------------
# rescale
# ---------------------------------------------------------------------------
def test_rescale_writes_prices_volume_and_vwap_and_records_the_edit(cli_db):
    db = cli_db
    sid = _mk(db, "SCAL")
    _load(
        db,
        "SCAL",
        [
            _vbar("2024-06-03", 1, 1.2, 0.9, 1.1, volume=20000, vwap=1.05),
            _vbar("2024-06-04", 1.1, 1.3, 1.0, 1.2, volume=30001),
        ],
    )

    out = _run(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-04",
        "--factor",
        20,
        "--volume-factor",
        "1/20",
        "-m",
        "1/20 scale",
        "--by",
        "claude",
        "--yes",
    )
    assert out.exit_code == 0, out.output

    b = _bars(db, sid)
    assert [float(b["2024-06-03"][f]) for f in ("open", "high", "low", "close")] == [
        20.0,
        24.0,
        18.0,
        22.0,
    ]
    assert float(b["2024-06-03"]["vwap"]) == 21.0
    assert (b["2024-06-03"]["volume"], b["2024-06-04"]["volume"]) == (1000, 1500)

    overrides = repo.list_operator_overrides(db, security_id=sid)
    assert sorted((o["operation"], str(o["key_date"])) for o in overrides) == [
        ("add", "2024-06-03"),
        ("add", "2024-06-04"),
        ("delete", "2024-06-03"),
        ("delete", "2024-06-04"),
    ]
    edits = {o["detail"]["transform"]["edit"] for o in overrides}
    assert len(edits) == 1
    delete = next(
        o
        for o in overrides
        if o["operation"] == "delete" and str(o["key_date"]) == "2024-06-03"
    )
    add = next(
        o
        for o in overrides
        if o["operation"] == "add" and str(o["key_date"]) == "2024-06-03"
    )
    assert delete["detail"]["row"]["close"] == "1.100000"
    assert delete["detail"]["transform"]["to_override"] == add["override_id"]
    assert add["detail"]["transform"]["from_override"] == delete["override_id"]
    assert add["detail"]["transform"]["price_factor"] == "20"
    assert (add["created_by"], add["note"]) == ("claude", "1/20 scale")
    assert "Undo with `fafnir override revoke" in out.output


def test_rescale_dry_run_prints_both_joins_and_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db, "JOIN")
    _load(
        db,
        "JOIN",
        [
            _vbar("2024-06-03", 1, 1, 1, 1),
            _vbar("2024-06-04", 1, 1, 1, 1),
            _vbar("2024-06-05", 20, 20, 20, 20),
        ],
    )
    before = _bars(db, sid)

    out = _run(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-04",
        "--to",
        "2024-06-04",
        "--factor",
        20,
        "-m",
        "x",
        "--dry-run",
    )

    assert out.exit_code == 0, out.output
    assert "Into the range: 2024-06-03 close 1 -> 2024-06-04 close 1" in out.output
    assert "after the edit 20 (x20)" in out.output
    assert "Out of the range: 2024-06-04 close 1 -> 2024-06-05 close 20 (x20)" in (
        out.output
    )
    assert "after the edit 20 -> 20 (x1)" in out.output
    assert "Dry run: 1 bar would be rescaled" in out.output
    assert _bars(db, sid) == before
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_rescale_refuses_a_bar_the_loader_would_quarantine_and_writes_nothing(cli_db):
    db = cli_db
    sid = _mk(db, "TINY")
    _load(
        db,
        "TINY",
        [
            _vbar("2024-06-03", 5, 5, 5, 5),
            _vbar("2024-06-04", 0.001, 0.001, 0.001, 0.001),
        ],
    )

    out = _fails(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-04",
        "--factor",
        "1/10000",
        "-m",
        "x",
        "--yes",
    )

    assert out.exit_code != 0
    assert "subresolution_price" in out.output
    assert "2024-06-04" in out.output
    assert _closes(db, sid) == {"2024-06-03": 5.0, "2024-06-04": 0.001}
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_rescale_refuses_an_empty_range(cli_db):
    sid = _mk(cli_db, "NONE")
    out = _fails(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-04",
        "--factor",
        2,
        "-m",
        "x",
        "--yes",
    )
    assert out.exit_code != 0
    assert "No stored bars" in out.output


def test_rescale_refuses_bars_an_operator_already_edited(cli_db):
    db = cli_db
    sid = _mk(db, "TWICE")
    _load(db, "TWICE", [_vbar("2024-06-03", 1, 1, 1, 1)])
    args = (
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-03",
        "--factor",
        2,
        "-m",
        "x",
        "--yes",
    )
    assert _run(*args).exit_code == 0

    out = _fails(*args)

    assert out.exit_code != 0
    assert "already written by an operator" in out.output
    assert _closes(db, sid) == {"2024-06-03": 2.0}


def test_rescale_recomputes_dividend_factors_against_the_corrected_close(cli_db):
    db = cli_db
    sid = _mk(db, "DIVS")
    _load(
        db,
        "DIVS",
        [_vbar("2024-06-03", 1, 1, 1, 1), _vbar("2024-06-04", 20, 20, 20, 20)],
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 4),
        dividend_amount=Decimal("0.5"),
    )
    adjustments.compute_for_security(db, sid)

    def factor():
        return float(
            db.fetchval(
                "SELECT min(cumulative_price_factor) FROM core.adjustment_factor "
                "WHERE security_id=%s",
                (sid,),
            )
        )

    assert factor() == pytest.approx(0.5)  # 0.5 on a prior close of 1

    out = _run(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-03",
        "--factor",
        20,
        "-m",
        "x",
        "--yes",
    )

    assert out.exit_code == 0, out.output
    assert factor() == pytest.approx(0.975)  # 0.5 on a prior close of 20
    assert "Factors:" in out.output


# ---------------------------------------------------------------------------
# shift
# ---------------------------------------------------------------------------
def test_shift_dry_run_shows_the_weekday_shape_and_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db, "FVI")
    _store_early(db, sid)
    before = _bars(db, sid)

    out = _shift_week(sid, "--dry-run")

    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    before_line = next(line for line in lines if line.startswith("before"))
    after_line = next(line for line in lines if line.startswith("after"))
    assert before_line.split()[1:] == ["1", "1", "1", "1", "0", "0", "1"]
    assert after_line.split()[1:] == ["1", "1", "1", "1", "1", "0", "0"]
    assert "Dry run: 5 bars would be shifted" in out.output
    assert _bars(db, sid) == before
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_shift_refuses_a_bar_landing_on_one_outside_the_range(cli_db):
    db = cli_db
    sid = _mk(db, "CLSH")
    _store_early(db, sid)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date(2024, 6, 7),
                "open": 99,
                "high": 99,
                "low": 99,
                "close": 99,
                "volume": 1,
            }
        ],
    )

    out = _fails(
        "prices",
        "shift",
        "--security-id",
        sid,
        "--from",
        "2024-06-02",
        "--to",
        "2024-06-06",
        "--days",
        1,
        "-m",
        "x",
        "--yes",
    )

    assert out.exit_code != 0
    assert "already has a bar outside the range" in out.output
    assert "2024-06-07" in out.output
    assert repo.list_operator_overrides(db, include_revoked=True) == []


def test_shift_refuses_a_non_session_target_unless_allowed(cli_db):
    db = cli_db
    sid = _mk(db, "SATD")
    _load(db, "SATD", [_vbar("2024-06-07", 10, 10, 10, 10)])  # a Friday
    args = [
        "prices",
        "shift",
        "--security-id",
        sid,
        "--from",
        "2024-06-07",
        "--to",
        "2024-06-07",
        "--days",
        1,
        "-m",
        "x",
        "--yes",
    ]

    refused = _fails(*args)
    assert refused.exit_code != 0
    assert "2024-06-08 (Sat)" in refused.output
    assert "--allow-non-session" in refused.output
    assert list(_bars(db, sid)) == ["2024-06-07"]

    allowed = _run(*args, "--allow-non-session")
    assert allowed.exit_code == 0, allowed.output
    assert list(_bars(db, sid)) == ["2024-06-08"]


def test_shift_refuses_a_future_date(cli_db):
    db = cli_db
    sid = _mk(db, "FUTR")
    today = dt.date.today()
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": today,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
    )
    out = _fails(
        "prices",
        "shift",
        "--security-id",
        sid,
        "--from",
        today,
        "--to",
        today,
        "--days",
        3,
        "--allow-non-session",
        "-m",
        "x",
        "--yes",
    )
    assert out.exit_code != 0
    assert "in the future" in out.output


def test_a_shift_off_the_end_of_the_calendar_is_refused(cli_db):
    """`--days` was unbounded, and a typed one is how a century-long shift arises. The
    session check cannot catch it: "no calendar row" means closed inside the calendar's
    span and unknown outside it, so a 1914 target read as a session. The bar landed in
    core.daily_price_default, which then blocks ensure_year_partition for that year."""
    db = cli_db
    sid = _mk(db, "FAR")
    _store_early(db, sid)

    out = _run(
        "prices",
        "shift",
        "--symbol",
        "FAR",
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-05",
        "--days",
        "-40000",
        "--allow-non-session",
        "-m",
        "typo",
        "--yes",
    )

    assert out.exit_code != 0
    assert "outside the venue calendar's span" in out.output
    assert "1914" in out.output
    # Nothing was written, and no bar escaped into the default partition.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id=%s "
            "AND trade_date < '2000-01-01'",
            (sid,),
        )
        == 0
    )
    assert db.fetchval("SELECT count(*) FROM ops.operator_override") == 0


def test_shift_leaves_corporate_actions_unless_asked(cli_db):
    db = cli_db
    sid = _mk(db, "ACTS")
    _store_early(db, sid)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 6, 4),
        dividend_amount=Decimal("0.1"),
    )

    plain = _run(*_shift_args(sid), "--dry-run")
    assert "stay where they are" in plain.output
    assert "2024-06-04  Tue" in plain.output

    moved = _run(*_shift_args(sid), "--with-actions", "--yes")
    assert moved.exit_code == 0, moved.output
    assert [
        (str(r["ex_date"]), r["source"])
        for r in db.fetchall(
            "SELECT ex_date, source FROM core.corporate_action WHERE security_id=%s",
            (sid,),
        )
    ] == [("2024-06-05", "operator")]


def test_revoking_a_with_actions_shift_takes_the_action_back_too(cli_db):
    """The command tells the operator to undo with `override revoke`, and the runbook
    says that undoes the whole edit. It used to restore the bars and leave the action
    a calendar day away from them, with the true ex-date still suppressed against
    `ingest actions` and the wrong date already baked into core.adjustment_factor --
    an adjusted series silently out by one session, which is the damage 0025 and 0026
    exist to prevent."""
    db = cli_db
    sid = _mk(db, "ACTS")
    _store_early(db, sid)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2024, 6, 4),
        split_numerator=2,
        split_denominator=1,
    )
    assert _run(*_shift_args(sid), "--with-actions", "--yes").exit_code == 0
    edit = db.fetchval(
        "SELECT min(override_id) FROM ops.operator_override "
        "WHERE target='daily_price' AND revoked_at IS NULL"
    )

    out = _run("override", "revoke", str(edit), "-m", "wrong call", "--yes")

    assert out.exit_code == 0, out.output
    # The action is back on its own ex-date, as the vendor's row.
    assert [
        (str(r["ex_date"]), r["source"])
        for r in db.fetchall(
            "SELECT ex_date, source FROM core.corporate_action WHERE security_id=%s",
            (sid,),
        )
    ] == [("2024-06-04", "fmp")]
    # Nothing is left suppressed, so a later `ingest actions` behaves normally.
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.operator_override WHERE revoked_at IS NULL"
        )
        == 0
    )
    # And the factors were recomputed against the restored ex-date (2024-06-04), not
    # the date the shift had moved it to (2024-06-05). An adjusted series out by one
    # session is the damage this guards.
    assert [
        str(r["effective_date"])
        for r in db.fetchall(
            "SELECT effective_date FROM core.adjustment_factor WHERE security_id=%s "
            "ORDER BY effective_date",
            (sid,),
        )
    ] == ["2024-06-04"]


def _shift_args(sid):
    return (
        "prices",
        "shift",
        "--security-id",
        sid,
        "--from",
        "2024-06-02",
        "--to",
        "2024-06-06",
        "--days",
        1,
        "-m",
        "x",
    )


# ---------------------------------------------------------------------------
# Undo, and the commands that must leave an edit alone
# ---------------------------------------------------------------------------
def test_revoking_a_shift_restores_the_vendor_bars_at_their_dates(cli_db):
    db = cli_db
    sid = _mk(db, "UNDO")
    _load(
        db, "UNDO", [_vbar("2024-06-03", 1, 1, 1, 1), _vbar("2024-06-04", 2, 2, 2, 2)]
    )
    original = _bars(db, sid)
    assert (
        _run(
            "prices",
            "shift",
            "--security-id",
            sid,
            "--from",
            "2024-06-03",
            "--to",
            "2024-06-04",
            "--days",
            1,
            "-m",
            "x",
            "--yes",
        ).exit_code
        == 0
    )
    assert list(_bars(db, sid)) == ["2024-06-04", "2024-06-05"]
    add = next(
        o
        for o in repo.list_operator_overrides(db, security_id=sid)
        if o["operation"] == "add"
    )

    # Any override of the edit undoes all of it -- here, an 'add' half.
    out = _run("override", "revoke", add["override_id"], "-m", "wrong call", "--yes")

    assert out.exit_code == 0, out.output
    assert "all 4 overrides of it are revoked together" in " ".join(out.output.split())
    assert _bars(db, sid) == original
    assert repo.list_operator_overrides(db, security_id=sid) == []
    assert {
        o["revoked_note"]
        for o in repo.list_operator_overrides(db, security_id=sid, include_revoked=True)
    } == {"wrong call"}

    # And the vendor's bars load normally again.
    stats = _load(db, "UNDO", [_vbar("2024-06-03", 1, 1, 1, 1)])
    assert stats.get("suppressed", 0) == 0


def test_revoking_one_half_directly_is_refused(cli_db):
    db = cli_db
    sid = _mk(db, "HALF")
    _load(db, "HALF", [_vbar("2024-06-03", 1, 1, 1, 1)])
    assert (
        _run(
            "prices",
            "rescale",
            "--security-id",
            sid,
            "--from",
            "2024-06-03",
            "--to",
            "2024-06-03",
            "--factor",
            2,
            "-m",
            "x",
            "--yes",
        ).exit_code
        == 0
    )
    half, _ = repo.list_operator_overrides(db, security_id=sid)

    with pytest.raises(repo.OverrideRefused, match="bar edit"):
        repo.revoke_operator_override(
            db, override_id=half["override_id"], note="x", revoked_by="x"
        )


def test_revoke_dry_run_changes_nothing(cli_db):
    db = cli_db
    sid = _mk(db, "DRYR")
    _load(db, "DRYR", [_vbar("2024-06-03", 1, 1, 1, 1)])
    out = _run(
        "prices",
        "rescale",
        "--security-id",
        sid,
        "--from",
        "2024-06-03",
        "--to",
        "2024-06-03",
        "--factor",
        2,
        "-m",
        "x",
        "--yes",
    )
    edited = _bars(db, sid)
    edit = repo.list_operator_overrides(db, security_id=sid)[0]["override_id"]

    out = _run("override", "revoke", edit, "-m", "x", "--dry-run")

    assert out.exit_code == 0, out.output
    assert "Dry run: nothing changed." in out.output
    assert _bars(db, sid) == edited
    assert len(repo.list_operator_overrides(db, security_id=sid)) == 2


def test_prices_delete_refuses_an_operator_bar_and_points_at_revoke(cli_db):
    db = cli_db
    sid = _mk(db, "KEEP")
    _load(db, "KEEP", [_vbar("2024-06-03", 1, 1, 1, 1)])
    assert (
        _run(
            "prices",
            "rescale",
            "--security-id",
            sid,
            "--from",
            "2024-06-03",
            "--to",
            "2024-06-03",
            "--factor",
            2,
            "-m",
            "x",
            "--yes",
        ).exit_code
        == 0
    )

    out = _fails(
        "prices",
        "delete",
        "--security-id",
        sid,
        "--date",
        "2024-06-03",
        "-m",
        "x",
        "--yes",
    )

    assert out.exit_code != 0
    assert "fafnir override revoke" in out.output
    assert _closes(db, sid) == {"2024-06-03": 2.0}


def test_override_list_names_the_transform(cli_db):
    db = cli_db
    sid = _mk(db, "LIST")
    _store_early(db, sid)
    assert _shift_week(sid, "--yes").exit_code == 0

    out = _run("override", "list", "--symbol", "LIST")

    assert "add (shift)" in out.output
    assert "delete (shift)" in out.output


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def test_a_bare_bar_add_is_still_refused_by_the_table(db):
    sid = _mk(db)
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO ops.operator_override "
            "(security_id, target, key_date, operation, detail, note, created_by) "
            "VALUES (%s, 'daily_price', '2024-06-05', 'add', '{\"row\": {}}', 'n', 'x')",
            (sid,),
        )


def test_a_bar_add_carrying_its_transform_is_accepted_by_the_table(db):
    sid = _mk(db)
    db.execute(
        "INSERT INTO ops.operator_override "
        "(security_id, target, key_date, operation, detail, note, created_by) "
        "VALUES (%s, 'daily_price', '2024-06-05', 'add', "
        '\'{"transform": {"kind": "rescale", "edit": 1}}\', \'n\', \'x\')',
        (sid,),
    )
    assert dt.date(2024, 6, 5) in repo.suppressed_price_dates(db, sid)


def test_a_bar_add_whose_transform_names_no_edit_is_refused_by_the_table(db):
    """`edit` is what makes the record undoable: `revoke_price_edit` finds an edit's
    halves by it. A bar add without one is a row the revoke path cannot resolve -- it
    reported the operator's bar removed, deleted nothing (the branch it took deletes a
    corporate action), and then revoked the override that was protecting the bar,
    leaving it in core.daily_price with nothing recording it."""
    sid = _mk(db)
    with pytest.raises(
        Exception, match="ck_operator_override_price_delete_or_transform"
    ):
        db.execute(
            "INSERT INTO ops.operator_override "
            "(security_id, target, key_date, operation, detail, note, created_by) "
            "VALUES (%s, 'daily_price', '2024-06-05', 'add', "
            "'{\"transform\": {\"kind\": \"rescale\"}}', 'n', 'x')",
            (sid,),
        )


def test_the_down_migration_refuses_while_bar_transforms_exist(db, migrated_dsn):
    from pathlib import Path

    from fafnir.db.connection import Database

    sid = _mk(db)
    db.execute(
        "INSERT INTO ops.operator_override "
        "(security_id, target, key_date, operation, detail, note, created_by) "
        "VALUES (%s, 'daily_price', '2024-06-05', 'add', "
        '\'{"transform": {"kind": "shift", "edit": 1}}\', \'n\', \'x\')',
        (sid,),
    )
    migrations = Path(__file__).resolve().parents[2] / "sql" / "migrations"
    down = (migrations / "0026_operator_bar_transforms.down.sql").read_text()
    up = (migrations / "0026_operator_bar_transforms.up.sql").read_text()

    with Database(migrated_dsn, autocommit=True) as other:
        with pytest.raises(Exception, match="cannot roll back 0026"):
            other.execute_script(down)
        other.execute("ROLLBACK")

    # With no transforms, down and up both apply and converge.
    db.execute("DELETE FROM ops.operator_override")
    with Database(migrated_dsn, autocommit=True) as other:
        other.execute_script(down)
        assert (
            other.fetchval(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'ck_operator_override_price_delete_only'"
            )
            == 1
        )
        other.execute_script(up)
        assert (
            other.fetchval(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = 'ck_operator_override_price_delete_or_transform'"
            )
            == 1
        )
