"""
Financial Modeling Prep (FMP) source client.

Uses the ``stable/`` endpoints already proven against the Professional plan in
the original ``duk`` codebase, plus profile / splits / dividends needed for the
security master and corporate actions. Each method returns the parsed payload;
``self.bytes_downloaded`` and ``self.request_count`` accumulate for lineage and
the 50 GB/month bandwidth budget.

NOTE ON ENDPOINTS: the split/dividend stable paths below should be confirmed
against a live response for a known symbol before the first production backfill
(see doc/ingestion.md). They are isolated here as constants so a correction is a
one-line change.
"""

from __future__ import annotations

from typing import Any, Optional

from fafnir.sources.base import BaseSource, SourceError, payload_hash

BASE_STABLE = "https://financialmodelingprep.com/stable"

__all__ = ["FMPClient", "SourceError", "payload_hash", "BASE_STABLE"]


class FMPClient(BaseSource):
    name = "fmp"

    # Endpoint paths (relative to BASE_STABLE), centralized for easy correction.
    EP_STOCK_LIST = "stock-list"
    EP_ETF_LIST = "etf-list"
    EP_PROFILE = "profile"
    EP_EOD_FULL = "historical-price-eod/full"
    EP_SPLITS = "splits"
    EP_DIVIDENDS = "dividends"
    EP_SECTORS = "available-sectors"
    EP_INDUSTRIES = "available-industries"
    EP_SCREENER = "company-screener"

    def __init__(self, api_key: str, rate_per_min: int = 280, **kwargs):
        if not api_key:
            raise ValueError("FMP API key is required")
        super().__init__(rate_per_min=rate_per_min, **kwargs)
        self._api_key = api_key

    def _call(self, endpoint: str, params: dict | None = None) -> tuple[Any, int, int]:
        url = f"{BASE_STABLE}/{endpoint}"
        merged = dict(params or {})
        merged["apikey"] = self._api_key
        return self._get(url, merged)

    # -- security master ----------------------------------------------------
    def stock_list(self) -> list[dict]:
        data, _, _ = self._call(self.EP_STOCK_LIST)
        return data if isinstance(data, list) else []

    def etf_list(self) -> list[dict]:
        data, _, _ = self._call(self.EP_ETF_LIST)
        return data if isinstance(data, list) else []

    def profile(self, symbol: str) -> Optional[dict]:
        data, _, _ = self._call(self.EP_PROFILE, {"symbol": symbol})
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
        return None

    # -- prices -------------------------------------------------------------
    def eod_full(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list[dict]:
        """Raw daily OHLCV. Returns list of bar dicts (date, open, high, low,
        close, volume, vwap, ...)."""
        params: dict[str, Any] = {"symbol": symbol}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data, _, _ = self._call(self.EP_EOD_FULL, params)
        if isinstance(data, dict) and "historical" in data:
            return data["historical"]
        return data if isinstance(data, list) else []

    # -- corporate actions --------------------------------------------------
    def splits(self, symbol: str) -> list[dict]:
        data, _, _ = self._call(self.EP_SPLITS, {"symbol": symbol})
        if isinstance(data, dict) and "historical" in data:
            return data["historical"]
        return data if isinstance(data, list) else []

    def dividends(self, symbol: str) -> list[dict]:
        data, _, _ = self._call(self.EP_DIVIDENDS, {"symbol": symbol})
        if isinstance(data, dict) and "historical" in data:
            return data["historical"]
        return data if isinstance(data, list) else []

    # -- taxonomy -----------------------------------------------------------
    def sectors(self) -> list[str]:
        data, _, _ = self._call(self.EP_SECTORS)
        return _flatten_names(data, "sector")

    def industries(self) -> list[str]:
        data, _, _ = self._call(self.EP_INDUSTRIES)
        return _flatten_names(data, "industry")


def _flatten_names(data: Any, key: str) -> list[str]:
    """FMP sometimes returns ['Technology', ...] and sometimes [{'sector': ...}]."""
    out: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                val = item.get(key) or next(iter(item.values()), None)
                if val:
                    out.append(str(val))
    return out
