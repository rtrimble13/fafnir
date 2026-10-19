"""Unit tests for FafnirConfig: env overrides and DSN assembly."""

from __future__ import annotations

import textwrap

import pytest

from fafnir.config import FafnirConfig


def _write(tmp_path, body: str):
    p = tmp_path / "fafnirrc"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_dsn_from_parts(tmp_path, monkeypatch):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    cfg = FafnirConfig(
        _write(
            tmp_path,
            """
        [database]
        host = "db.example"
        port = 6543
        dbname = "warehouse"
        user = "loader"
    """,
        )
    )
    dsn = cfg.dsn
    assert "host=db.example" in dsn
    assert "port=6543" in dsn
    assert "dbname=warehouse" in dsn
    assert "user=loader" in dsn


def test_env_dsn_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", "host=override dbname=x user=y")
    cfg = FafnirConfig(
        _write(
            tmp_path,
            """
        [database]
        dsn = "host=fromfile"
    """,
        )
    )
    assert cfg.dsn == "host=override dbname=x user=y"


def test_env_password_injected(tmp_path, monkeypatch):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    # Placeholder assembled at runtime so secret scanners don't flag a literal
    # credential in this fixture; the value itself is meaningless.
    fake_pw = "env-" + "placeholder"
    monkeypatch.setenv("FAFNIR_DB_PASSWORD", fake_pw)
    cfg = FafnirConfig(
        _write(
            tmp_path,
            """
        [database]
        host = "localhost"
        user = "loader"
        dbname = "fafnir"
    """,
        )
    )
    assert f"password={fake_pw}" in cfg.dsn


def test_api_key_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "envkey")
    cfg = FafnirConfig(
        _write(
            tmp_path,
            """
        [api]
        fmp_key = "filekey"
    """,
        )
    )
    assert cfg.fmp_key == "envkey"


def test_defaults_when_no_file(monkeypatch):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    cfg = FafnirConfig("/nonexistent/fafnirrc")
    assert cfg.universe == "us-equity-etf"
    assert cfg.request_rate_per_min == 280
    assert cfg.overlap_days == 5
    assert not cfg.is_loaded()


# -- Sharadar (plan SA-0106, #47) --------------------------------------------


def _no_sharadar_env(monkeypatch):
    for var in ("SHARADAR_API_KEY", "SHARADAR_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_sharadar_defaults_when_no_file(monkeypatch):
    from fafnir.sources.sharadar import DEFAULT_BASE_URL

    _no_sharadar_env(monkeypatch)
    cfg = FafnirConfig("/nonexistent/fafnirrc")
    assert cfg.sharadar_api_key == ""
    assert cfg.sharadar_base_url == DEFAULT_BASE_URL
    assert cfg.sharadar_rate_per_min == 60
    assert cfg.sharadar_bulk_dir == "var/fafnir/landing/sharadar"
    assert cfg.primary_price_vendor == "fmp"


def test_the_sharadar_key_comes_from_the_environment_only(tmp_path, monkeypatch):
    _no_sharadar_env(monkeypatch)
    path = _write(
        tmp_path,
        """
        [api]
        sharadar_key = "duk-laptop-key"
        [sharadar]
        api_key = "not-read-either"
    """,
    )
    cfg = FafnirConfig(path)
    assert cfg.sharadar_api_key == ""
    assert cfg.sharadar_key_in_rc
    monkeypatch.setenv("SHARADAR_API_KEY", " envkey ")
    assert FafnirConfig(path).sharadar_api_key == "envkey"


def test_sharadar_settings_from_the_file(tmp_path, monkeypatch):
    _no_sharadar_env(monkeypatch)
    path = _write(
        tmp_path,
        """
        [api]
        sharadar_base_url = "https://file.example/v1"
        [general]
        primary_price_vendor = "Sharadar"
        [sharadar]
        rate_per_min = 20
        bulk_dir = "/srv/fafnir/landing/sharadar"
    """,
    )
    cfg = FafnirConfig(path)
    assert cfg.sharadar_base_url == "https://file.example/v1"
    assert cfg.sharadar_rate_per_min == 20
    assert cfg.sharadar_bulk_dir == "/srv/fafnir/landing/sharadar"
    assert cfg.primary_price_vendor == "sharadar"
    monkeypatch.setenv("SHARADAR_BASE_URL", "https://env.example/v1")
    assert FafnirConfig(path).sharadar_base_url == "https://env.example/v1"


@pytest.mark.parametrize(
    "body, prop, message",
    [
        (
            '[general]\nprimary_price_vendor = "tiingo"',
            "primary_price_vendor",
            "tiingo",
        ),
        ("[sharadar]\nrate_per_min = 0", "sharadar_rate_per_min", "at least 1"),
    ],
)
def test_bad_sharadar_settings_fail_by_name(tmp_path, body, prop, message):
    cfg = FafnirConfig(_write(tmp_path, body))
    with pytest.raises(ValueError, match=message):
        getattr(cfg, prop)
