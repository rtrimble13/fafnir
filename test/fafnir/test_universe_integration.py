"""
Integration tests for automatic universe maintenance (needs FAFNIR_TEST_DSN).

Two things have to be true for the warehouse to stay in scope on its own:
new listings enter it, and a renamed ticker stays the *same* security rather than
becoming a second one. Both are SQL-shaped problems (the partial unique index of
0009, the validity periods of core.symbol_xref), so they are tested against a real
database rather than fakes.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import security_master
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

CHANGE_DATE = dt.date(2024, 6, 10)


def _mk_security(db, symbol, *, exchange="NASDAQ", name="Test Co"):
    repo.ensure_exchange(db, exchange, exchange, "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code=exchange,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _give_history(db, sid, trade_date=dt.date(2024, 6, 3), close=100):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": trade_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            }
        ],
    )


def _xref(db, sid):
    return db.fetchall(
        "SELECT symbol, valid_from, valid_to FROM core.symbol_xref "
        "WHERE security_id = %s ORDER BY valid_from",
        (sid,),
    )


# ---------------------------------------------------------------------------
# Renames keep one company one security
# ---------------------------------------------------------------------------


def test_rename_keeps_history_watermark_and_identity(db):
    sid = _mk_security(db, "FB", name="Facebook, Inc.")
    _give_history(db, sid)
    repo.set_watermark(db, "fmp", PRICE_ENDPOINT, dt.date(2024, 6, 3), sid)

    outcome = repo.apply_symbol_change(
        db,
        old_symbol="FB",
        new_symbol="META",
        change_date=CHANGE_DATE,
        company_name="Meta Platforms, Inc.",
    )

    assert outcome.status == repo.CHANGE_APPLIED
    assert outcome.security_id == sid  # the whole point: no second security_id
    # One security, now trading under the new ticker.
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    row = db.fetchone(
        "SELECT primary_symbol, company_name FROM core.security WHERE security_id = %s",
        (sid,),
    )
    assert row["primary_symbol"] == "META"
    assert row["company_name"] == "Meta Platforms, Inc."
    # History and the incremental watermark stay attached, so the next nightly run
    # asks META for the tail rather than re-backfilling 15 years.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s", (sid,)
        )
        == 1
    )
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2024, 6, 3)


def test_rename_closes_the_old_ticker_the_day_before_the_change(db):
    sid = _mk_security(db, "FB")
    repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    periods = {r["symbol"]: r for r in _xref(db, sid)}
    # Contiguous, never both open: an overlap would make the ticker ambiguous on
    # the changeover day, and XREF_RESOLVE_SQL reads only open periods.
    assert periods["FB"]["valid_to"] == CHANGE_DATE - dt.timedelta(days=1)
    assert periods["META"]["valid_from"] == CHANGE_DATE
    assert periods["META"]["valid_to"] is None
    # Both tickers still resolve to the one security -- the old one through the
    # closed period (research on the pre-rename name), the new one as current.
    assert repo.resolve_security_id(db, "META") == sid
    assert repo.resolve_security_id(db, "FB") == sid


def test_rename_is_idempotent(db):
    sid = _mk_security(db, "FB")
    first = repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )
    before = _xref(db, sid)
    second = repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    assert first.status == repo.CHANGE_APPLIED
    # The second pass finds FB unknown (its period is closed) and META already
    # ours, so it changes nothing -- rather than reopening the old ticker.
    assert second.status in (repo.CHANGE_UNKNOWN, repo.CHANGE_APPLIED)
    assert _xref(db, sid) == before
    assert db.fetchval("SELECT count(*) FROM core.security") == 1


def test_rename_chain_applied_in_order_lands_on_the_final_ticker(db):
    sid = _mk_security(db, "AAA")
    repo.apply_symbol_change(
        db, old_symbol="AAA", new_symbol="BBB", change_date=dt.date(2024, 1, 5)
    )
    repo.apply_symbol_change(
        db, old_symbol="BBB", new_symbol="CCC", change_date=dt.date(2024, 3, 2)
    )

    assert (
        db.fetchval(
            "SELECT primary_symbol FROM core.security WHERE security_id = %s", (sid,)
        )
        == "CCC"
    )
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert [r["symbol"] for r in _xref(db, sid)] == ["AAA", "BBB", "CCC"]


# ---------------------------------------------------------------------------
# Collisions with a duplicate the security master already minted
# ---------------------------------------------------------------------------


def test_empty_duplicate_minted_by_the_screener_is_folded_in(db):
    # The security master ran before the rename feed caught up, so the new ticker
    # already exists as a bare row with nothing in it.
    sid = _mk_security(db, "FB")
    _give_history(db, sid)
    stub = _mk_security(db, "META")
    repo.add_dq_flag(db, check_name="test_flag", security_id=stub)

    outcome = repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    assert outcome.status == repo.CHANGE_APPLIED
    assert outcome.folded_security_id == stub
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert repo.resolve_security_id(db, "META") == sid
    # The stub's annotations move to the survivor rather than dangling.
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s", (sid,)
        )
        == 1
    )


def test_duplicate_that_carries_history_is_a_conflict_not_a_merge(db):
    sid = _mk_security(db, "FB")
    _give_history(db, sid)
    other = _mk_security(db, "META")
    _give_history(db, other, close=200)

    outcome = repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    # Merging two price histories is not a loader's decision.
    assert outcome.status == repo.CHANGE_CONFLICT
    assert db.fetchval("SELECT count(*) FROM core.security") == 2
    assert (
        db.fetchval(
            "SELECT primary_symbol FROM core.security WHERE security_id = %s", (sid,)
        )
        == "FB"
    )
    assert repo.resolve_security_id(db, "META") == other


def test_fold_refuses_a_security_that_owns_anything(db):
    survivor = _mk_security(db, "FB")
    victim = _mk_security(db, "META")
    _give_history(db, victim)

    assert repo.fold_empty_security(db, victim_id=victim, survivor_id=survivor) is False
    assert db.fetchval("SELECT count(*) FROM core.security") == 2


# ---------------------------------------------------------------------------
# Ticker reuse is not a rename
# ---------------------------------------------------------------------------


def test_rename_never_resurrects_a_delisted_issuer(db):
    # Circuit City (CC, delisted 2009) is not renamed into anything -- 0009 mints a
    # new id for the ticker's next owner, and this step must not interfere.
    dead = _mk_security(db, "CC", exchange="NYSE", name="Circuit City")
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2009, 3, 8))

    outcome = repo.apply_symbol_change(
        db, old_symbol="CC", new_symbol="CCX", change_date=CHANGE_DATE
    )

    assert outcome.status == repo.CHANGE_IGNORED
    row = db.fetchone(
        "SELECT primary_symbol, delisted_date, is_actively_trading "
        "FROM core.security WHERE security_id = %s",
        (dead,),
    )
    assert row["primary_symbol"] == "CC"
    assert row["delisted_date"] == dt.date(2009, 3, 8)
    assert row["is_actively_trading"] is False


def test_rename_onto_a_ticker_a_dead_issuer_used_is_allowed(db):
    # The new ticker is only "taken" by a delisted row, which 0009's partial unique
    # index does not cover -- so the rename must go through.
    dead = _mk_security(db, "CCX", exchange="NYSE")
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2009, 3, 8))
    live = _mk_security(db, "AAA", exchange="NYSE")

    outcome = repo.apply_symbol_change(
        db, old_symbol="AAA", new_symbol="CCX", change_date=CHANGE_DATE
    )

    assert outcome.status == repo.CHANGE_APPLIED
    assert repo.resolve_security_id(db, "CCX") == live  # the live one, not the dead one
    assert db.fetchval(
        "SELECT delisted_date FROM core.security WHERE security_id = %s", (dead,)
    ) == dt.date(2009, 3, 8)


def test_unknown_ticker_is_left_for_a_later_sweep(db):
    outcome = repo.apply_symbol_change(
        db, old_symbol="NOPE", new_symbol="NAH", change_date=CHANGE_DATE
    )
    assert outcome.status == repo.CHANGE_UNKNOWN
    assert db.fetchval("SELECT count(*) FROM core.security") == 0


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


def test_recorded_status_is_never_downgraded_from_applied(db):
    sid = _mk_security(db, "FB")
    repo.record_symbol_change(
        db,
        old_symbol="FB",
        new_symbol="META",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_APPLIED,
        security_id=sid,
    )
    # A later sweep re-reads the same feed row; the ticker is legitimately taken
    # now (by us), and that must not rewrite history as a conflict.
    repo.record_symbol_change(
        db,
        old_symbol="FB",
        new_symbol="META",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_CONFLICT,
    )

    assert (
        repo.symbol_change_status(
            db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
        )
        == repo.CHANGE_APPLIED
    )
    assert db.fetchval("SELECT count(*) FROM core.symbol_change") == 1


def test_conflicts_stay_in_the_review_queue_until_they_are_applied(db):
    repo.record_symbol_change(
        db,
        old_symbol="AAA",
        new_symbol="BBB",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_CONFLICT,
        detail={"reason": "taken"},
    )
    assert [r["new_symbol"] for r in repo.unapplied_symbol_changes(db)] == ["BBB"]

    sid = _mk_security(db, "AAA")
    repo.record_symbol_change(
        db,
        old_symbol="AAA",
        new_symbol="BBB",
        change_date=CHANGE_DATE,
        status=repo.CHANGE_APPLIED,
        security_id=sid,
    )
    assert repo.unapplied_symbol_changes(db) == []


# ---------------------------------------------------------------------------
# New listings enter scope
# ---------------------------------------------------------------------------


class _ScreenerFMP:
    """Screener stub serving one venue's rows."""

    bytes_downloaded = 0
    request_count = 0

    def __init__(self, rows):
        self.rows = rows

    def company_screener(self, *, exchange=None, **_kw):
        return self.rows if exchange == "NASDAQ" else []


def _load(db, symbols):
    rows = [
        {"symbol": s, "exchangeShortName": "NASDAQ", "name": f"{s} Inc", "isEtf": False}
        for s in symbols
    ]
    return security_master.load_securities(db, _ScreenerFMP(rows))


def test_security_master_reports_which_listings_are_new(db):
    first = _load(db, ["AAA", "BBB"])
    assert (first.total, sorted(first.new_symbols)) == (2, ["AAA", "BBB"])

    # A re-run of the same universe is a refresh, not two more listings.
    second = _load(db, ["AAA", "BBB"])
    assert (second.total, second.new_symbols) == (2, [])

    # An IPO shows up in the screener the day it lists; this is the whole point of
    # running the security master nightly.
    third = _load(db, ["AAA", "BBB", "IPOX"])
    assert third.new_symbols == ["IPOX"]
    assert repo.resolve_security_id(db, "IPOX") is not None
    assert repo.count_recent_listings(db, days=7) == 3


def test_new_listing_has_no_watermark_so_its_first_pull_is_a_backfill(db):
    _load(db, ["IPOX"])
    sid = repo.resolve_security_id(db, "IPOX")
    # No watermark means load_symbol_prices leaves start_date None, i.e. asks for
    # the symbol's whole available history on the first nightly run after listing.
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) is None


def test_listed_securities_ignores_delisted_rows(db):
    sid = _mk_security(db, "AAA")
    # The key is the symbol alone (0012); the venue is an attribute, so that a
    # company changing exchange is a refresh rather than a new listing.
    assert "AAA" in repo.listed_securities(db)

    repo.mark_delisted(db, security_id=sid, delisted_date=dt.date(2024, 1, 2))
    # A delisted row is invisible to the partial index, so the same ticker
    # listing again is genuinely new -- and must be reported as such.
    assert "AAA" not in repo.listed_securities(db)
    assert _load(db, ["AAA"]).new_symbols == ["AAA"]
    assert db.fetchval("SELECT count(*) FROM core.security") == 2


# ---------------------------------------------------------------------------
# Resolving a ticker after it has moved
# ---------------------------------------------------------------------------


def test_a_reused_ticker_resolves_to_its_live_owner_not_its_former_user(db):
    # AAA renames to BBB, then a different company lists as AAA. Both have a claim
    # on the string "AAA"; only one of them is trading under it.
    renamed = _mk_security(db, "AAA", name="Old Co")
    _give_history(db, renamed)
    repo.apply_symbol_change(
        db, old_symbol="AAA", new_symbol="BBB", change_date=CHANGE_DATE
    )
    newcomer = _mk_security(db, "AAA", name="New Co")

    assert repo.resolve_security_id(db, "AAA") == newcomer
    assert repo.resolve_security_id(db, "BBB") == renamed


def test_duk_and_the_loader_resolve_a_renamed_ticker_identically(db):
    # The read path duplicates these queries rather than importing them, so the
    # two copies have to be checked against each other, not just asserted about.
    from duk.datasource.db import _resolve_security_id

    sid = _mk_security(db, "FB")
    _give_history(db, sid)
    repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    from psycopg.rows import dict_row

    with db.conn.cursor(row_factory=dict_row) as cur:
        for symbol in ("FB", "META", "NOPE"):
            assert _resolve_security_id(cur, symbol) == repo.resolve_security_id(
                db, symbol
            ), symbol
        assert _resolve_security_id(cur, "FB") == sid


# ---------------------------------------------------------------------------
# The nightly sweep, end to end
# ---------------------------------------------------------------------------


class _RenameFMP:
    """Rename-feed stub (no network); rows arrive newest first, as FMP sends them."""

    bytes_downloaded = 0

    def __init__(self, rows):
        self.rows = rows

    def symbol_changes(self, *, max_pages=5):
        return self.rows


def test_nightly_sweep_applies_the_rename_and_leaves_a_trail(db):
    from fafnir.ingest.symbol_changes import load_symbol_changes

    sid = _mk_security(db, "FB", name="Facebook, Inc.")
    _give_history(db, sid)
    fmp = _RenameFMP(
        [
            # A rename fafnir does not track (the feed is global) and the one it does.
            {"date": "2024-06-10", "oldSymbol": "2958.HK", "newSymbol": "2959.HK"},
            {
                "date": "2024-06-10",
                "oldSymbol": "FB",
                "newSymbol": "META",
                "companyName": "Meta Platforms, Inc.",
            },
        ]
    )

    counts = load_symbol_changes(db, fmp)

    assert (counts["applied"], counts["unknown"]) == (1, 1)
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert repo.resolve_security_id(db, "META") == sid
    # Lineage: a run row, the raw feed, and the audit row -- only for the rename
    # that was ours.
    assert (
        db.fetchval(
            "SELECT status FROM ops.ingestion_run WHERE endpoint = 'symbol-change'"
        )
        == "success"
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM landing.fmp_raw WHERE endpoint = 'symbol-change'"
        )
        == 1
    )
    assert db.fetchall(
        "SELECT old_symbol, new_symbol, status FROM core.symbol_change"
    ) == [{"old_symbol": "FB", "new_symbol": "META", "status": "applied"}]

    # A second sweep over the same feed is a no-op, not a second rename.
    again = load_symbol_changes(db, fmp)
    assert (again["skipped"], again["applied"]) == (1, 0)
    assert db.fetchval("SELECT count(*) FROM core.symbol_change") == 1


def test_nightly_sweep_flags_a_conflict_for_review(db):
    from fafnir.ingest.symbol_changes import load_symbol_changes

    sid = _mk_security(db, "FB")
    _give_history(db, sid)
    other = _mk_security(db, "META")
    _give_history(db, other, close=200)

    counts = load_symbol_changes(
        db, _RenameFMP([{"date": "2024-06-10", "oldSymbol": "FB", "newSymbol": "META"}])
    )

    assert counts["conflict"] == 1
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name = 'symbol_change_conflict' AND resolved_at IS NULL"
        )
        == 1
    )
    assert [r["new_symbol"] for r in repo.unapplied_symbol_changes(db)] == ["META"]
    # Nothing moved: both securities keep their tickers and their history.
    assert db.fetchval("SELECT count(*) FROM core.security") == 2


# ---------------------------------------------------------------------------
# The rename fallback must not reach the write paths
# ---------------------------------------------------------------------------


def test_a_retired_ticker_cannot_delist_the_security_it_was_renamed_away_from(db):
    """Regression: the delisted feed reports retired tickers, and FB is retired by
    the rename. Resolving it through the read path's historical fallback would
    stamp a one-way delisting on the live META security and drop it out of the
    active price universe for good."""
    from fafnir.ingest.delisted import load_delisted

    sid = _mk_security(db, "FB", name="Facebook, Inc.")
    _give_history(db, sid)
    repo.apply_symbol_change(
        db, old_symbol="FB", new_symbol="META", change_date=CHANGE_DATE
    )

    class _DelistedFMP:
        bytes_downloaded = 0

        def delisted_companies(self, *, max_pages=5):
            return [
                {
                    "symbol": "FB",
                    "exchange": "NASDAQ",
                    "delistedDate": CHANGE_DATE.isoformat(),
                }
            ]

    marked, _seen = load_delisted(db, _DelistedFMP())

    assert marked == 0
    row = db.fetchone(
        "SELECT primary_symbol, is_actively_trading, delisted_date "
        "FROM core.security WHERE security_id = %s",
        (sid,),
    )
    assert row["is_actively_trading"] is True
    assert row["delisted_date"] is None
    # The read path still reaches the company by its former ticker; only the
    # write path is narrowed.
    assert repo.resolve_security_id(db, "FB") == sid
    assert repo.active_security_for_symbol(db, "FB") is None


def test_delisting_still_marks_a_security_trading_under_its_own_ticker(db):
    from fafnir.ingest.delisted import load_delisted

    sid = _mk_security(db, "DEAD", exchange="NYSE")

    class _DelistedFMP:
        bytes_downloaded = 0

        def delisted_companies(self, *, max_pages=5):
            return [
                {"symbol": "DEAD", "exchange": "NYSE", "delistedDate": "2024-06-10"}
            ]

    marked, seen = load_delisted(db, _DelistedFMP())

    assert (marked, seen) == (1, 1)
    assert db.fetchval(
        "SELECT delisted_date FROM core.security WHERE security_id = %s", (sid,)
    ) == dt.date(2024, 6, 10)


# ---------------------------------------------------------------------------
# The review queue has to be able to empty
# ---------------------------------------------------------------------------


def test_a_conflict_resolved_by_retiring_the_stale_row_leaves_the_queue(db):
    """A non-terminal audit row can only be closed by a later sweep reaching a
    terminal outcome, so every way an operator can resolve a conflict has to end
    in one -- otherwise `fafnir status` shows it forever with nothing able to
    clear it."""
    from fafnir.ingest.symbol_changes import load_symbol_changes

    fb = _mk_security(db, "FB")
    _give_history(db, fb)
    _give_history(db, _mk_security(db, "META"), close=200)
    feed = _RenameFMP([{"date": "2024-06-10", "oldSymbol": "FB", "newSymbol": "META"}])

    assert load_symbol_changes(db, feed)["conflict"] == 1
    assert repo.count_unapplied_symbol_changes(db) == 1

    # The operator decides the duplicate is the real Meta and retires the stale row.
    repo.mark_delisted(db, security_id=fb, delisted_date=dt.date(2024, 6, 10))

    # FB now belongs to a delisted issuer, which is terminal: nothing left to apply.
    assert load_symbol_changes(db, feed)["ignored"] == 1
    assert repo.count_unapplied_symbol_changes(db) == 0


def test_a_conflict_resolved_by_merging_leaves_the_queue(db):
    from fafnir.ingest.symbol_changes import load_symbol_changes

    fb = _mk_security(db, "FB")
    meta = _mk_security(db, "META")
    _give_history(db, meta, close=200)
    feed = _RenameFMP([{"date": "2024-06-10", "oldSymbol": "FB", "newSymbol": "META"}])

    assert load_symbol_changes(db, feed)["conflict"] == 1

    # The operator merges the other way: the FB row was the empty one.
    assert repo.fold_empty_security(db, victim_id=fb, survivor_id=meta) is True

    # The end state the rename asked for now holds, however it got there -- and
    # saying so is what releases the audit row.
    counts = load_symbol_changes(db, feed)
    assert counts["applied"] == 1
    assert repo.count_unapplied_symbol_changes(db) == 0
    assert repo.unapplied_symbol_changes(db) == []


def test_an_unresolved_conflict_is_flagged_once_not_once_a_night(db):
    from fafnir.ingest.symbol_changes import load_symbol_changes

    _give_history(db, _mk_security(db, "FB"))
    _give_history(db, _mk_security(db, "META"), close=200)
    feed = _RenameFMP([{"date": "2024-06-10", "oldSymbol": "FB", "newSymbol": "META"}])

    for _ in range(3):  # three nightly sweeps, one unresolved problem
        load_symbol_changes(db, feed)

    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name = 'symbol_change_conflict'"
        )
        == 1
    )
    # Still in the durable queue, though -- the flag is the notification, the
    # audit row is the state.
    assert repo.count_unapplied_symbol_changes(db) == 1


def test_the_queue_count_is_not_capped_by_the_display_page(db):
    for i in range(55):
        repo.record_symbol_change(
            db,
            old_symbol=f"OLD{i}",
            new_symbol=f"NEW{i}",
            change_date=CHANGE_DATE,
            status=repo.CHANGE_CONFLICT,
        )

    assert repo.count_unapplied_symbol_changes(db) == 55
    assert len(repo.unapplied_symbol_changes(db)) == 50  # a page, by design


def test_a_delisting_closes_a_renamed_tickers_period_even_if_backdated(db):
    """Regression: `mark_delisted` used to skip periods starting after the
    delisting date. That was unreachable while every period began at 1900-01-01,
    but a rename starts one on the change date -- so a delisting backdated before
    the rename left the ticker open on a dead issuer, and the next company to list
    under it lost the resolution race (open periods order by valid_from DESC)."""
    dead = _mk_security(db, "ABC", name="Old Co")
    repo.apply_symbol_change(
        db, old_symbol="ABC", new_symbol="XYZ", change_date=CHANGE_DATE
    )
    # The delisted feed reports it with a date before the rename.
    repo.mark_delisted(
        db, security_id=dead, delisted_date=CHANGE_DATE - dt.timedelta(days=40)
    )

    assert (
        db.fetchval(
            "SELECT count(*) FROM core.symbol_xref WHERE security_id = %s AND valid_to IS NULL",
            (dead,),
        )
        == 0
    )

    # A different company lists under the freed ticker.
    newcomer = _mk_security(db, "XYZ", name="New Co")
    _give_history(db, newcomer)

    assert repo.resolve_security_id(db, "XYZ") == newcomer
    assert repo.active_security_for_symbol(db, "XYZ") == newcomer


# ---------------------------------------------------------------------------
# A venue transfer is not a new company
# ---------------------------------------------------------------------------


class _VenueFMP:
    """Screener stub that lists one symbol on whichever venue it is given."""

    bytes_downloaded = 0
    request_count = 0

    def __init__(self, venue, symbol="ABC"):
        self.venue = venue
        self.symbol = symbol

    def company_screener(self, *, exchange=None, **_kw):
        if exchange != self.venue:
            return []
        return [
            {
                "symbol": self.symbol,
                "exchangeShortName": self.venue,
                "name": "Acme Corp",
                "isEtf": False,
            }
        ]


def test_a_venue_transfer_keeps_the_security_its_history_and_its_watermark(db):
    """Regression: the exchange used to be part of the identity key, so a company
    moving NYSE -> NASDAQ failed to match and inserted a *second* listed row. That
    row then captured the ticker's xref period, leaving the company's entire price
    history unreachable by symbol -- `duk ph ABC` returned nothing — and, having no
    watermark, caused the next price run to re-download all of it."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    security_master.load_securities(db, _VenueFMP("NYSE"))
    sid = repo.resolve_security_id(db, "ABC")
    _give_history(db, sid)
    repo.set_watermark(db, "fmp", PRICE_ENDPOINT, dt.date(2024, 6, 3), sid)

    result = security_master.load_securities(db, _VenueFMP("NASDAQ"))

    # One company, one security_id, now listed on the new venue.
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert (
        db.fetchval(
            "SELECT exchange_code FROM core.security WHERE security_id = %s", (sid,)
        )
        == "NASDAQ"
    )
    # A transfer is a refresh, not an arrival.
    assert result.new_symbols == []
    # The history and the watermark stay reachable by ticker -- this is the part
    # that was broken.
    assert repo.resolve_security_id(db, "ABC") == sid
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s", (sid,)
        )
        == 1
    )
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2024, 6, 3)


def test_the_duk_read_path_still_reaches_a_transferred_security(db):
    from psycopg.rows import dict_row

    from duk.datasource.db import _resolve_security_id

    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    security_master.load_securities(db, _VenueFMP("NYSE"))
    sid = repo.resolve_security_id(db, "ABC")
    _give_history(db, sid)
    security_master.load_securities(db, _VenueFMP("NASDAQ"))

    with db.conn.cursor(row_factory=dict_row) as cur:
        assert _resolve_security_id(cur, "ABC") == sid


def test_ticker_reuse_after_a_delisting_still_mints_a_new_security(db):
    """The venue drop must not weaken 0009: the key is still scoped to LISTED rows,
    so a dead issuer's ticker cannot be overwritten by its next owner."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    security_master.load_securities(db, _VenueFMP("NYSE"))
    dead = repo.resolve_security_id(db, "ABC")
    _give_history(db, dead)
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2024, 6, 10))

    security_master.load_securities(db, _VenueFMP("NASDAQ"))

    newcomer = repo.resolve_security_id(db, "ABC")
    assert newcomer != dead
    assert db.fetchval("SELECT count(*) FROM core.security") == 2
    # The dead issuer keeps its history and stays dead.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s", (dead,)
        )
        == 1
    )
    assert db.fetchval(
        "SELECT delisted_date FROM core.security WHERE security_id = %s", (dead,)
    ) == dt.date(2024, 6, 10)


# ---------------------------------------------------------------------------
# The safety net under 0012's identity key
# ---------------------------------------------------------------------------


class _NamedFMP:
    """Screener stub that can change a symbol's company name between runs."""

    bytes_downloaded = 0
    request_count = 0

    def __init__(self, name, symbol="ABC", venue="NASDAQ"):
        self.name = name
        self.symbol = symbol
        self.venue = venue

    def company_screener(self, *, exchange=None, **_kw):
        if exchange != self.venue:
            return []
        return [
            {
                "symbol": self.symbol,
                "exchangeShortName": self.venue,
                "name": self.name,
                "isEtf": False,
            }
        ]


def _drift_flags(db):
    return db.fetchall(
        "SELECT security_id, record_key, detail, severity FROM ops.data_quality_flag "
        "WHERE check_name = 'security_company_name_drift'"
    )


def test_an_unrelated_company_name_on_a_held_ticker_is_flagged(db):
    """0012 keys a listed security on (source, symbol), which assumes one issuer
    per ticker. If that were ever violated the second company would silently
    UPDATE the first rather than insert. The tell is the name changing into
    something unrelated while the ticker stays put."""
    security_master.load_securities(db, _NamedFMP("Acme Corporation"))
    sid = repo.resolve_security_id(db, "ABC")
    _give_history(db, sid)

    security_master.load_securities(db, _NamedFMP("Zebra Industries Inc"))

    flags = _drift_flags(db)
    assert len(flags) == 1
    assert flags[0]["security_id"] == sid
    assert flags[0]["severity"] == "warn"
    assert flags[0]["record_key"] == {"symbol": "ABC"}
    assert flags[0]["detail"]["stored_name"] == "Acme Corporation"
    assert flags[0]["detail"]["incoming_name"] == "Zebra Industries Inc"
    # Advisory only: the row is still stored and the run is not marked partial.
    assert (
        db.fetchval(
            "SELECT company_name FROM core.security WHERE security_id = %s", (sid,)
        )
        == "Zebra Industries Inc"
    )
    assert (
        db.fetchval(
            "SELECT status FROM ops.ingestion_run ORDER BY ingestion_run_id DESC LIMIT 1"
        )
        == "success"
    )


def test_restyling_the_same_company_name_is_not_flagged(db):
    # The feed churns corporate-form boilerplate between revisions; that must not
    # produce a flag a human then has to dismiss every time.
    security_master.load_securities(db, _NamedFMP("Apple Inc."))
    for restyled in ("Apple Inc", "Apple, Inc.", "Apple Incorporated"):
        security_master.load_securities(db, _NamedFMP(restyled))

    assert _drift_flags(db) == []


def test_a_new_listing_is_never_flagged_for_drift(db):
    # There is no stored name to drift from on the run that mints the security.
    result = security_master.load_securities(db, _NamedFMP("Acme Corporation"))

    assert result.new_symbols == ["ABC"]
    assert _drift_flags(db) == []


def test_the_drift_flag_does_not_repeat_every_night(db):
    # add_dq_flag is an unguarded insert, so a check that kept firing would add an
    # unresolved flag per night for one problem. The upsert stores the new name, so
    # the next run compares like with like.
    security_master.load_securities(db, _NamedFMP("Acme Corporation"))
    for _ in range(3):
        security_master.load_securities(db, _NamedFMP("Zebra Industries Inc"))

    assert len(_drift_flags(db)) == 1


def test_a_rename_does_not_trip_the_drift_check(db):
    # The rename sweep moves company_name across with the ticker, so the next
    # security-master run sees the name it just stored.
    security_master.load_securities(db, _NamedFMP("Facebook, Inc.", symbol="FB"))
    sid = repo.resolve_security_id(db, "FB")
    repo.apply_symbol_change(
        db,
        old_symbol="FB",
        new_symbol="META",
        change_date=CHANGE_DATE,
        company_name="Meta Platforms, Inc.",
    )

    security_master.load_securities(
        db, _NamedFMP("Meta Platforms, Inc.", symbol="META")
    )

    assert repo.resolve_security_id(db, "META") == sid
    assert _drift_flags(db) == []
