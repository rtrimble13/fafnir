"""
Live data source: the FMP API. Thin adapters over the carried-over fmp_api
functions so the CLI can dispatch uniformly. This preserves duk's original
standalone behaviour.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from duk.fmp_api import get_price_history
from duk.ls_utils import screen_securities


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
    )


def screen(*, api_key: str, **kwargs) -> pd.DataFrame:
    return screen_securities(api_key=api_key, **kwargs)
