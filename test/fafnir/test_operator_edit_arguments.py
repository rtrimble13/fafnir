"""`fafnir actions add` rejects a malformed --split or --dividend before it opens a
database, and it has to do so as a usage error rather than a traceback.

click.BadParameter's second positional argument is the click Context, not the
parameter hint. Passing the hint there raised AttributeError from inside click's
own constructor, so `--split 0:1` and `--dividend abc` crashed instead of saying
what was wrong. The hint has to go by keyword.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from fafnir import cli


class _NoDB:
    """Stands in for Database: these commands must fail before they reach it."""

    def __init__(self, dsn):
        raise AssertionError("argument validation must not open a connection")


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(cli, "Database", _NoDB)
    return CliRunner()


def _add(runner, *extra):
    return runner.invoke(
        cli.main,
        ["actions", "add", "--security-id", "1", "--ex-date", "2024-06-05", "-m", "x"]
        + list(extra),
        catch_exceptions=False,
    )


@pytest.mark.parametrize("ratio", ["0:1", "2:0", "-2:1"])
def test_a_non_positive_split_is_a_usage_error(runner, ratio):
    out = _add(runner, "--split", ratio)
    assert out.exit_code == 2, out.output
    assert "Invalid value for --split" in out.output
    assert "must be positive" in out.output


def test_a_split_that_is_not_n_colon_d_is_a_usage_error(runner):
    out = _add(runner, "--split", "2-for-1")
    assert out.exit_code == 2, out.output
    assert "Invalid value for --split" in out.output


def test_a_dividend_that_is_not_a_number_is_a_usage_error(runner):
    out = _add(runner, "--dividend", "abc")
    assert out.exit_code == 2, out.output
    assert "Invalid value for --dividend" in out.output


def _split(runner, *extra):
    return runner.invoke(
        cli.main,
        ["security", "split-history", "--security-id", "1", "-m", "x"] + list(extra),
        catch_exceptions=False,
    )


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--new-symbol", "BID", "--new-name", "x"], "--to is required"),
        (["--to", "2020-01-01"], "exactly one destination"),
        (
            ["--to", "2020-01-01", "--new-symbol", "B", "--into-security-id", "2"],
            "exactly one destination",
        ),
        (
            ["--to", "2020-01-01", "--into-security-id", "2", "--new-name", "x"],
            "do not apply with --into-security-id",
        ),
        (["--undo"], "--undo needs"),
        (["--undo", "--into-security-id", "2", "--to", "2020-01-01"], "--undo reads"),
        # --restore-deleted was accepted and silently ignored under --undo, although
        # every other option that describes a split is rejected there.
        (
            ["--undo", "--into-security-id", "2", "--restore-deleted"],
            "--undo reads",
        ),
        # --asset-type describes a minted destination, so it cannot apply to one that
        # already exists. It carries a default, so the check compares against it.
        (
            [
                "--to",
                "2020-01-01",
                "--into-security-id",
                "2",
                "--asset-type",
                "etf",
            ],
            "do not apply with --into-security-id",
        ),
    ],
)
def test_split_history_rejects_an_incoherent_request_before_the_database(
    runner, extra, message
):
    out = _split(runner, *extra)
    assert out.exit_code == 1, out.output
    assert message in out.output
