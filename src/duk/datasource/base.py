"""Data-source dispatch and shared shaping helpers."""

from __future__ import annotations

from typing import Optional

import pandas as pd


class DataSourceError(Exception):
    """Raised when a requested data source is unavailable or misconfigured."""


def resolve_source(requested: Optional[str], default_source: str) -> str:
    """Normalise the requested source ('live'|'db') against the configured default."""
    source = (requested or default_source or "live").lower()
    if source not in ("live", "db"):
        raise DataSourceError(f"Unknown source '{source}'. Use 'live' or 'db'.")
    return source


# Aggregation map shared by live and db frequency resampling.
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
_FREQ = {
    "day": None,
    "week": "W",
    "month": "ME",
    "quarter": "QE",
    "semi-annual": "6ME",
    "annual": "YE",
}


def shape_price_dataframe(
    df: pd.DataFrame,
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Resample to frequency, select fields, and apply limit.

    Mirrors the live (FMP) shaping so db-mode output is identical. Input is a
    date-indexed OHLCV DataFrame (ascending).

    The ``limit`` head/tail rule matches ``fmp_api.get_price_history`` exactly: keep
    the first ``limit`` rows only when a start date was given without an end date,
    otherwise keep the last ``limit`` rows. (This keys off the supplied date
    arguments, not the frequency.)
    """
    if df.empty:
        return df
    rule = _FREQ.get(frequency)
    if rule is not None:
        agg = {c: _AGG[c] for c in df.columns if c in _AGG}
        df = df.resample(rule).agg(agg).dropna(how="all")
    if fields:
        keep = [c for c in fields if c in df.columns]
        if keep:
            df = df[keep]
    if limit is not None and limit > 0:
        if start_date is not None and end_date is None:
            df = df.head(limit)
        else:
            df = df.tail(limit)
    return df
