"""Unit tests for the delisting reconciliation loader (no DB, no network)."""

from __future__ import annotations

from datetime import date

import pytest

from fafnir.ingest.delisted import (
    _parse_date,
    audit_delisted,
    format_audit_report,
    load_delisted,
)


class _FakeFMP:
    bytes_downloaded = 0

    def __init__(self, rows):
        self.rows = rows
        self.max_pages = None

    def delisted_companies(self, *, max_pages=5):
        self.max_pages = max_pages
        return self.rows


class _FakeRun:
    run_id = 42
    symbols_requested = 0
    rows_inserted = 0
    bytes_downloaded = 0


class _FakeDB:
    """Stands in for Database + the repository calls the loader makes."""

    def __init__(
        self,
        known: dict[str, int],
        already_delisted=frozenset(),
        resolvable: dict[str, int] | None = None,
        last_bar: dict[int, date] | None = None,
    ):
        self.known = known  # what active_security_for_symbol answers
        # security_id -> the last trade_date it has a bar for, if any.
        self.last_bar = dict(last_bar or {})
        self.flags: list[dict] = []
        # What resolve_security_id answers *beyond* `known`: a ticker the master
        # has heard of but which nothing is listed under.
        self.resolvable = dict(resolvable or {})
        self.already_delisted = set(already_delisted)
        self.marked: list[tuple[int, date]] = []
        self.minted: list[dict] = []
        self.landed: list[dict] = []
        self.commits = 0
        self.rollbacks = 0
        self._next_id = 900

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


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

    def resolve_any(db, symbol, source="fmp"):
        if symbol in db.known:
            return db.known[symbol]
        return db.resolvable.get(symbol)

    monkeypatch.setattr(mod.repo, "resolve_security_id", resolve_any)

    def mark(db, *, security_id, delisted_date):
        if security_id in db.already_delisted:
            return False
        db.already_delisted.add(security_id)
        db.marked.append((security_id, delisted_date))
        return True

    monkeypatch.setattr(mod.repo, "mark_delisted", mark)

    def traded_after(db, security_id, after):
        last = db.last_bar.get(security_id)
        return last is not None and last > after

    monkeypatch.setattr(mod.repo, "security_traded_after", traded_after)

    def flag_once(db, **kw):
        db.flags.append(kw)
        return True

    monkeypatch.setattr(mod.repo, "add_dq_flag_once", flag_once)

    def _unguarded(db, **kw):  # pragma: no cover -- the point is that it is unused
        raise AssertionError(
            "use add_dq_flag_once: a --full sweep re-reads the same rows every run, "
            "so an unguarded insert grows the DQ queue without bound"
        )

    monkeypatch.setattr(mod.repo, "add_dq_flag", _unguarded)

    def land(db, **kw):
        db.landed.append(kw)

    monkeypatch.setattr(mod.repo, "land_payload", land)

    def upsert_security(db, **kw):
        db._next_id += 1
        db.minted.append(kw)
        # A minted security is immediately resolvable, which is what makes a
        # second sweep skip it.
        db.resolvable[kw["primary_symbol"]] = db._next_id
        return db._next_id

    monkeypatch.setattr(mod.repo, "upsert_security", upsert_security)
    monkeypatch.setattr(mod.repo, "upsert_symbol_xref", lambda db, **kw: None)
    monkeypatch.setattr(mod.repo, "ensure_exchange", lambda db, code, *a, **k: None)
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
    result = load_delisted(db, fmp)

    assert result.marked == 1
    assert result.seen == 2  # ACME + GHOST; the HKSE row is not ours
    assert result.minted == 0
    assert result.unmatched == 1  # GHOST
    assert db.marked == [(1, date(2026, 8, 17))]


def test_skips_rows_with_no_usable_date(patched):
    # A NULL delisted_date would leave the row inside 0009's active unique index,
    # so it must not be marked at all.
    db = _FakeDB({"ACME": 1})
    fmp = _FakeFMP([{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": None}])

    result = load_delisted(db, fmp)

    assert (result.marked, result.seen, result.undated) == (0, 1, 1)
    assert db.marked == []


def test_is_idempotent(patched):
    db = _FakeDB({"ACME": 1})
    rows = [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]

    assert load_delisted(db, _FakeFMP(rows)).marked == 1
    # Second sweep: mark_delisted refuses to re-stamp an already-delisted row.
    second = load_delisted(db, _FakeFMP(rows))
    assert (second.marked, second.already) == (0, 1)
    assert len(db.marked) == 1


def test_full_sweep_pages_deeper_than_the_nightly_tail(patched):
    db = _FakeDB({})
    fmp = _FakeFMP([])
    load_delisted(db, fmp, max_pages=500)
    assert fmp.max_pages == 500


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def test_the_feed_is_landed_before_it_is_interpreted(patched):
    """Without this there is no record of what the vendor offered on a given night,
    which is the question a survivorship audit asks in hindsight -- when the feed
    has already rolled forward and cannot be re-fetched."""
    rows = [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]
    db = _FakeDB({"ACME": 1})

    load_delisted(db, _FakeFMP(rows), max_pages=500)

    assert len(db.landed) == 1
    landed = db.landed[0]
    assert landed["endpoint"] == "delisted-companies"
    assert landed["payload"] == rows
    assert landed["params"]["max_pages"] == 500
    assert landed["ingestion_run_id"] == 42
    assert landed["payload_hash"]


def test_an_empty_feed_still_lands_a_payload(patched):
    # "The feed returned nothing" is itself a finding, and it is indistinguishable
    # from "the loader did not run" unless the empty payload is on record.
    db = _FakeDB({})
    load_delisted(db, _FakeFMP([]))
    assert len(db.landed) == 1
    assert db.landed[0]["payload"] == []


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_mints_names_the_master_never_held(patched):
    db = _FakeDB({})
    rows = [
        {
            "symbol": "ABMD",
            "companyName": "Abiomed, Inc.",
            "exchange": "NASDAQ",
            "ipoDate": "1987-07-30",
            "delistedDate": "2022-12-22",
        }
    ]

    result = load_delisted(db, _FakeFMP(rows), backfill=True)

    assert (result.minted, result.marked, result.unmatched) == (1, 0, 1)
    minted = db.minted[0]
    assert minted["primary_symbol"] == "ABMD"
    assert minted["company_name"] == "Abiomed, Inc."
    assert minted["exchange_code"] == "NASDAQ"
    assert minted["ipo_date"] == date(1987, 7, 30)
    # Minted inactive, then stamped -- never inserted with the date already set,
    # which would miss 0009's partial-index arbiter and duplicate on a re-run.
    assert minted["is_actively_trading"] is False
    assert minted["asset_type"] == "equity"
    assert db.marked == [(901, date(2022, 12, 22))]


def test_backfill_is_idempotent(patched):
    rows = [
        {"symbol": "ABMD", "exchange": "NASDAQ", "delistedDate": "2022-12-22"},
    ]
    db = _FakeDB({})

    assert load_delisted(db, _FakeFMP(rows), backfill=True).minted == 1
    # Second sweep: the ticker now resolves, so it is never minted again.
    second = load_delisted(db, _FakeFMP(rows), backfill=True)
    assert (second.minted, second.unmatched) == (0, 1)
    assert len(db.minted) == 1


def test_backfill_refuses_a_ticker_the_master_already_knows(patched):
    """Ticker reuse: CC was Circuit City, and is now Chemours. Minting past a known
    ticker means writing past the two unique indexes that keep a live company's
    identity and price history separate from a dead one's."""
    db = _FakeDB({}, resolvable={"CC": 7})
    rows = [{"symbol": "CC", "exchange": "NYSE", "delistedDate": "2009-01-16"}]

    result = load_delisted(db, _FakeFMP(rows), backfill=True)

    assert (result.minted, result.unmatched, result.unmintable) == (0, 1, 1)
    assert db.minted == []
    assert db.marked == []


def test_backfill_does_not_touch_a_security_trading_under_its_own_ticker(patched):
    # The ordinary marking path still owns this row; backfill must not divert it.
    db = _FakeDB({"ACME": 1})
    rows = [{"symbol": "ACME", "exchange": "NASDAQ", "delistedDate": "2026-08-17"}]

    result = load_delisted(db, _FakeFMP(rows), backfill=True)

    assert (result.marked, result.minted) == (1, 0)
    assert db.minted == []


def test_without_backfill_nothing_is_minted(patched):
    db = _FakeDB({})
    rows = [{"symbol": "GHOST", "exchange": "NYSE", "delistedDate": "2020-01-02"}]

    result = load_delisted(db, _FakeFMP(rows))

    assert (result.minted, result.unmatched, result.unmintable) == (0, 1, 1)
    assert db.minted == []


def test_backfill_skips_an_undated_row(patched):
    # Same reason as the marking path: a minted row with a NULL delisted_date sits
    # in the active unique index, claiming to be a listed company.
    db = _FakeDB({})
    rows = [{"symbol": "GHOST", "exchange": "NYSE", "delistedDate": None}]

    result = load_delisted(db, _FakeFMP(rows), backfill=True)

    assert (result.minted, result.undated) == (0, 1)
    assert db.minted == []


# ---------------------------------------------------------------------------
# Venue normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_in_scope",
    [
        ("NASDAQ", True),
        ("New York Stock Exchange", True),
        ("NASDAQ Capital Market", True),
        ("NYSE American", True),
        # Foreign venues that share a prefix with a US one: the exact-match alias
        # table must not admit these, which a startswith() rule would.
        ("NASDAQ Dubai", False),
        ("CBOE Europe", False),
        ("HKSE", False),
    ],
)
def test_long_form_us_venue_names_are_in_scope(patched, raw, expected_in_scope):
    db = _FakeDB({"ACME": 1})
    fmp = _FakeFMP([{"symbol": "ACME", "exchange": raw, "delistedDate": "2026-08-17"}])

    result = load_delisted(db, fmp)

    assert (result.seen == 1) is expected_in_scope
    assert (result.marked == 1) is expected_in_scope


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_patched(monkeypatch):
    import fafnir.ingest.delisted as mod
    from fafnir.db.repository import SymbolCoverage

    def index(db):
        return SymbolCoverage(listed=set(db.listed), known=set(db.known_ever))

    monkeypatch.setattr(mod.repo, "symbol_coverage_index", index)
    return mod


class _AuditDB:
    def __init__(self, listed=(), known_ever=()):
        self.listed = set(listed)
        self.known_ever = set(known_ever) | set(listed)


def test_audit_classifies_every_row_and_writes_nothing(audit_patched):
    db = _AuditDB(listed={"LIVE"}, known_ever={"REUSED"})
    fmp = _FakeFMP(
        [
            {"symbol": "LIVE", "exchange": "NYSE", "delistedDate": "2024-01-02"},
            {"symbol": "REUSED", "exchange": "NYSE", "delistedDate": "2009-01-16"},
            {"symbol": "GHOST", "exchange": "NASDAQ", "delistedDate": "2001-06-01"},
            {"symbol": "NODATE", "exchange": "NYSE", "delistedDate": None},
            {"symbol": "2958.HK", "exchange": "HKSE", "delistedDate": "2024-01-02"},
        ]
    )

    report = audit_delisted(db, fmp, max_pages=500)

    assert report["feed_rows"] == 5
    assert report["in_scope"] == 4
    assert report["held"] == 1
    assert report["reused"] == 1
    assert report["mintable"] == 1
    assert report["undated"] == 1
    assert report["out_of_scope"] == 1
    assert report["oldest"] == "2001-06-01"
    assert report["newest"] == "2024-01-02"
    assert report["by_year"] == {"2001": 1, "2009": 1, "2024": 1}
    assert {r["symbol"]: r["status"] for r in report["rows"]}["GHOST"] == "mintable"


def test_audit_surfaces_dropped_venue_names(audit_patched):
    """The whole point: a US venue the feed spells long-form shows up here as a
    fixable alias rather than as silently missing coverage."""
    db = _AuditDB()
    fmp = _FakeFMP(
        [
            {
                "symbol": "A",
                "exchange": "Some Regional Exchange",
                "delistedDate": "2020-01-01",
            },
            {
                "symbol": "B",
                "exchange": "Some Regional Exchange",
                "delistedDate": "2020-01-02",
            },
            {"symbol": "C", "exchange": "HKSE", "delistedDate": "2020-01-03"},
        ]
    )

    report = audit_delisted(db, fmp)

    assert report["out_of_scope"] == 3
    assert report["unmapped_venues"]["SOME REGIONAL EXCHANGE"] == 2
    assert "Some Regional Exchange".upper() in format_audit_report(report)


def test_audit_report_warns_when_the_row_count_looks_truncated(audit_patched):
    db = _AuditDB()
    rows = [
        {"symbol": f"S{i}", "exchange": "NYSE", "delistedDate": "2020-01-01"}
        for i in range(100)
    ]

    text = format_audit_report(audit_delisted(db, _FakeFMP(rows), max_pages=1))

    assert "truncation" in text


def test_audit_report_renders_an_empty_feed(audit_patched):
    text = format_audit_report(audit_delisted(_AuditDB(), _FakeFMP([])))
    assert "Nothing to backfill" in text


# ---------------------------------------------------------------------------
# The reuse guard
# ---------------------------------------------------------------------------


def test_a_deep_sweep_refuses_to_retire_a_live_company_on_a_reused_ticker(patched):
    """CC was Circuit City until 2009 and has been Chemours since 2015. Marking on
    ticker alone retires the LIVE company on a stranger's date -- one-way, silent,
    and it drops Chemours out of the price universe for good. A security that
    stopped trading in 2009 has no 2024 bar, so the bar is the disproof."""
    db = _FakeDB({"CC": 5}, last_bar={5: date(2024, 6, 3)})
    fmp = _FakeFMP([{"symbol": "CC", "exchange": "NYSE", "delistedDate": "2009-01-16"}])

    result = load_delisted(db, fmp, max_pages=500)

    assert (result.marked, result.reused, result.seen) == (0, 1, 1)
    assert db.marked == []
    assert db.flags[0]["check_name"] == "delisted_ticker_reuse"
    assert db.flags[0]["security_id"] == 5
    assert db.flags[0]["record_key"] == {"symbol": "CC", "delisted_date": "2009-01-16"}


def test_a_genuine_delisting_is_still_marked_when_bars_stop_before_it(patched):
    # The guard must not block the ordinary case: a company whose last bar is at or
    # before the delisting date is exactly the company that delisted.
    db = _FakeDB({"DEAD": 5}, last_bar={5: date(2026, 8, 17)})
    fmp = _FakeFMP(
        [{"symbol": "DEAD", "exchange": "NYSE", "delistedDate": "2026-08-17"}]
    )

    result = load_delisted(db, fmp)

    assert (result.marked, result.reused) == (1, 0)
    assert db.marked == [(5, date(2026, 8, 17))]


def test_a_security_with_no_bars_at_all_is_still_markable(patched):
    # No bars is not evidence of reuse -- it is a security minted before its first
    # price run. The guard only fires on positive evidence of later trading.
    db = _FakeDB({"NEW": 5})
    fmp = _FakeFMP(
        [{"symbol": "NEW", "exchange": "NYSE", "delistedDate": "2026-08-17"}]
    )

    assert load_delisted(db, fmp).marked == 1


def test_the_reuse_flag_goes_through_the_deduplicating_helper(patched):
    """A --full sweep re-reads the same rows every run. `add_dq_flag` would add one
    unresolved flag per run for one standing problem; the fixture makes reaching for
    it an error, so this asserts the loader took the deduplicating path."""
    db = _FakeDB({"CC": 5}, last_bar={5: date(2024, 6, 3)})
    rows = [{"symbol": "CC", "exchange": "NYSE", "delistedDate": "2009-01-16"}]

    load_delisted(db, _FakeFMP(rows), max_pages=500)
    load_delisted(db, _FakeFMP(rows), max_pages=500)

    assert len(db.flags) == 2
    assert all(f["check_name"] == "delisted_ticker_reuse" for f in db.flags)
