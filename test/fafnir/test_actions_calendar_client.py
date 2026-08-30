"""The action calendars are windowed feeds with two limits, one of them undocumented.

`splits-calendar` / `dividends-calendar` accept `from` and `to` with a documented
maximum span of three months, so a catch-up after a missed quarter has to be sliced.

The one that actually bit is the other one: a response carries at most 4000 rows and
drops the OLDEST to fit, silently -- the same failure `historical-price-eod` already
taught this codebase, and the same shape of wrong answer. `limit` does not lift it;
`page` walks backwards through the rows and does. Both halves are pinned here,
because a truncated response is indistinguishable from a short feed unless something
checks.

Also covered: the coverage probe that gates adopting the sweep at all, including the
verdict that tells those two apart.
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


# ---------------------------------------------------------------------------
# The row cap, and paging past it
# ---------------------------------------------------------------------------
#
# The span between `from` and `to` is the documented limit and it is not the one
# that bites. Measured against the live feed on 2026-08-30, a calendar response
# carries at most 4000 rows and drops the OLDEST to fit, silently: a request for
# June 1-30 came back holding June 23-30, so KO's June 15 dividend read as an event
# the vendor does not carry. `limit` does not lift it (limit=20000 returned 4000);
# `page` does work, walking backwards in time. These pin the paging that follows
# from that, because the failure it prevents leaves no trace in the payload.


def _paging_client(rows_by_page, cap=3):
    """FMP stand-in that serves fixed pages and caps each response at `cap` rows."""
    client = FMPClient.__new__(FMPClient)
    client.ACTIONS_MAX_ROWS = cap
    calls: list[tuple] = []

    def _call(endpoint, params=None):
        params = params or {}
        page = params.get("page", 0)
        calls.append((params.get("from"), params.get("to"), page))
        return list(rows_by_page.get(page, [])), 200, 0

    client._call = _call
    client.calls = calls
    return client


def _row(symbol, day):
    return {"symbol": symbol, "date": f"2026-06-{day:02d}", "dividend": 1.0}


def test_a_full_page_is_followed_by_the_next_one():
    """A response at the cap means there is more behind it, not that there is not."""
    client = _paging_client(
        {
            0: [_row("A", 28), _row("B", 27), _row("C", 26)],  # full -> more to come
            1: [_row("D", 12), _row("E", 11)],  # short -> done
        }
    )

    rows = client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert [r["symbol"] for r in rows] == ["A", "B", "C", "D", "E"]
    assert [c[2] for c in client.calls] == [0, 1]


def test_the_oldest_events_survive_paging():
    """The regression this whole change exists for.

    Without paging the loader kept only the newest page, so the events at the front
    of the window vanished -- and looked exactly like securities the feed does not
    carry rather than like a truncated response.
    """
    client = _paging_client(
        {
            0: [_row("AAPL", 30), _row("MSFT", 29), _row("X", 28)],
            1: [_row("KO", 15), _row("SPY", 18)],
        }
    )

    rows = client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert {r["symbol"] for r in rows} >= {"KO", "SPY"}


def test_a_day_split_across_two_pages_is_not_two_events():
    """Pages tile rows, not dates, so a boundary day appears on both."""
    client = _paging_client(
        {
            0: [_row("A", 23), _row("B", 23), _row("C", 23)],
            1: [_row("B", 23), _row("D", 12)],  # B repeats from the boundary day
        }
    )

    rows = client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert len(rows) == 4
    assert sorted(r["symbol"] for r in rows) == ["A", "B", "C", "D"]


def test_a_server_that_ignores_page_is_not_downloaded_fifty_times():
    """The repeat guard. Without it an endpoint that ignores `page` costs the bound."""
    same = [_row("A", 28), _row("B", 27), _row("C", 26)]
    client = _paging_client({p: list(same) for p in range(FMPClient.ACTIONS_MAX_PAGES)})

    rows = client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert len(client.calls) == 2  # page 0, then page 1 recognised as a repeat
    assert len(rows) == 3


def test_paging_by_symbol_alone_would_stop_after_one_page():
    """Why key_fields is (symbol, date) and not the helper's (symbol,) default.

    One symbol legitimately appears on many pages -- a monthly payer, or simply the
    alphabetical head of each page. Fingerprinting the page on symbol alone would
    call page 1 a repeat of page 0 and silently stop, which is the same data loss
    this change is fixing, reintroduced one layer up.
    """
    client = _paging_client(
        {
            0: [_row("SPY", 28), _row("B", 27), _row("C", 26)],
            1: [_row("SPY", 12), _row("D", 11)],  # same symbol leads both pages
        }
    )

    rows = client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert len(client.calls) == 2
    assert {(r["symbol"], r["date"]) for r in rows} == {
        ("SPY", "2026-06-28"),
        ("B", "2026-06-27"),
        ("C", "2026-06-26"),
        ("SPY", "2026-06-12"),
        ("D", "2026-06-11"),
    }


def test_running_out_of_pages_with_a_full_page_warns(caplog):
    """Exhausting the bound is data loss with nothing in the payload to say so."""
    full = {p: [_row("A", 28), _row("B", 27), _row("C", 26)] for p in range(60)}
    # Distinct leading row per page, so the repeat guard does not fire first.
    for p in full:
        full[p] = [_row(f"S{p}", 28)] + full[p][1:]
    client = _paging_client(full)

    with caplog.at_level("WARNING"):
        client.dividends_calendar(date(2026, 6, 1), date(2026, 6, 30))

    assert len(client.calls) == FMPClient.ACTIONS_MAX_PAGES
    assert "page bound" in caplog.text


def test_a_deliberate_tail_sweep_does_not_warn(caplog):
    """`delisted_companies` reads only a recent tail on purpose (max_pages=5).

    Warning there would fire every night for the intended behaviour, which is how a
    warning stops being read.
    """
    client = FMPClient.__new__(FMPClient)
    client._call = lambda endpoint, params=None: (
        [{"symbol": f"S{params.get('page', 0)}"}] * 100,
        200,
        0,
    )

    with caplog.at_level("WARNING"):
        client._paged("delisted-companies", page_size=100, max_pages=5)

    assert caplog.text == ""


# ---------------------------------------------------------------------------
# Telling a truncated response apart from a real coverage gap
# ---------------------------------------------------------------------------


def _big_calendar(day_offsets, per_day=40):
    """A market-wide calendar: many symbols, so its span means something."""
    return [
        {"symbol": f"SYM{off}_{i}", "date": _recent(off), "dividend": 1.0}
        for off in day_offsets
        for i in range(per_day)
    ]


def test_the_probe_calls_a_cut_off_response_truncated_not_incomplete():
    """The verdict that would have saved a wrong conclusion about the vendor.

    KO and SPY looked absent from the calendar when the response had simply been cut
    off before their ex-dates. Reported as `calendar_incomplete` that reads as "the
    feed does not carry these securities" and stops the rollout for the wrong reason.
    """
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": _recent(80), "dividend": 0.53}]},
        calendar={"dividend": _big_calendar([10, 12, 14])},
    )

    report = probe.probe_actions(fmp, ["KO"], days=90)

    assert report["verdict"] == "calendar_truncated"
    assert "SHORT" in probe.format_actions_report(report)


def test_a_miss_among_events_the_calendar_did_carry_is_still_incomplete():
    """A real gap: the calendar reached that period and lacks this security anyway."""
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": _recent(12), "dividend": 0.53}]},
        calendar={"dividend": _big_calendar([10, 12, 14, 85])},
    )

    report = probe.probe_actions(fmp, ["KO"], days=90)

    assert report["verdict"] == "calendar_incomplete"


def test_a_calendar_too_small_to_judge_is_not_called_truncated():
    """With a handful of rows, a late start is not evidence of a cut-off."""
    fmp = _ProbeFMP(
        per_symbol={("KO", "dividend"): [{"date": _recent(80), "dividend": 0.53}]},
        calendar={"dividend": [{"symbol": "X", "date": _recent(10), "dividend": 1.0}]},
    )

    assert probe.probe_actions(fmp, ["KO"], days=90)["verdict"] == "calendar_incomplete"
