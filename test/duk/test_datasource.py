"""Unit tests for the duk data-source seam (no database)."""

from __future__ import annotations

import pandas as pd
import pytest

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


def test_shape_limit_daily_head():
    out = shape_price_dataframe(_daily_df(), frequency="day", limit=2)
    assert len(out) == 2
    assert out.index[0] == pd.Timestamp("2023-01-02")


def test_shape_weekly_resample_aggregates():
    out = shape_price_dataframe(_daily_df(), frequency="week")
    # All three days fall in the same ISO week -> one row, OHLC aggregated.
    assert len(out) == 1
    assert out.iloc[0]["open"] == 1
    assert out.iloc[0]["close"] == 3.5
    assert out.iloc[0]["volume"] == 60
