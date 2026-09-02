"""CLI behaviour for `duk ls <QUERY>` (profile mode).

The datasource is monkeypatched throughout: what is under test is the command's
contract -- which flag combinations are refused, what a miss and an ambiguity do,
which exit codes scripts can rely on -- not the SQL, which the integration tests
cover.

The list and screen regressions at the bottom matter as much as the new
behaviour: profile mode is an added branch on a command people already use, and
the whole point of returning early is that it changes nothing for them.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db

CANDIDATE = {
    "security_id": 1,
    "symbol": "AAPL",
    "company_name": "Apple Inc.",
    "exchange_code": "NASDAQ",
    "exchange_name": "Nasdaq",
    "is_actively_trading": True,
    "delisted_date": None,
    "matched_former_symbol": None,
    "matched_former_valid_to": None,
}

RAW = {
    "profile": {
        "security_id": 1,
        "symbol": "AAPL",
        "company_name": "Apple Inc.",
        "exchange_code": "NASDAQ",
        "currency": "USD",
        "country": "US",
        "sector_name": "Technology",
        "industry_name": "Consumer Electronics",
        "is_actively_trading": True,
        "market_cap_usd": 3_420_000_000_000,
        "beta": 1.24,
    },
    "coverage": None,
    "actions": None,
    "last_bar": None,
    "dq_flags": [],
    "fundamentals": None,
    "adjusted_prices": pd.DataFrame(),
}


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    """A runner with a DSN configured and the datasource stubbed."""
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setattr(ds_db, "resolve_company", lambda **kw: [dict(CANDIDATE)])
    monkeypatch.setattr(ds_db, "company_summary", lambda **kw: dict(RAW))
    # An empty ~/.dukrc, so a developer's real config cannot change the result.
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return CliRunner(), ["-c", str(cfg)]


def _run(runner, args):
    cli, prefix = runner
    return cli.invoke(main, prefix + args)


class TestProfileModeRefusals:
    def test_screening_flag_with_a_query_is_refused(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--sector", "Technology"])
        assert result.exit_code == 1
        assert "selects one company" in result.output

    def test_list_flag_with_a_query_is_refused(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--sectors"])
        assert result.exit_code == 1
        assert "selects one company" in result.output

    def test_summary_flag_is_refused(self, runner):
        # --summary means "row count" in list mode; the profile IS the summary,
        # so honouring it would print "1".
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--summary"])
        assert result.exit_code == 1
        assert "already a summary" in result.output

    def test_live_source_is_refused_rather_than_silently_falling_back(self, runner):
        # Unlike `yc`, which falls back to live: three of the four sections
        # describe what the warehouse holds, so a fallback would quietly answer a
        # different question.
        result = _run(runner, ["-S", "live", "ls", "AAPL"])
        assert result.exit_code == 1
        assert "-S db" in result.output

    def test_both_output_formats_is_refused(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--csv", "--json"])
        assert result.exit_code == 1
        assert "Only one of" in result.output


class TestResolutionOutcomes:
    def test_no_match_exits_one_with_the_query_named(self, runner, monkeypatch):
        monkeypatch.setattr(ds_db, "resolve_company", lambda **kw: [])
        result = _run(runner, ["-S", "db", "ls", "NOPE"])
        assert result.exit_code == 1
        assert "No company found matching 'NOPE'." in result.output

    def test_several_matches_print_a_did_you_mean_and_never_guess(
        self, runner, monkeypatch
    ):
        monkeypatch.setattr(
            ds_db,
            "resolve_company",
            lambda **kw: [
                dict(CANDIDATE, symbol="APLD", company_name="Applied Digital Corp"),
                dict(CANDIDATE, symbol="MSFT", company_name="Microsoft Corp"),
            ],
        )
        result = _run(runner, ["-S", "db", "ls", "Corp"])
        assert result.exit_code == 1
        assert "matches 2 companies" in result.output
        assert "APLD" in result.output and "MSFT" in result.output
        # Crucially, it must NOT have rendered a report for the first one.
        assert "PRICE HISTORY" not in result.output

    def test_a_single_match_renders_the_report(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL"])
        assert result.exit_code == 0
        for heading in (
            "PRICE HISTORY",
            "CORPORATE ACTIONS",
            "FUNDAMENTALS",
            "DATA QUALITY",
        ):
            assert heading in result.output


class TestOutputFormats:
    def test_json_is_one_nested_object(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        # A nested object, where list mode emits an array of records. The
        # divergence is deliberate and documented -- flattening would destroy the
        # DQ breakdown.
        assert isinstance(payload, dict)
        assert set(payload) == {"meta", "prices", "actions", "fundamentals", "dq"}

    def test_csv_is_a_single_header_and_row(self, runner):
        result = _run(runner, ["-S", "db", "ls", "AAPL", "--csv"])
        assert result.exit_code == 0
        rows = [ln for ln in result.output.strip().splitlines() if ln]
        assert len(rows) == 2
        assert rows[0].startswith("symbol,company_name")

    def test_quiet_suppresses_stdout_but_still_writes_the_file(self, runner, tmp_path):
        out = tmp_path / "aapl.json"
        result = _run(
            runner, ["-S", "db", "ls", "AAPL", "--json", "-q", "-o", str(out)]
        )
        assert result.exit_code == 0
        assert result.output.strip() == ""
        assert json.loads(out.read_text())["meta"]["symbol"] == "AAPL"

    def test_output_file_gets_the_report(self, runner, tmp_path):
        out = tmp_path / "nested" / "aapl.txt"
        result = _run(runner, ["-S", "db", "ls", "AAPL", "-o", str(out)])
        assert result.exit_code == 0
        assert "PRICE HISTORY" in out.read_text()

    def test_limit_is_ignored_rather_than_refused(self, runner):
        # A group-shaped flag people leave in shell history; refusing it would be
        # pedantry, and honouring it would be meaningless.
        result = _run(runner, ["-S", "db", "ls", "AAPL", "-n", "5"])
        assert result.exit_code == 0
        assert "PRICE HISTORY" in result.output


class TestListAndScreenAreUnaffected:
    """Profile mode is an added branch; without a QUERY nothing may change."""

    def test_plain_ls_still_lists(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "list_actively_trading",
            lambda **kw: pd.DataFrame([{"symbol": "AAPL", "name": "Apple Inc."}]),
        )
        result = _run(runner, ["-S", "db", "ls"])
        assert result.exit_code == 0
        assert "symbol,name" in result.output
        assert "AAPL,Apple Inc." in result.output

    def test_screening_still_screens(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "screen",
            lambda **kw: pd.DataFrame([{"symbol": "AAPL", "sector": "Technology"}]),
        )
        result = _run(runner, ["-S", "db", "ls", "--sector", "Technology"])
        assert result.exit_code == 0
        assert "AAPL" in result.output

    def test_summary_flag_still_counts_rows_in_list_mode(self, runner, monkeypatch):
        monkeypatch.setattr(
            ds_db,
            "list_actively_trading",
            lambda **kw: pd.DataFrame([{"symbol": "AAPL", "name": "Apple Inc."}]),
        )
        result = _run(runner, ["-S", "db", "ls", "--summary"])
        assert result.exit_code == 0
        assert "Number of results: 1" in result.output
