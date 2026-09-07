"""`duk ph`/`duk ls` given a security identifier instead of a ticker.

Both data sources are monkeypatched: what is under test is the command's
contract -- that an identifier resolves to exactly one security before anything
else happens, that ambiguity stops rather than guesses, and that the resolved
ticker (not the raw argument) is what reaches the query, the log and the -o
filename. The SQL behind db-mode resolution is covered by the integration tests
in test_db_identifier_resolution.py.

The plain-ticker cases at the bottom carry as much weight as the new behaviour:
this is an added branch on two commands people already use.
"""

from __future__ import annotations

import pandas as pd
import pytest
from click.testing import CliRunner

from duk import identifiers as ids
from duk.cli import main
from duk.datasource import db as ds_db
from duk.datasource import live as ds_live

IBM = {
    "security_id": 42,
    "symbol": "IBM",
    "company_name": "International Business Machines",
    "exchange_code": "NYSE",
    "exchange_name": "New York Stock Exchange",
    "is_actively_trading": True,
    "delisted_date": None,
}

PRICES = pd.DataFrame(
    {"close": [100.0, 101.0]},
    index=pd.to_datetime(["2024-01-02", "2024-01-03"]).rename("date"),
)


@pytest.fixture()
def calls():
    return {}


@pytest.fixture()
def runner(monkeypatch, tmp_path, calls):
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    def fake_resolve(*, dsn=None, api_key=None, identifier):
        calls["identifier"] = identifier
        return [dict(IBM)]

    def fake_prices(**kwargs):
        calls["price_kwargs"] = kwargs
        return PRICES.copy()

    monkeypatch.setattr(ds_db, "resolve_identifier", fake_resolve)
    monkeypatch.setattr(ds_live, "resolve_identifier", fake_resolve)
    monkeypatch.setattr(ds_db, "price_history", fake_prices)
    monkeypatch.setattr(ds_live, "price_history", fake_prices)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return CliRunner(), ["-c", str(cfg)]


def _run(runner, args):
    cli, prefix = runner
    return cli.invoke(main, prefix + args)


class TestPriceHistoryByIdentifier:
    def test_cusip_resolves_and_queries_the_resolved_security(self, runner, calls):
        result = _run(runner, ["-S", "db", "ph", "cusip:459200101"])
        assert result.exit_code == 0
        assert calls["identifier"].scheme == ids.CUSIP
        assert calls["identifier"].value == "459200101"
        # The ticker, not the argument, is what the price query sees...
        assert calls["price_kwargs"]["symbol"] == "IBM"
        # ...and the identity found by the identifier is carried through, so the
        # ticker is never re-resolved (a reused ticker resolves to its current
        # owner, which need not be this security).
        assert calls["price_kwargs"]["security_id"] == 42

    def test_cik_ignores_zero_padding(self, runner, calls):
        assert _run(runner, ["-S", "db", "ph", "cik:0000051143"]).exit_code == 0
        assert calls["identifier"].value == "51143"

    def test_live_mode_resolves_through_the_vendor_search(self, runner, calls):
        result = _run(runner, ["-S", "live", "ph", "isin:US4592001014"])
        assert result.exit_code == 0
        assert calls["identifier"].scheme == ids.ISIN
        assert calls["price_kwargs"]["symbol"] == "IBM"
        # Live mode has no security master, so there is no id to carry.
        assert "security_id" not in calls["price_kwargs"]

    def test_output_file_is_named_for_the_ticker_not_the_identifier(
        self, runner, tmp_path
    ):
        # A colon in a filename is not portable, and "cik-51143.csv" would not be
        # findable by anyone looking for IBM's prices either.
        out = tmp_path / "prices"
        out.mkdir()
        result = _run(runner, ["-S", "db", "ph", "cik:51143", "-o", str(out)])
        assert result.exit_code == 0
        written = [p.name for p in out.iterdir()]
        assert written == ["IBM_earliest_latest.csv"]


class TestRefusals:
    def test_a_malformed_identifier_is_a_parse_error_not_an_empty_series(self, runner):
        result = _run(runner, ["-S", "db", "ph", "cik:five"])
        assert result.exit_code == 1
        assert "is not a CIK" in result.output

    def test_no_match_names_the_identifier(self, runner, monkeypatch):
        monkeypatch.setattr(ds_db, "resolve_identifier", lambda **kw: [])
        result = _run(runner, ["-S", "db", "ph", "cik:51143"])
        assert result.exit_code == 1
        assert "No security found for CIK 51143." in result.output

    def test_several_matches_stop_rather_than_pick_a_share_class(
        self, runner, monkeypatch
    ):
        # One CIK, two listed classes. Answering about GOOG when the caller meant
        # GOOGL is the failure that cannot be spotted from the output.
        monkeypatch.setattr(
            ds_db,
            "resolve_identifier",
            lambda **kw: [
                dict(IBM, security_id=1, symbol="GOOG", company_name="Alphabet Inc"),
                dict(IBM, security_id=2, symbol="GOOGL", company_name="Alphabet Inc"),
            ],
        )
        result = _run(runner, ["-S", "db", "ph", "cik:1652044"])
        assert result.exit_code == 1
        assert "matches 2 securities" in result.output
        assert "GOOG" in result.output and "GOOGL" in result.output


class TestPlainTickersAreUnchanged:
    def test_a_bare_ticker_never_touches_identifier_resolution(self, runner, calls):
        result = _run(runner, ["-S", "db", "ph", "ibm"])
        assert result.exit_code == 0
        assert "identifier" not in calls
        assert calls["price_kwargs"]["symbol"] == "IBM"
        assert calls["price_kwargs"]["security_id"] is None

    def test_an_explicit_ticker_scheme_is_still_just_a_ticker(self, runner, calls):
        result = _run(runner, ["-S", "db", "ph", "ticker:ibm"])
        assert result.exit_code == 0
        assert "identifier" not in calls
        assert calls["price_kwargs"]["symbol"] == "IBM"


class TestCompanySummaryByIdentifier:
    """`ls` needs no new branch: resolve_company handles the identifier itself."""

    def test_an_identifier_reaches_resolve_company_verbatim(
        self, runner, monkeypatch, calls
    ):
        def fake_resolve_company(*, dsn, query):
            calls["query"] = query
            return [dict(IBM)]

        monkeypatch.setattr(ds_db, "resolve_company", fake_resolve_company)
        monkeypatch.setattr(
            ds_db,
            "company_summary",
            lambda **kw: {
                "profile": dict(IBM),
                "coverage": None,
                "actions": None,
                "last_bar": None,
                "dq_flags": [],
                "fundamentals": None,
                "adjusted_prices": pd.DataFrame(),
            },
        )
        result = _run(runner, ["-S", "db", "ls", "cusip:459200101"])
        assert result.exit_code == 0
        assert calls["query"] == "cusip:459200101"
        assert "International Business Machines" in result.output

    def test_a_malformed_identifier_is_refused_before_the_lookup(self, runner):
        result = _run(runner, ["-S", "db", "ls", "isin:US459"])
        assert result.exit_code == 1
        assert "is not an ISIN" in result.output

    def test_a_miss_is_worded_for_the_identifier_not_for_a_name(
        self, runner, monkeypatch
    ):
        monkeypatch.setattr(ds_db, "resolve_company", lambda **kw: [])
        result = _run(runner, ["-S", "db", "ls", "cik:51143"])
        assert result.exit_code == 1
        assert "No security found for CIK 51143." in result.output

    def test_several_share_classes_are_offered_by_ticker(self, runner, monkeypatch):
        # "Re-run with ... a more specific name" is useless advice to someone who
        # typed a CIK: one CIK is one issuer, and its classes differ only by ticker.
        monkeypatch.setattr(
            ds_db,
            "resolve_company",
            lambda **kw: [
                dict(IBM, security_id=1, symbol="GOOG", company_name="Alphabet Inc"),
                dict(IBM, security_id=2, symbol="GOOGL", company_name="Alphabet Inc"),
            ],
        )
        result = _run(runner, ["-S", "db", "ls", "cik:1652044"])
        assert result.exit_code == 1
        assert "CIK 1652044 matches 2 securities" in result.output
        assert "Re-run with one of these tickers" in result.output
