"""
Unit tests for setting aside bars dated on days the venue did not trade.

FMP returns weekend and holiday bars for some symbols: money-market funds strike a
NAV every day of the week (19 of them had a $1.00 bar every Saturday and Sunday),
and a ticker shared with another instrument can carry that instrument's weekend
prints (PI's ~$0.09 Saturday bars between ~$150 closes, which made 1,878-1,998x
"outliers" every Monday). Stored, they corrupt the series; quarantined, they wrote a
price_non_positive_price flag every weekend (209 of 255 such bars). Neither is right:
they are not sessions of the security's venue at all.
"""

from __future__ import annotations

from datetime import date

import fafnir.ingest.daily_price as dp
from fafnir.ingest.daily_price import _drop_non_session, _session_calendar

# Thu 2023-06-01, Fri 06-02, Mon 06-05 are sessions; Sat 06-03 / Sun 06-04 are not.
_CAL = (
    frozenset({date(2023, 6, 1), date(2023, 6, 2), date(2023, 6, 5)}),
    date(2023, 1, 3),
    date(2024, 12, 31),
)


def _bar(d, close=10):
    return {"date": d, "open": close, "high": close, "low": close, "close": close}


def test_weekend_bars_are_set_aside():
    bars = [
        _bar("2023-06-02"),
        _bar("2023-06-03", 0.09),
        _bar("2023-06-04"),
        _bar("2023-06-05"),
    ]
    kept, dropped = _drop_non_session(bars, _CAL)
    assert [b["date"] for b in kept] == ["2023-06-02", "2023-06-05"]
    assert dropped == 2


def test_a_weekday_without_an_open_session_is_set_aside():
    # Inside the calendar's span, a weekday with no open row is a holiday or closure.
    kept, dropped = _drop_non_session([_bar("2023-07-04")], _CAL)
    assert kept == [] and dropped == 1


def test_a_date_outside_the_calendar_span_is_kept():
    # Past the ensure-horizon range nothing is known, so nothing is judged.
    kept, dropped = _drop_non_session([_bar("2026-09-05"), _bar("1989-12-30")], _CAL)
    assert len(kept) == 2 and dropped == 0


def test_an_unparseable_date_is_left_for_validation():
    kept, dropped = _drop_non_session([_bar("not-a-date")], _CAL)
    assert len(kept) == 1 and dropped == 0


def test_no_calendar_means_nothing_is_dropped():
    bars = [_bar("2023-06-03")]
    assert _drop_non_session(bars, None) == (bars, 0)


def test_calendar_falls_back_when_the_venue_has_none(monkeypatch):
    asked = []

    def fake_open_sessions(db, code, start, end):
        asked.append((code, start, end))
        return None if code == "MUTF" else _CAL

    monkeypatch.setattr(dp.repo, "open_sessions", fake_open_sessions)
    cal = _session_calendar(object(), "MUTF", [_bar("2023-06-05"), _bar("2023-06-03")])
    assert cal is _CAL
    assert asked == [
        ("MUTF", date(2023, 6, 3), date(2023, 6, 5)),
        (dp.CALENDAR_FALLBACK_EXCHANGE, date(2023, 6, 3), date(2023, 6, 5)),
    ]


def test_calendar_is_not_read_when_no_bar_has_a_date(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no dates, so no calendar read")

    monkeypatch.setattr(dp.repo, "open_sessions", boom)
    assert _session_calendar(object(), "NASDAQ", [{"date": None}]) is None
    assert _session_calendar(object(), "NASDAQ", []) is None


def test_the_venue_is_not_asked_twice_when_it_is_the_fallback(monkeypatch):
    asked = []
    monkeypatch.setattr(
        dp.repo, "open_sessions", lambda db, code, s, e: asked.append(code) or None
    )
    assert _session_calendar(object(), "NASDAQ", [_bar("2023-06-05")]) is None
    assert asked == ["NASDAQ"]
