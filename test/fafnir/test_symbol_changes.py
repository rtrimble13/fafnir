"""Unit tests for the ticker-rename loader (no DB, no network)."""

from __future__ import annotations

from datetime import date

import pytest

from fafnir.db.repository import (
    CHANGE_APPLIED,
    CHANGE_CONFLICT,
    CHANGE_IGNORED,
    CHANGE_UNKNOWN,
    SymbolChangeOutcome,
)
from fafnir.ingest.symbol_changes import (
    _ordered_changes,
    _parse_date,
    load_symbol_changes,
)
from fafnir.sources.fmp import FMPClient, _row_fingerprint


class _FakeFMP:
    bytes_downloaded = 0

    def __init__(self, rows):
        self.rows = rows
        self.max_pages = None

    def symbol_changes(self, *, max_pages=5):
        self.max_pages = max_pages
        return self.rows


class _FakeRun:
    run_id = 1
    symbols_requested = 0
    rows_inserted = 0
    bytes_downloaded = 0


class _FakeDB:
    """Stands in for Database plus the repository calls the loader makes."""

    def __init__(self, outcomes=None, recorded=None):
        # (old, new) -> SymbolChangeOutcome the fake repository should return.
        self.outcomes = outcomes or {}
        # (old, new, date) -> status already in core.symbol_change.
        self.recorded = recorded or {}
        self.applied: list[tuple[str, str, date]] = []
        self.audit: list[dict] = []
        self.flags: list[str] = []
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
def patched(monkeypatch):
    """Route RunLog and the repository at the fakes."""
    import fafnir.ingest.symbol_changes as mod

    class _RunLogStub:
        def __init__(self, *a, **kw):
            self.run = _FakeRun()

        def __enter__(self):
            return self.run

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mod, "RunLog", _RunLogStub)
    monkeypatch.setattr(mod.repo, "land_payload", lambda db, **kw: None)

    def status(db, *, old_symbol, new_symbol, change_date, source="fmp"):
        return db.recorded.get((old_symbol, new_symbol, change_date))

    def apply(
        db, *, old_symbol, new_symbol, change_date, company_name=None, source="fmp"
    ):
        db.applied.append((old_symbol, new_symbol, change_date))
        return db.outcomes.get(
            (old_symbol, new_symbol), SymbolChangeOutcome(CHANGE_UNKNOWN, None)
        )

    def record(db, **kw):
        db.audit.append(kw)

    def flag(db, *, check_name, **kw):
        db.flags.append(check_name)

    monkeypatch.setattr(mod.repo, "symbol_change_status", status)
    monkeypatch.setattr(mod.repo, "apply_symbol_change", apply)
    monkeypatch.setattr(mod.repo, "record_symbol_change", record)
    monkeypatch.setattr(mod.repo, "add_dq_flag", flag)
    return mod


def test_parse_date_handles_the_feed_and_junk():
    assert _parse_date("2026-08-17") == date(2026, 8, 17)
    assert _parse_date("2026-08-17T00:00:00") == date(2026, 8, 17)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("not-a-date") is None


def test_ordered_changes_applies_oldest_first():
    # The feed arrives newest first. A ticker that changed twice (A->B->C) only
    # resolves as a chain when the renames are applied in the order they happened.
    rows = [
        {"date": "2026-03-02", "oldSymbol": "BBB", "newSymbol": "CCC"},
        {"date": "2026-01-05", "oldSymbol": "AAA", "newSymbol": "BBB"},
    ]
    assert [(o, n) for _, o, n, _ in _ordered_changes(rows)] == [
        ("AAA", "BBB"),
        ("BBB", "CCC"),
    ]


def test_ordered_changes_drops_rows_it_cannot_use():
    rows = [
        {"date": None, "oldSymbol": "AAA", "newSymbol": "BBB"},  # no period boundary
        {"date": "2026-01-05", "oldSymbol": "", "newSymbol": "BBB"},
        {"date": "2026-01-05", "oldSymbol": "AAA", "newSymbol": ""},
        {"date": "2026-01-05", "oldSymbol": "AAA", "newSymbol": "AAA"},  # not a change
    ]
    assert _ordered_changes(rows) == []


def test_ordered_changes_normalizes_case_and_whitespace():
    rows = [{"date": "2026-01-05", "oldSymbol": " fb ", "newSymbol": "meta"}]
    assert [(o, n) for _, o, n, _ in _ordered_changes(rows)] == [("FB", "META")]


def test_applies_a_rename_and_records_it(patched):
    db = _FakeDB(outcomes={("FB", "META"): SymbolChangeOutcome(CHANGE_APPLIED, 7)})
    fmp = _FakeFMP(
        [
            {
                "date": "2026-06-09",
                "oldSymbol": "FB",
                "newSymbol": "META",
                "companyName": "Meta",
            }
        ]
    )

    counts = load_symbol_changes(db, fmp)

    assert counts["applied"] == 1
    assert db.applied == [("FB", "META", date(2026, 6, 9))]
    assert db.audit[0]["status"] == CHANGE_APPLIED
    assert db.audit[0]["security_id"] == 7
    assert db.commits == 1


def test_untracked_renames_are_counted_but_not_recorded(patched):
    # The feed is global. Recording every rename fafnir does not track would build
    # an audit table of other people's tickers.
    db = _FakeDB()
    fmp = _FakeFMP(
        [{"date": "2026-06-09", "oldSymbol": "2958.HK", "newSymbol": "2959.HK"}]
    )

    counts = load_symbol_changes(db, fmp)

    assert (counts["unknown"], counts["applied"]) == (1, 0)
    assert db.audit == []


def test_already_applied_renames_are_skipped(patched):
    # Idempotency: the same tail is re-read every night, and re-applying a rename
    # would move a ticker that has legitimately moved on since.
    db = _FakeDB(recorded={("FB", "META", date(2026, 6, 9)): CHANGE_APPLIED})
    fmp = _FakeFMP([{"date": "2026-06-09", "oldSymbol": "FB", "newSymbol": "META"}])

    counts = load_symbol_changes(db, fmp)

    assert (counts["skipped"], counts["applied"]) == (1, 0)
    assert db.applied == []


def test_conflicts_are_flagged_and_left_unapplied(patched):
    db = _FakeDB(outcomes={("AAA", "BBB"): SymbolChangeOutcome(CHANGE_CONFLICT, 3)})
    fmp = _FakeFMP([{"date": "2026-06-09", "oldSymbol": "AAA", "newSymbol": "BBB"}])

    counts = load_symbol_changes(db, fmp)

    assert counts["conflict"] == 1
    assert db.flags == ["symbol_change_conflict"]
    assert db.audit[0]["status"] == CHANGE_CONFLICT


def test_conflicts_are_retried_on_the_next_sweep(patched):
    # A conflict usually means the feed lagged the screener; it can clear itself,
    # so it must not be treated as terminal the way `applied` is.
    db = _FakeDB(
        outcomes={("AAA", "BBB"): SymbolChangeOutcome(CHANGE_APPLIED, 3)},
        recorded={("AAA", "BBB", date(2026, 6, 9)): CHANGE_CONFLICT},
    )
    fmp = _FakeFMP([{"date": "2026-06-09", "oldSymbol": "AAA", "newSymbol": "BBB"}])

    assert load_symbol_changes(db, fmp)["applied"] == 1


def test_ticker_reuse_by_a_dead_issuer_is_ignored_not_applied(patched):
    db = _FakeDB(outcomes={("CC", "CCX"): SymbolChangeOutcome(CHANGE_IGNORED, None)})
    fmp = _FakeFMP([{"date": "2026-06-09", "oldSymbol": "CC", "newSymbol": "CCX"}])

    counts = load_symbol_changes(db, fmp)

    assert counts["ignored"] == 1
    assert db.audit[0]["status"] == CHANGE_IGNORED


def test_folded_duplicates_are_counted(patched):
    db = _FakeDB(outcomes={("FB", "META"): SymbolChangeOutcome(CHANGE_APPLIED, 7, 42)})
    fmp = _FakeFMP([{"date": "2026-06-09", "oldSymbol": "FB", "newSymbol": "META"}])

    counts = load_symbol_changes(db, fmp)

    assert (counts["applied"], counts["folded"]) == (1, 1)
    assert db.audit[0]["detail"]["folded_security_id"] == 42


def test_full_sweep_pages_deeper_than_the_nightly_tail(patched):
    db = _FakeDB()
    fmp = _FakeFMP([])
    load_symbol_changes(db, fmp, max_pages=500)
    assert fmp.max_pages == 500


# -- paging guard -----------------------------------------------------------


def test_row_fingerprint_falls_back_when_the_key_fields_are_absent():
    # Regression guard: the rename feed carries no `symbol`, so a fingerprint of
    # None on every page would disable the repeat-page guard entirely.
    assert _row_fingerprint({"symbol": "AAPL"}, ("symbol",)) == "AAPL"
    assert (
        _row_fingerprint(
            {"oldSymbol": "FB", "newSymbol": "META"}, ("oldSymbol", "newSymbol")
        )
        == "FB|META"
    )
    assert _row_fingerprint({"oldSymbol": "FB"}, ("symbol",)) == '{"oldSymbol": "FB"}'
    assert _row_fingerprint("not-a-dict", ("symbol",)) is None


def test_symbol_change_paging_stops_when_the_server_ignores_page(monkeypatch):
    # An endpoint that ignores `page` returns the same full page forever. Without
    # a working fingerprint that costs max_pages requests and multiplies the rows.
    client = FMPClient("key")
    page = [{"date": "2026-06-09", "oldSymbol": "FB", "newSymbol": "META"}] * (
        FMPClient.SYMBOL_CHANGE_PAGE_SIZE
    )
    calls = []

    def fake_call(endpoint, params=None):
        calls.append(params)
        return page, 200, 0

    monkeypatch.setattr(client, "_call", fake_call)

    rows = client.symbol_changes(max_pages=5)

    assert len(calls) == 2  # first page, then one repeat that ends it
    assert len(rows) == len(page)
