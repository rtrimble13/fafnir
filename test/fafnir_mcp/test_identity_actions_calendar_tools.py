"""`ticker_history`, `corporate_actions` and `trading_calendar` on the MCP read
profile, against fafnir (needs FAFNIR_TEST_DSN).

Each is a thin wrapper over the `duk.datasource.db` read behind `duk th`, `duk ca`
and `duk tc`, so the headline assertion is ADR 0008's again: an agent and a person
asking the same question get the same rows. The rest is what the envelope adds --
exact decimals as strings, nothing picked, no venue substituted.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from duk.datasource import db as ds_db
from fafnir.db import repository as repo
from fafnir_mcp import tools as T
from fafnir_mcp.errors import ToolError

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


@pytest.fixture()
def seeded(db, migrated_dsn):
    repo.ensure_exchange(db, "NYSE", "New York Stock Exchange", "US")
    old = repo.upsert_security(
        db,
        primary_symbol="XYZ",
        company_name="Old XYZ",
        exchange_code="NYSE",
        cusip="000000011",
        is_actively_trading=False,
        delisted_date=dt.date(2023, 12, 29),
    )
    new = repo.upsert_security(
        db, primary_symbol="XYZ", company_name="New XYZ", exchange_code="NYSE"
    )
    for sid, symbol, start, end in (
        (old, "XYZ", "1900-01-01", "2023-12-31"),
        (new, "ABC", "2010-01-04", "2023-12-31"),
        (new, "XYZ", "2024-01-01", None),
    ):
        db.execute(
            "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s)",
            (sid, symbol, start, end),
        )
    repo.upsert_corporate_action(
        db,
        security_id=new,
        action_type="dividend",
        ex_date=dt.date(2024, 2, 9),
        dividend_amount=Decimal("0.2425"),
        payment_date=dt.date(2024, 2, 15),
    )
    repo.upsert_corporate_action(
        db,
        security_id=new,
        action_type="split",
        ex_date=dt.date(2024, 6, 10),
        split_numerator=4,
        split_denominator=1,
    )
    return old, new


def test_ticker_history_is_duks_rows(migrated_dsn, seeded):
    old, new = seeded
    got = T.ticker_history(dsn=migrated_dsn, symbol="xyz")
    assert [(r["security_id"], r["valid_to"]) for r in got["rows"]] == [
        (old, "2023-12-31"),
        (new, None),
    ]
    assert got["rows"] == T.jsonable(
        ds_db.ticker_history(dsn=migrated_dsn, symbol="XYZ")
    )
    assert got["truncated"] is False and "first seen" in got["note"]


def test_ticker_history_of_a_security_lists_its_tickers(migrated_dsn, seeded):
    _, new = seeded
    got = T.ticker_history(dsn=migrated_dsn, symbol=f"sid:{new}")
    assert [r["symbol"] for r in got["rows"]] == ["ABC", "XYZ"]


def test_ticker_history_refuses_an_unknown_identifier(migrated_dsn, seeded):
    with pytest.raises(ToolError, match="no security carries"):
        T.ticker_history(dsn=migrated_dsn, symbol="sid:999999")


def test_corporate_actions_are_exact_strings(migrated_dsn, seeded):
    _, new = seeded
    got = T.corporate_actions(dsn=migrated_dsn, symbol="XYZ")
    assert got["security_id"] == new
    assert [(r["action_type"], r["ex_date"]) for r in got["rows"]] == [
        ("dividend", "2024-02-09"),
        ("split", "2024-06-10"),
    ]
    assert got["rows"][0]["dividend_amount"] == "0.242500", "never a float"
    assert got["rows"][1]["split_numerator"] == "4.000000"


def test_corporate_actions_filter_by_type_and_date(migrated_dsn, seeded):
    got = T.corporate_actions(
        dsn=migrated_dsn, symbol="XYZ", start_date="2024-03-01", action_type="SPLIT"
    )
    assert [r["action_type"] for r in got["rows"]] == ["split"]
    with pytest.raises(ToolError, match="split or dividend"):
        T.corporate_actions(dsn=migrated_dsn, symbol="XYZ", action_type="spinoff")


def test_trading_calendar_venues_and_days(migrated_dsn, seeded):
    venues = T.trading_calendar(dsn=migrated_dsn)
    assert "NYSE" in {r["exchange_code"] for r in venues["rows"]}
    days = T.trading_calendar(
        dsn=migrated_dsn,
        exchange="nyse",
        start_date="2024-12-24",
        end_date="2024-12-26",
    )
    assert days["exchange"] == "NYSE"
    assert [r["date"] for r in days["rows"]] == ["2024-12-24", "2024-12-26"]
    assert days["rows"] == T.jsonable(
        ds_db.calendar_days(
            dsn=migrated_dsn,
            exchange="NYSE",
            start_date="2024-12-24",
            end_date="2024-12-26",
        )
    )


def test_trading_calendar_never_answers_with_another_venue(migrated_dsn, seeded):
    got = T.trading_calendar(dsn=migrated_dsn, exchange="NOWHERE")
    assert got["rows"] == [] and "No other venue" in got["note"]
    with pytest.raises(ToolError, match="name it"):
        T.trading_calendar(dsn=migrated_dsn, start_date="2024-01-01")
