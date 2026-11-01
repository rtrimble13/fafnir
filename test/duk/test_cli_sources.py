"""`duk -S …`: the warehouse is the default; FMP and Sharadar are explicit.

Every backend is monkeypatched. Under test is the dispatch contract: which
source a bare invocation reads, what `live` now means, what each vendor source
needs before it is called, and which commands a vendor source can serve.
"""

from __future__ import annotations

import pandas as pd
import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db
from duk.datasource import live as ds_live
from duk.datasource import sharadar as ds_sharadar

PRICES = pd.DataFrame(
    {"close": [100.0, 101.0]},
    index=pd.to_datetime(["2024-01-02", "2024-01-03"]).rename("date"),
)


@pytest.fixture()
def calls():
    return {}


@pytest.fixture()
def harness(monkeypatch, tmp_path, calls):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    monkeypatch.delenv("SHARADAR_BASE_URL", raising=False)

    def record(name):
        def fake(**kwargs):
            calls.setdefault("backend", []).append(name)
            calls["kwargs"] = kwargs
            return PRICES.copy()

        return fake

    monkeypatch.setattr(ds_db, "price_history", record("db"))
    monkeypatch.setattr(ds_live, "price_history", record("fmp"))
    monkeypatch.setattr(ds_sharadar, "price_history", record("sharadar"))

    def run(args, rc=""):
        cfg = tmp_path / "dukrc.toml"
        cfg.write_text(rc)
        return CliRunner().invoke(main, ["-c", str(cfg)] + args)

    return run


DB_RC = '[database]\ndsn = "host=x dbname=fafnir"\n'
FMP_RC = '[api]\nfmp_key = "k"\n'
SHARADAR_RC = (
    '[api]\nsharadar_key = "s"\nsharadar_base_url = "https://example.test/v1"\n'
)


class TestDefaultSource:
    def test_bare_invocation_reads_the_warehouse(self, harness, calls):
        result = harness(["ph", "AAPL"], rc=DB_RC)
        assert result.exit_code == 0, result.output
        assert calls["backend"] == ["db"]

    def test_db_default_without_a_dsn_is_configuration_advice(self, harness, calls):
        # Before 1.6 this silently fell back to FMP; now the warehouse is the
        # default and a missing DSN says so, naming the vendor flags instead.
        result = harness(["ph", "AAPL"], rc=FMP_RC)
        assert result.exit_code == 1
        assert "no fafnir DSN configured" in result.output
        assert "-S fmp" in result.output
        assert "backend" not in calls

    def test_configured_default_source_is_honoured(self, harness, calls):
        rc = FMP_RC + '[general]\ndefault_source = "fmp"\n'
        assert harness(["ph", "AAPL"], rc=rc).exit_code == 0
        assert calls["backend"] == ["fmp"]

    def test_configured_live_default_means_fmp(self, harness, calls):
        rc = FMP_RC + '[general]\ndefault_source = "live"\n'
        assert harness(["ph", "AAPL"], rc=rc).exit_code == 0
        assert calls["backend"] == ["fmp"]


class TestVendorSources:
    def test_fmp_flag_reads_fmp(self, harness, calls):
        assert harness(["-S", "fmp", "ph", "AAPL"], rc=FMP_RC).exit_code == 0
        assert calls["backend"] == ["fmp"]

    def test_live_flag_still_reads_fmp(self, harness, calls):
        assert harness(["-S", "live", "ph", "AAPL"], rc=FMP_RC).exit_code == 0
        assert calls["backend"] == ["fmp"]

    def test_fmp_without_a_key_is_refused_before_any_call(self, harness, calls):
        result = harness(["-S", "fmp", "ph", "AAPL"], rc=DB_RC)
        assert result.exit_code == 1
        assert "FMP API key not configured" in result.output
        assert "backend" not in calls

    def test_sharadar_flag_reads_sharadar_with_its_key_and_base_url(
        self, harness, calls
    ):
        result = harness(["-S", "sharadar", "ph", "AAPL", "--adj"], rc=SHARADAR_RC)
        assert result.exit_code == 0, result.output
        assert calls["backend"] == ["sharadar"]
        assert calls["kwargs"]["api_key"] == "s"
        assert calls["kwargs"]["base_url"] == "https://example.test/v1"
        assert calls["kwargs"]["adjusted"] is True
        assert calls["kwargs"]["symbol"] == "AAPL"

    def test_sharadar_key_from_the_environment(self, harness, calls, monkeypatch):
        monkeypatch.setenv("SHARADAR_API_KEY", "env-key")
        assert harness(["-S", "sharadar", "ph", "AAPL"]).exit_code == 0
        assert calls["kwargs"]["api_key"] == "env-key"
        assert calls["kwargs"]["base_url"] is None

    def test_sharadar_without_a_key_is_refused(self, harness, calls):
        result = harness(["-S", "sharadar", "ph", "AAPL"], rc=DB_RC)
        assert result.exit_code == 1
        assert "Sharadar API key not configured" in result.output
        assert "backend" not in calls

    def test_sharadar_cannot_resolve_identifiers_yet(self, harness, calls):
        result = harness(["-S", "sharadar", "ph", "cik:51143"], rc=SHARADAR_RC)
        assert result.exit_code == 1
        assert "cannot be resolved against the Sharadar source" in result.output
        assert "backend" not in calls

    def test_unknown_source_is_rejected_by_click(self, harness):
        result = harness(["-S", "polygon", "ph", "AAPL"], rc=DB_RC)
        assert result.exit_code == 2


class TestCommandsAVendorCannotServe:
    def test_ls_is_not_served_by_sharadar(self, harness):
        result = harness(["-S", "sharadar", "ls", "--sectors"], rc=SHARADAR_RC)
        assert result.exit_code == 1
        assert "not available from the Sharadar source" in result.output

    def test_yc_is_not_served_by_sharadar(self, harness):
        result = harness(["-S", "sharadar", "yc"], rc=SHARADAR_RC)
        assert result.exit_code == 1
        assert "no Sharadar backend" in result.output

    def test_company_summary_still_demands_the_warehouse(self, harness):
        result = harness(["-S", "fmp", "ls", "AAPL"], rc=FMP_RC)
        assert result.exit_code == 1
        assert "re-run with -S db" in result.output


class TestYieldCurveSource:
    """The warehouse holds no treasury data: under -S db, yc reads FMP and says so."""

    CURVE = pd.DataFrame(
        {"month3": [4.31], "year10": [4.25]},
        index=pd.to_datetime(["2025-06-27"]).rename("date"),
    )

    @pytest.fixture()
    def curve(self, monkeypatch):
        monkeypatch.setattr(
            "duk.fmp_api.get_yield_curve", lambda **kw: self.CURVE.copy()
        )

    def test_db_says_on_stderr_that_the_curve_is_fmps(self, harness, curve):
        result = harness(["yc"], rc=DB_RC + FMP_RC)
        assert result.exit_code == 0, result.output
        assert "yc reads FMP's curve" in result.stderr
        assert "Note:" not in result.stdout, "stdout is the data, unchanged"
        assert result.stdout.startswith("date,month3,year10")

    def test_fmp_asked_explicitly_prints_no_note(self, harness, curve):
        result = harness(["-S", "fmp", "yc"], rc=FMP_RC)
        assert result.exit_code == 0, result.output
        assert "Note:" not in result.stderr

    def test_db_without_an_fmp_key_is_still_refused(self, harness, curve):
        result = harness(["yc"], rc=DB_RC)
        assert result.exit_code == 1
        assert "yc requires the FMP API" in result.output
