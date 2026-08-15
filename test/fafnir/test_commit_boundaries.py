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
