"""`fafnir adjust` has to tell a scheduler the truth about what it did.

Step 5 of the backfill, and the nightly job, both run this command under
`set -euo pipefail`. Two ways that goes wrong quietly: a run where every security
failed still exiting 0, and a mistyped `--symbol` falling through to "all".
"""

from __future__ import annotations

import types

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import adjustments


class _NoDB:
    """Stands in for Database: the command's DB work is monkeypatched out."""

    def __init__(self, dsn):
        self.dsn = dsn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(cli, "Database", _NoDB)
    return CliRunner()


def _invoke(runner, args=()):
    return runner.invoke(
        cli.adjust, list(args), obj={"config": types.SimpleNamespace(dsn="dsn")}
    )


def test_a_handful_of_failures_still_exits_zero(runner, monkeypatch):
    """A few bad securities are flagged and stepped over -- the backfill goes on."""
    monkeypatch.setattr(
        adjustments,
        "adjust_all",
        lambda db, security_id=None: {"securities": 999, "failed": 1},
    )

    result = _invoke(runner)

    assert result.exit_code == 0
    assert "999 securities" in result.output
    # Not "left without factors": the failed transaction rolled back, so whatever the
    # last successful run wrote is still there -- stale, not absent.
    assert "kept the factors from their last successful run" in result.output


def test_a_universe_wide_failure_exits_nonzero(runner, monkeypatch):
    """Everything failing is the schema or a lock, not the data.

    Exiting 0 here would let `initial_backfill.sh` sail on to refresh a mart built on
    no factors at all, and a nightly cron report success while writing nothing.
    """
    monkeypatch.setattr(
        adjustments,
        "adjust_all",
        lambda db, security_id=None: {"securities": 0, "failed": 21106},
    )

    result = _invoke(runner)

    assert result.exit_code != 0
    assert "systemic" in result.output


def test_an_unknown_symbol_is_an_error_not_the_whole_universe(runner, monkeypatch):
    """resolve_security_id returns None for a typo, and None means 'all'."""
    monkeypatch.setattr(repo, "resolve_security_id", lambda db, symbol: None)
    called = []
    monkeypatch.setattr(
        adjustments,
        "adjust_all",
        lambda db, security_id=None: called.append(security_id)
        or {"securities": 0, "failed": 0},
    )

    result = _invoke(runner, ["--symbol", "aapl"])

    assert result.exit_code != 0
    assert "Unknown symbol AAPL" in result.output
    assert not called, "a mistyped ticker must not recompute the universe"
