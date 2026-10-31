"""`duk ls`: case-insensitive names, an industry's sector, and security counts.

The datasource and the FMP list endpoints are monkeypatched; the SQL behind the
counts is in test_db_classification_integration.py.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from click.testing import CliRunner

from duk import cli
from duk.cli import canonical_names, main
from duk.datasource import db as ds_db
from duk.datasource import live as ds_live

SECTORS = ["Consumer Cyclical", "Energy", "Technology"]
INDUSTRIES = [
    "Furnishings, Fixtures & Appliances",
    "Oil & Gas Integrated",
    "Software - Infrastructure",
]


class TestCanonicalNames:
    @pytest.mark.parametrize("typed", ["energy", "Energy", "ENERGY", "  eNeRgY "])
    def test_case_and_spacing_do_not_matter(self, typed):
        assert canonical_names(typed, SECTORS, "sector") == ["Energy"]

    def test_several_names(self):
        assert canonical_names("technology,ENERGY", SECTORS, "sector") == [
            "Technology",
            "Energy",
        ]

    def test_a_name_containing_a_comma_is_one_name(self):
        # Split naively, this was two unknown industries and "No data found".
        assert canonical_names(
            "furnishings, fixtures & appliances,oil & gas integrated",
            INDUSTRIES,
            "industry",
        ) == ["Furnishings, Fixtures & Appliances", "Oil & Gas Integrated"]

    def test_an_unknown_name_is_refused_with_the_nearest(self):
        with pytest.raises(
            ValueError, match="Unknown sector 'enrgy'. Did you mean: Energy"
        ):
            canonical_names("enrgy", SECTORS, "sector")

    def test_nothing_near_points_at_the_list(self):
        with pytest.raises(ValueError, match="duk ls --industries"):
            canonical_names("zzzz", INDUSTRIES, "industry")


@pytest.fixture()
def calls(monkeypatch):
    seen: dict = {}

    def record(name, frame):
        def fake(**kw):
            seen.setdefault(name, []).append(kw)
            return frame.copy()

        return fake

    monkeypatch.setattr(ds_db, "sector_names", lambda **kw: list(SECTORS))
    monkeypatch.setattr(ds_db, "industry_names", lambda **kw: list(INDUSTRIES))
    monkeypatch.setattr(
        ds_db,
        "screen",
        record(
            "screen",
            pd.DataFrame(
                [
                    {
                        "symbol": "XOM",
                        "sector": "Energy",
                        "industry": "Oil & Gas Integrated",
                    }
                ]
            ),
        ),
    )
    sectors = pd.DataFrame(
        [
            {"sector_id": 1, "sector_name": "Energy", "security_count": 3},
            {"sector_id": 2, "sector_name": "Technology", "security_count": 2},
        ]
    )
    sectors.attrs["unclassified"] = 4
    monkeypatch.setattr(ds_db, "list_sectors", record("list_sectors", sectors))
    industries = pd.DataFrame(
        [
            {
                "industry_id": 7,
                "industry_name": "Oil & Gas Integrated",
                "sector_name": "Energy",
                "security_count": 2,
            }
        ]
    )
    monkeypatch.setattr(ds_db, "list_industries", record("list_industries", industries))
    return seen


@pytest.fixture()
def run(monkeypatch, tmp_path):
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return lambda args: CliRunner().invoke(main, ["-c", str(cfg)] + args)


class TestWarehouse:
    @pytest.mark.parametrize("typed", ["energy", "Energy", "ENERGY"])
    def test_sector_screen_is_case_insensitive(self, run, calls, typed):
        result = run(["ls", f"--sector={typed}"])
        assert result.exit_code == 0, result.output
        assert calls["screen"][0]["sector"] == ["Energy"]

    def test_industry_screen_carries_the_sector_column(self, run, calls):
        result = run(["ls", "--industry", "OIL & GAS INTEGRATED"])
        assert result.exit_code == 0, result.output
        assert calls["screen"][0]["industry"] == ["Oil & Gas Integrated"]
        assert result.output.splitlines()[0] == "symbol,sector,industry"

    def test_exchange_and_country_are_uppercased(self, run, calls):
        result = run(["ls", "--exchange", "nyse", "--country", "us"])
        assert result.exit_code == 0, result.output
        assert calls["screen"][0]["exchange"] == "NYSE"
        assert calls["screen"][0]["country"] == "US"

    def test_an_unknown_sector_is_refused_not_screened(self, run, calls):
        result = run(["ls", "--sector", "enrgy"])
        assert result.exit_code == 1
        assert "Did you mean: Energy?" in result.output
        assert "screen" not in calls

    def test_sectors_list_has_counts(self, run, calls):
        result = run(["ls", "--sectors", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)[0] == {
            "sector_id": 1,
            "sector_name": "Energy",
            "security_count": 3,
        }
        assert calls["list_sectors"][0]["includeDelisted"] is False

    def test_sectors_table_footer_totals_and_says_what_was_unclassified(
        self, run, calls
    ):
        result = run(["ls", "--sectors", "--table"])
        assert result.exit_code == 0, result.output
        assert "SECURITY COUNT" in result.output
        assert (
            "2 sectors · 5 securities, actively trading · 4 with no sector · "
            "as of the last mart refresh"
        ) in result.output

    def test_industries_list_names_each_sector(self, run, calls):
        result = run(["ls", "--industries"])
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0] == (
            "industry_id,industry_name,sector_name,security_count"
        )

    def test_filters_scope_the_counts(self, run, calls):
        result = run(
            [
                "ls",
                "--industries",
                "--sector",
                "energy",
                "--exchange",
                "nyse",
                "--include-delisted",
            ]
        )
        assert result.exit_code == 0, result.output
        kw = calls["list_industries"][0]
        assert kw["sector"] == ["Energy"] and kw["exchange"] == "NYSE"
        assert kw["includeDelisted"] is True

    def test_a_dividend_screen_is_refused_not_silently_ignored(self, run, monkeypatch):
        # The warehouse snapshot has no dividend: the filter used to be dropped
        # and the unfiltered screen printed as if it had applied.
        monkeypatch.setattr(ds_db, "_connect", lambda dsn: pytest.fail("no query"))
        result = run(["ls", "--dividend", ">2"])
        assert result.exit_code == 1
        assert "--dividend cannot be screened in the warehouse" in result.output


class TestFmp:
    @pytest.fixture(autouse=True)
    def _fmp(self, monkeypatch):
        monkeypatch.setattr(
            cli, "sector_list_api", lambda key: [{"sector": s} for s in SECTORS]
        )
        monkeypatch.setattr(
            cli, "industry_list_api", lambda key: [{"industry": i} for i in INDUSTRIES]
        )
        self.screens = []

        def screen(**kw):
            self.screens.append(kw)
            return pd.DataFrame([{"symbol": "XOM", "sector": "Energy"}])

        monkeypatch.setattr(ds_live, "screen", screen)

    def test_fmp_gets_the_vendor_spelling(self, run):
        result = run(["-S", "fmp", "ls", "--sector", "ENERGY", "--exchange", "nasdaq"])
        assert result.exit_code == 0, result.output
        assert self.screens[0]["sector"] == ["Energy"]
        assert self.screens[0]["exchange"] == "NASDAQ"

    def test_fmp_lists_carry_explicit_nulls_and_say_why(self, run):
        result = run(["-S", "fmp", "ls", "--industries", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert rows[0]["sector_name"] is None and rows[0]["security_count"] is None
        assert "come from the warehouse (-S db)" in result.stderr

    def test_fmp_lists_refuse_scoping_filters(self, run):
        result = run(["-S", "fmp", "ls", "--sectors", "--exchange", "NYSE"])
        assert result.exit_code == 1
        assert "scope the warehouse's counts" in result.output
