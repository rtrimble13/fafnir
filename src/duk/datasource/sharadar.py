"""
Sharadar data source (``-S sharadar``): the vendor's table API, read directly.

A thin adapter over ``duk.sharadar_api`` so the CLI dispatches uniformly across
``db``, ``fmp`` and ``sharadar``. Only price history is served from here: Sharadar's
TICKERS table could list securities, but the screener's attributes (market cap,
beta) come from its DAILY table and the company summary describes the warehouse,
so ``ls`` and ``yc`` stay on the other two sources.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from duk.sharadar_api import get_price_history


def price_history(
    *,
    api_key: str,
    symbol: str,
    start_date: Optional[str],
    end_date: Optional[str],
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[list[str]] = None,
    adjusted: bool = False,
    base_url: Optional[str] = None,
) -> pd.DataFrame:
    return get_price_history(
        api_key=api_key,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        limit=limit,
        fields=fields,
        adjusted=adjusted,
        base_url=base_url,
    )
