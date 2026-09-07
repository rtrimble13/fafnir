"""
Live data source: the FMP API. Thin adapters over the carried-over fmp_api
functions so the CLI can dispatch uniformly. This preserves duk's original
standalone behaviour.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from duk import identifiers
from duk.fmp_api import get_price_history, identifier_search_api
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


def resolve_identifier(
    *, api_key: str, identifier: identifiers.Identifier
) -> list[dict]:
    """Resolve ``cik:``/``isin:``/``cusip:`` to candidate securities, vendor-side.

    Shaped like the db path's candidates (``symbol``, ``company_name``, …) so the
    CLI renders one did-you-mean table regardless of source. The warehouse fields
    the vendor does not return are absent rather than invented: `exchange_code` may
    be there under FMP's spelling, `is_actively_trading` is not, and a candidate
    table that claims "active" for every row because the key was missing would be
    stating something the live API never said.
    """
    rows = identifier_search_api(identifier.scheme, identifier.value, api_key)
    candidates = []
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        candidates.append(
            {
                "security_id": None,
                "symbol": str(symbol).upper(),
                "company_name": row.get("companyName") or row.get("name"),
                "exchange_code": row.get("exchangeShortName") or row.get("exchange"),
                "exchange_name": row.get("exchangeFullName"),
                "is_actively_trading": row.get("isActivelyTrading"),
                "delisted_date": None,
            }
        )
    return candidates
