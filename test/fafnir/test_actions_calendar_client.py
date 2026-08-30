"""The action calendars are windowed feeds, and the window has a documented cap.

`splits-calendar` / `dividends-calendar` accept `from` and `to` with a maximum span
of three months. A catch-up after a missed week is fine; a catch-up after a missed
quarter has to be sliced, or the request is rejected (or worse, silently trimmed --
the failure mode migration-era `historical-price-eod` already taught this codebase).

Also covered: the coverage probe that gates adopting the sweep at all.
"""

from __future__ import annotations

from datetime import date, timedelta

from fafnir.sources import probe
from fafnir.sources.fmp import FMPClient


def _client(events=None):
    """FMP stand-in that answers a calendar window from a fixed event list."""
    client = FMPClient.__new__(FMPClient)
    calls: list[tuple[str, str, str]] = []
    events = events or []

    def _call(endpoint, params=None):
        params = params or {}
        calls.append((endpoint, params.get("from"), params.get("to")))
        start = date.fromisoformat(params["from"])
        end = date.fromisoformat(params["to"])
        rows = [
            e
            for e in events
            if e["endpoint"] == endpoint
            and start <= date.fromisoformat(e["date"]) <= end
        ]
        return [{k: v for k, v in e.items() if k != "endpoint"} for e in rows], 200, 0

    client._call = _call
    client.calls = calls
    return client


def test_a_short_window_is_one_request():
    """The nightly case. Two endpoints, one request each -- the whole point of ADR 0007."""
    client = _client()

    client.dividends_calendar(date(2026, 8, 23), date(2026, 8, 30))

    assert client.calls == [("dividends-calendar", "2026-08-23", "2026-08-30")]


def test_a_long_window_is_sliced_under_the_documented_cap():
    """A catch-up after an outage must not exceed the 3-month span the feed allows."""
    client = _client()
    start, end = date(2026, 1, 1), date(2026, 8, 30)

    client.splits_calendar(start, end)

    assert len(client.calls) > 1
    for _, frm, to in client.calls:
        span = (date.fromisoformat(to) - date.fromisoformat(frm)).days
        assert span < 90, f"{frm}..{to} exceeds the documented 3-month cap"


def test_the_slices_tile_the_window_without_gaps_or_overlap():
    """A day falling between two slices is an event nobody ever loads."""
    client = _client()
    start, end = date(2026, 1, 1), date(2026, 8, 30)

    client.splits_calendar(start, end)

    windows = [
        (date.fromisoformat(f), date.fromisoformat(t)) for _, f, t in client.calls
    ]
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == prev_end + timedelta(days=1)


def test_events_from_every_slice_are_stitched_together():
    client = _client(
        events=[
            {
                "endpoint": "dividends-calendar",
                "symbol": "KO",
                "date": "2026-02-15",
                "dividend": 0.48,
            },
            {
                "endpoint": "dividends-calendar",
                "symbol": "KO",
                "date": "2026-07-15",
                "dividend": 0.48,
            },
        ]
    )

    rows = client.dividends_calendar(date(2026, 1, 1), date(2026, 8, 30))

    assert [r["date"] for r in rows] == ["2026-02-15", "2026-07-15"]


def test_a_row_repeated_across_a_slice_boundary_is_not_two_events():
    """Deduplicated on (symbol, date), as the price loader dedupes on date."""
    client = _client()
    client._call = lambda endpoint, params=None: (
        [{"symbol": "KO", "date": "2026-02-15", "dividend": 0.48}],
        200,
        0,
    )

    rows = client.dividends_calendar(date(2026, 1, 1), date(2026, 8, 30))

    assert len(rows) == 1


def test_an_inverted_window_asks_for_nothing():
    """Guards the case where the watermark is already past as_of."""
    client = _client()

    assert client.splits_calendar(date(2026, 8, 30), date(2026, 8, 20)) == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# The adoption gate
# ---------------------------------------------------------------------------


class _ProbeFMP:
    def __init__(self, per_symbol, calendar):
        self._per_symbol = per_symbol
        self._calendar = calendar
        self.bytes_downloaded = 0
        self.request_count = 0

    def splits(self, symbol):
        return self._per_symbol.get((symbol, "split"), [])

    def dividends(self, symbol):
        return self._per_symbol.get((symbol, "dividend"), [])

    def splits_calendar(self, f, t):
        return self._calendar.get("split", [])

    def dividends_calendar(self, f, t):
        return self._calendar.get("dividend", [])


def _recent(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_the_probe_passes_when_the_calendar_carries_the_same_events():
    day = _recent(10)
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": day, "dividend": 0.48}]},
        calendar={"dividend": [{"symbol": "KO", "date": day, "dividend": 0.48}]},
    )

    report = probe.probe_actions(fmp, ["KO"], days=90)

    assert report["verdict"] == "calendar_complete"
    assert report["missing_total"] == 0


def test_the_probe_fails_when_the_calendar_omits_an_event():
    """The finding that stops the rollout. Missing is not a warning, it is a stop."""
    fmp = _ProbeFMP(
        per_symbol={
            ("KO", "dividend"): [
                {"date": _recent(10), "dividend": 0.48},
                {"date": _recent(80), "dividend": 0.46},
            ]
        },
        calendar={
            "dividend": [{"symbol": "KO", "date": _recent(10), "dividend": 0.48}]
        },
    )

    report = probe.probe_actions(fmp, ["KO"], days=90)

    assert report["verdict"] == "calendar_incomplete"
    assert report["missing_total"] == 1
    assert "symbol" in probe.format_actions_report(report)


def test_the_probe_ignores_a_difference_in_numeric_formatting():
    """0.48 and "0.4800" are one dividend; a spurious FAIL would stall the rollout."""
    day = _recent(10)
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": day, "dividend": 0.48}]},
        calendar={"dividend": [{"symbol": "KO", "date": day, "dividend": "0.4800"}]},
    )

    assert probe.probe_actions(fmp, ["KO"], days=90)["verdict"] == "calendar_complete"


def test_the_probe_notices_a_value_that_actually_disagrees():
    day = _recent(10)
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": day, "dividend": 0.48}]},
        calendar={"dividend": [{"symbol": "KO", "date": day, "dividend": 0.52}]},
    )

    report = probe.probe_actions(fmp, ["KO"], days=90)

    # A different amount on the same ex-date is a missing event plus an extra one --
    # either way the calendar does not carry what the per-symbol feed does.
    assert report["verdict"] == "calendar_incomplete"


def test_the_probe_is_inconclusive_rather_than_passing_on_no_events():
    """An empty window proves nothing, and must not read as a green light."""
    fmp = _ProbeFMP(per_symbol={}, calendar={})

    report = probe.probe_actions(fmp, ["KO"], days=90)

    assert report["verdict"] == "no_events"


def test_the_probe_ignores_events_outside_the_window():
    """Comparing a bounded calendar against an unbounded per-symbol history would
    report every old dividend as missing."""
    fmp = _ProbeFMP(
        per_symbol={
            ("KO", "dividend"): [
                {"date": _recent(10), "dividend": 0.48},
                {"date": "1998-06-15", "dividend": 0.15},  # long before the window
            ]
        },
        calendar={
            "dividend": [{"symbol": "KO", "date": _recent(10), "dividend": 0.48}]
        },
    )

    assert probe.probe_actions(fmp, ["KO"], days=90)["verdict"] == "calendar_complete"
