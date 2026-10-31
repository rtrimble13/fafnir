"""The console contract: a table for a person, the old CSV/JSON for a program.

CliRunner's stdout is not a terminal, so a bare invocation here is the *piped*
case, which must print exactly what duk printed before tables existed. ``--table``
(or ``DUK_FORMAT=table``) stands in for a terminal.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pandas as pd
import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db

BARS = pd.DataFrame(
    {
        "open": [187.15, 184.22],
        "close": [1185.64, 0.00031],
        "volume": [82488700, 58414500],
    },
    index=pd.to_datetime(["2024-01-02", "2024-01-03"]).rename("date"),
)


@pytest.fixture()
def run(monkeypatch, tmp_path):
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(ds_db, "price_history", lambda **kw: BARS.copy())
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    cfg = tmp_path / "dukrc.toml"

    def invoke(args, rc=""):
        cfg.write_text(rc)
        return CliRunner().invoke(main, ["-c", str(cfg)] + args)

    return invoke


class TestPh:
    def test_piped_default_is_the_csv_it_always_was(self, run):
        result = run(["ph", "AAPL"])
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[:3] == [
            "date,open,close,volume",
            "2024-01-02,187.15,1185.64,82488700",
            "2024-01-03,184.22,0.0003100,58414500",
        ]

    def test_table_is_bordered_with_separators_and_a_footer(self, run):
        result = run(["ph", "AAPL", "--table"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0].startswith("╭") and lines[-2].startswith("╰")
        assert "│ 2024-01-02 │ 187.15 │  1,185.64 │ 82,488,700 │" in lines
        assert "0.0003100" in result.output, "a sub-penny price is not 0.00"
        assert lines[-1] == "2 bars · AAPL · raw · day · 2024-01-02 → 2024-01-03"

    def test_duk_format_env_selects_the_table(self, run, monkeypatch):
        monkeypatch.setenv("DUK_FORMAT", "table")
        assert run(["ph", "AAPL"]).output.startswith("╭")

    def test_config_selects_the_format(self, run):
        result = run(["ph", "AAPL"], rc='[general]\nconsole_format = "json"\n')
        assert json.loads(result.output)[0]["close"] == 1185.64

    def test_two_formats_are_refused(self, run):
        result = run(["ph", "AAPL", "--table", "--json"])
        assert result.exit_code == 1
        assert "Only one of --table, --csv or --json" in result.output

    def test_a_file_is_csv_even_when_the_console_is_a_table(self, run, tmp_path):
        out = tmp_path / "p.csv"
        result = run(["ph", "AAPL", "--table", "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert out.read_text().startswith("date,open,close,volume")
        assert "Data written to" in result.output and "╭" in result.output

    def test_summary_as_a_table(self, run):
        result = run(["ph", "AAPL", "--summary", "--table"])
        assert result.exit_code == 0, result.output
        assert "AAPL summary statistics" in result.output
        assert "MEDIAN" in result.output and "2 observations" in result.output

    def test_exact_table_shows_every_stored_digit(self, run, monkeypatch):
        exact = [
            {
                "date": pd.Timestamp("2024-01-02").date(),
                "close": Decimal("13247980.736555"),
                "volume": 100,
            }
        ]
        monkeypatch.setattr(ds_db, "exact_bars", lambda **kw: list(exact))
        result = run(["ph", "AAPL", "--close", "--exact", "--table"])
        assert result.exit_code == 0, result.output
        assert "13,247,980.736555" in result.output
        piped = run(["ph", "AAPL", "--close", "--exact", "--json"])
        assert json.loads(piped.output) == [
            {"date": "2024-01-02", "close": "13247980.736555", "volume": "100"}
        ]


class TestProfile:
    RAW = {
        "profile": {
            "security_id": 1,
            "symbol": "AAPL",
            "company_name": "Apple Inc.",
            "exchange_code": "NASDAQ",
            "is_actively_trading": True,
        },
        "coverage": None,
        "actions": None,
        "last_bar": None,
        "dq_flags": [],
        "fundamentals": None,
        "adjusted_prices": pd.DataFrame(),
    }

    @pytest.fixture(autouse=True)
    def _company(self, monkeypatch):
        candidate = {"security_id": 1, "symbol": "AAPL", "company_name": "Apple Inc."}
        monkeypatch.setattr(ds_db, "resolve_company", lambda **kw: [candidate])
        monkeypatch.setattr(ds_db, "company_summary", lambda **kw: dict(self.RAW))

    def test_piped_it_is_still_the_plain_report(self, run):
        result = run(["ls", "AAPL"])
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[1].startswith("-----")
        assert "│" not in result.output

    def test_on_a_terminal_it_is_boxed_with_section_rules(self, run):
        result = run(["ls", "AAPL", "--table"])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("╭")
        assert "├─ PRICE HISTORY" in result.output
        assert "├─ DATA QUALITY" in result.output

    def test_csv_still_flattens(self, run):
        assert run(["ls", "AAPL", "--csv"]).output.startswith("symbol,company_name")


class TestFundamentals:
    def test_fa_as_a_field_value_table(self, run, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "factor_row",
            lambda **kw: {
                "security_id": 1,
                "source": "mart.security_fundamentals_latest",
                "as_of_date": pd.Timestamp("2024-01-31").date(),
                "statements_of": None,
                "row": {"security_id": 601133, "earnings_yield": Decimal("0.034512")},
            },
        )
        result = run(["fa", "AAPL", "--table"])
        assert result.exit_code == 0, result.output
        assert "601133" in result.output and "601,133" not in result.output
        assert "0.0345" in result.output
        assert "as of 2024-01-31" in result.output

    def test_fs_puts_periods_across_and_items_down(self, run, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "statements",
            lambda **kw: {
                "security_id": 1,
                "as_of": pd.Timestamp("2024-06-30").date(),
                "statements_of": None,
                "rows": [
                    {"fiscal_date": "2022-12-31", "revenue": Decimal("1000000.5")},
                    {"fiscal_date": "2023-12-31", "revenue": Decimal("2000000.25")},
                ],
            },
        )
        result = run(["fs", "AAPL", "--table"])
        assert result.exit_code == 0, result.output
        header = next(line for line in result.output.splitlines() if "ITEM" in line)
        assert "2022-12-31" in header and "2023-12-31" in header
        assert "1,000,000.50" in result.output and "2,000,000.25" in result.output


class TestComputeCommands:
    def test_ti_sma_table_and_case_insensitive_column(self, run, tmp_path):
        prices = tmp_path / "p.csv"
        prices.write_text("date,close\n2024-01-02,10\n2024-01-03,12\n2024-01-04,14\n")
        result = run(
            ["ti", "sma", "-i", str(prices), "-c", "CLOSE", "-w", "2", "--table"]
        )
        assert result.exit_code == 0, result.output
        assert "3 rows · SMA · p.csv" in result.output
        assert "13.000" in result.output
