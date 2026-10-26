"""`duk fs` / `duk fa`'s reads against a warehouse (#107; needs FAFNIR_TEST_DSN).

What only a database can answer: that a statement is every period at its version in
force on the --as-of date (a later period absent, a later restatement unseen, a
vendor correction's superseded row never shown), that a secondary share class reads
its issuer's statements through the CIK and only when one class holds them, and that
the factor row is the panel's month-end row, or the latest snapshot's. Every row is
synthetic.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from duk.datasource import db as ds_db
from duk.datasource.base import DataSourceError
from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")
D = date
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
T1 = datetime(2026, 2, 5, 12, tzinfo=timezone.utc)
HIDDEN = {"security_id", "period_type", "known_to", "loaded_at", "superseded_at"}


def _security(db, symbol, *, cik=None):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
        source="sharadar",
        cik=cik,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _put(
    db,
    statement,
    sid,
    fiscal_date,
    label,
    known_from,
    *,
    version=1,
    known_to=None,
    loaded_at=T0,
    superseded_at=None,
    **items,
):
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900107",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version,
        "known_from": known_from,
        "known_to": known_to,
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": loaded_at,
        "superseded_at": superseded_at,
        "reported_currency": "USD",
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _history(db, sid):
    """FY2020 restated on 2022-03-01; FY2021 corrected by the vendor; one quarter."""
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        "2020-FY",
        D(2021, 2, 1),
        known_to=D(2022, 3, 1),
        revenue=1000,
    )
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        "2020-FY",
        D(2022, 3, 1),
        version=2,
        revenue=1100,
    )
    # The vendor's first record of FY2021, superseded on the load timeline.
    _put(
        db,
        "income",
        sid,
        D(2021, 12, 31),
        "2021-FY",
        D(2022, 2, 15),
        superseded_at=T1,
        revenue=1199,
    )
    _put(
        db,
        "income",
        sid,
        D(2021, 12, 31),
        "2021-FY",
        D(2022, 2, 15),
        loaded_at=T1,
        revenue=1200,
    )
    _put(db, "income", sid, D(2021, 3, 31), "2021-Q1", D(2021, 5, 1), revenue=250)
    _put(
        db,
        "balance_sheet",
        sid,
        D(2021, 12, 31),
        "2021-FY",
        D(2022, 2, 15),
        total_assets=5000,
    )


def _revenue(found):
    return [(r["fiscal_year"], r["version_no"], r["revenue"]) for r in found["rows"]]


def test_a_statement_is_every_period_as_known_on_a_date(db):
    sid = _security(db, "ZZF")
    _history(db, sid)

    early = ds_db.statements(dsn=DSN, symbol="zzf", as_of="2021-06-30")
    assert early["security_id"] == sid and early["as_of"] == D(2021, 6, 30)
    assert _revenue(early) == [(2020, 1, Decimal("1000.00"))]
    # FY2021 filed, FY2020 not yet restated: oldest first.
    assert _revenue(ds_db.statements(dsn=DSN, symbol="ZZF", as_of=D(2022, 2, 20))) == [
        (2020, 1, Decimal("1000.00")),
        (2021, 1, Decimal("1200.00")),
    ]
    # The restatement's day: version 2 is in force, version 1 is not.
    today = ds_db.statements(dsn=DSN, security_id=sid)
    assert _revenue(today) == [
        (2020, 2, Decimal("1100.00")),
        (2021, 1, Decimal("1200.00")),
    ]
    assert today["as_of"] == db.fetchval("SELECT current_date")
    assert _revenue(ds_db.statements(dsn=DSN, symbol="ZZF", as_of="2022-03-01")) == (
        _revenue(today)
    )

    row = today["rows"][0]
    assert not HIDDEN & set(row)
    assert (row["known_from"], row["knowledge_source"]) == (D(2022, 3, 1), "observed")
    assert row["net_income"] is None  # NULL stays NULL

    assert _revenue(ds_db.statements(dsn=DSN, symbol="ZZF", limit=1)) == [
        (2021, 1, Decimal("1200.00"))
    ]
    quarters = ds_db.statements(dsn=DSN, symbol="ZZF", period="quarter")
    assert [(r["fiscal_period"], r["revenue"]) for r in quarters["rows"]] == [
        ("Q1", Decimal("250.00"))
    ]
    balance = ds_db.statements(dsn=DSN, symbol="ZZF", statement="balance")
    assert [r["total_assets"] for r in balance["rows"]] == [Decimal("5000.00")]
    assert ds_db.statements(dsn=DSN, symbol="ZZF", statement="cash")["rows"] == []


def test_what_it_refuses_and_what_it_cannot_find(db):
    for bad in (
        {"statement": "equity"},
        {"period": "monthly"},
        {"limit": 0},
        {"as_of": "26/10/2026"},
    ):
        with pytest.raises(DataSourceError):
            ds_db.statements(dsn=DSN, symbol="ZZF", **bad)
    missing = ds_db.statements(dsn=DSN, symbol="NOPE")
    assert (missing["security_id"], missing["rows"]) == (None, [])
    assert ds_db.factor_row(dsn=DSN, symbol="NOPE")["security_id"] is None


def test_a_secondary_class_reads_its_issuers_statements(db):
    primary = _security(db, "ZZCA", cik="0001652044")
    secondary = _security(db, "ZZC", cik="1652044")
    _put(db, "income", primary, D(2021, 12, 31), "2021-FY", D(2022, 2, 1), revenue=7)

    found = ds_db.statements(dsn=DSN, symbol="ZZC")
    assert found["security_id"] == secondary
    assert found["statements_of"] == {
        "security_id": primary,
        "symbol": "ZZCA",
        "cik": "0001652044",
    }
    assert _revenue(found) == [(2021, 1, Decimal("7.00"))]
    # The class that holds them answers for itself.
    assert ds_db.statements(dsn=DSN, symbol="ZZCA")["statements_of"] is None

    # Two classes holding statements under one CIK: no guess.
    other = _security(db, "ZZCB", cik="1652044")
    _put(db, "income", other, D(2021, 12, 31), "2021-FY", D(2022, 2, 1), revenue=8)
    ambiguous = ds_db.statements(dsn=DSN, symbol="ZZC")
    assert (ambiguous["statements_of"], ambiguous["rows"]) == (None, [])

    # No CIK, no issuer.
    _security(db, "ZZN")
    assert ds_db.statements(dsn=DSN, symbol="ZZN")["statements_of"] is None


def _price(db, sid, day, close):
    c = Decimal(str(close))
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000,
            }
        ],
        source="sharadar",
    )


def _year(db, sid, year, filed, end=None):
    end = end or D(year, 12, 31)
    _put(
        db,
        "income",
        sid,
        end,
        f"{year}-FY",
        filed,
        revenue=1000,
        net_income=100,
        net_income_common=100,
        weighted_average_shares=100,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        end,
        f"{year}-FY",
        filed,
        total_assets=2000,
        total_equity_parent=1000,
        shares_outstanding=100,
    )


def test_the_factor_row_is_the_panels_month_end_or_the_latest(db):
    today = date.today()
    sid = _security(db, "ZZP", cik="0000900107")
    secondary = _security(db, "ZZPB", cik="900107")
    _year(db, sid, 2020, D(2021, 2, 1))
    _year(db, sid, today.year - 1, today - timedelta(days=100), today - timedelta(200))
    _price(db, sid, D(2021, 6, 30), 20)  # a Wednesday: June 2021's last weekday
    _price(db, sid, today - timedelta(days=1), 25)
    _price(db, secondary, today - timedelta(days=1), 24)
    # Someone in the panel traded at July 2021's month-end, as the market does.
    other = _security(db, "ZZQ")
    _year(db, other, 2020, D(2021, 2, 1))
    _price(db, other, D(2021, 7, 30), 5)
    maintenance.refresh_marts(
        db, views=["mart.fundamental_panel", "mart.security_fundamentals_latest"]
    )

    at = ds_db.factor_row(dsn=DSN, symbol="ZZP", as_of="2021-07-15")
    assert (at["source"], at["as_of_date"]) == (
        "mart.fundamental_panel",
        D(2021, 6, 30),
    )
    assert (at["row"]["security_id"], at["row"]["fiscal_year"]) == (sid, 2020)
    assert at["row"]["market_cap"] == 2000  # 20 x 100 shares
    # A month-end it did not trade in the week to: no row, and the date says which,
    # never the row of a month before.
    gap = ds_db.factor_row(dsn=DSN, symbol="ZZP", as_of="2021-08-15")
    assert (gap["as_of_date"], gap["row"]) == (D(2021, 7, 30), None)
    # Before the panel's first month-end.
    early = ds_db.factor_row(dsn=DSN, symbol="ZZP", as_of="1997-12-31")
    assert (early["as_of_date"], early["row"]) == (None, None)

    latest = ds_db.factor_row(dsn=DSN, symbol="ZZP")
    assert latest["source"] == "mart.security_fundamentals_latest"
    assert latest["as_of_date"] == db.fetchval(
        "SELECT max(as_of_date) FROM mart.security_fundamentals_latest"
    )
    assert (latest["row"]["symbol"], latest["row"]["fiscal_year"]) == (
        "ZZP",
        today.year - 1,
    )
    # The secondary class is answered with its issuer's row.
    other = ds_db.factor_row(dsn=DSN, symbol="ZZPB")
    assert other["statements_of"]["symbol"] == "ZZP"
    assert other["row"]["security_id"] == sid


def test_an_unrefreshed_panel_says_how_to_fill_it(db):
    _security(db, "ZZU")
    db.execute("REFRESH MATERIALIZED VIEW mart.fundamental_panel WITH NO DATA")
    try:
        with pytest.raises(DataSourceError, match="fafnir db refresh-marts"):
            ds_db.factor_row(dsn=DSN, symbol="ZZU", as_of="2021-07-15")
    finally:
        maintenance.refresh_marts(db, views=["mart.fundamental_panel"])
