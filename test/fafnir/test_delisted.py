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

    def __init__(self, known: dict[str, int], already_delisted=frozenset()):
        self.known = known
        self.already_delisted = set(already_delisted)
        self.marked: list[tuple[int, date]] = []


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

    def mark(db, *, security_id, delisted_date):
        if security_id in db.already_delisted:
            return False
        db.already_delisted.add(security_id)
        db.marked.append((security_id, delisted_date))
        return True

    monkeypatch.setattr(mod.repo, "resolve_security_id", resolve)
    monkeypatch.setattr(mod.repo, "mark_delisted", mark)
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
