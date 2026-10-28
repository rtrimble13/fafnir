"""A fed security held inactive (#125): `ingest sharadar-universe` step 4 flags it,
and `fafnir security relist` lists it trading again on the vendor's ticker.

At the Sharadar cutover some 87 rows FMP had called inactive -- most of them renames
FMP's delisted feed reported as delistings (NLOK when it became GEN) -- were linked by
permaticker and fed. They took the company's bars every night while fafnir held them
inactive on the old ticker. Every key, ticker and price here is synthetic (ADR 0012
§8). The tests need FAFNIR_TEST_DSN.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import relist as rl
from fafnir.ingest import sharadar_universe as su

pytestmark = pytest.mark.integration

Y = date
SINCE = Y(2026, 9, 1)
NEWEST = Y(2026, 10, 20)  # the vendor's newest price date (a Tuesday)
RECENT = (Y(2026, 10, 19), NEWEST)
INACTIVE = "security_inactive_but_trading"


def _vendor(db, key, ticker, *, delisted=False, last=NEWEST, exchange="NYSE"):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, category, exchange, is_delisted, first_price_date, last_price_date) "
        "VALUES ('sharadar', 'stocks', %s, %s, %s, 'Domestic Common Stock', %s, "
        "%s, %s, %s)",
        (key, ticker, f"{ticker} CORP", exchange, delisted, Y(2000, 1, 3), last),
    )


def _rename(db, key, day, old, new):
    for action, contra in (("tickerchangefrom", old), ("tickerchangeto", new)):
        db.execute(
            "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
            "action, ticker, contra_ticker, contra_name) "
            "VALUES ('sharadar', %s, %s, %s, %s, %s, 'N/A')",
            (key, day, action, new, contra),
        )


def _bars(db, sid, *days, volume=100):
    c = Decimal("10")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": volume,
            }
            for d in days
        ],
    )


def _security(
    db,
    symbol,
    key=None,
    *,
    active=True,
    delisted=None,
    feed=True,
    bars=RECENT,
    link_from=Y(2000, 1, 3),
):
    """A security on ``symbol``; ``delisted`` stamps it the way the FMP sweep did
    (mark_delisted: inactive, its ticker period closed at the date)."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        is_actively_trading=active,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    if delisted is not None:
        assert repo.mark_delisted(db, security_id=sid, delisted_date=delisted)
    if key:
        repo.insert_vendor_link(
            db,
            security_id=sid,
            vendor="sharadar",
            vendor_key=key,
            valid_from=link_from,
            match_method="manual",
            confidence=None,
            evidence={},
            created_by="tester",
            is_feed=feed,
        )
    if bars:
        _bars(db, sid, *bars)
    return sid


def _row(db, sid):
    return db.fetchone("SELECT * FROM core.security WHERE security_id = %s", (sid,))


def _periods(db, sid):
    return [
        (r["symbol"], r["valid_from"], r["valid_to"])
        for r in db.fetchall(
            "SELECT symbol, valid_from, valid_to FROM core.symbol_xref "
            "WHERE security_id = %s ORDER BY valid_from",
            (sid,),
        )
    ]


def _inactive_flags(db, state="open"):
    cond = "resolved_at IS NULL" if state == "open" else "resolved_at IS NOT NULL"
    return {
        int(r["security_id"]): r
        for r in db.fetchall(
            f"SELECT * FROM ops.data_quality_flag WHERE check_name = %s AND {cond}",
            (INACTIVE,),
        )
    }


def _outcomes(result):
    return {c.symbol: c.outcome for c in result.changes if c.step == "inactive"}


def _shell(db):
    """ZZR1, renamed ZZR2 on 2025-02-28 and stamped delisted 2025-03-03 by FMP's
    sweep, fed by the vendor key that now trades as ZZR2."""
    _vendor(db, "900501", "ZZR2")
    _rename(db, "900501", Y(2025, 2, 28), "ZZR1", "ZZR2")
    return _security(db, "ZZR1", "900501", active=False, delisted=Y(2025, 3, 3))


@pytest.fixture()
def run_cli(db, tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "security", *map(str, args)]
        )

    return run


# ---------------------------------------------------------------------------
# Step 4 of `ingest sharadar-universe`
# ---------------------------------------------------------------------------


def test_the_vendor_spells_a_share_class_with_a_dot():
    assert su.fafnir_ticker("brk.b") == "BRK-B"
    assert su.fafnir_ticker("N/A") is None


def test_the_latest_rename_to_a_ticker_is_found_whatever_its_date():
    def e(day, action, contra):
        return {
            "event_date": day,
            "action": action,
            "ticker": "ZZ.C",
            "contra_ticker": contra,
        }

    events = [
        e(Y(2019, 5, 1), "tickerchangefrom", "ZZA"),
        e(Y(2019, 5, 1), "tickerchangeto", "ZZB"),
        e(Y(2021, 7, 1), "tickerchangefrom", "ZZB"),
        e(Y(2021, 7, 1), "tickerchangeto", "ZZ.C"),
    ]
    assert su.latest_rename_to(events, "ZZ-C") == (Y(2021, 7, 1), "ZZB", "ZZ.C")
    assert su.latest_rename_to(events, "ZZQ") is None


def test_the_universe_flags_a_fed_security_held_inactive(db):
    shell = _shell(db)
    _vendor(db, "900502", "ZZS")
    in_place = _security(db, "ZZS", "900502", active=False)
    # Two rows for one company: the shell is fed, the other row trades as ZZT2.
    _vendor(db, "900503", "ZZT2")
    _rename(db, "900503", Y(2024, 6, 3), "ZZT1", "ZZT2")
    held = _security(db, "ZZT1", "900503", active=False, delisted=Y(2024, 6, 3))
    successor = _security(db, "ZZT2")
    # Not flagged: the vendor calls it delisted; its bars stopped; not fed.
    _vendor(db, "900504", "ZZU", delisted=True)
    _security(db, "ZZU", "900504", active=False)
    _vendor(db, "900505", "ZZV")
    _security(db, "ZZV", "900505", active=False, bars=(Y(2026, 9, 1),))
    _vendor(db, "900506", "ZZW")
    _security(db, "ZZW", "900506", active=False, feed=False)
    # Same ticker on another venue: a venue transfer (ADR 0014).
    _vendor(db, "900509", "ZZM", exchange="TXSE")
    moved = _security(db, "ZZM", "900509", active=False)

    result = su.run(db, since=SINCE)
    assert _outcomes(result) == {
        "ZZR1->ZZR2": "flagged",
        "ZZS": "flagged",
        "ZZT1->ZZT2": "flagged",
        "ZZM": "flagged",
    }
    flags = _inactive_flags(db)
    assert set(flags) == {shell, in_place, held, moved}
    assert flags[moved]["detail"]["next"] == "venue_transfer"
    assert flags[moved]["detail"]["vendor_exchange"] == "TXSE"
    d = flags[shell]["detail"]
    assert flags[shell]["severity"] == "warn"
    assert flags[shell]["record_key"] == {"symbol": "ZZR1"}
    assert d["source"] == "sharadar" and d["vendor_key"] == "900501"
    assert d["rename"] == {"date": "2025-02-28", "old": "ZZR1", "new": "ZZR2"}
    assert (d["delisted_date"], d["next"], d["held_by"]) == (
        "2025-03-03",
        "relist",
        None,
    )
    assert flags[in_place]["detail"]["rename"] is None
    assert flags[held]["detail"]["held_by"] == successor
    assert flags[held]["detail"]["next"] == "held"
    # It changed no security.
    assert _row(db, shell)["primary_symbol"] == "ZZR1"
    assert not _row(db, in_place)["is_actively_trading"]

    # Re-detected, not re-flagged.
    again = su.run(db, since=SINCE)
    assert set(_outcomes(again).values()) == {"open"}
    assert len(_inactive_flags(db)) == 4

    # Trading again: the next run resolves its flag, and only that one.
    db.execute(
        "UPDATE core.security SET is_actively_trading = TRUE WHERE security_id = %s",
        (in_place,),
    )
    fixed = su.run(db, since=SINCE)
    assert _outcomes(fixed)["ZZS"] == "resolved"
    assert set(_inactive_flags(db)) == {shell, held, moved}
    assert in_place in _inactive_flags(db, "resolved")


def test_a_pending_venue_decision_is_named_and_the_dry_run_writes_nothing(db):
    _vendor(db, "900507", "ZZX")
    sid = _security(db, "ZZX", "900507", active=False)
    repo.add_dq_flag_once(
        db,
        check_name="security_delisting_contradicted",
        security_id=sid,
        record_key={"symbol": "ZZX", "delisted_date": "2026-10-05"},
    )
    dry = su.run(db, since=SINCE, dry_run=True)
    (change,) = [c for c in dry.changes if c.step == "inactive"]
    assert (change.outcome, change.detail) == ("flagged", "pending_decision")
    assert not _inactive_flags(db)


def test_a_flag_another_path_raised_is_left_alone(db):
    """FMP's universe load raises the same check, without a ``source``: step 4
    neither duplicates nor resolves it."""
    sid = _security(db, "ZZY", active=False)
    repo.add_dq_flag_once(
        db,
        check_name=INACTIVE,
        severity="info",
        security_id=sid,
        record_key={"symbol": "ZZY"},
        detail={"last_bar": "2026-10-20", "vendor_exchange": "NYSE"},
    )
    _vendor(db, "900508", "ZZY2")
    su.run(db, since=SINCE)
    assert set(_inactive_flags(db)) == {sid}


# ---------------------------------------------------------------------------
# `fafnir security relist`
# ---------------------------------------------------------------------------


def test_relist_moves_a_shell_to_the_vendors_ticker(db, run_cli):
    sid = _shell(db)
    su.run(db, since=SINCE)
    (flag_id,) = [int(f["dq_flag_id"]) for f in _inactive_flags(db).values()]
    repo.add_dq_flag_once(
        db,
        check_name="symbol_change_conflict",
        severity="error",
        security_id=sid,
        record_key={"old_symbol": "ZZR1", "new_symbol": "ZZR2"},
    )

    out = run_cli("relist", sid, "-m", "ZZR1 became ZZR2", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Fed by sharadar 900501: ZZR2" in out.output
    assert "Delisting reversed: 2025-03-03" in out.output
    assert "Rename: ZZR1 -> ZZR2 on 2025-02-28" in out.output
    assert f"Flags resolved: {flag_id}" in out.output
    assert "After: ZZR2, trading, delisted never" in out.output
    assert "Dry run: nothing changed." in out.output
    assert _row(db, sid)["primary_symbol"] == "ZZR1"
    assert not db.fetchall("SELECT 1 FROM ops.ingestion_run WHERE source = 'operator'")

    out = run_cli("relist", sid, "-m", "ZZR1 became ZZR2", "--by", "tester", "--yes")
    assert out.exit_code == 0, out.output
    assert "Relisted ZZR1" in out.output and "as ZZR2 from 2025-02-28" in out.output
    row = _row(db, sid)
    assert (
        row["primary_symbol"],
        row["is_actively_trading"],
        row["delisted_date"],
    ) == (
        "ZZR2",
        True,
        None,
    )
    # The old ticker ends the day before the rename; the new one is open from it.
    assert _periods(db, sid) == [
        ("ZZR1", Y(1900, 1, 1), Y(2025, 2, 27)),
        ("ZZR2", Y(2025, 2, 28), None),
    ]
    assert repo.resolve_security_id(db, "ZZR2") == sid
    change = db.fetchone("SELECT * FROM core.symbol_change")
    assert (change["old_symbol"], change["new_symbol"], change["change_date"]) == (
        "ZZR1",
        "ZZR2",
        Y(2025, 2, 28),
    )
    assert (change["status"], change["source"]) == ("applied", "sharadar")
    assert change["detail"]["reason"] == "relist" and change["detail"]["by"] == "tester"
    assert not _inactive_flags(db)
    assert not db.fetchall(
        "SELECT 1 FROM ops.data_quality_flag WHERE resolved_at IS NULL"
    )
    run = db.fetchone(
        "SELECT * FROM ops.ingestion_run WHERE endpoint = 'security-relist'"
    )
    assert (run["source"], run["status"]) == ("operator", "success")
    assert run["params"]["rename"]["new"] == "ZZR2"
    assert run["params"]["flags_resolved"] == [flag_id]

    # The universe agrees now, and a second relist has nothing to do.
    assert _outcomes(su.run(db, since=SINCE)) == {}
    out = run_cli("relist", sid, "-m", "again", "--dry-run")
    assert out.exit_code != 0 and "is trading already, on ZZR2" in out.output


def test_relist_in_place_reopens_the_ticker_a_stamp_closed(db, run_cli):
    _vendor(db, "900511", "ZZP")
    stamped = _security(db, "ZZP", "900511", active=False, delisted=Y(2026, 10, 5))
    _vendor(db, "900512", "ZZQ")
    unstamped = _security(db, "ZZQ", "900512", active=False)

    for sid in (stamped, unstamped):
        out = run_cli("relist", sid, "-m", "trades on", "--yes")
        assert out.exit_code == 0, out.output
        row = _row(db, sid)
        assert row["is_actively_trading"] and row["delisted_date"] is None
    assert "Ticker period re-opened: ZZP" not in out.output  # ZZQ's was never closed
    assert _periods(db, stamped) == [("ZZP", Y(1900, 1, 1), None)]
    assert _periods(db, unstamped) == [("ZZQ", Y(1900, 1, 1), None)]
    assert not db.fetchall("SELECT 1 FROM core.symbol_change")


def test_relist_folds_an_empty_row_on_the_new_ticker(db, run_cli):
    sid = _shell(db)
    stub = _security(db, "ZZR2", bars=())
    out = run_cli("relist", sid, "-m", "stub on ZZR2", "--yes")
    assert out.exit_code == 0, out.output
    assert f"folds empty security {stub} on ZZR2" in out.output
    assert _row(db, stub) is None
    assert repo.resolve_security_id(db, "ZZR2") == sid


def test_relist_takes_the_new_ticker_when_another_row_took_the_old_one(db, run_cli):
    # SATS (2026-10): EchoStar became ECHO, and Strive's preferred was listed on SATS
    # since. Reversing the stamp listed the row on SATS first, and the listed rows'
    # (source, ticker) index refused it before the rename could run.
    sid = _shell(db)
    reuse = repo.upsert_security(
        db, primary_symbol="ZZR1", company_name="ZZR1 Preferred", exchange_code="NYSE"
    )
    repo.upsert_symbol_xref(
        db, security_id=reuse, symbol="ZZR1", valid_from=Y(2025, 6, 2)
    )
    out = run_cli("relist", sid, "-m", "ZZR1 became ZZR2", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "After: ZZR2, trading, delisted never" in out.output
    out = run_cli("relist", sid, "-m", "ZZR1 became ZZR2", "--yes")
    assert out.exit_code == 0, out.output
    row = _row(db, sid)
    assert (
        row["primary_symbol"],
        row["is_actively_trading"],
        row["delisted_date"],
    ) == (
        "ZZR2",
        True,
        None,
    )
    assert _periods(db, sid) == [
        ("ZZR1", Y(1900, 1, 1), Y(2025, 2, 27)),
        ("ZZR2", Y(2025, 2, 28), None),
    ]
    # The other issuer keeps the old ticker, listed.
    other = _row(db, reuse)
    assert (other["primary_symbol"], other["delisted_date"]) == ("ZZR1", None)
    assert repo.resolve_security_id(db, "ZZR1") == reuse


def test_relist_refuses_a_ticker_another_listed_row_holds(db, run_cli):
    # In place, with no open period to show the other row: the plan names it,
    # rather than the write failing on the listed rows' (source, ticker) index.
    _vendor(db, "900511", "ZZP")
    stamped = _security(db, "ZZP", "900511", active=False, delisted=Y(2026, 10, 5))
    other = repo.upsert_security(
        db, primary_symbol="ZZP", company_name="ZZP Two", exchange_code="NYSE"
    )
    before = _row(db, stamped)
    out = run_cli("relist", stamped, "-m", "x", "--yes")
    assert out.exit_code != 0
    assert f"Security {other} is listed on ZZP" in " ".join(
        out.output.split()
    ), out.output
    assert "Nothing was changed." in out.output
    assert _row(db, stamped) == before


@pytest.mark.parametrize(
    "case, refusal",
    [
        ("held", "ZZR2 is held by security"),
        ("pending", "delisting_contradicted flag waits for"),
        ("vendor_delisted", "as delisted"),
        ("no_recent_bar", "Nothing here shows it trading"),
        ("not_fed", "has no feeding vendor link"),
        ("no_rename_event", "Pass --rename-date"),
        ("venue", "is `security venue-transfer`'s"),
    ],
)
def test_relist_refuses(db, run_cli, case, refusal):
    if case == "no_rename_event":
        _vendor(db, "900501", "ZZR2")
        sid = _security(db, "ZZR1", "900501", active=False)
    elif case == "vendor_delisted":
        _vendor(db, "900501", "ZZR1", delisted=True)
        sid = _security(db, "ZZR1", "900501", active=False)
    elif case == "no_recent_bar":
        _vendor(db, "900501", "ZZR1")
        sid = _security(db, "ZZR1", "900501", active=False, bars=(Y(2026, 9, 1),))
    elif case == "venue":
        _vendor(db, "900501", "ZZR1", exchange="TXSE")
        sid = _security(db, "ZZR1", "900501", active=False, delisted=Y(2026, 10, 5))
    elif case == "not_fed":
        _vendor(db, "900501", "ZZR1")
        sid = _security(db, "ZZR1", "900501", active=False, feed=False)
    else:
        sid = _shell(db)
        if case == "held":
            _security(db, "ZZR2")  # the same company, or a reuse: not ours to settle
        else:
            repo.add_dq_flag_once(
                db,
                check_name="security_delisting_contradicted",
                security_id=sid,
                record_key={"symbol": "ZZR1", "delisted_date": "2025-03-03"},
            )
    before = _row(db, sid)
    out = run_cli("relist", sid, "-m", "x", "--yes")
    assert out.exit_code != 0
    assert refusal in " ".join(out.output.split()), out.output
    assert "Nothing was changed." in out.output
    assert _row(db, sid) == before


def test_rename_date_dates_a_move_the_events_do_not_name(db, run_cli):
    _vendor(db, "900501", "ZZR2")
    sid = _security(db, "ZZR1", "900501", active=False)
    out = run_cli(
        "relist",
        sid,
        "-m",
        "renamed per the issuer",
        "--rename-date",
        "2026-01-05",
        "--yes",
    )
    assert out.exit_code == 0, out.output
    assert _periods(db, sid) == [
        ("ZZR1", Y(1900, 1, 1), Y(2026, 1, 4)),
        ("ZZR2", Y(2026, 1, 5), None),
    ]


def test_apply_relist_refuses_a_plan_that_refuses(db):
    _vendor(db, "900501", "ZZR1", delisted=True)
    sid = _security(db, "ZZR1", "900501", active=False)
    plan = rl.plan_relist(db, security_id=sid)
    assert plan.refusals
    with pytest.raises(rl.RelistRefused):
        rl.apply_relist(db, plan, note="x", by="tester")


def test_a_rename_onto_another_venue_relists_with_a_note(db, run_cli):
    _vendor(db, "900501", "ZZR2", exchange="TXSE")
    _rename(db, "900501", Y(2025, 2, 28), "ZZR1", "ZZR2")
    sid = _security(db, "ZZR1", "900501", active=False)
    out = run_cli("relist", sid, "-m", "renamed, and moved", "--yes")
    assert out.exit_code == 0, out.output
    assert "lists ZZR2 on TXSE, not NYSE" in " ".join(out.output.split())
    assert _row(db, sid)["exchange_code"] == "NYSE"  # venue-transfer's to change


def test_bats_and_cboe_are_one_venue():
    assert su.same_venue("BATS", "CBOE") and su.same_venue("NYSE", None)
    assert not su.same_venue("TXSE", "NYSE")


def test_zero_volume_rows_are_not_trading(db, run_cli):
    """The vendor keeps printing a flat, zero-volume row for a halted listing it
    has not called delisted (MAGH after its 2026-08-12 delisting)."""
    _vendor(db, "900521", "ZZH")
    sid = _security(db, "ZZH", "900521", active=False, delisted=Y(2026, 8, 12), bars=())
    _bars(db, sid, *RECENT, volume=0)
    assert _outcomes(su.run(db, since=SINCE)) == {}
    out = run_cli("relist", sid, "-m", "x", "--yes")
    assert out.exit_code != 0
    assert "no traded bar (volume > 0)" in " ".join(out.output.split()), out.output


def test_an_earlier_issuer_on_the_row_is_not_relisted(db, run_cli):
    """A reused ticker on one row: an earlier issuer's bars to its 2022 delisting,
    then the vendor's issuer from the link's 2026 start."""
    _vendor(db, "900522", "ZZE")
    sid = _security(
        db,
        "ZZE",
        "900522",
        active=False,
        delisted=Y(2022, 4, 7),
        link_from=Y(2026, 3, 6),
        bars=(Y(2021, 6, 1), *RECENT),
    )
    result = su.run(db, since=SINCE)
    (change,) = [c for c in result.changes if c.step == "inactive"]
    assert (change.outcome, change.detail) == ("flagged", "earlier_issuer")
    detail = _inactive_flags(db)[sid]["detail"]
    assert (detail["first_bar"], detail["link_from"]) == ("2021-06-01", "2026-03-06")
    out = run_cli("relist", sid, "-m", "x", "--yes")
    assert out.exit_code != 0
    assert "an earlier issuer of the ticker" in " ".join(out.output.split())


def test_a_delisting_with_no_bars_before_the_link_is_relisted(db, run_cli):
    """RVI and STSB: an earlier issuer's delisting stamp on the row (from FMP's
    delisted feed), but none of its bars."""
    _vendor(db, "900523", "ZZF")
    sid = _security(
        db, "ZZF", "900523", active=False, delisted=Y(2021, 8, 23), link_from=RECENT[0]
    )
    out = run_cli("relist", sid, "-m", "the fund trades", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, sid)["is_actively_trading"]
