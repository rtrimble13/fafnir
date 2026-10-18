"""Unit tests for the delisting reconciliation loader (no DB, no network)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fafnir.ingest.delisted import _parse_date, load_delisted
from fafnir.sources.fmp import SourceError


class _FakeFMP:
    bytes_downloaded = 0

    def __init__(self, rows, bars=None):
        self.rows = rows
        self.max_pages = None
        # symbol -> the vendor's bars, or an exception to raise. A symbol absent
        # here was never meant to be asked about, and asking fails the test.
        self.bars = bars or {}
        self.asked: list[tuple[str, str, str]] = []

    def delisted_companies(self, *, max_pages=5):
        self.max_pages = max_pages
        return self.rows

    def eod_raw(self, symbol, from_date=None, to_date=None):
        self.asked.append((symbol, from_date, to_date))
        answer = self.bars[symbol]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _FakeRun:
    symbols_requested = 0
    rows_inserted = 0
    rows_quarantined = 0
    bytes_downloaded = 0
    run_id = 7

    def __init__(self):
        self.params = {}


class _FakeDB:
    """Stands in for Database + the repository calls the loader makes."""

    def __init__(
        self,
        known: dict[str, int],
        already_delisted=frozenset(),
        spans: dict[int, tuple[date, date]] | None = None,
        accepted=frozenset(),
    ):
        self.known = known
        self.already_delisted = set(already_delisted)
        # security_id -> (first bar, last bar); absent means no bars at all.
        self.spans = spans or {}
        # (security_id, symbol, delisted_date) an operator accepted as a transfer.
        self.accepted = set(accepted)
        self.marked: list[tuple[int, date]] = []
        self.flags: list[dict] = []
        self.landed: list[dict] = []
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
def patched(monkeypatch):
    """Route RunLog and the repository at the fakes."""
    import fafnir.ingest.delisted as mod

    class _RunLogStub:
        last = None

        def __init__(self, *a, **kw):
            self.run = _RunLogStub.last = _FakeRun()

        def __enter__(self):
            return self.run

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mod, "RunLog", _RunLogStub)

    def resolve(db, symbol, source="fmp"):
        return db.known.get(symbol)

    monkeypatch.setattr(mod.repo, "active_security_for_symbol", resolve)

    def mark(db, *, security_id, delisted_date):
        if security_id in db.already_delisted:
            return False
        db.already_delisted.add(security_id)
        db.marked.append((security_id, delisted_date))
        return True

    monkeypatch.setattr(mod.repo, "mark_delisted", mark)
    monkeypatch.setattr(
        mod.repo,
        "security_bar_span",
        lambda db, security_id: db.spans.get(security_id, (None, None)),
    )

    def status(db, *, security_id, check_name, record_key):
        assert check_name == "security_delisting_contradicted"
        key = (security_id, record_key["symbol"], record_key["delisted_date"])
        if key in db.accepted:
            return "accepted"
        flagged = (security_id, check_name, record_key)
        if any(
            (f["security_id"], f["check_name"], f["record_key"]) == flagged
            for f in db.flags
        ):
            return "open"
        return None

    monkeypatch.setattr(mod.repo, "condition_status", status)

    def flag_once(db, **flag):
        # add_dq_flag_once: one open flag per (security, check, record_key).
        same = [
            f
            for f in db.flags
            if (f["security_id"], f["check_name"], f["record_key"])
            == (flag["security_id"], flag["check_name"], flag["record_key"])
        ]
        if same:
            return False
        db.flags.append(flag)
        return True

    monkeypatch.setattr(mod.repo, "add_dq_flag_once", flag_once)
    monkeypatch.setattr(
        mod.repo, "land_payload", lambda db, **payload: db.landed.append(payload)
    )
    monkeypatch.setattr(mod.repo, "median_volume_before", lambda *a, **kw: 7.6e6)
    # No calendar: sessions are counted as weekdays.
    monkeypatch.setattr(mod.repo, "open_sessions", lambda *a, **kw: None)
    return mod


def test_parse_date_handles_the_feed_and_junk():
    assert _parse_date("2026-08-17") == date(2026, 8, 17)
    assert _parse_date("2026-08-17T00:00:00") == date(2026, 8, 17)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("not-a-date") is None


def test_marks_only_tracked_us_names(patched):
    db = _FakeDB({"ACME": 1, "BETA": 2})
    fmp = _FakeFMP(
        [
            {"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"},
            # Foreign venue -- out of our universe entirely.
            {"symbol": "2958.HK", "exchange": "HKSE", "delistedDate": "2026-08-17"},
            # On our venues but never ingested: nothing to protect, skip quietly.
            {"symbol": "GHOST", "exchange": "NYSE", "delistedDate": "2026-08-01"},
        ]
    )
    marked, seen = load_delisted(db, fmp)

    assert marked == 1
    assert seen == 2  # ACME + GHOST; the HKSE row is not ours
    assert db.marked == [(1, date(2026, 8, 17))]


def test_skips_rows_with_no_usable_date(patched):
    # A NULL delisted_date would leave the row inside 0009's active unique index,
    # so it must not be marked at all.
    db = _FakeDB({"ACME": 1})
    fmp = _FakeFMP([{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": None}])

    marked, seen = load_delisted(db, fmp)

    assert (marked, seen) == (0, 1)
    assert db.marked == []


def test_is_idempotent(patched):
    db = _FakeDB({"ACME": 1})
    rows = [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]

    assert load_delisted(db, _FakeFMP(rows))[0] == 1
    # Second sweep: mark_delisted refuses to re-stamp an already-delisted row.
    assert load_delisted(db, _FakeFMP(rows))[0] == 0
    assert len(db.marked) == 1


def test_the_sweep_lands_the_feed_it_read(patched):
    """`source vendor-gaps` reads the newest one: a security the feed names is
    delisted, not a symbol the vendor stopped serving."""
    db = _FakeDB({"ACME": 1})
    rows = [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]
    load_delisted(db, _FakeFMP(rows))
    (landed,) = db.landed
    assert landed["endpoint"] == "delisted-companies"
    assert landed["payload"] == rows and landed["ingestion_run_id"] == 7


def test_full_sweep_pages_deeper_than_the_nightly_tail(patched):
    db = _FakeDB({})
    fmp = _FakeFMP([])
    load_delisted(db, fmp, max_pages=500)
    assert fmp.max_pages == 500


def test_a_delisting_older_than_the_security_is_not_applied(patched):
    # CMDT in production: the feed still carries an earlier CMDT's 2018 delisting,
    # and applied by ticker it took PIMCO's live fund (first bar 2023-05-11) out of
    # the active universe. The next security-master load minted it a second row.
    db = _FakeDB({"CMDT": 1}, spans={1: (date(2023, 5, 11), date(2026, 9, 9))})
    fmp = _FakeFMP(
        [{"symbol": "CMDT", "exchange": "NYSE", "delistedDate": "2018-10-10"}]
    )

    marked, seen = load_delisted(db, fmp)

    assert (marked, seen) == (0, 1)
    assert db.marked == []


def test_a_delisting_the_security_traded_straight_through_is_not_applied(patched):
    # WBIF in production: bars from 2014 to 2026-08, "delisted" 2019-10-25.
    db = _FakeDB({"WBIF": 1}, spans={1: (date(2014, 8, 27), date(2026, 8, 24))})
    fmp = _FakeFMP(
        [{"symbol": "WBIF", "exchange": "NYSE", "delistedDate": "2019-10-25"}]
    )

    assert load_delisted(db, fmp) == (0, 1)
    assert db.marked == []


def test_a_delisting_the_feed_reported_late_is_still_applied(patched):
    # The nightly tail lags a delisting by weeks, and the price step keeps loading
    # the name until it is marked -- a few weeks of bars after it is normal.
    db = _FakeDB({"ACME": 1}, spans={1: (date(2019, 1, 2), date(2026, 9, 5))})
    fmp = _FakeFMP(
        [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]
    )

    assert load_delisted(db, fmp) == (1, 1)
    assert db.marked == [(1, date(2026, 8, 17))]


@pytest.mark.parametrize(
    ("delisted", "first", "last", "expected"),
    [
        (date(2018, 10, 10), date(2023, 5, 11), date(2026, 9, 9), "precedes_first_bar"),
        (date(2026, 3, 18), date(2026, 7, 20), date(2026, 9, 9), "precedes_first_bar"),
        (date(2019, 10, 25), date(2014, 8, 27), date(2026, 8, 24), "traded_after"),
        (date(2026, 8, 17), date(2019, 1, 2), date(2026, 8, 17), None),
        (date(2026, 8, 17), date(2019, 1, 2), date(2026, 9, 16), None),  # 30 days
        (date(2026, 8, 17), date(2019, 1, 2), date(2026, 9, 17), "traded_after"),
        (date(2026, 8, 17), None, None, None),  # no bars: nothing contradicts it
    ],
)
def test_delisting_contradicted(delisted, first, last, expected):
    from fafnir.ingest.delisted import delisting_contradicted

    assert delisting_contradicted(delisted, first, last) == expected


# ---------------------------------------------------------------------------
# A security still trading is not delisted (ADR 0014)
# ---------------------------------------------------------------------------

TODAY = date(2026, 10, 12)
ET = 12441


def _et_row(when="2026-10-05", venue="NYSE"):
    return {"symbol": "ET", "exchange": venue, "delistedDate": when}


def _bar(day, volume=7_200_000):
    return {"date": day, "close": 20.62, "volume": volume}


# ET's bars as FMP served them after its move to TXSE: the date of the "delisting"
# is its first TXSE session, and the sessions after it are there too.
_TXSE_BARS = [_bar(d) for d in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06")]


def _et_db(last_bar=date(2026, 10, 5), **kw):
    return _FakeDB({"ET": ET}, spans={ET: (date(2006, 2, 3), last_bar)}, **kw)


def test_a_recent_delisting_the_vendor_has_bars_after_is_declined_and_flagged(
    patched,
):
    """ET, 2026-10-05: NYSE to TXSE under the same ticker. The feed reported a NYSE
    delisting, and the sweep stamped it while the price feed kept serving bars."""
    db = _et_db()
    fmp = _FakeFMP([_et_row()], bars={"ET": _TXSE_BARS})

    assert load_delisted(db, fmp, today=TODAY) == (0, 1)
    assert db.marked == []
    # One request, from about ten sessions before the date to today.
    assert fmp.asked == [("ET", "2026-09-21", "2026-10-12")]
    (flag,) = db.flags
    assert flag["check_name"] == "security_delisting_contradicted"
    assert flag["severity"] == "warn" and flag["table_name"] == "core.security"
    assert flag["security_id"] == ET
    assert flag["record_key"] == {"symbol": "ET", "delisted_date": "2026-10-05"}
    assert flag["detail"] == {
        "feed_venue": "NYSE",
        "bars_after": 1,
        "first_after": "2026-10-06",
        "last_after": "2026-10-06",
        "median_volume_before_20": 7.6e6,
        "median_volume_after": 7.2e6,
    }
    run = patched.RunLog.last
    assert run.params["declined"] == {"still_trading": 1}
    assert run.params["prices_requested"] == 1
    assert run.rows_quarantined == 1  # wants a decision

    # The next night: declined again as undecided, unasked, and no second flag.
    assert load_delisted(db, fmp, today=TODAY + timedelta(days=1)) == (0, 1)
    assert len(db.flags) == 1 and db.marked == [] and len(fmp.asked) == 1
    assert patched.RunLog.last.params["declined"] == {"pending_decision": 1}

    # Nor is it stamped once the record is too old to ask about: the flag waits for
    # an operator's `security venue-transfer` or `security delist`.
    assert load_delisted(db, fmp, today=TODAY + timedelta(days=90)) == (0, 1)
    assert db.marked == []


def test_a_bar_on_the_delisting_date_is_not_a_bar_after_it(patched):
    """WBD-like: FMP's delistedDate is often the last trading day (AMZE, GBTG, BTAI,
    GWH, AETH, ZSC), so a genuine delisting has a bar on the date and none after."""
    db = _et_db()
    fmp = _FakeFMP([_et_row()], bars={"ET": _TXSE_BARS[:3]})

    assert load_delisted(db, fmp, today=TODAY) == (1, 1)
    assert db.marked == [(ET, date(2026, 10, 5))] and db.flags == []


def test_bars_after_a_move_to_otc_are_declined_too(patched):
    """GRP-UN left the NYSE for OTCQX on 2025-12-31 and FMP served its OTC bars on
    thin volume. That is a delisting here, but the bars cannot say so: the operator
    confirms it with `security delist`."""
    db = _et_db(last_bar=date(2025, 12, 31))
    otc = [_bar(d, volume=1_317) for d in ("2026-01-02", "2026-01-05", "2026-01-06")]
    fmp = _FakeFMP([_et_row(when="2025-12-31")], bars={"ET": otc})

    assert load_delisted(db, fmp, today=date(2026, 1, 7)) == (0, 1)
    assert db.marked == [] and db.flags[0]["detail"]["bars_after"] == 3


def test_an_accepted_contradiction_declines_silently(patched):
    """`security venue-transfer` accepts the flag for the feed record, and the old
    NYSE record then costs nothing, for as long as the feed keeps it."""
    db = _et_db(accepted={(ET, "ET", "2026-10-05")})
    fmp = _FakeFMP([_et_row()])  # asking fails the test

    assert load_delisted(db, fmp, today=TODAY) == (0, 1)
    assert fmp.asked == [] and db.flags == [] and db.marked == []
    run = patched.RunLog.last
    assert run.params["declined"] == {"known_transfer": 1}
    assert run.rows_quarantined == 0  # nothing to look at


def test_an_accepted_contradiction_is_for_that_feed_record_only(patched):
    db = _et_db(accepted={(ET, "ET", "2026-10-05")})
    fmp = _FakeFMP([_et_row(when="2026-10-07")], bars={"ET": _TXSE_BARS})

    assert load_delisted(db, fmp, today=TODAY) == (1, 1)


@pytest.mark.parametrize("answer", [SourceError("GET eod returned 503")], ids=["error"])
def test_a_failed_request_declines_for_tonight(patched, answer):
    db = _et_db()
    fmp = _FakeFMP([_et_row()], bars={"ET": answer})

    assert load_delisted(db, fmp, today=TODAY) == (0, 1)
    assert db.marked == [] and db.flags == []
    assert patched.RunLog.last.params["declined"] == {"fetch_failed": 1}
    assert patched.RunLog.last.rows_quarantined == 1


def test_an_old_delisting_is_never_fetched(patched):
    """The window keeps a `--full` sweep (4,041 candidates in 2026-09) from asking
    about each one. _FakeFMP fails any request it was not given."""
    db = _et_db(last_bar=date(2026, 7, 1))
    fmp = _FakeFMP([_et_row(when="2026-07-01")])

    assert load_delisted(db, fmp, today=TODAY) == (1, 1)
    assert fmp.asked == []


def test_a_security_whose_bars_stopped_well_before_is_not_fetched(patched):
    """Recent, but its last bar is more than ten sessions before the delisting:
    it stopped trading, it did not move."""
    db = _et_db(last_bar=date(2026, 9, 1))
    fmp = _FakeFMP([_et_row()])

    assert load_delisted(db, fmp, today=TODAY) == (1, 1)
    assert fmp.asked == []


def test_a_txse_delisting_is_read(patched):
    """TXSE is a listed venue: should the feed ever name it, the sweep reads it."""
    db = _et_db(last_bar=date(2026, 7, 1))
    fmp = _FakeFMP([_et_row(when="2026-07-01", venue="TXSE")])

    assert load_delisted(db, fmp, today=TODAY) == (1, 1)


@pytest.mark.parametrize(
    ("delisted", "last_bar", "recent"),
    [
        (date(2026, 10, 5), date(2026, 10, 5), True),
        (date(2026, 10, 8), date(2026, 10, 9), True),
        (date(2026, 10, 5), date(2026, 9, 21), True),  # 10 weekdays before
        (date(2026, 10, 5), date(2026, 9, 18), False),  # 11
        (date(2026, 8, 13), date(2026, 8, 13), True),  # 60 days
        (date(2026, 8, 12), date(2026, 8, 12), False),  # 61
        (date(2026, 10, 5), None, False),  # no bars: nothing to protect
    ],
)
def test_is_recent_delisting(patched, delisted, last_bar, recent):
    assert patched.is_recent_delisting(None, delisted, last_bar, TODAY) is recent


def test_sessions_are_counted_on_the_calendar_only_where_it_spans_the_dates(
    patched, monkeypatch
):
    """Labor Day 2026 is closed. Outside the calendar's span a missing row means
    unknown, so the count falls back to weekdays rather than to zero."""
    labor_day = date(2026, 9, 7)

    def calendar(last):
        def open_sessions(db, exchange_code, start, end):
            days = (start + timedelta(days=n) for n in range((end - start).days + 1))
            opened = frozenset(
                d for d in days if d.weekday() < 5 and d != labor_day and d <= last
            )
            return opened, date(1990, 1, 2), last

        return open_sessions

    repo = patched.repo
    monkeypatch.setattr(repo, "open_sessions", calendar(date(2027, 12, 31)))
    assert repo.sessions_apart(None, date(2026, 9, 8), date(2026, 9, 4)) == 1
    monkeypatch.setattr(repo, "open_sessions", calendar(date(2024, 12, 31)))
    assert repo.sessions_apart(None, date(2026, 9, 4), date(2026, 9, 8)) == 2
