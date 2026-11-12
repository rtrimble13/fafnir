"""DQ303's cohort gap judged without the series fafnir knows have ended.

dorq counts a series that stops before the as-of date as missing on the session
after its last bar, and its ``--meta`` cannot say a series ends. On 2026-11-10
3,946 funds nothing feeds were frozen at their 11-06 bar (ADR 0020), and every
night with 11-09 in its window DQ303 reported 4,008 missing series as a failed
load. :func:`fafnir.dq.dorq.adjust_ended_cohorts` takes the ended series out and
runs DQ303's own test again.
"""

from __future__ import annotations

import datetime as dt
import stat
import sys

import pytest

from fafnir.db import repository as repo
from fafnir.dq import dorq


class _FakeDb:
    """Answers :func:`dorq.ended_on`'s count; records the days it was asked."""

    def __init__(self, ended, members=()):
        self.ended = ended
        self.members = list(members)
        self.days = []

    def fetchval(self, query, params):
        self.days.append(params["day"])
        return self.ended

    def fetchall(self, query, params):
        return [{"security_id": m} for m in self.members]


def _cohort(day, expected, missing, explained, tail):
    message = (
        f"{missing:,} of {expected:,} series that have a bar on nearly every "
        "session have none on this one"
    )
    return {
        "security_id": None,
        "table_name": "core.daily_price",
        "record_key": {"trade_date": day},
        "check_name": dorq.COHORT_GAP_CHECK,
        "severity": "error",
        "detail": {
            "code": "DQ303",
            "date": day,
            "p_error": 1 - tail,
            "message": message,
            "detail": {
                "calendar": "XNYS",
                "series_expected": expected,
                "series_missing": missing,
                "expected_missing": explained,
                "tail_probability": tail,
            },
            "dorq": {"version": "0.14.0", "config_hash": "x"},
        },
    }


@pytest.fixture()
def settings(tmp_path):
    """No dorq to ask: DQ303's default thresholds."""
    return dorq.DorqSettings(path=str(tmp_path / "no-dorq"))


def test_the_tail_is_dorqs_own():
    # The 2026-11-02 report: 66 of 15,838 missing against 32.007 expected.
    assert dorq.poisson_tail(66, 32.007456570510506) == pytest.approx(
        9.709286739205623e-08, rel=1e-9
    )
    assert dorq.poisson_tail(0, 5.0) == 1.0
    assert dorq.poisson_tail(3, 0.0) == 0.0
    assert dorq.poisson_tail(2, 30.0) == pytest.approx(1 - 31 * 2.718281828**-30)
    assert dorq.poisson_tail(4008, 32.1) == 0.0


def test_the_frozen_funds_cohort_is_restated_on_the_series_still_fed(settings):
    """11-09 on the 11-11 night: 3,954 of the 4,008 end on 11-06. What is left (54
    of 11,883, about 24.1 expected) is still a cohort, and is said so."""
    report = _cohort("2026-11-09", 15837, 4008, 32.13057580070712, 0.0)
    bad_print = {"check_name": "dorq_bad_print", "security_id": 7, "detail": {}}
    db = _FakeDb(3954, members=[18126, 19467, 17506])  # BCGS, CLIM, LGDX
    kept, left_out = dorq.adjust_ended_cohorts(
        db, settings, "NASDAQ", [bad_print, report]
    )
    assert left_out == {}
    assert db.days == [dt.date(2026, 11, 9)]
    assert kept[0] is bad_print
    got = kept[1]["detail"]
    assert got["detail"]["series_expected"] == 11883
    assert got["detail"]["series_missing"] == 54
    assert got["detail"]["expected_missing"] == pytest.approx(24.108, abs=1e-3)
    assert got["detail"]["tail_probability"] == pytest.approx(1.12e-7, rel=0.01)
    assert got["p_error"] == pytest.approx(1 - got["detail"]["tail_probability"])
    assert got["message"].startswith("54 of 11,883 series")
    assert "3,954 more stop the session before" in got["message"]
    assert got["fafnir"]["series_ended"] == 3954
    # DQ303 names no members, and hides their stale tails at info: fafnir's
    # estimate names them, as dorq writes a cohort's (a comma-separated scalar).
    assert got["detail"]["members"] == "18126,19467,17506"
    assert got["fafnir"]["members_estimated"] is True
    assert dorq.member_ids(got["detail"]["members"]) == [17506, 18126, 19467]
    assert got["fafnir"]["dorq"]["series_missing"] == 4008
    assert got["fafnir"]["message"].startswith("4,008 of 15,837")
    # The record the caller holds is not changed underneath it.
    assert report["detail"]["detail"]["series_missing"] == 4008
    assert kept[1]["record_key"] == {"trade_date": "2026-11-09"}


def test_a_cohort_the_ended_series_explain_is_left_out(settings):
    """11-02: 66 of 15,838, eleven of them delisted on 10-30. Without them, 55
    against 32.0 is not improbable enough (tail 1.4e-4 > 1e-6)."""
    report = _cohort("2026-11-02", 15838, 66, 32.007456570510506, 9.7e-8)
    kept, left_out = dorq.adjust_ended_cohorts(
        _FakeDb(11), settings, "NASDAQ", [report]
    )
    assert kept == []
    assert left_out == {dorq.COHORT_GAP_CHECK: 1}


def test_too_few_left_is_no_cohort(settings):
    report = _cohort("2026-11-09", 100, 5, 0.0001, 1e-20)
    kept, left_out = dorq.adjust_ended_cohorts(_FakeDb(3), settings, "NASDAQ", [report])
    assert kept == [] and left_out == {dorq.COHORT_GAP_CHECK: 1}


def test_a_cohort_with_no_ended_series_is_dorqs_as_it_stands(settings):
    report = _cohort("2026-11-02", 15838, 66, 32.0, 9.7e-8)
    kept, left_out = dorq.adjust_ended_cohorts(_FakeDb(0), settings, "NASDAQ", [report])
    assert kept == [report] and left_out == {}


def test_no_cohort_row_asks_nothing(settings):
    db = _FakeDb(10)
    rows = [{"check_name": "dorq_stale_feed", "security_id": 3, "detail": {}}]
    assert dorq.adjust_ended_cohorts(db, settings, "NASDAQ", rows) == (rows, {})
    assert db.days == []


def test_a_cohort_without_dorqs_figures_is_kept(settings):
    report = _cohort("2026-11-09", 10, 5, 0.1, 0.0)
    del report["detail"]["detail"]["series_expected"]
    db = _FakeDb(5)
    assert dorq.adjust_ended_cohorts(db, settings, "NASDAQ", [report]) == (
        [report],
        {},
    )
    assert db.days == []


def test_the_thresholds_are_the_runs_config(tmp_path):
    fake = tmp_path / "dorq"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "assert sys.argv[1:4] == ['config', 'show', '--config'], sys.argv\n"
        "print('# source: x')\n"
        "print('[coverage]')\n"
        "print('cohort_min_series = 50')\n"
        "print('cohort_max_tail = 1e-09')\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    settings = dorq.DorqSettings(path=str(fake), config=str(tmp_path / "d.toml"))
    assert dorq.cohort_thresholds(settings) == (50, 1e-9)
    # 54 left is above 50, but 1.1e-7 is not below 1e-9.
    report = _cohort("2026-11-09", 15837, 4008, 32.13057580070712, 0.0)
    kept, left_out = dorq.adjust_ended_cohorts(
        _FakeDb(3954), settings, "NASDAQ", [report]
    )
    assert kept == [] and left_out == {dorq.COHORT_GAP_CHECK: 1}


def test_no_dorq_to_ask_gives_dq303s_defaults(settings):
    assert dorq.cohort_thresholds(settings) == (
        dorq.COHORT_MIN_SERIES,
        dorq.COHORT_MAX_TAIL,
    )


# ---------------------------------------------------------------------------
# Against a database
# ---------------------------------------------------------------------------


def _sessions(db, through, count):
    rows = db.fetchall(
        "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
        " AND is_open AND trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
        (through, count),
    )
    return sorted(r["trade_date"] for r in rows)


def _security(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


def _bars(db, sid, days):
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
                "volume": 1000,
            }
            for d in days
        ],
    )


@pytest.mark.integration
def test_ended_on_counts_the_near_daily_series_that_stop_the_session_before(db):
    days = _sessions(db, dt.date(2024, 12, 31), 25)
    *history, last, cohort_day = days
    frozen = _security(db, "CHFRZ")
    delisted = _security(db, "CHDEL")
    outage = _security(db, "CHOUT")  # trading and fed: a real missed bar
    sparse = _security(db, "CHSPA")  # frozen, but not near-daily
    early = _security(db, "CHEAR")  # frozen two sessions before
    for sid in (frozen, delisted, outage):
        _bars(db, sid, [*history[-19:], last])
    _bars(db, sparse, [*history[-10:], last])
    _bars(db, early, history[-20:])
    repo.freeze_feed(db, [frozen, sparse, early], note="test")
    db.execute(
        "UPDATE core.security SET is_actively_trading = false WHERE security_id = %s",
        (delisted,),
    )
    db.commit()
    assert dorq.ended_on(db, "NASDAQ", cohort_day) == 2  # frozen and delisted

    # The fed series missing the day: the outage alone. Not the ended two, nor the
    # sparse one, nor the one that stopped a session earlier: dorq counts a series
    # that stops once, on the session after its last bar.
    assert dorq.fed_missing_on(db, "NASDAQ", cohort_day) == [outage]

    # A bar after the session before is a series that did not end there.
    _bars(db, delisted, [cohort_day])
    db.commit()
    assert dorq.ended_on(db, "NASDAQ", cohort_day) == 1
