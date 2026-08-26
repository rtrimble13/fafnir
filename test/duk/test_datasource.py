"""Unit tests for the duk data-source seam (no database)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from duk.datasource import db as ds_db
from duk.datasource.base import DataSourceError, resolve_source, shape_price_dataframe


def test_resolve_source_default():
    assert resolve_source(None, "db") == "db"
    assert resolve_source(None, "live") == "live"


def test_resolve_source_override():
    assert resolve_source("live", "db") == "live"
    assert resolve_source("DB", "live") == "db"


def test_resolve_source_invalid():
    with pytest.raises(DataSourceError):
        resolve_source("sql", "db")


def _daily_df():
    idx = pd.to_datetime(["2023-01-02", "2023-01-03", "2023-01-04"])
    return pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [2, 3, 4],
            "low": [0.5, 1.5, 2.5],
            "close": [1.5, 2.5, 3.5],
            "volume": [10, 20, 30],
        },
        index=idx,
    )


def test_shape_field_selection():
    out = shape_price_dataframe(_daily_df(), frequency="day", fields=["close"])
    assert list(out.columns) == ["close"]


def test_shape_limit_start_only_keeps_head():
    # start_date supplied, no end_date -> first `limit` rows (matches live).
    out = shape_price_dataframe(
        _daily_df(), frequency="day", limit=2, start_date="2023-01-02"
    )
    assert len(out) == 2
    assert out.index[0] == pd.Timestamp("2023-01-02")


def test_shape_limit_no_dates_keeps_tail():
    # No dates supplied -> last `limit` rows (matches live, not head-by-frequency).
    out = shape_price_dataframe(_daily_df(), frequency="day", limit=2)
    assert len(out) == 2
    assert out.index[-1] == pd.Timestamp("2023-01-04")
    assert out.index[0] == pd.Timestamp("2023-01-03")


def test_shape_limit_end_only_keeps_tail():
    # end_date supplied, no start_date -> last `limit` rows, even for daily.
    out = shape_price_dataframe(
        _daily_df(), frequency="day", limit=2, end_date="2023-01-04"
    )
    assert out.index[-1] == pd.Timestamp("2023-01-04")


def test_shape_weekly_resample_aggregates():
    out = shape_price_dataframe(_daily_df(), frequency="week")
    # All three days fall in the same ISO week -> one row, OHLC aggregated.
    assert len(out) == 1
    assert out.iloc[0]["open"] == 1
    assert out.iloc[0]["close"] == 3.5
    assert out.iloc[0]["volume"] == 60


class _FakeCursor:
    """Records executed statements; resolves any symbol to security_id 1."""

    def __init__(self, rows=None):
        self.calls: list[tuple[str, list]] = []
        self.rows = rows or []

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))

    def fetchone(self):
        return {"security_id": 1}

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_price_history(monkeypatch, rows=None, **kwargs):
    cur = _FakeCursor(rows)
    monkeypatch.setattr(ds_db, "_connect", lambda dsn: _FakeConn(cur))
    df = ds_db.price_history(dsn="fake", symbol="aapl", **kwargs)
    return cur, df


def test_price_history_parses_string_start_date_with_limit(monkeypatch):
    # Regression: string start_date reached get_api_date_range's date arithmetic
    # and raised "can only concatenate str (not datetime.timedelta) to str".
    cur, df = _run_price_history(
        monkeypatch, start_date="1990-01-01", end_date=None, limit=5
    )
    assert df.empty
    where_params = cur.calls[-1][1]
    # security_id, start, end -- start/end must be dates, not strings. The end is
    # start + an 18-day window (the span that holds 5 daily bars).
    assert where_params[1] == date(1990, 1, 1)
    assert where_params[2] == date(1990, 1, 19)


def test_price_history_parses_string_end_date_with_limit(monkeypatch):
    cur, _ = _run_price_history(
        monkeypatch, start_date=None, end_date="2023-12-31", limit=10
    )
    where_params = cur.calls[-1][1]
    assert where_params[1] == date(2023, 12, 6)  # end - 25-day window for 10 bars
    assert where_params[2] == date(2023, 12, 31)


def test_price_history_rejects_malformed_date(monkeypatch):
    with pytest.raises(DataSourceError, match="expected YYYY-MM-DD"):
        _run_price_history(monkeypatch, start_date="01/01/1990", end_date=None, limit=5)


def _bar(day: int) -> dict:
    return {
        "date": date(1990, 1, day),
        "open": 1,
        "high": 2,
        "low": 0.5,
        "close": 1.5,
        "volume": 10,
    }


def test_price_history_limit_counts_trading_bars(monkeypatch):
    # `-n 5` means 5 bars, not 5 calendar days: Jan 1 1990 is a holiday and two
    # weekends fall inside the first fortnight, so the query window has to be wide
    # enough to still yield five rows after the head(limit) trim.
    trading_days = [2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19]
    cur, df = _run_price_history(
        monkeypatch,
        rows=[_bar(d) for d in trading_days],
        start_date="1990-01-01",
        end_date=None,
        limit=5,
    )

    assert len(df) == 5
    assert df.index[0] == pd.Timestamp("1990-01-02")
    assert df.index[-1] == pd.Timestamp("1990-01-08")
    # The fetched window spans more calendar days than the bar count.
    window_start, window_end = cur.calls[-1][1][1], cur.calls[-1][1][2]
    assert (window_end - window_start).days > 5
