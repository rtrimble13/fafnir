"""A security nothing feeds is frozen, not stale (ADR 0020), against a real database.

With [general] fmp_secondary false the FMP loaders stop, and what FMP alone fed --
3,937 mutual funds on 2026-11-10 -- takes no new bar while it still trades. The
marker keeps the freshness check, its recheck, `dq compare` and dorq's stale-feed
drop from calling that stale, and comes off when a vendor feeds the security again.
"""

from __future__ import annotations

import datetime as dt

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import checks, compare, dorq
from fafnir.dq import recheck as rc
from fafnir.ingest import vendor_feed as vf

pytestmark = pytest.mark.integration


def _trading_days(db, count, *, through):
    rows = db.fetchall(
        "SELECT trade_date FROM ref.trading_calendar "
        "WHERE exchange_code = 'NASDAQ' AND is_open AND trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT %s",
        (through, count),
    )
    return sorted(r["trade_date"] for r in rows)


def _price_on(db, sec_id, day, close=100):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sec_id,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 0,
            }
        ],
    )
    db.commit()


def _fund(db, symbol="VFIAX"):
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name="Fund",
        asset_type="equity",
        is_fund=True,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    db.commit()
    return sid


def _equity(db, symbol="AAA"):
    sid = repo.upsert_security(
        db, primary_symbol=symbol, company_name="Test", asset_type="equity"
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    db.commit()
    return sid


def _frozen(db, sid):
    return db.fetchone(
        "SELECT feed_frozen_on, feed_frozen_note FROM core.security "
        "WHERE security_id = %s",
        (sid,),
    )


def _stale_ids(db):
    (result,) = rc.recheck(db, checks=["stale"])
    return set(result.stale_flag_ids)


@pytest.fixture()
def run_cli(db, tmp_path, monkeypatch):
    """Run `fafnir security freeze|thaw` against the test database in Sharadar mode,
    with [general] fmp_secondary as the test says."""
    monkeypatch.delenv("FAFNIR_DSN", raising=False)

    def run(*args, secondary=False):
        rc_path = tmp_path / "fafnirrc"
        rc_path.write_text(
            f'[database]\ndsn = "{db.dsn}"\n'
            "[general]\n"
            'primary_price_vendor = "sharadar"\n'
            f"fmp_secondary = {'true' if secondary else 'false'}\n"
        )
        return CliRunner().invoke(
            cli.main, ["--config", str(rc_path), "security", *map(str, args)]
        )

    return run


# -- the marker ---------------------------------------------------------------


def test_unfed_is_fmp_fed_in_the_nightly_universe_and_not_yet_marked(db):
    fund = _fund(db)
    fed = _equity(db, "FED")
    repo.insert_vendor_link(
        db,
        security_id=fed,
        vendor="sharadar",
        vendor_key="123",
        valid_from=dt.date(2020, 1, 1),
        match_method="manual",
        confidence=1.0,
        evidence={},
        created_by="test",
        is_feed=True,
    )
    inactive = repo.upsert_security(
        db,
        primary_symbol="OLD",
        company_name="Gone",
        asset_type="equity",
        is_actively_trading=False,
    )
    db.commit()
    today = dt.date(2024, 12, 31)
    for sid in (fund, fed):
        _price_on(db, sid, today)

    unfed = {r["security_id"]: r for r in repo.unfed_securities(db)}
    assert set(unfed) == {fund}
    assert unfed[fund]["last_bar"] == today
    assert unfed[fund]["is_fund"] is True
    assert inactive not in unfed  # not in the nightly universe

    assert repo.freeze_feed(db, [fund], note="test") == [fund]
    assert repo.unfed_securities(db) == []
    assert repo.freeze_feed(db, [fund], note="again") == []  # stamped once
    assert _frozen(db, fund) == {"feed_frozen_on": today, "feed_frozen_note": "test"}


def test_a_security_with_no_bars_freezes_on_today(db):
    fund = _fund(db)
    repo.freeze_feed(db, [fund], note="test")
    assert _frozen(db, fund)["feed_frozen_on"] == dt.date.today()


def test_the_note_goes_with_the_date(db):
    import psycopg

    fund = _fund(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE core.security SET feed_frozen_on = current_date "
            "WHERE security_id = %s",
            (fund,),
        )


# -- what reads it ------------------------------------------------------------


def test_a_frozen_fund_is_not_stale_and_its_old_flag_closes_on_recheck(db):
    """The flag written before the marker closes by the recheck, not a resolve."""
    oldest, older, yesterday, today = _trading_days(
        db, 4, through=dt.date(2024, 12, 31)
    )
    leader = _equity(db)
    fund = _fund(db)
    _price_on(db, leader, today)
    _price_on(db, fund, oldest)  # four sessions behind: past a NAV's allowance

    assert checks.check_freshness(db) == 1
    flag = db.fetchone(
        "SELECT dq_flag_id, security_id FROM ops.data_quality_flag "
        "WHERE check_name = 'stale'"
    )
    assert flag["security_id"] == fund
    assert _stale_ids(db) == set()

    repo.freeze_feed(db, [fund], note="test")
    db.commit()
    assert _stale_ids(db) == {flag["dq_flag_id"]}
    assert checks.check_freshness(db) == 0

    # Thawed, the condition holds again: the open flag stays, and nothing is
    # written twice for one last_date.
    repo.thaw_feed(db, [fund])
    db.commit()
    assert _stale_ids(db) == set()
    assert checks.check_freshness(db) == 0


def test_compare_reads_a_frozen_securitys_stale_flag_as_inactive(db):
    oldest, older, yesterday, today = _trading_days(
        db, 4, through=dt.date(2024, 12, 31)
    )
    leader = _equity(db)
    fund = _fund(db)
    _price_on(db, leader, today)
    _price_on(db, fund, oldest)
    assert checks.check_freshness(db) == 1
    flag_id = db.fetchval("SELECT dq_flag_id FROM ops.data_quality_flag")

    def state():
        snap = compare.holding_snapshot(
            db, window_from=oldest, as_of=today, started_at=None
        )
        return snap["states"][str(flag_id)]

    assert state() == "holding"
    repo.freeze_feed(db, [fund], note="test")
    db.commit()
    assert state() == "inactive"


def test_dorq_drops_the_stale_feed_report_on_a_frozen_security(db):
    fund = _fund(db)
    live = _equity(db)
    records = [
        {"check_name": dorq.STALE_FEED_CHECK, "security_id": fund},
        {"check_name": dorq.STALE_FEED_CHECK, "security_id": live},
        {"check_name": "dorq_bad_print", "security_id": fund},
    ]
    kept, left_out = dorq.drop_not_trading(db, records)
    assert len(kept) == 3 and left_out == {}

    repo.freeze_feed(db, [fund], note="test")
    db.commit()
    kept, left_out = dorq.drop_not_trading(db, records)
    assert [(r["check_name"], r["security_id"]) for r in kept] == [
        (dorq.STALE_FEED_CHECK, live),
        ("dorq_bad_print", fund),  # only the stale-feed report goes
    ]
    assert left_out == {dorq.STALE_FEED_CHECK: 1}


# -- what clears it -----------------------------------------------------------


def test_a_feeding_link_thaws(db):
    sec = _equity(db)
    repo.freeze_feed(db, [sec], note="test")
    repo.insert_vendor_link(
        db,
        security_id=sec,
        vendor="sharadar",
        vendor_key="123",
        valid_from=dt.date(2020, 1, 1),
        match_method="manual",
        confidence=1.0,
        evidence={},
        created_by="test",
        is_feed=True,
    )
    db.commit()
    assert _frozen(db, sec)["feed_frozen_on"] is None


def test_vendor_feed_thaws(db):
    sec = _equity(db)
    repo.insert_vendor_link(
        db,
        security_id=sec,
        vendor="sharadar",
        vendor_key="123",
        valid_from=dt.date(2020, 1, 1),
        match_method="manual",
        confidence=1.0,
        evidence={},
        created_by="test",
        is_feed=False,
    )
    repo.freeze_feed(db, [sec], note="test")
    db.commit()
    assert _frozen(db, sec)["feed_frozen_on"] is not None

    vf.feed(db, "sharadar", security_ids=[sec], by="test", dry_run=False)
    db.commit()
    assert _frozen(db, sec)["feed_frozen_on"] is None


def test_a_link_that_does_not_feed_leaves_the_marker(db):
    sec = _equity(db)
    repo.freeze_feed(db, [sec], note="test")
    repo.insert_vendor_link(
        db,
        security_id=sec,
        vendor="sharadar",
        vendor_key="123",
        valid_from=dt.date(2020, 1, 1),
        match_method="manual",
        confidence=1.0,
        evidence={},
        created_by="test",
        is_feed=False,
    )
    db.commit()
    assert _frozen(db, sec)["feed_frozen_on"] is not None


# -- the commands -------------------------------------------------------------


def test_freeze_unfed_refuses_while_fmp_still_feeds(run_cli):
    result = run_cli("freeze", "--unfed", secondary=True)
    assert result.exit_code != 0
    assert "fmp_secondary is true" in result.output


def test_freeze_unfed_dry_run_then_yes_then_thaw(db, run_cli):
    fund = _fund(db)
    _price_on(db, fund, dt.date(2024, 12, 31))

    dry = run_cli("freeze", "--unfed", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "1 security (1 funds, 0 others)" in dry.output
    assert "Dry run" in dry.output
    assert _frozen(db, fund)["feed_frozen_on"] is None

    real = run_cli("freeze", "--unfed", "--yes")
    assert real.exit_code == 0, real.output
    assert "Froze 1 security" in real.output
    assert _frozen(db, fund) == {
        "feed_frozen_on": dt.date(2024, 12, 31),
        "feed_frozen_note": repo.UNFED_FREEZE_NOTE,
    }
    run = db.fetchone(
        "SELECT source, endpoint, params FROM ops.ingestion_run "
        "ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert (run["source"], run["endpoint"]) == ("operator", "security-freeze")
    assert run["params"]["frozen"] == 1

    again = run_cli("freeze", "--unfed", "--yes")
    assert again.exit_code == 0 and "Nothing to freeze" in again.output

    thaw = run_cli("thaw", "--unfed", "--yes")
    assert thaw.exit_code == 0, thaw.output
    assert "Thawed 1 security" in thaw.output
    assert _frozen(db, fund)["feed_frozen_on"] is None

    nothing = run_cli("thaw", "--unfed", "--yes")
    assert nothing.exit_code == 0 and "Nothing to thaw" in nothing.output


def test_thaw_by_id_leaves_other_notes_alone(db, run_cli):
    a = _equity(db, "AAA")
    b = _equity(db, "BBB")
    repo.freeze_feed(db, [a], note=repo.UNFED_FREEZE_NOTE)
    repo.freeze_feed(db, [b], note="an operator's own reason")
    db.commit()

    unfed = run_cli("thaw", "--unfed", "--dry-run")
    assert unfed.exit_code == 0 and "Frozen: 1 security" in unfed.output

    one = run_cli("thaw", str(b), "--yes")
    assert one.exit_code == 0, one.output
    assert _frozen(db, b)["feed_frozen_on"] is None
    assert _frozen(db, a)["feed_frozen_on"] is not None

    both = run_cli("thaw", "--unfed", str(a))
    assert both.exit_code != 0


def test_the_counts_say_securities():
    """The night freezes thousands at once; its log must not read "securitys"."""
    assert cli._plural(1, "security", "securities") == "1 security"
    assert cli._plural(3937, "security", "securities") == "3937 securities"
    assert cli._plural(2, "bar") == "2 bars"


def test_the_profile_view_carries_the_marker(db):
    fund = _fund(db)
    repo.freeze_feed(db, [fund], note="test")
    db.commit()
    row = db.fetchone(
        "SELECT feed_frozen_on, feed_frozen_note FROM mart.v_security_profile "
        "WHERE security_id = %s",
        (fund,),
    )
    assert row["feed_frozen_note"] == "test"
