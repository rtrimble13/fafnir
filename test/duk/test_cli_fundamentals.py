"""CLI behaviour for `duk fs` and `duk fa` (#107).

The datasource is monkeypatched throughout: what is under test is the commands'
contract -- the db-only refusal, how a miss, an empty answer and a secondary share
class are reported, the output formats and precision -- not the SQL, which
test_db_fundamentals.py covers against a database. Every value is synthetic.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db

ROWS = [
    {
        "fiscal_date": date(2020, 12, 31),
        "calendar_date": date(2020, 12, 31),
        "fiscal_year": 2020,
        "fiscal_period": "FY",
        "version_no": 2,
        "known_from": date(2022, 3, 1),
        "knowledge_source": "observed",
        "filing_date": None,
        "reported_currency": "USD",
        "revenue": Decimal("1100.00"),
        "net_income": None,
        "weighted_average_shares": Decimal("100.1234"),
    },
    {
        "fiscal_date": date(2021, 12, 31),
        "calendar_date": date(2021, 12, 31),
        "fiscal_year": 2021,
        "fiscal_period": "FY",
        "version_no": 1,
        "known_from": date(2022, 2, 15),
        "knowledge_source": "filed",
        "filing_date": date(2022, 2, 15),
        "reported_currency": "USD",
        "revenue": Decimal("1200.00"),
        "net_income": Decimal("130.00"),
        "weighted_average_shares": Decimal("100.0000"),
    },
]
FACTORS = {
    "security_id": 7,
    "as_of_date": date(2026, 9, 30),
    "fiscal_year": 2025,
    "basis": "ttm",
    "market_cap": Decimal("123456789.123456"),
    "earnings_yield": 0.034567,
    "roe": None,
    "piotroski_f": 7,
}
GOOGL = {"security_id": 8, "symbol": "GOOGL", "cik": "1652044"}


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    """A runner with a DSN configured and the datasource stubbed."""
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    calls = {}

    def statements(**kw):
        calls["statements"] = kw
        return {
            "security_id": 7,
            "as_of": date(2026, 10, 26),
            "statements_of": None,
            "rows": [dict(r) for r in ROWS],
        }

    def factor_row(**kw):
        calls["factor_row"] = kw
        return {
            "security_id": 7,
            "source": "mart.security_fundamentals_latest",
            "as_of_date": date(2026, 9, 30),
            "statements_of": None,
            "row": dict(FACTORS),
        }

    monkeypatch.setattr(ds_db, "statements", statements)
    monkeypatch.setattr(ds_db, "factor_row", factor_row)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return CliRunner(), ["-c", str(cfg)], calls


def _run(runner, args):
    cli, prefix, _ = runner
    return cli.invoke(main, prefix + args)


class TestRefusals:
    @pytest.mark.parametrize("source, vendor", [("fmp", "FMP"), ("live", "FMP")])
    def test_a_vendor_source_is_db_mode_only(self, runner, source, vendor):
        for command in ("fs", "fa"):
            result = _run(runner, ["-S", source, command, "AAPL"])
            assert result.exit_code == 1
            assert f"no {vendor} backend (db mode only)" in result.output
            assert "-S db" in result.output

    def test_sharadar_is_db_mode_only_too(self, runner):
        result = _run(runner, ["-S", "sharadar", "fs", "AAPL"])
        assert result.exit_code == 1
        assert "no Sharadar backend (db mode only)" in result.output

    def test_both_output_formats_are_refused(self, runner):
        for command in ("fs", "fa"):
            result = _run(runner, ["-S", "db", command, "AAPL", "--csv", "--json"])
            assert result.exit_code == 1 and "Only one of" in result.output

    def test_a_limit_below_one_is_refused(self, runner):
        result = _run(runner, ["-S", "db", "fs", "AAPL", "-n", "0"])
        assert result.exit_code == 1 and "--limit must be at least 1" in result.output

    def test_an_unknown_statement_is_a_usage_error(self, runner):
        result = _run(runner, ["-S", "db", "fs", "AAPL", "--statement", "equity"])
        assert result.exit_code == 2


class TestStatements:
    def test_csv_one_row_per_period_oldest_first(self, runner):
        result = _run(runner, ["-S", "db", "fs", "aapl", "--as-of", "2026-10-26"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0].startswith("fiscal_date,calendar_date,fiscal_year,")
        assert lines[1].startswith(
            "2020-12-31,2020-12-31,2020,FY,2,2022-03-01,observed"
        )
        # NULL is empty, never 0; precision 2 by default.
        assert lines[1].endswith(",USD,1100.0,,100.12")
        assert lines[2].endswith(",USD,1200.0,130.0,100.0")
        _, _, calls = runner
        assert calls["statements"] == {
            "dsn": "host=/nonexistent dbname=fafnir",
            "symbol": "AAPL",
            "security_id": None,
            "statement": "income",
            "period": "annual",
            "as_of": "2026-10-26",
            "limit": None,
        }

    def test_options_reach_the_datasource(self, runner):
        result = _run(
            runner,
            [
                "-S",
                "db",
                "fs",
                "AAPL",
                "--statement",
                "Balance",
                "--period",
                "quarter",
                "-n",
                "4",
            ],
        )
        assert result.exit_code == 0, result.output
        _, _, calls = runner
        assert (
            calls["statements"]["statement"],
            calls["statements"]["period"],
            calls["statements"]["limit"],
        ) == ("balance", "quarter", 4)

    def test_json_records_with_null_and_precision(self, runner):
        result = _run(runner, ["-S", "db", "fs", "AAPL", "--json", "-p", "3"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert rows[0]["revenue"] == 1100.0 and rows[0]["net_income"] is None
        assert rows[0]["weighted_average_shares"] == 100.123
        assert rows[0]["known_from"] == "2022-03-01" and rows[0]["version_no"] == 2

    def test_a_secondary_class_says_whose_statements(self, runner, monkeypatch):
        _, _, calls = runner
        monkeypatch.setattr(
            ds_db,
            "statements",
            lambda **kw: {
                "security_id": 9,
                "as_of": date(2026, 10, 26),
                "statements_of": GOOGL,
                "rows": [dict(ROWS[0])],
            },
        )
        result = _run(runner, ["-S", "db", "fs", "GOOG"])
        assert result.exit_code == 0, result.output
        assert (
            "Note: GOOG holds no statements of its own; SF1 files its issuer's "
            "under GOOGL (CIK 1652044). Shown: GOOGL's." in result.output
        )

    def test_no_security_exits_one(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "statements",
            lambda **kw: {
                "security_id": None,
                "as_of": date(2026, 10, 26),
                "statements_of": None,
                "rows": [],
            },
        )
        result = _run(runner, ["-S", "db", "fs", "NOPE"])
        assert result.exit_code == 1 and "No security found for NOPE." in result.output

    def test_no_periods_is_an_answer_not_an_error(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "statements",
            lambda **kw: {
                "security_id": 7,
                "as_of": date(2001, 1, 1),
                "statements_of": None,
                "rows": [],
            },
        )
        result = _run(runner, ["-S", "db", "fs", "AAPL", "--as-of", "2001-01-01"])
        assert result.exit_code == 0
        assert "No annual income statements for AAPL as known on 2001-01-01" in (
            result.output
        )

    def test_output_to_a_directory_names_the_file(self, runner, tmp_path):
        result = _run(
            runner,
            ["-S", "db", "fs", "AAPL", "--statement", "cash", "-o", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        written = tmp_path / "AAPL_cash_annual.csv"
        assert written.read_text().startswith("fiscal_date,")


class TestFactorRow:
    def test_csv_is_one_field_per_line(self, runner):
        result = _run(runner, ["-S", "db", "fa", "AAPL"])
        assert result.exit_code == 0, result.output
        assert result.output.splitlines() == [
            "field,value",
            "security_id,7",
            "as_of_date,2026-09-30",
            "fiscal_year,2025",
            "basis,ttm",
            "market_cap,123456789.1235",
            "earnings_yield,0.0346",
            "roe,",
            "piotroski_f,7",
        ]
        _, _, calls = runner
        assert calls["factor_row"]["as_of"] is None

    def test_json_is_an_object(self, runner):
        result = _run(
            runner, ["-S", "db", "fa", "AAPL", "--json", "--as-of", "2020-06-15"]
        )
        assert result.exit_code == 0, result.output
        row = json.loads(result.output)
        assert row["roe"] is None and row["earnings_yield"] == 0.0346
        _, _, calls = runner
        assert calls["factor_row"]["as_of"] == "2020-06-15"

    def test_no_row_says_why(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "factor_row",
            lambda **kw: {
                "security_id": 7,
                "source": "mart.fundamental_panel",
                "as_of_date": date(2010, 6, 30),
                "statements_of": None,
                "row": None,
            },
        )
        result = _run(runner, ["-S", "db", "fa", "AAPL", "--as-of", "2010-07-02"])
        assert result.exit_code == 0
        assert (
            "No factor row for AAPL at 2010-06-30: it did not trade in the week to "
            "that date" in result.output
        )

    @pytest.mark.parametrize(
        "args, source, message",
        [
            (
                ["--as-of", "1997-06-30"],
                "mart.fundamental_panel",
                "No factor row for AAPL: the panel's first month-end is after "
                "1997-06-30",
            ),
            (
                [],
                "mart.security_fundamentals_latest",
                "No factor row for AAPL: mart.security_fundamentals_latest is empty",
            ),
        ],
    )
    def test_no_date_at_all_says_which(
        self, runner, monkeypatch, args, source, message
    ):
        monkeypatch.setattr(
            ds_db,
            "factor_row",
            lambda **kw: {
                "security_id": 7,
                "source": source,
                "as_of_date": None,
                "statements_of": None,
                "row": None,
            },
        )
        result = _run(runner, ["-S", "db", "fa", "AAPL", *args])
        assert result.exit_code == 0 and message in result.output

    def test_a_refusal_from_the_datasource_is_an_error(self, runner, monkeypatch):
        def unrefreshed(**kw):
            raise ds_db.DataSourceError(
                "mart.security_fundamentals_latest has not been refreshed yet: run "
                "`fafnir db refresh-marts`"
            )

        monkeypatch.setattr(ds_db, "factor_row", unrefreshed)
        result = _run(runner, ["-S", "db", "fa", "AAPL"])
        assert result.exit_code == 1
        assert "fafnir db refresh-marts" in result.output
