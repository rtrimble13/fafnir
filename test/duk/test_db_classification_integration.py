"""`duk ls`'s classification reads against fafnir (needs FAFNIR_TEST_DSN).

Counted over mart.security_latest, so each test refreshes it after seeding.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from duk.datasource import base as ds_base
from duk.datasource import db as ds_db
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration


def _security(
    db, symbol, sector, industry, *, active=True, exchange="NYSE", country="US"
):
    repo.ensure_exchange(db, exchange, exchange, "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code=exchange,
        sector_id=repo.get_or_create_sector(db, sector) if sector else None,
        industry_id=repo.get_or_create_industry(db, industry) if industry else None,
        country=country,
        is_actively_trading=active,
        delisted_date=None if active else dt.date(2024, 1, 31),
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


@pytest.fixture()
def classified(db):
    _security(db, "XOM", "Energy", "Oil & Gas Integrated")
    _security(db, "CVX", "Energy", "Oil & Gas Integrated", exchange="NASDAQ")
    _security(db, "SLB", "Energy", "Oil & Gas Equipment & Services")
    _security(db, "DEAD", "Energy", "Oil & Gas Drilling", active=False)
    _security(db, "AAPL", "Technology", "Consumer Electronics", exchange="NASDAQ")
    # One industry classified under two sectors: listed once under each.
    _security(db, "CONG", "Industrials", "Conglomerates")
    _security(db, "CONH", "Consumer Cyclical", "Conglomerates")
    _security(db, "NONE", None, None)
    repo.get_or_create_sector(db, "Utilities")
    repo.get_or_create_industry(db, "Utilities - Regulated Electric")
    db.execute("REFRESH MATERIALIZED VIEW mart.security_latest")
    return db


def _by(frame, *keys):
    """``{(key columns...): value}``, a missing name as None (pandas says NaN)."""

    def plain(value):
        return None if pd.isna(value) else value

    return {
        tuple(plain(r[k]) for k in keys[:-1]): r[keys[-1]]
        for r in frame.to_dict("records")
    }


def test_sectors_count_actively_trading_securities(migrated_dsn, classified):
    frame = ds_db.list_sectors(dsn=migrated_dsn)
    assert list(frame.columns) == ["sector_id", "sector_name", "security_count"]
    counts = _by(frame, "sector_name", "security_count")
    assert counts[("Energy",)] == 3, "DEAD is delisted"
    assert counts[("Utilities",)] == 0, "listed with 0, not dropped"
    assert frame.attrs["unclassified"] == 1


def test_include_delisted_counts_them_too(migrated_dsn, classified):
    frame = ds_db.list_sectors(dsn=migrated_dsn, includeDelisted=True)
    assert _by(frame, "sector_name", "security_count")[("Energy",)] == 4


def test_filters_scope_the_counts_case_insensitively(migrated_dsn, classified):
    frame = ds_db.list_sectors(dsn=migrated_dsn, exchange="nasdaq")
    counts = _by(frame, "sector_name", "security_count")
    assert counts[("Energy",)] == 1 and counts[("Technology",)] == 1


def test_industries_name_their_sector_and_count(migrated_dsn, classified):
    frame = ds_db.list_industries(dsn=migrated_dsn)
    assert list(frame.columns) == [
        "industry_id",
        "industry_name",
        "sector_name",
        "security_count",
    ]
    rows = _by(frame, "industry_name", "sector_name", "security_count")
    assert rows[("Oil & Gas Integrated", "Energy")] == 2
    assert rows[("Conglomerates", "Consumer Cyclical")] == 1
    assert rows[("Conglomerates", "Industrials")] == 1
    # No security, so no sector to name -- and 0, not dropped.
    assert rows[("Utilities - Regulated Electric", None)] == 0
    assert rows[("Oil & Gas Drilling", None)] == 0, "its only security is delisted"


def test_a_sector_filter_lists_only_that_sectors_industries(migrated_dsn, classified):
    frame = ds_db.list_industries(dsn=migrated_dsn, sector=["ENERGY"])
    assert sorted(frame["industry_name"]) == [
        "Oil & Gas Equipment & Services",
        "Oil & Gas Integrated",
    ]
    assert set(frame["sector_name"]) == {"Energy"}


def test_an_industry_filter_lists_only_the_sectors_holding_it(migrated_dsn, classified):
    frame = ds_db.list_sectors(dsn=migrated_dsn, industry=["conglomerates"])
    assert _by(frame, "sector_name", "security_count") == {
        ("Consumer Cyclical",): 1,
        ("Industrials",): 1,
    }


@pytest.mark.parametrize("typed", ["energy", "Energy", "ENERGY", " energy "])
def test_screen_matches_sector_in_any_case(migrated_dsn, classified, typed):
    frame = ds_db.screen(dsn=migrated_dsn, sector=[typed])
    assert sorted(frame["symbol"]) == ["CVX", "SLB", "XOM"]
    assert set(frame["sector"]) == {"Energy"}


def test_screen_matches_industry_exchange_and_country_in_any_case(
    migrated_dsn, classified
):
    frame = ds_db.screen(
        dsn=migrated_dsn,
        industry=["oil & gas integrated"],
        exchange="nyse",
        country="us",
    )
    assert list(frame["symbol"]) == ["XOM"]
    assert list(frame["sector"]) == ["Energy"], "an industry screen carries the sector"


def test_a_dividend_screen_is_refused(migrated_dsn, classified):
    with pytest.raises(ds_base.DataSourceError, match="--dividend"):
        ds_db.screen(dsn=migrated_dsn, dividendMoreThan=2.0)


def test_the_known_names(migrated_dsn, classified):
    assert "Energy" in ds_db.sector_names(dsn=migrated_dsn)
    assert "Conglomerates" in ds_db.industry_names(dsn=migrated_dsn)
