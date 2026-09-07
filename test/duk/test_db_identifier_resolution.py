"""Identifier resolution against a real warehouse (needs FAFNIR_TEST_DSN).

The unit tests cover the grammar and which statement runs when; what only a
database can answer is whether the normalising predicates actually match stored
values, and whether migration 0023's expression indexes are the ones those
predicates use. Both are silent failures otherwise: a normalisation mismatch
reports a security missing, and an index mismatch just gets slower.
"""

from __future__ import annotations

import os

import pytest

from duk import identifiers as ids
from duk.datasource import db as ds_db
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


def _company(db, symbol, name, **identifiers_):
    repo.ensure_exchange(db, "NYSE", "New York Stock Exchange", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code="NYSE",
        **identifiers_,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _resolve(query):
    return [c["security_id"] for c in ds_db.resolve_company(dsn=DSN, query=query)]


def test_cik_matches_however_it_is_padded(db):
    sid = _company(db, "IBM", "IBM Corp", cik="0000051143")
    # The SEC's zero-padded form, the bare form, and the padded form typed back.
    assert _resolve("cik:51143") == [sid]
    assert _resolve("cik:0000051143") == [sid]
    assert _resolve("cik:0051143") == [sid]
    assert _resolve("cik:51144") == []


def test_isin_and_cusip_match_case_and_grouping_insensitively(db):
    sid = _company(db, "IBM", "IBM Corp", isin="US4592001014", cusip="459200101")
    assert _resolve("isin:us4592001014") == [sid]
    assert _resolve("isin: US4592 0010 14") == [sid]
    assert _resolve("cusip:459200101") == [sid]
    assert _resolve("cusip:459200-10-1") == [sid]
    # Eight characters is the same issue without its check digit.
    assert _resolve("cusip:45920010") == [sid]


def test_a_cusip_still_resolves_when_only_the_isin_was_loaded(db):
    # FMP fills these columns independently; a security with an ISIN and no CUSIP
    # is common, and the CUSIP is literally inside the ISIN.
    sid = _company(db, "ONLYISIN", "Isin Only Inc", isin="US4592001014")
    assert _resolve("cusip:459200101") == [sid]
    assert _resolve("cusip:45920010") == [sid]


def test_an_isin_still_resolves_when_only_the_cusip_was_loaded(db):
    sid = _company(db, "ONLYCUS", "Cusip Only Inc", cusip="459200101")
    assert _resolve("isin:US4592001014") == [sid]


def test_a_foreign_isin_does_not_fall_back_to_the_cusip_column(db):
    # Only US/CA ISINs embed a CUSIP; treating a German NSIN as one would be a
    # coincidence match on nine characters that mean something else.
    _company(db, "SAPG", "SAP SE", cusip="519000308")
    assert _resolve("isin:DE0005190003") == []


def test_one_cik_two_share_classes_returns_both(db):
    a = _company(db, "GOOG", "Alphabet Inc", cik="1652044")
    b = _company(db, "GOOGL", "Alphabet Inc", cik="0001652044")
    assert sorted(_resolve("cik:1652044")) == sorted([a, b])


def test_an_identifier_never_falls_through_to_the_name_search(db):
    # A company literally named after the query string must not be reachable by an
    # identifier that matches nothing: `cik:` is an assertion about the key space.
    _company(db, "ODD", "cik:51143 Holdings")
    assert _resolve("cik:51143") == []


def test_delisted_and_missing_identifiers_do_not_match_an_empty_query(db):
    _company(db, "NOIDS", "No Identifiers Inc")
    assert _resolve("cusip:00000000") == []
    assert _resolve("isin:US0000000000") == []


def test_price_history_by_security_id_skips_ticker_resolution(db):
    """The reused-ticker case the identifier path exists to survive.

    Two securities, the delisted one holding the CUSIP and the live one holding
    the ticker. Resolving the found security's `primary_symbol` back through the
    ladder returns the LIVE owner -- so the summary and `ph` must read prices by
    security_id, not by round-tripping the ticker.
    """
    import datetime as dt

    old = _company(db, "TWTR", "Old Twitter Inc", cusip="90184L102")
    db.execute(
        "UPDATE core.security SET delisted_date = '2022-10-27', "
        "is_actively_trading = false WHERE security_id = %s",
        (old,),
    )
    db.execute(
        "UPDATE core.symbol_xref SET valid_to = '2022-10-27' WHERE security_id = %s",
        (old,),
    )
    new = _company(db, "TWTR", "Reused Ticker Corp")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": old,
                "trade_date": dt.date(2022, 10, 26),
                "open": 53,
                "high": 54,
                "low": 52,
                "close": 53.7,
                "volume": 100,
            }
        ],
    )
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": new,
                "trade_date": dt.date(2024, 1, 2),
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 200,
            }
        ],
    )

    assert _resolve("cusip:90184L102") == [old]
    by_id = ds_db.price_history(
        dsn=DSN, security_id=old, start_date=None, end_date=None
    )
    assert list(by_id["close"]) == [53.7]
    # The round trip everyone would write by accident lands on the other company.
    by_ticker = ds_db.price_history(
        dsn=DSN, symbol="TWTR", start_date=None, end_date=None
    )
    assert list(by_ticker["close"]) == [10.5]


@pytest.mark.parametrize(
    "identifier,column",
    [
        ("cik:51143", "ix_security_cik_normalised"),
        ("isin:US4592001014", "ix_security_isin_normalised"),
        ("cusip:459200101", "ix_security_cusip_normalised"),
    ],
)
def test_the_expression_indexes_exist_and_serve_their_predicate(db, identifier, column):
    """Migration 0023's indexes must be the ones the resolution SQL can use.

    Asserted by planning the real statement rather than by reading pg_indexes: an
    index whose expression is spelled differently from the query still EXISTS, and
    is still never used.
    """
    ident = ids.parse(identifier)
    sql = {
        ids.CIK: ds_db._CIK_RESOLVE_SQL,
        ids.ISIN: ds_db._ISIN_RESOLVE_SQL,
        ids.CUSIP: ds_db._CUSIP_RESOLVE_SQL,
    }[ident.scheme]

    # A sequential scan is cheaper than an index on a tiny table, so the planner
    # must be told to prefer the index before its availability can be observed.
    db.execute("SET enable_seqscan = off")
    plan = "\n".join(str(row) for row in db.fetchall("EXPLAIN " + sql, (ident.value,)))
    assert column in plan, plan
