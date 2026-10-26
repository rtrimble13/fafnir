"""
`statements_as_of` and `factor_row` (#107): the fundamentals on the read profile.

ADR 0008's rule for this surface, again: an agent and `duk` reading the same
security get the same rows, because the tools are `duk.datasource.db`'s reads. The
integration tests assert exactly that, plus what only the tools add: validation
before the warehouse is dialled, the point-in-time note, and the secondary share
class named in the payload. Every row is synthetic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from decimal import Decimal

import pytest

from duk.datasource import db as ds_db
from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals_map as fm
from fafnir_mcp import tools as T
from fafnir_mcp.errors import ToolError
from fafnir_mcp.results import jsonable

DSN = os.environ.get("FAFNIR_TEST_DSN", "")
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = dt.datetime(2026, 1, 5, 12, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Validation: refused before the warehouse is dialled (no DSN needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments, phrase",
    [
        ({"statement": "equity"}, "statement must be one of income, balance, cash"),
        ({"period": "monthly"}, "period must be annual or quarter"),
        ({"as_of": "26/10/2026"}, "as_of must be an ISO date"),
        ({"limit": 0}, "limit"),
    ],
)
def test_statements_as_of_validates_before_it_connects(arguments, phrase):
    with pytest.raises(ToolError) as exc:
        T.statements_as_of(dsn="", symbol="AAPL", **arguments)
    assert phrase in str(exc.value)
    assert "no DSN configured" not in str(exc.value)


def test_factor_row_validates_before_it_connects():
    with pytest.raises(ToolError) as exc:
        T.factor_row(dsn="", symbol="AAPL", as_of="yesterday")
    assert "as_of must be an ISO date" in str(exc.value)
    with pytest.raises(ToolError, match="symbol is required"):
        T.factor_row(dsn="", symbol=" ")


# ---------------------------------------------------------------------------
# Against the warehouse
# ---------------------------------------------------------------------------


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


def _put(db, statement, sid, fiscal_date, label, known_from, *, version=1, **items):
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900108",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version,
        "known_from": known_from,
        "known_to": items.pop("known_to", None),
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": T0,
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


def _company(db):
    """ZZM, with FY2020 restated in 2022 and FY2021; ZZMB a second class of it."""
    sid = _security(db, "ZZM", cik="0000900108")
    _security(db, "ZZMB", cik="900108")
    end = dt.date(2020, 12, 31)
    _put(
        db,
        "income",
        sid,
        end,
        "2020-FY",
        dt.date(2021, 2, 1),
        known_to=dt.date(2022, 3, 1),
        revenue=1000,
        net_income=100,
        net_income_common=100,
        weighted_average_shares=100,
    )
    _put(
        db,
        "income",
        sid,
        end,
        "2020-FY",
        dt.date(2022, 3, 1),
        version=2,
        revenue=1100,
        net_income=100,
        net_income_common=100,
        weighted_average_shares=100,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        end,
        "2020-FY",
        dt.date(2021, 2, 1),
        total_assets=2000,
        total_equity_parent=1000,
        shares_outstanding=100,
    )
    _put(
        db,
        "income",
        sid,
        dt.date(2021, 12, 31),
        "2021-FY",
        dt.date(2022, 2, 15),
        revenue=1200,
    )
    return sid


@pytest.mark.integration
def test_statements_as_of_returns_duks_rows(db):
    sid = _company(db)
    for as_of in (None, "2021-06-30", "2022-02-20"):
        tool = T.statements_as_of(dsn=DSN, symbol="ZZM", as_of=as_of)
        duk = ds_db.statements(dsn=DSN, symbol="ZZM", as_of=as_of)
        assert tool["rows"] == jsonable(duk["rows"])
        assert (tool["security_id"], tool["as_of"]) == (sid, duk["as_of"].isoformat())
        assert tool["statements_of"] is None and not tool["truncated"]
    early = T.statements_as_of(dsn=DSN, symbol="ZZM", as_of="2021-06-30")
    # Exact decimals as strings, and the restatement not yet known.
    assert [r["revenue"] for r in early["rows"]] == ["1000.00"]
    assert "a later restatement does not show" in early["note"]
    latest = T.statements_as_of(dsn=DSN, symbol="ZZM", limit=1)
    assert [r["fiscal_year"] for r in latest["rows"]] == [2021]
    balance = T.statements_as_of(dsn=DSN, symbol="zzm", statement="BALANCE")
    assert [r["total_assets"] for r in balance["rows"]] == ["2000.00"]


@pytest.mark.integration
def test_a_secondary_class_is_named_in_the_payload(db):
    _company(db)
    found = T.statements_as_of(dsn=DSN, symbol="ZZMB")
    assert found["statements_of"] == "ZZM"
    assert "ZZMB holds no statements of its own" in found["note"]
    assert [r["revenue"] for r in found["rows"]] == ["1100.00", "1200.00"]


@pytest.mark.integration
def test_the_dated_ticker_form_names_the_security(db):
    sid = _company(db)
    _price(db, sid, dt.date(2021, 6, 30), 20)
    dated = T.statements_as_of(dsn=DSN, symbol="ZZM@2021-06-30")
    assert dated["security_id"] == sid
    assert dated["rows"] == T.statements_as_of(dsn=DSN, symbol="ZZM")["rows"]


@pytest.mark.integration
def test_factor_row_returns_duks_row(db):
    sid = _company(db)
    _price(db, sid, dt.date(2021, 6, 30), 20)
    maintenance.refresh_marts(
        db, views=["mart.fundamental_panel", "mart.security_fundamentals_latest"]
    )
    tool = T.factor_row(dsn=DSN, symbol="ZZM", as_of="2021-07-15")
    duk = ds_db.factor_row(dsn=DSN, symbol="ZZM", as_of="2021-07-15")
    assert tool["factors"] == jsonable(duk["row"])
    assert (tool["source"], tool["as_of_date"]) == (
        "mart.fundamental_panel",
        "2021-06-30",
    )
    assert Decimal(tool["factors"]["market_cap"]) == 2000  # exact, as a string
    assert "Every input as known at as_of_date" in tool["note"]

    # Nobody traded in the week to today: the latest snapshot is empty.
    gone = T.factor_row(dsn=DSN, symbol="ZZM")
    assert gone["factors"] is None
    assert gone["note"] == "No row: mart.security_fundamentals_latest is empty."
    # A month-end ZZM did not trade in the week to.
    _price(db, _security(db, "ZZO"), dt.date(2021, 7, 30), 5)
    _put(
        db,
        "income",
        repo.resolve_security_id(db, "ZZO"),
        dt.date(2020, 12, 31),
        "2020-FY",
        dt.date(2021, 2, 1),
        revenue=1,
    )
    maintenance.refresh_marts(db, views=["mart.fundamental_panel"])
    later = T.factor_row(dsn=DSN, symbol="ZZM", as_of="2021-08-15")
    assert later["factors"] is None and later["as_of_date"] == "2021-07-30"
    assert later["note"] == "No row: ZZM did not trade in the week to 2021-07-30."
    early = T.factor_row(dsn=DSN, symbol="ZZM", as_of="1997-06-30")
    assert (early["factors"], early["as_of_date"]) == (None, None)
    assert early["note"] == "No row: the panel's first month-end is after 1997-06-30."
