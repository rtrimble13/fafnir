"""`duk th`, `duk ca`, `duk tc` and `sid:` against fafnir (needs FAFNIR_TEST_DSN).

These are the reads portable makes instead of querying `mart` itself (portable
ADR 0032), so the last class pins the JSON they print -- field names, types,
exact decimal text -- as the contract portable's adapter parses. A change that
breaks one of those assertions breaks portable's `pt price load`; change both
together.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from click.testing import CliRunner

from duk import identifiers as ids
from duk.cli import main
from duk.datasource import db as ds_db
from fafnir.db import repository as repo

pytestmark = pytest.mark.integration


def _security(db, symbol, *, cusip=None, isin=None, cik=None, active=True):
    repo.ensure_exchange(db, "NYSE", "New York Stock Exchange", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        country="US",
        cusip=cusip,
        isin=isin,
        cik=cik,
        is_actively_trading=active,
        delisted_date=None if active else dt.date(2023, 12, 29),
    )


def _period(db, sid, symbol, valid_from, valid_to=None, primary=True):
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to, "
        "is_primary) VALUES (%s, %s, %s, %s, %s)",
        (sid, symbol, valid_from, valid_to, primary),
    )


@pytest.fixture()
def reused(db):
    """XYZ named security 1 through 2023 and security 2 -- formerly ABC -- after."""
    one = _security(db, "XYZ", cusip="000000011", active=False)
    two = _security(db, "XYZ", cusip="000000022", isin="US0000000222")
    _period(db, one, "XYZ", "1900-01-01", "2023-12-31")
    _period(db, two, "ABC", "2010-01-04", "2023-12-31")
    _period(db, two, "XYZ", "2024-01-01")
    return db, one, two


class TestTickerHistory:
    def test_every_security_the_ticker_named(self, migrated_dsn, reused):
        _, one, two = reused
        rows = ds_db.ticker_history(dsn=migrated_dsn, symbol="xyz")
        assert [(r["security_id"], r["valid_from"], r["valid_to"]) for r in rows] == [
            (one, dt.date(1900, 1, 1), dt.date(2023, 12, 31)),
            (two, dt.date(2024, 1, 1), None),
        ]
        assert (
            rows[0]["cusip"] == "000000011" and rows[0]["is_actively_trading"] is False
        )
        assert rows[1]["primary_symbol"] == "XYZ" and rows[1]["asset_type"] == "equity"

    def test_a_range_keeps_the_overlapping_periods(self, migrated_dsn, reused):
        _, one, two = reused
        rows = ds_db.ticker_history(
            dsn=migrated_dsn,
            symbol="XYZ",
            start_date="2023-06-01",
            end_date="2023-12-31",
        )
        assert [r["security_id"] for r in rows] == [one]
        rows = ds_db.ticker_history(
            dsn=migrated_dsn,
            symbol="XYZ",
            start_date="2023-12-31",
            end_date="2024-01-01",
        )
        assert [r["security_id"] for r in rows] == [one, two]

    def test_a_securitys_tickers_over_time(self, migrated_dsn, reused):
        _, _, two = reused
        rows = ds_db.ticker_history(dsn=migrated_dsn, security_ids=[two])
        assert [(r["symbol"], r["valid_to"]) for r in rows] == [
            ("ABC", dt.date(2023, 12, 31)),
            ("XYZ", None),
        ]


class TestSecurityIdIdentifier:
    def test_sid_resolves_exactly_one_security(self, migrated_dsn, reused):
        _, one, _ = reused
        found = ds_db.resolve_identifier(
            dsn=migrated_dsn, identifier=ids.parse(f"sid:{one}")
        )
        assert [c["security_id"] for c in found] == [one]

    def test_an_unknown_sid_finds_nothing(self, migrated_dsn, reused):
        assert (
            ds_db.resolve_identifier(
                dsn=migrated_dsn, identifier=ids.parse("sid:999999")
            )
            == []
        )


@pytest.fixture()
def actions(db):
    sid = _security(db, "DIV", cusip="000000033")
    _period(db, sid, "DIV", "1900-01-01")
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2024, 2, 9),
        dividend_amount=Decimal("0.2425"),
        record_date=dt.date(2024, 2, 12),
        payment_date=dt.date(2024, 2, 15),
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2024, 6, 10),
        split_numerator=4,
        split_denominator=1,
    )
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date(2024, 2, 9),
                "open": Decimal("10"),
                "high": Decimal("13247980.736555"),
                "low": Decimal("9.5"),
                "close": Decimal("13247980.736555"),
                "volume": 100,
            }
        ],
    )
    return sid


class TestCorporateActions:
    def test_events_by_ex_date_as_stored(self, migrated_dsn, actions):
        found = ds_db.corporate_actions(dsn=migrated_dsn, security_id=actions)
        assert [(r["action_type"], r["ex_date"]) for r in found["rows"]] == [
            ("dividend", dt.date(2024, 2, 9)),
            ("split", dt.date(2024, 6, 10)),
        ]
        assert found["rows"][0]["dividend_amount"] == Decimal("0.242500")
        assert found["rows"][1]["split_numerator"] == Decimal("4")

    def test_a_range_and_a_type(self, migrated_dsn, actions):
        found = ds_db.corporate_actions(
            dsn=migrated_dsn, symbol="DIV", start_date="2024-03-01", action_type="split"
        )
        assert [r["action_type"] for r in found["rows"]] == ["split"]

    def test_no_such_security(self, migrated_dsn, actions):
        assert (
            ds_db.corporate_actions(dsn=migrated_dsn, symbol="NOPE")["security_id"]
            is None
        )


class TestCalendar:
    def test_each_venue_and_its_span(self, migrated_dsn, db):
        venues = {r["exchange_code"]: r for r in ds_db.calendars(dsn=migrated_dsn)}
        assert "NYSE" in venues
        assert venues["NYSE"]["first_date"] <= dt.date(2023, 1, 3)
        assert venues["NYSE"]["open_days"] > 0

    def test_a_venues_days_in_a_range(self, migrated_dsn, db):
        days = ds_db.calendar_days(
            dsn=migrated_dsn,
            exchange="nyse",
            start_date="2024-12-23",
            end_date="2024-12-27",
        )
        assert [d["date"] for d in days if d["is_open"]] == [
            dt.date(2024, 12, 23),
            dt.date(2024, 12, 24),
            dt.date(2024, 12, 26),
            dt.date(2024, 12, 27),
        ], "Christmas is no session"

    def test_an_unknown_venue_has_no_days_and_no_fallback(self, migrated_dsn, db):
        assert ds_db.calendar_days(dsn=migrated_dsn, exchange="NOWHERE") == []


class TestTheContractPortableReads:
    """The JSON portable's FafnirProvider parses (portable ADR 0032)."""

    @pytest.fixture()
    def duk(self, migrated_dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("FAFNIR_DSN", migrated_dsn)
        monkeypatch.delenv("DUK_FORMAT", raising=False)
        cfg = tmp_path / "dukrc.toml"
        cfg.write_text("")

        def run(*args):
            result = CliRunner().invoke(main, ["-c", str(cfg), "-S", "db", *args])
            assert result.exit_code == 0, result.output
            return json.loads(result.stdout)

        return run

    def test_th(self, duk, reused):
        _, one, two = reused
        rows = duk(
            "th",
            "ticker:XYZ",
            "--start-date",
            "2023-01-01",
            "--end-date",
            "2024-06-30",
            "--json",
        )
        assert rows[0] == {
            "symbol": "XYZ",
            "security_id": one,
            "valid_from": "1900-01-01",
            "valid_to": "2023-12-31",
            "is_primary": True,
            "primary_symbol": "XYZ",
            "company_name": "XYZ Inc",
            "asset_type": "equity",
            "exchange_code": "NYSE",
            "currency": "USD",
            "country": "US",
            "cik": None,
            "isin": None,
            "cusip": "000000011",
            "is_actively_trading": False,
            "delisted_date": "2023-12-29",
        }
        assert rows[1]["security_id"] == two and rows[1]["valid_to"] is None

    def test_ca_exact(self, duk, actions):
        rows = duk("ca", f"sid:{actions}", "--json", "--exact")
        assert set(rows[0]) == {
            "ex_date",
            "action_type",
            "split_numerator",
            "split_denominator",
            "dividend_amount",
            "currency",
            "record_date",
            "payment_date",
            "declaration_date",
            "corporate_action_id",
            "security_id",
            "source",
        }
        assert (rows[0]["dividend_amount"], rows[0]["payment_date"]) == (
            "0.242500",
            "2024-02-15",
        )
        assert (rows[1]["split_numerator"], rows[1]["split_denominator"]) == (
            "4.000000",
            "1.000000",
        )
        assert isinstance(rows[0]["corporate_action_id"], str)

    def test_tc(self, duk, db):
        venues = duk("tc", "--json")
        nyse = next(v for v in venues if v["exchange_code"] == "NYSE")
        assert set(nyse) == {
            "exchange_code",
            "exchange_name",
            "first_date",
            "last_date",
            "open_days",
            "closed_days",
        }
        days = duk(
            "tc",
            "NYSE",
            "--start-date",
            "2024-12-24",
            "--end-date",
            "2024-12-26",
            "--json",
        )
        assert days == [
            {"date": "2024-12-24", "is_open": True, "session_note": None},
            {"date": "2024-12-26", "is_open": True, "session_note": None},
        ]

    def test_ph_by_sid_exact(self, duk, actions):
        bars = duk(
            "ph", f"sid:{actions}", "--start-date", "2024-02-09", "--end-date", "2024-02-09",
            "--hlc", "--json", "--exact",
        )  # fmt: skip
        assert bars == [
            {
                "date": "2024-02-09",
                "high": "13247980.736555",
                "low": "9.500000",
                "close": "13247980.736555",
            }
        ]
