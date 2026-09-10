"""
Unit tests for where a symbol's first price load starts.

A symbol with no watermark used to be fetched with no start date, which FMP answers
with its own default window of about five years -- not the whole history. GBF, DDI
and MRT, loaded after the initial backfill without ``--from``, hold histories that
start 2021-08-30..09-02 while their dividends go back decades (255 open
dividend_no_prior_close flags on the rows cut short this way).
"""

from __future__ import annotations

from datetime import date

import fafnir.ingest.daily_price as dp
from fafnir.ingest.daily_price import _window_start, load_prices

_FLOOR = date(1990, 1, 1)


def test_an_explicit_window_wins():
    assert _window_start(date(2020, 1, 1), date(2026, 9, 8), 5, _FLOOR) == date(
        2020, 1, 1
    )


def test_a_watermark_resumes_with_the_overlap():
    assert _window_start(None, date(2026, 9, 8), 5, _FLOOR) == date(2026, 9, 3)


def test_a_first_load_starts_at_the_backfill_start():
    assert _window_start(None, None, 5, _FLOOR) == _FLOOR


def test_without_a_backfill_start_the_first_load_is_left_to_the_vendor():
    # The old behaviour, kept for callers that pass nothing.
    assert _window_start(None, None, 5, None) is None


class _DB:
    def commit(self):
        pass

    def fetchval(self, sql, params=None):
        return 0  # a fresh warehouse: the changeover guard has nothing to say


class _FMP:
    bytes_downloaded = 0


class _RunLogStub:
    def __init__(self, *a, **kw):
        self.run = type("R", (), {"run_id": 1, "rows_quarantined": 0})()

    def __enter__(self):
        return self.run

    def __exit__(self, *exc):
        return False


def test_load_prices_hands_the_backfill_start_to_every_symbol(monkeypatch):
    seen = []

    def fake(db, fmp, symbol, *, run, stats=None, **kw):
        seen.append((symbol, kw.get("start_date"), kw.get("backfill_start")))
        stats["bars"] = stats.get("bars", 0) + 1
        return 1

    monkeypatch.setattr(dp, "RunLog", _RunLogStub)
    monkeypatch.setattr(dp, "load_symbol_prices", fake)
    load_prices(_DB(), _FMP(), ["GBF", "DDI"], backfill_start=_FLOOR)
    assert seen == [("GBF", None, _FLOOR), ("DDI", None, _FLOOR)]
