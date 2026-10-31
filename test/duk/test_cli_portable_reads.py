"""`sid:`, `duk th`, `duk ca` and `duk tc` without a database: the grammar, the
refusals, and what reaches the datasource. The SQL and the printed contract are in
test_db_portable_reads_integration.py."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from click.testing import CliRunner

from duk import identifiers as ids
from duk.cli import main
from duk.datasource import db as ds_db
from duk.format_utils import plain_value


class TestSecurityIdScheme:
    @pytest.mark.parametrize(
        "text", ["sid:601133", "SID:601133", "security_id:601133", "sid:0601133"]
    )
    def test_parses_to_the_integer(self, text):
        ident = ids.parse(text)
        assert (ident.scheme, ident.value) == (ids.SECURITY_ID, "601133")
        assert ident.describe() == "security_id 601133"

    @pytest.mark.parametrize("text", ["sid:abc", "sid:-4", "sid:0", "sid:1.5"])
    def test_malformed_is_refused(self, text):
        with pytest.raises(ids.IdentifierError, match="not a security_id"):
            ids.parse(text)


class TestPlainValue:
    def test_exact_numbers_are_text(self):
        assert plain_value(Decimal("0.240000"), exact=True) == "0.240000"
        assert plain_value(7, exact=True) == "7"
        assert plain_value(dt.date(2024, 2, 9), exact=True) == "2024-02-09"
        assert plain_value(True, exact=True) is True
        assert plain_value(None, exact=True) is None

    def test_a_float_is_refused_under_exact(self):
        with pytest.raises(TypeError, match="float"):
            plain_value(0.24, exact=True)

    def test_without_exact_a_decimal_is_a_number(self):
        assert plain_value(Decimal("0.24")) == 0.24


@pytest.fixture()
def run(monkeypatch, tmp_path):
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return lambda args: CliRunner().invoke(main, ["-c", str(cfg)] + args)


@pytest.mark.parametrize("command", [["th", "AAPL"], ["ca", "AAPL"], ["tc"]])
@pytest.mark.parametrize("source", ["fmp", "sharadar"])
def test_vendor_sources_are_refused(run, command, source):
    result = run(["-S", source, *command])
    assert result.exit_code == 1
    assert "db mode only" in result.output


def test_sid_is_refused_for_a_vendor(run, monkeypatch):
    monkeypatch.setattr(
        "duk.datasource.live.resolve_identifier", lambda **kw: pytest.fail("no lookup")
    )
    result = run(["-S", "fmp", "ph", "sid:5"])
    assert result.exit_code == 1
    assert "warehouse's own key" in result.output


class TestTh:
    ROW = {
        "symbol": "XYZ",
        "security_id": 2,
        "valid_from": dt.date(2024, 1, 1),
        "valid_to": None,
        "is_primary": True,
        "primary_symbol": "XYZ",
        "company_name": "XYZ Inc",
        "asset_type": "equity",
        "exchange_code": "NYSE",
        "currency": "USD",
        "country": "US",
        "cik": None,
        "isin": None,
        "cusip": "000000022",
        "is_actively_trading": True,
        "delisted_date": None,
    }

    def test_a_ticker_reads_its_periods(self, run, monkeypatch):
        seen = []
        monkeypatch.setattr(
            ds_db, "ticker_history", lambda **kw: seen.append(kw) or [dict(self.ROW)]
        )
        result = run(["th", "xyz", "-s", "2024-01-01", "--json"])
        assert result.exit_code == 0, result.output
        assert seen[0]["symbol"] == "XYZ" and seen[0]["start_date"] == "2024-01-01"
        assert json.loads(result.output)[0]["valid_from"] == "2024-01-01"

    def test_an_identifier_lists_every_matching_securitys_tickers(
        self, run, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            ds_db,
            "resolve_identifier",
            lambda **kw: [{"security_id": 1}, {"security_id": 2}],
        )
        monkeypatch.setattr(
            ds_db, "ticker_history", lambda **kw: seen.append(kw) or [dict(self.ROW)]
        )
        result = run(["th", "cik:1652044"])
        assert result.exit_code == 0, result.output
        assert seen[0]["security_ids"] == [1, 2], "a listing, not a pick"

    def test_the_table_is_the_narrow_view(self, run, monkeypatch):
        monkeypatch.setattr(ds_db, "ticker_history", lambda **kw: [dict(self.ROW)])
        result = run(["th", "XYZ", "--table"])
        assert "TRADES NOW AS" in result.output and "CUSIP" not in result.output
        assert "1 period · XYZ · 1 security" in result.output

    def test_nothing_found_is_said(self, run, monkeypatch):
        monkeypatch.setattr(ds_db, "ticker_history", lambda **kw: [])
        result = run(["th", "NOPE"])
        assert result.exit_code == 0
        assert "No ticker history for NOPE" in result.output


class TestCa:
    def test_resolves_and_passes_the_filters(self, run, monkeypatch):
        seen = []

        def corporate_actions(**kw):
            seen.append(kw)
            return {
                "security_id": 7,
                "rows": [
                    {
                        "ex_date": dt.date(2024, 6, 10),
                        "action_type": "split",
                        "split_numerator": Decimal("4.000000"),
                        "split_denominator": Decimal("1.000000"),
                        "dividend_amount": None,
                        "currency": "USD",
                        "record_date": None,
                        "payment_date": None,
                        "declaration_date": None,
                        "corporate_action_id": 3,
                        "security_id": 7,
                        "source": "sharadar",
                    }
                ],
            }

        monkeypatch.setattr(ds_db, "corporate_actions", corporate_actions)
        result = run(["ca", "AAPL", "--type", "SPLIT", "-e", "2024-12-31", "--table"])
        assert result.exit_code == 0, result.output
        assert seen[0]["action_type"] == "split" and seen[0]["symbol"] == "AAPL"
        assert "4-for-1" in result.output
        csv_out = run(["ca", "AAPL", "--csv"]).output
        assert "4.000000,1.000000" in csv_out, "CSV is the stored text"

    def test_no_such_security(self, run, monkeypatch):
        monkeypatch.setattr(
            ds_db, "corporate_actions", lambda **kw: {"security_id": None, "rows": []}
        )
        result = run(["ca", "NOPE"])
        assert result.exit_code == 1
        assert "No security found for NOPE" in result.output


class TestTc:
    def test_dates_need_a_venue(self, run):
        result = run(["tc", "-s", "2024-01-01"])
        assert result.exit_code == 2
        assert "name it" in result.output

    def test_a_venue_is_uppercased_and_its_days_read(self, run, monkeypatch):
        seen = []
        monkeypatch.setattr(
            ds_db,
            "calendar_days",
            lambda **kw: seen.append(kw)
            or [{"date": dt.date(2024, 12, 24), "is_open": True, "session_note": None}],
        )
        result = run(["tc", "nyse", "--table"])
        assert result.exit_code == 0, result.output
        assert seen[0]["exchange"] == "nyse"  # the datasource uppercases
        assert "NYSE · 1 open day · 0 closures" in result.output
