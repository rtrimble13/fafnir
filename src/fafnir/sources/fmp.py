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

    # Screener rows per request. Confirmed to return a full 5000-row page, but
    # 1000 keeps a single page small enough to retry cheaply.
    SCREENER_PAGE_SIZE = 1000

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

    def company_screener(
        self,
        *,
        exchange: Optional[str] = None,
        country: Optional[str] = None,
        is_etf: Optional[bool] = None,
        max_pages: int = 60,
    ) -> list[dict]:
        """Bulk company rows, paged until a short page comes back.

        The stable ``stock-list`` / ``etf-list`` payloads carry only ``symbol``
        and a name -- no exchange, no country. The screener is the one bulk
        endpoint that also returns ``exchangeShortName``, ``country`` and the
        ``isEtf`` / ``isFund`` flags, so the US universe has to be built from it.
        """
        out: list[dict] = []
        prev_first: Optional[str] = None
        for page in range(max_pages):
            params: dict[str, Any] = {"limit": self.SCREENER_PAGE_SIZE, "page": page}
            if exchange:
                params["exchange"] = exchange
            if country:
                params["country"] = country
            if is_etf is not None:
                params["isEtf"] = "true" if is_etf else "false"
            data, _, _ = self._call(self.EP_SCREENER, params)
            if not isinstance(data, list) or not data:
                break
            # A server that ignores `page` hands back the same slice forever;
            # stop on a repeat rather than re-downloading it max_pages times.
            first = data[0].get("symbol") if isinstance(data[0], dict) else None
            if first is not None and first == prev_first:
                break
            prev_first = first
            out.extend(data)
            if len(data) < self.SCREENER_PAGE_SIZE:
                break
        return out

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
