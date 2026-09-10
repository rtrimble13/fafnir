"""What a mutating command reports must already be committed (no database).

The connection commits when its `with` block exits cleanly. A command that prints
its summary from inside that block therefore prints *before* committing, and any
reader that stops early turns the next write to stdout into an exception -- which
exits the block through the rollback path. On 2026-09-10
`fafnir dq resolve ... | head -2` printed "Resolved 77 flags" and then undid all
77 when `head` closed the pipe on the third line.

These fake the connection and the repository so the order of events is visible:
the write, then the commit, then the first line of output.
"""

from __future__ import annotations

import types

import click
import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo

_OBJ = {"config": types.SimpleNamespace(dsn="unused")}


@pytest.fixture()
def log(monkeypatch):
    """The ordered record of writes, commits and output lines."""
    events: list = []

    class _FakeDatabase:
        def __init__(self, dsn):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            # The real connection commits here on a clean exit, rolls back
            # otherwise; either is too late for output already printed.
            events.append("exit" if exc_type is None else "rollback")
            return False

        def commit(self):
            events.append("commit")

    monkeypatch.setattr(cli, "Database", _FakeDatabase)
    monkeypatch.setattr(
        click, "echo", lambda message=None, *a, **kw: events.append(("echo", message))
    )
    return events


def _first_output(events) -> int:
    return next(i for i, e in enumerate(events) if isinstance(e, tuple))


def _fake_resolve(monkeypatch, events):
    monkeypatch.setattr(
        repo,
        "dq_flag_totals",
        lambda db, filt=None: {"flags": 2, "securities": 1, "checks": 1},
    )

    def resolve(db, filt, *, note=None, resolved_by=None):
        events.append("resolve")
        return list(filt.flag_ids)

    monkeypatch.setattr(repo, "resolve_dq_flags", resolve)


def test_resolve_commits_before_it_reports(monkeypatch, log):
    _fake_resolve(monkeypatch, log)

    result = CliRunner().invoke(
        cli.dq_resolve, ["1", "2", "-m", "why", "--by", "tester"], obj=_OBJ
    )

    assert result.exit_code == 0, result.output
    assert log.index("resolve") < log.index("commit") < _first_output(log)


def test_a_reader_that_stops_early_does_not_undo_the_resolve(monkeypatch, log):
    """`| head -2`: the third line of output raises, and the block exits dirty."""
    _fake_resolve(monkeypatch, log)
    printed: list = []

    def echo(message=None, *a, **kw):
        if len(printed) == 2:
            raise BrokenPipeError
        printed.append(message)
        log.append(("echo", message))

    monkeypatch.setattr(click, "echo", echo)

    result = CliRunner().invoke(
        cli.dq_resolve, ["1", "2", "-m", "why", "--by", "tester"], obj=_OBJ
    )

    assert isinstance(result.exception, BrokenPipeError)
    assert log[-1] == "rollback", "the pipe did break mid-summary"
    assert "commit" in log, "but the resolve was already durable"
    assert log.index("commit") < _first_output(log)


def test_reopen_commits_before_it_reports(monkeypatch, log):
    def reopen(db, flag_ids):
        log.append("reopen")
        return list(flag_ids), []

    monkeypatch.setattr(repo, "reopen_dq_flags", reopen)

    result = CliRunner().invoke(cli.dq_reopen, ["7"], obj=_OBJ)

    assert result.exit_code == 0, result.output
    assert log.index("reopen") < log.index("commit") < _first_output(log)
