"""
Vendor identity (migration 0031, plan SA-0306, #55; needs FAFNIR_TEST_DSN).

Three tables hold a vendor's own view of a security, beside core.security and never
in it (ADR 0015): its master (core.vendor_security), the dated link from fafnir's
security_id to its key (core.security_vendor_xref), and its lifecycle events
(core.vendor_security_event). The link also carries feed assignment: a security is
fed by a linked vendor only through an open is_feed link, and by one at a time.

Every key and value here is synthetic. No vendor row is committed (ADR 0012 §8).
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from fafnir.db import migrate as m
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration


def _sec(db, symbol, **kw):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        **kw,
    )


def _link(db, sid, key, *, vendor="sharadar", valid_from="2020-01-02", **kw):
    row = {
        "security_id": sid,
        "vendor": vendor,
        "vendor_key": key,
        "valid_from": valid_from,
        "match_method": "manual",
        "created_by": "tester",
        **kw,
    }
    cols = ", ".join(row)
    db.execute(
        f"INSERT INTO core.security_vendor_xref ({cols}) "
        f"VALUES ({', '.join(['%s'] * len(row))})",
        tuple(row.values()),
    )


def _links(db):
    return db.fetchall(
        "SELECT security_id, vendor_key, valid_to, is_feed "
        "FROM core.security_vendor_xref ORDER BY vendor_key, valid_from"
    )


# ---------------------------------------------------------------------------
# What the tables refuse
# ---------------------------------------------------------------------------


def test_one_open_link_per_key_and_per_security_and_vendor(db):
    a, b = _sec(db, "ZZA"), _sec(db, "ZZB")
    _link(db, a, "900001")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _link(db, b, "900001", valid_from="2021-01-04")  # one key, two securities
    with pytest.raises(psycopg.errors.UniqueViolation):
        _link(db, a, "900002")  # one security, two Sharadar identities
    # Closed history is allowed beside an open link: a key that moved.
    _link(db, b, "900003", valid_from="2010-01-04", valid_to="2019-12-31")
    _link(db, a, "900003", valid_from="2020-01-02", vendor="tiingo")
    assert len(_links(db)) == 3


def test_one_feeding_vendor_and_only_on_an_open_link(db):
    a = _sec(db, "ZZA")
    _link(db, a, "900001", is_feed=True)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _link(db, a, "T1", vendor="tiingo", is_feed=True)
    with pytest.raises(psycopg.errors.CheckViolation):
        _link(db, _sec(db, "ZZB"), "900002", valid_to="2021-01-04", is_feed=True)


@pytest.mark.parametrize(
    "over",
    [
        {"vendor": "polygon"},
        {"match_method": "guess"},
        {"valid_to": "2019-01-01"},  # before valid_from
        {"confidence": 1.5},
        {"created_by": "  "},
    ],
)
def test_a_link_must_say_what_it_means(db, over):
    with pytest.raises(psycopg.errors.CheckViolation):
        _link(db, _sec(db, "ZZA"), "900001", **over)


def test_the_vendor_master_and_events(db):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "cusips, related_tickers, first_price_date, last_price_date) VALUES "
        "('sharadar', 'stocks', '900001', 'ZZOLD1', ARRAY['00000A101'], "
        "ARRAY['ZZOLD'], '1998-01-02', '2009-10-28'), "
        "('sharadar', 'fundamentals', '900001', 'ZZOLD1', NULL, NULL, NULL, NULL)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
            "first_price_date, last_price_date) VALUES "
            "('sharadar', 'funds', '900002', 'ZZF', '2010-01-04', '2009-01-02')"
        )

    event = (
        "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
        "action, contra_ticker) VALUES ('sharadar', '900001', '2009-10-28', %s, %s)"
    )
    db.execute(event, ("delisted", None))
    db.execute(event, ("mergerto", "ZZNEW"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(event, ("delisted", None))  # the grain holds without a contra party


# ---------------------------------------------------------------------------
# Feed assignment
# ---------------------------------------------------------------------------


def test_is_fed_by_reads_the_open_feed_link(db):
    a = _sec(db, "ZZA")
    assert repo.is_fed_by(db, a, "fmp")  # FMP minted it
    assert not repo.is_fed_by(db, a, "sharadar")
    _link(db, a, "900001")
    assert not repo.is_fed_by(db, a, "sharadar")  # linked, but not its feed
    db.execute("UPDATE core.security_vendor_xref SET is_feed = true")
    assert repo.is_fed_by(db, a, "sharadar")
    assert not repo.is_fed_by(db, 999_999, "sharadar")


# ---------------------------------------------------------------------------
# A security that goes away takes no link with it
# ---------------------------------------------------------------------------


def _bars(db, sid, start, n, close=10.0):
    rows, day = [], start
    while len(rows) < n:
        if day.weekday() < 5:
            rows.append(
                {
                    "security_id": sid,
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100,
                }
            )
        day += dt.timedelta(days=1)
    repo.upsert_daily_prices(db, rows)


def test_a_merge_moves_the_links_to_the_survivor(db):
    survivor, victim = _sec(db, "ZZA"), _sec(db, "ZZB")
    _bars(db, survivor, dt.date(2024, 1, 2), 5)
    _bars(db, victim, dt.date(2024, 1, 2), 5)
    _link(db, victim, "900001", is_feed=True)
    _link(db, victim, "900009", valid_from="2001-01-02", valid_to="2009-12-31")
    repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)
    assert {r["security_id"] for r in _links(db)} == {survivor}
    assert repo.is_fed_by(db, survivor, "sharadar")


def test_a_merge_of_two_vendor_identities_is_refused_even_with_force(db):
    survivor, victim = _sec(db, "ZZA"), _sec(db, "ZZB")
    _bars(db, survivor, dt.date(2024, 1, 2), 5)
    _bars(db, victim, dt.date(2024, 1, 2), 5)
    _link(db, survivor, "900001")
    _link(db, victim, "900002")
    with pytest.raises(repo.MergeRefused, match="both hold an open sharadar link"):
        repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)
    assert db.fetchval("SELECT count(*) FROM core.security") == 2


def test_a_merge_of_two_fed_securities_is_refused(db):
    survivor, victim = _sec(db, "ZZA"), _sec(db, "ZZB")
    _bars(db, survivor, dt.date(2024, 1, 2), 5)
    _bars(db, victim, dt.date(2024, 1, 2), 5)
    _link(db, survivor, "T1", vendor="tiingo", is_feed=True)
    _link(db, victim, "900002", is_feed=True)
    with pytest.raises(repo.MergeRefused, match="one security has one feed"):
        repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)


def test_a_stub_fold_moves_a_link_and_declines_a_collision(db):
    survivor, stub = _sec(db, "ZZA"), _sec(db, "ZZB")
    _link(db, stub, "900001")
    assert repo.fold_empty_security(db, victim_id=stub, survivor_id=survivor)
    assert [r["security_id"] for r in _links(db)] == [survivor]

    other = _sec(db, "ZZC")
    _link(db, other, "900002")
    # Two Sharadar identities: not a duplicate stub. Declined, nothing raised: the
    # fold runs inside the nightly rename sweep.
    assert not repo.fold_empty_security(db, victim_id=other, survivor_id=survivor)
    assert db.fetchval(
        "SELECT count(*) FROM core.security WHERE security_id = %s", (other,)
    )


# ---------------------------------------------------------------------------
# The upgrade path
# ---------------------------------------------------------------------------


def _fingerprint(db):
    return db.fetchone("""
        SELECT (SELECT count(*) FROM core.security) AS securities,
               (SELECT md5(string_agg(t::text, '|' ORDER BY security_id))
                  FROM core.security t) AS security_md5,
               (SELECT count(*) FROM core.daily_price) AS bars,
               (SELECT md5(string_agg(t::text, '|' ORDER BY security_id, trade_date))
                  FROM core.daily_price t) AS price_md5
        """)


def test_0031_leaves_existing_rows_byte_identical(db):
    a, b = _sec(db, "ZZA"), _sec(db, "ZZB", delisted_date=dt.date(2020, 1, 2))
    _bars(db, a, dt.date(2024, 1, 2), 30)
    _bars(db, b, dt.date(2019, 12, 2), 20)
    before = _fingerprint(db)
    try:
        assert m.rollback(db.dsn, steps=1) == ["0031"]
        assert db.fetchval("SELECT to_regclass('core.security_vendor_xref')") is None
        assert _fingerprint(db) == before
    finally:
        # Re-applied whatever happened: the rest of the suite needs 0031.
        applied = m.migrate(db.dsn)
    assert applied == ["0031"]
    assert _fingerprint(db) == before
