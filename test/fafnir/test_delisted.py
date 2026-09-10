"""Unit tests for the delisting reconciliation loader (no DB, no network)."""

from __future__ import annotations

from datetime import date

import pytest

from fafnir.ingest.delisted import _parse_date, load_delisted


class _FakeFMP:
    bytes_downloaded = 0

    def __init__(self, rows):
        self.rows = rows
        self.max_pages = None

    def delisted_companies(self, *, max_pages=5):
        self.max_pages = max_pages
        return self.rows


class _FakeRun:
    symbols_requested = 0
    rows_inserted = 0
    bytes_downloaded = 0


class _FakeDB:
    """Stands in for Database + the repository calls the loader makes."""

    def __init__(
        self,
        known: dict[str, int],
        already_delisted=frozenset(),
        spans: dict[int, tuple[date, date]] | None = None,
    ):
        self.known = known
        self.already_delisted = set(already_delisted)
        # security_id -> (first bar, last bar); absent means no bars at all.
        self.spans = spans or {}
        self.marked: list[tuple[int, date]] = []
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
def patched(monkeypatch):
    """Route RunLog and the repository at the fakes."""
    import fafnir.ingest.delisted as mod

    class _RunLogStub:
        def __init__(self, *a, **kw):
            self.run = _FakeRun()

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
