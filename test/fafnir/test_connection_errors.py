"""
The connection failure surface.

libpq's ``fe_sendauth: no password supplied`` is accurate and unhelpful at once:
it names a symptom whose cause is a ``host`` value in a file the message never
mentions, and it arrived as a psycopg traceback because every command in the CLI
ends in ``with Database(cfg.dsn)``.

The redaction tests are the load-bearing ones. A diagnostic that prints the DSN is
only an improvement while it cannot print a password into a terminal, a log file,
or a paste into a bug report.
"""

from __future__ import annotations

import pytest

from fafnir.db.connection import (
    DatabaseConnectionError,
    _connection_error,
    _diagnose,
    redact_dsn,
)

NO_PASSWORD = (
    'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
    "fe_sendauth: no password supplied"
)


# -- redaction --------------------------------------------------------------
@pytest.mark.parametrize(
    "dsn, secret",
    [
        ("host=x dbname=fafnir password=hunter2 user=u", "hunter2"),
        ("postgresql://fafnir_ingest:s3cr3t@db.example:5432/fafnir", "s3cr3t"),
        ("host=x password='quoted secret' user=u", "quoted secret"),
        ("HOST=x PASSWORD=Upper123 user=u", "Upper123"),
        ("host=x password=trailing", "trailing"),
    ],
)
def test_redaction_removes_the_secret(dsn, secret):
    out = redact_dsn(dsn)
    assert secret not in out, f"leaked {secret!r} in {out!r}"
    assert "***" in out


def test_redaction_leaves_a_passwordless_dsn_alone():
    dsn = "host=/var/run/postgresql port=5432 dbname=fafnir user=fafnir_ingest"
    assert redact_dsn(dsn) == dsn


def test_the_error_message_never_carries_the_password():
    """The whole diagnostic, not just the dsn line."""
    dsn = "host=localhost dbname=fafnir user=u password=hunter2"
    message = str(_connection_error(dsn, Exception(NO_PASSWORD)))
    assert "hunter2" not in message
    assert "password=***" in message


# -- the hints --------------------------------------------------------------
def test_tcp_host_with_no_password_is_pointed_at_the_socket(monkeypatch):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    hints = " ".join(_diagnose("host=localhost user=fafnir_ingest", NO_PASSWORD))
    assert "/var/run/postgresql" in hints
    assert "peer auth" in hints


def test_socket_host_is_not_told_to_use_the_socket():
    """host=/var/run/postgresql is already the advice; repeating it is noise."""
    hints = " ".join(_diagnose("host=/var/run/postgresql user=u", NO_PASSWORD))
    assert "is a TCP host" not in hints


def test_fafnir_dsn_set_surfaces_the_documented_gotcha(monkeypatch):
    """FAFNIR_DSN is used verbatim, so FAFNIR_DB_PASSWORD is silently ignored.

    install_hetzner.md 4.4 gives this its own callout because it is the
    combination that fails while looking correct.
    """
    monkeypatch.setenv("FAFNIR_DSN", "host=db user=u")
    hints = " ".join(_diagnose("host=db user=u", NO_PASSWORD))
    assert "ignored" in hints
    assert "PGPASSWORD" in hints


@pytest.mark.parametrize(
    "message, expected",
    [
        ("connection refused", "Nothing is listening"),
        ('FATAL: database "fafnir" does not exist', "database is missing"),
        ('FATAL: role "fafnir_ingest" does not exist', "role is missing"),
        ("FATAL: Peer authentication failed for user", "pg_hba.conf"),
    ],
)
def test_each_failure_mode_gets_its_own_hint(message, expected):
    assert expected in " ".join(_diagnose("host=localhost user=u", message))


def test_an_unrecognised_failure_still_reports_cleanly():
    """No hint is fine; a message with no dsn or provenance is not."""
    text = str(_connection_error("host=x user=u", Exception("something novel")))
    assert "cannot connect" in text
    assert "host=x user=u" in text
    assert "from  :" in text


# -- the CLI surface --------------------------------------------------------
def test_cli_reports_connection_failure_without_a_traceback(tmp_path, monkeypatch):
    """One line per subcommand, via the group's invoke -- not ten click frames."""
    from click.testing import CliRunner

    from fafnir.cli import main

    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[general]\nlog_dir = "{tmp_path / "log"}"\n'
        '[database]\nhost = "127.0.0.1"\nport = 1\ndbname = "fafnir"\n'
        'user = "fafnir_ingest"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_LOG_DIR", raising=False)
    result = CliRunner().invoke(main, ["-c", str(rc), "status"])
    assert result.exit_code == 1
    assert "cannot connect to the fafnir database" in result.output
    assert "Traceback" not in result.output


def test_the_error_type_is_not_swallowed_by_the_generic_handler():
    """DatabaseConnectionError must stay distinguishable from any RuntimeError."""
    assert issubclass(DatabaseConnectionError, RuntimeError)
    assert DatabaseConnectionError is not RuntimeError


def test_cli_names_the_actual_config_file_not_a_guess(tmp_path, monkeypatch):
    """Under `-c`, provenance must name that file.

    connection.py can only inspect the environment, so it guesses '~/.fafnirrc'.
    The CLI holds the config and corrects it; without that wiring the diagnostic
    points at a file the operator is not using.
    """
    from click.testing import CliRunner

    from fafnir.cli import main

    rc = tmp_path / "custom.toml"
    rc.write_text(
        f'[general]\nlog_dir = "{tmp_path / "log"}"\n'
        '[database]\nhost = "127.0.0.1"\nport = 1\ndbname = "fafnir"\n'
        'user = "fafnir_ingest"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_LOG_DIR", raising=False)
    result = CliRunner().invoke(main, ["-c", str(rc), "status"])
    assert str(rc) in result.output
    assert "~/.fafnirrc" not in result.output
