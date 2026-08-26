"""Console logging is opt-in: records reach the terminal only under --verbose."""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.logging_config import enable_console_logging, setup_logging

# The formatter stamps every record with " - duk - <LEVEL> - ", which is what
# distinguishes a log line from the command's own click.echo output.
LOG_LINE_MARKER = " - duk - "


def _console_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


@pytest.fixture
def log_dir(tmp_path):
    """Keep test logs out of the repo's var/duk/log."""
    return str(tmp_path / "log")


def test_setup_logging_can_skip_the_console(log_dir):
    logger = setup_logging(log_dir=log_dir, console_output=False)

    assert _console_handlers(logger) == []
    # The file handler is still there -- quiet means unlogged nowhere else.
    assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)


def test_enable_console_logging_attaches_a_handler(log_dir):
    logger = setup_logging(log_dir=log_dir, console_output=False)

    handler = enable_console_logging(logger)

    assert _console_handlers(logger) == [handler]


def test_console_handler_inherits_the_configured_level(log_dir):
    # `log_level = debug` in ~/.dukrc should give a debug console, not an INFO one
    # clamped by --verbose.
    logger = setup_logging(log_level="debug", log_dir=log_dir, console_output=False)

    handler = enable_console_logging(logger)

    assert handler.level == logging.DEBUG
    assert logger.level == logging.DEBUG


def test_console_handler_accepts_an_explicit_level(log_dir):
    logger = setup_logging(log_dir=log_dir, console_output=False)

    handler = enable_console_logging(logger, level=logging.ERROR)

    assert handler.level == logging.ERROR


class TestCliConsoleOutput:
    """End-to-end through a command that needs no database or API key."""

    @staticmethod
    def _run(tmp_path, *args):
        prices = tmp_path / "prices.csv"
        prices.write_text("date,close\n2023-01-02,1\n2023-01-03,2\n2023-01-04,3\n")
        dukrc = tmp_path / "dukrc"
        dukrc.write_text(f'[general]\nlog_dir = "{tmp_path / "log"}"\n')

        return CliRunner().invoke(
            main,
            ["--config", str(dukrc), "ti", "sma", "-i", str(prices), "-c", "close"]
            + list(args),
        )

    def test_quiet_by_default(self, tmp_path):
        result = self._run(tmp_path, "-w", "2")

        assert result.exit_code == 0
        assert LOG_LINE_MARKER not in result.stderr
        # The data itself still goes to stdout.
        assert "close" in result.stdout

    def test_verbose_prints_log_records(self, tmp_path):
        result = self._run(tmp_path, "-w", "2", "-v")

        assert result.exit_code == 0
        assert LOG_LINE_MARKER in result.stderr
        assert "Computing SMA" in result.stderr

    def test_errors_reach_the_console_without_verbose(self, tmp_path):
        result = self._run(tmp_path, "-w", "0")

        assert result.exit_code == 1
        assert "Error: Window must be greater than 0" in result.stderr
        assert LOG_LINE_MARKER not in result.stderr

    def test_nan_warning_reaches_the_console_without_verbose(self, tmp_path):
        # A window wider than the data yields an all-NaN column; that must not be
        # a silent surprise now that logged warnings no longer print.
        result = self._run(tmp_path, "-w", "99")

        assert "Warning:" in result.stderr
        assert "will be NaN" in result.stderr
        assert LOG_LINE_MARKER not in result.stderr
