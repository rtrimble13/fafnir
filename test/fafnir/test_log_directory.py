"""
The log directory is configured before every command, so it fails before every
command.

``setup_logging`` runs in ``cli.main``, ahead of the subcommand, which makes a bad
``log_dir`` the first thing a fresh install hits -- and it blocks read-only
commands like ``fafnir status`` just as hard as a write. Two things made that
worse than it needed to be, and both are asserted here:

* the default ``var/fafnir/log`` is **relative**, so the path in the error is not
  the path in the config file, and
* the failure arrived as a ``PermissionError`` traceback through click and
  pathlib rather than as a sentence naming the fix.
"""

from __future__ import annotations

import os

import pytest

from fafnir.config import FafnirConfig
from fafnir.logging_config import LogDirectoryError, setup_logging

# chmod-based refusal tests are meaningless as root: root writes anywhere.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses the directory permissions these assert on",
)


def test_happy_path_creates_the_directory(tmp_path):
    target = tmp_path / "logs"
    logger = setup_logging(log_dir=str(target), console_output=False)
    logger.info("hello")
    assert target.is_dir()
    assert (target / "fafnir.log").exists()


@skip_as_root
def test_unwritable_parent_raises_log_directory_error(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir(mode=0o500)
    try:
        with pytest.raises(LogDirectoryError) as excinfo:
            setup_logging(log_dir=str(parent / "logs"), console_output=False)
    finally:
        parent.chmod(0o700)  # so tmp_path cleanup can remove it
    assert "cannot use the log directory" in str(excinfo.value)


@skip_as_root
def test_existing_but_unwritable_directory_is_caught(tmp_path):
    """mkdir(exist_ok=True) succeeds on a directory owned by someone else.

    That is the common shape on a host: an operator creates /var/log/fafnir and
    the service user cannot write to it. Without the explicit access check the
    failure would surface later, from inside a log handler, mid-command.
    """
    target = tmp_path / "readonly"
    target.mkdir(mode=0o555)
    try:
        with pytest.raises(LogDirectoryError) as excinfo:
            setup_logging(log_dir=str(target), console_output=False)
    finally:
        target.chmod(0o700)
    assert "not writable" in str(excinfo.value)


@skip_as_root
def test_the_message_names_the_resolved_path_and_the_cwd(tmp_path, monkeypatch):
    """A relative log_dir reports where it actually resolved, and against what.

    This is the diagnostic that was missing: the user sees 'var/fafnir/log' in
    the traceback, greps the config for it, and finds exactly that -- with no
    indication that it landed under their home directory.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir(mode=0o500)
    monkeypatch.chdir(workdir)
    try:
        with pytest.raises(LogDirectoryError) as excinfo:
            setup_logging(log_dir="var/fafnir/log", console_output=False)
    finally:
        workdir.chmod(0o700)
    message = str(excinfo.value)
    assert "var/fafnir/log" in message  # what was configured
    assert str(workdir) in message  # and where that actually pointed
    assert "relative to cwd" in message
    assert "FAFNIR_LOG_DIR" in message  # and the fix


@skip_as_root
def test_absolute_path_message_omits_the_cwd_line(tmp_path):
    """The cwd is only relevant when the value was relative; saying it otherwise
    is noise pointing at an innocent directory."""
    target = tmp_path / "nope"
    target.mkdir(mode=0o555)
    try:
        with pytest.raises(LogDirectoryError) as excinfo:
            setup_logging(log_dir=str(target), console_output=False)
    finally:
        target.chmod(0o700)
    assert "relative to cwd" not in str(excinfo.value)


def test_env_var_overrides_the_config_file(tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text('[general]\nlog_dir = "/from/the/file"\n')
    cfg = FafnirConfig(str(rc))
    assert cfg.log_dir == "/from/the/file"

    monkeypatch.setenv("FAFNIR_LOG_DIR", "/from/the/env")
    assert FafnirConfig(str(rc)).log_dir == "/from/the/env"


def test_blank_env_var_does_not_override(tmp_path, monkeypatch):
    """An exported-but-empty FAFNIR_LOG_DIR is not a configured value.

    `set -a; . /etc/fafnir/fafnir.env` exports whatever is in the file, and a
    commented-out or emptied line must not silently become the log directory --
    which, as an empty string, would disable file logging entirely.
    """
    rc = tmp_path / "fafnirrc"
    rc.write_text('[general]\nlog_dir = "/from/the/file"\n')
    monkeypatch.setenv("FAFNIR_LOG_DIR", "   ")
    assert FafnirConfig(str(rc)).log_dir == "/from/the/file"


@skip_as_root
def test_cli_reports_it_as_one_line_not_a_traceback(tmp_path, monkeypatch):
    """`fafnir status` must exit 1 with a message, not raise through click."""
    from click.testing import CliRunner

    from fafnir.cli import main

    target = tmp_path / "ro"
    target.mkdir(mode=0o555)
    monkeypatch.setenv("FAFNIR_LOG_DIR", str(target))
    try:
        result = CliRunner().invoke(main, ["status"])
    finally:
        target.chmod(0o700)
    assert result.exit_code == 1
    assert "cannot use the log directory" in result.output
    assert "Traceback" not in result.output
