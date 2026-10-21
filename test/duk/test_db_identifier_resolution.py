"""Identifier resolution against a real warehouse (needs FAFNIR_TEST_DSN).

The unit tests cover the grammar and which statement runs when; what only a
database can answer is whether the normalising predicates actually match stored
values, and whether migration 0023's expression indexes are the ones those
predicates use. Both are silent failures otherwise: a normalisation mismatch
reports a security missing, and an index mismatch just gets slower.
"""

from __future__ import annotations

import datetime as dt
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


# A ticker two issuers traded under (#80), the way Circuit City and Chemours were
# both CC: one dead, minted from Sharadar with a dated period, and one live, from
# FMP, whose period starts at the "first seen" 1900 fallback. Synthetic.
DEAD_DAY, LIVE_DAY, NOBODY_DAY = (
    dt.date(2008, 6, 30),
    dt.date(2020, 6, 30),
    dt.date(2012, 1, 3),
)


def _reused_ticker(db):
    live = _company(db, "ZZCC", "ZZ Live Chemicals")
    dead = repo.upsert_security(
        db,
        primary_symbol="ZZCC",
        company_name="ZZ Dead Stores",
        asset_type="equity",
        exchange_code="NYSE",
        is_actively_trading=False,
        delisted_date=dt.date(2009, 3, 6),
        source="sharadar",
    )
    repo.insert_symbol_period(
        db,
        security_id=dead,
        symbol="ZZCC",
        valid_from=dt.date(1998, 1, 2),
        valid_to=dt.date(2009, 3, 6),
        source="sharadar",
    )

    def bar(sid, day, close):
        return {
            "security_id": sid,
            "trade_date": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 10,
        }

    repo.upsert_daily_prices(
        db, [bar(dead, DEAD_DAY, 7), bar(dead, dt.date(2009, 3, 6), 1)]
    )
    repo.upsert_daily_prices(
        db, [bar(live, dt.date(2015, 7, 1), 20), bar(live, LIVE_DAY, 50)]
    )
    return dead, live


def test_a_dated_ticker_names_who_traded_under_it_then(db):
    dead, live = _reused_ticker(db)
    expected = {DEAD_DAY: dead, LIVE_DAY: live, NOBODY_DAY: None, None: live}
    for day, sid in expected.items():
        with ds_db._connect(DSN) as conn, conn.cursor() as cur:
            via_mart = ds_db._resolve_security_id(cur, "ZZCC", as_of=day)
        assert via_mart == sid, day
        # The two copies of the ladder agree, the date rung included.
        assert repo.resolve_security_id(db, "ZZCC", as_of=day) == sid, day

    assert _resolve("ZZCC@2008-06-30") == [dead]
    assert _resolve("ZZCC@2012-01-03") == []
    assert _resolve("ZZCC") == [live]  # the bare ticker is unchanged
    periods = ds_db.ticker_periods(dsn=DSN, symbol="zzcc")
    assert [p["security_id"] for p in periods] == [live, dead]
    assert periods[1]["last_day"] == dt.date(2009, 3, 6)


def test_duk_ph_reads_the_dated_ticker(db, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from duk.cli import main

    _reused_ticker(db)
    monkeypatch.setenv("FAFNIR_DSN", DSN)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")

    def ph(argument):
        return CliRunner().invoke(
            main,
            ["-c", str(cfg), "-S", "db", "ph", argument, "--close"]
            + ["-s", "2008-01-01", "-e", "2020-12-31"],
        )

    dated = ph("ZZCC@2008-06-30")
    assert dated.exit_code == 0, dated.output
    assert "2008-06-30" in dated.output and "2020-06-30" not in dated.output
    bare = ph("ZZCC")
    assert bare.exit_code == 0, bare.output
    assert "2020-06-30" in bare.output
    # The bare ticker still answers, and says what else it has named.
    assert "ZZCC has named 2 securities" in bare.output
    assert "ZZCC@2009-03-06" in bare.output
