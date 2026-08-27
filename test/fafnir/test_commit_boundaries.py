"""A long load must not be all-or-nothing.

Before these boundaries existed, Database.__exit__ rolled back the whole
connection on any exception, so a backfill that raised at hour three discarded
every watermark, every landed payload and the run record itself -- and re-spent
the FMP bandwidth on the retry. initial_backfill.sh advertised resumability that
the code did not provide.
"""

from __future__ import annotations

import types
from datetime import date

import pytest

import fafnir.ingest.adjustments as adj
import fafnir.ingest.daily_price as dp
from fafnir.ingest.daily_price import load_prices
from fafnir.ingest.runlog import RunLog


class _FakeDB:
    """Models durability: writes are pending until commit() makes them permanent."""

    def __init__(self):
        self.pending: list[str] = []
        self.durable: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def write(self, item: str) -> None:
        self.pending.append(item)

    def commit(self) -> None:
        self.durable.extend(self.pending)
        self.pending.clear()
        self.commits += 1

    def rollback(self) -> None:
        self.pending.clear()
        self.rollbacks += 1

    def fetchone(self, sql, params=None):
        return {"ingestion_run_id": 7}

    def execute(self, sql, params=None):
        self.write("runlog-update")
        return 1


class _FMP:
    bytes_downloaded = 0


def test_runlog_open_row_is_durable_immediately():
    # Until the open row commits, no other session can see the run -- nothing can
    # monitor a backfill in flight.
    db = _FakeDB()
    with RunLog(db, source="fmp", endpoint="x") as run:
        assert db.commits == 1
        assert run.run_id == 7


def test_failed_run_still_records_its_status():
    db = _FakeDB()
    with pytest.raises(RuntimeError):
        with RunLog(db, source="fmp", endpoint="x"):
            db.write("half-done work")
            raise RuntimeError("boom")

    # The aborted transaction is cleared so the status UPDATE can go through...
    assert db.rollbacks == 1
    assert "half-done work" not in db.durable
    # ...and the record that the run failed survives Database.__exit__'s rollback.
    assert "runlog-update" in db.durable


def test_a_crash_mid_loop_keeps_every_earlier_symbol(monkeypatch):
    class _Ctx:
        def __enter__(self):
            return types.SimpleNamespace(run_id=1, rows_quarantined=0)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dp, "RunLog", lambda *a, **kw: _Ctx())

    def flaky(db, fmp, symbol, *, run, stats=None, **kw):
        if symbol == "BOOM":
            raise RuntimeError("network died")
        db.write(f"bars:{symbol}")
        stats["bars"] = stats.get("bars", 0) + 10
        return 10

    monkeypatch.setattr(dp, "load_symbol_prices", flaky)

    db = _FakeDB()
    with pytest.raises(RuntimeError):
        load_prices(
            db, _FMP(), ["AAPL", "MSFT", "BOOM", "NVDA"], start_date=date(1990, 1, 1)
        )

    assert db.durable == ["bars:AAPL", "bars:MSFT"]
    assert db.commits == 2


def test_adjust_all_commits_each_security_and_survives_a_bad_one(monkeypatch):
    """Step 5 had no boundary at all: `fafnir adjust` recomputed all 21,106
    securities inside one transaction, so the first security whose factors would not
    store both ended the backfill and discarded every factor computed before it.
    """
    monkeypatch.setattr(adj.repo, "securities_with_actions", lambda db: [1, 2, 3])

    def flaky(db, security_id):
        if security_id == 2:
            raise ValueError("numeric field overflow")
        db.write(f"factors:{security_id}")

    monkeypatch.setattr(adj, "compute_for_security", flaky)
    monkeypatch.setattr(
        adj.repo,
        "add_dq_flag",
        lambda db, **kw: db.write(f"flag:{kw['check_name']}:{kw['security_id']}"),
    )

    db = _FakeDB()
    result = adj.adjust_all(db)

    assert result == {"securities": 2, "failed": 1, "aborted": False}
    # Security 1's factors are durable even though 2 blew up after them, and 3 was
    # still computed after the failure.
    assert db.durable == ["factors:1", "flag:adjustment_failed:2", "factors:3"]
    assert db.rollbacks == 1


def test_adjust_all_stops_once_it_is_clear_nothing_will_succeed(monkeypatch):
    """A systemic failure is not worth 21,106 error flags.

    With the migration unapplied (or a lock on the table) every security fails for
    the same reason. Grinding through the universe writes one committed
    `adjustment_failed` flag per security, which buries the genuine flags and
    permanently inflates the open-flag count `fafnir status` reports.
    """
    monkeypatch.setattr(
        adj.repo, "securities_with_actions", lambda db: list(range(500))
    )

    def always_fails(db, security_id):
        raise ValueError("numeric field overflow")

    monkeypatch.setattr(adj, "compute_for_security", always_fails)
    monkeypatch.setattr(
        adj.repo, "add_dq_flag", lambda db, **kw: db.write(f"flag:{kw['security_id']}")
    )

    db = _FakeDB()
    result = adj.adjust_all(db)

    assert result["aborted"] is True
    assert result["failed"] == adj.EARLY_ABORT_FAILURES
    assert len(db.durable) == adj.EARLY_ABORT_FAILURES


def test_one_late_success_keeps_the_run_going(monkeypatch):
    """The abort is only for a run that has not managed a single success."""
    monkeypatch.setattr(
        adj.repo, "securities_with_actions", lambda db: list(range(200))
    )

    def flaky(db, security_id):
        if security_id % 2:
            raise ValueError("bad data")
        db.write(f"factors:{security_id}")

    monkeypatch.setattr(adj, "compute_for_security", flaky)
    monkeypatch.setattr(adj.repo, "add_dq_flag", lambda db, **kw: None)

    db = _FakeDB()
    result = adj.adjust_all(db)

    assert result["aborted"] is False
    assert result["securities"] == 100
    assert result["failed"] == 100
