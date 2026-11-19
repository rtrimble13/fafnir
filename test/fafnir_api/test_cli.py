"""`fafnir-api`: the DSN it will and will not use, and the role it refuses."""

from __future__ import annotations

from fafnir_api import cli


def _found(**overrides):
    found = {
        "role": "fafnir_api",
        "read_only": "on",
        "statement_timeout": "30s",
        "mart": True,
        "ref": True,
        "ops": True,
        "keys_readable": True,
        "writable": False,
        "live_keys": 1,
    }
    found.update(overrides)
    return found


def test_no_dsn_is_an_error_with_instructions(monkeypatch, capsys):
    monkeypatch.delenv("FAFNIR_API_DSN", raising=False)
    assert cli.main([]) == 2
    assert "FAFNIR_API_DSN" in capsys.readouterr().err


def test_the_write_roles_dsn_is_never_inherited(monkeypatch, capsys):
    # On the host FAFNIR_DSN names fafnir_ingest. A service that fell back to it
    # would serve the internet as the write role and appear to work.
    monkeypatch.delenv("FAFNIR_API_DSN", raising=False)
    monkeypatch.setenv("FAFNIR_DSN", "dbname=fafnir user=fafnir_ingest")
    assert cli.main(["--check"]) == 2
    assert "NOT used" in capsys.readouterr().err


def test_the_read_role_may_serve():
    assert cli.problems(_found()) == []


def test_a_role_that_can_write_may_not():
    (line,) = cli.problems(_found(writable=True))
    assert "can write" in line and "fafnir_app" in line


def test_a_role_that_cannot_read_the_keys_may_not():
    (line,) = cli.problems(_found(keys_readable=False, ops=False))
    assert "ops.api_key" in line and "GRANT" in line


def test_a_role_that_cannot_read_the_warehouse_may_not():
    lines = cli.problems(_found(mart=False))
    assert any("USAGE on mart" in line for line in lines)


def test_a_writable_role_stops_the_server_before_it_listens(monkeypatch, capsys):
    monkeypatch.setattr(cli, "probe", lambda dsn: _found(writable=True))
    assert cli.main(["--dsn", "dbname=fafnir"]) == 1
    assert "refusing to start" in capsys.readouterr().err


def test_check_reports_and_passes_for_the_read_role(monkeypatch, capsys):
    monkeypatch.setattr(cli, "probe", lambda dsn: _found())
    assert cli.main(["--dsn", "dbname=fafnir", "--check"]) == 0
    out = capsys.readouterr().out
    assert "role:              fafnir_api" in out and out.rstrip().endswith("OK")


def test_check_fails_when_the_database_is_unreachable(monkeypatch, capsys):
    def unreachable(dsn):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "probe", unreachable)
    assert cli.main(["--dsn", "dbname=fafnir", "--check"]) == 1
    assert "cannot connect" in capsys.readouterr().err
