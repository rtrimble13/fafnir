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

NOTE ON PRICE ADJUSTMENT: prices come from ``historical-price-eod/non-split-adjusted``,
NOT ``.../full``. FMP's ``full`` payload is already **split-adjusted** -- its ``close``
is adjusted for splits and its ``adjClose`` for splits *and* dividends. Storing ``full``
in ``core.daily_price`` and then applying fafnir's own split factor double-counts every
split: AAPL's 1990-01-02 close arrives as ~$0.35 (the true raw close ~$39.20 divided by
the 112:1 cumulative split since) and the adjustment routine drives it to ~$0.003.
``core.daily_price`` is defined as genuinely raw, so the unadjusted endpoint is the only
correct feed. See doc/adr/0004-unadjusted-price-feed.md.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from fafnir.logging_config import get_logger
from fafnir.sources.base import BaseSource, SourceError, payload_hash

logger = get_logger("source.fmp")

BASE_STABLE = "https://financialmodelingprep.com/stable"

__all__ = ["FMPClient", "SourceError", "payload_hash", "BASE_STABLE"]


class FMPClient(BaseSource):
    name = "fmp"

    # Endpoint paths (relative to BASE_STABLE), centralized for easy correction.
    EP_STOCK_LIST = "stock-list"
    EP_ETF_LIST = "etf-list"
    EP_PROFILE = "profile"
    # Unadjusted OHLCV. `.../full` is split-adjusted -- see the module docstring.
    EP_EOD_RAW = "historical-price-eod/non-split-adjusted"
    # Diagnostics only (`fafnir source probe-prices`); never ingested.
    EP_EOD_SPLIT_ADJUSTED = "historical-price-eod/full"
    EP_SPLITS = "splits"
    EP_DIVIDENDS = "dividends"
    EP_SECTORS = "available-sectors"
    EP_INDUSTRIES = "available-industries"
    EP_SCREENER = "company-screener"
    EP_DELISTED = "delisted-companies"
    EP_SYMBOL_CHANGE = "symbol-change"

    # Screener rows per request. Confirmed to return a full 5000-row page, but
    # 1000 keeps a single page small enough to retry cheaply.
    SCREENER_PAGE_SIZE = 1000
    DELISTED_PAGE_SIZE = 100
    SYMBOL_CHANGE_PAGE_SIZE = 100

    # The historical-price-eod endpoints silently truncate to the most recent 5000
    # bars, ignoring how far back `from` reaches: a 1990-01-01 request for AAPL and
    # MSFT both came back starting 2006-09-28 -- the same date for two companies,
    # which is a row cap, not history. 5000 bars is ~19.8 trading years, so a 15-year
    # window (~3780 bars) leaves ~25% headroom and needs three requests to cover a
    # 1990-to-now backfill.
    EOD_MAX_ROWS = 5000
    EOD_CHUNK_DAYS = 5475

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

    def _paged(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        page_size: int,
        max_pages: int,
        key_fields: Sequence[str] = ("symbol",),
    ) -> list[dict]:
        """Accumulate an endpoint's pages until it runs out of rows.

        Stops on an empty page, a short page, or a page whose first row repeats
        the previous page's -- that last guard catches a server that ignores
        ``page`` and would otherwise be re-downloaded ``max_pages`` times.

        ``key_fields`` names the fields that identify a row on *this* endpoint.
        It is not decoration: a payload with no ``symbol`` (the rename feed carries
        ``oldSymbol``/``newSymbol``) would make every page's fingerprint None, the
        repeat guard would never fire, and an endpoint that ignores ``page`` would
        be downloaded ``max_pages`` times and its rows counted that many times over.
        """
        out: list[dict] = []
        prev_first: Optional[str] = None
        base = dict(params or {})
        for page in range(max_pages):
            merged: dict[str, Any] = dict(base, limit=page_size, page=page)
            data, _, _ = self._call(endpoint, merged)
            if not isinstance(data, list) or not data:
                break
            first = _row_fingerprint(data[0], key_fields)
            if first is not None and first == prev_first:
                break
            prev_first = first
            out.extend(data)
            if len(data) < page_size:
                break
        return out

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
        params: dict[str, Any] = {}
        if exchange:
            params["exchange"] = exchange
        if country:
            params["country"] = country
        if is_etf is not None:
            params["isEtf"] = "true" if is_etf else "false"
        return self._paged(
            self.EP_SCREENER,
            params,
            page_size=self.SCREENER_PAGE_SIZE,
            max_pages=max_pages,
        )

    def delisted_companies(self, *, max_pages: int = 5) -> list[dict]:
        """Delisted companies, newest delisting first.

        Rows carry ``symbol``, ``companyName``, ``exchange``, ``ipoDate`` and
        ``delistedDate``, and the list is global -- callers filter to the venues
        they ingest. The ordering is why ``max_pages`` defaults low: the nightly
        run only needs the recent tail, and a full sweep is an explicit choice.
        """
        return self._paged(
            self.EP_DELISTED,
            page_size=self.DELISTED_PAGE_SIZE,
            max_pages=max_pages,
        )

    def symbol_changes(self, *, max_pages: int = 5) -> list[dict]:
        """Ticker renames, newest first.

        Rows carry ``date``, ``companyName``, ``oldSymbol`` and ``newSymbol``. Like
        the delisted feed the list is global, and it carries no exchange -- so the
        loader filters by whether fafnir tracks the *old* ticker, not by venue.

        ``max_pages`` defaults low for the same reason as the delisted sweep: the
        nightly run only needs the recent tail, and a full sweep is an explicit
        choice.
        """
        return self._paged(
            self.EP_SYMBOL_CHANGE,
            page_size=self.SYMBOL_CHANGE_PAGE_SIZE,
            max_pages=max_pages,
            key_fields=("oldSymbol", "newSymbol", "date"),
        )

    def profile(self, symbol: str) -> Optional[dict]:
        data, _, _ = self._call(self.EP_PROFILE, {"symbol": symbol})
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
        return None

    # -- prices -------------------------------------------------------------
    # The unadjusted endpoint names its price fields adjOpen/adjHigh/adjLow/
    # adjClose. The values are as-traded; only the *names* carry "adj". Map them
    # to the canonical names so the loader never has to know which endpoint the
    # bars came from.
    _EOD_FIELD_ALIASES = {
        "adjOpen": "open",
        "adjHigh": "high",
        "adjLow": "low",
        "adjClose": "close",
    }

    @classmethod
    def _normalize_bar(cls, bar: dict) -> dict:
        if not isinstance(bar, dict):
            return bar
        out = dict(bar)
        for alias, canonical in cls._EOD_FIELD_ALIASES.items():
            if canonical not in out and alias in out:
                out[canonical] = out[alias]
        return out

    def _eod_window(
        self,
        symbol: str,
        from_date: Optional[str],
        to_date: Optional[str],
        endpoint: Optional[str] = None,
    ) -> list[dict]:
        """One request. May be silently truncated to ``EOD_MAX_ROWS``."""
        params: dict[str, Any] = {"symbol": symbol}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data, _, _ = self._call(endpoint or self.EP_EOD_RAW, params)
        if isinstance(data, dict) and "historical" in data:
            return data["historical"]
        if not isinstance(data, list):
            return []
        return [self._normalize_bar(bar) for bar in data]

    def eod_split_adjusted(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list[dict]:
        """SPLIT-ADJUSTED bars from ``historical-price-eod/full``. **Diagnostics only.**

        Never ingest this into ``core.daily_price``: applying fafnir's split factors
        on top of an already-split-adjusted series adjusts every split twice (see
        doc/adr/0004-unadjusted-price-feed.md). It exists so ``fafnir source
        probe-prices`` can compare the two feeds and prove which one is raw.

        Deduplicated by date for parity with ``eod_raw`` -- a vendor-side repeat
        would otherwise leave two bars for one day on this side of the comparison.
        """
        bars = self._eod_window(symbol, from_date, to_date, self.EP_EOD_SPLIT_ADJUSTED)
        by_date: dict[str, dict] = {}
        for bar in bars:
            key = str(bar.get("date") or "")[:10]
            if key:
                by_date[key] = bar
        return [by_date[k] for k in sorted(by_date)]

    def eod_raw(
        self,
        symbol: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list[dict]:
        """Unadjusted daily OHLCV over the whole window, in ascending date order.

        "Unadjusted" is the point: these bars carry the prices as they actually
        traded, so fafnir's own split/dividend factors are the only adjustment ever
        applied. Payloads are returned exactly as FMP sends them (the caller lands
        them verbatim); note that the unadjusted endpoint labels its OHLC fields
        ``adjOpen``/``adjHigh``/``adjLow``/``adjClose`` -- an FMP naming quirk, not a
        second adjustment. ``fafnir.ingest.daily_price`` normalizes at the transform
        boundary.

        Splits the window into ``EOD_CHUNK_DAYS`` slices and stitches them,
        because a single request is capped at ``EOD_MAX_ROWS`` and drops the
        *oldest* bars to fit -- without an error, a flag, or a short-payload hint.
        Asking for 1990 and receiving 2006 onward looks exactly like success.

        Incremental callers pass a from_date a few days back, so they still cost
        one request; only a genuine backfill pays for the extra slices.
        """
        if from_date is None:
            # No lower bound to slice against. One request, but say so if it
            # comes back exactly at the cap, since that means truncation.
            bars = self._eod_window(symbol, None, to_date)
            if len(bars) >= self.EOD_MAX_ROWS:
                logger.warning(
                    "%s: %d bars is the endpoint cap -- older history was dropped; "
                    "pass an explicit from date so the window can be chunked",
                    symbol,
                    len(bars),
                )
            return bars

        start = _as_date(from_date)
        end = _as_date(to_date) or date.today()
        by_date: dict[str, dict] = {}
        cursor = start
        while cursor <= end:
            window_end = min(cursor + timedelta(days=self.EOD_CHUNK_DAYS - 1), end)
            bars = self._eod_window(symbol, cursor.isoformat(), window_end.isoformat())
            if len(bars) >= self.EOD_MAX_ROWS:
                logger.warning(
                    "%s: %s..%s returned the %d-row cap; bars may be missing -- "
                    "lower FMPClient.EOD_CHUNK_DAYS",
                    symbol,
                    cursor,
                    window_end,
                    self.EOD_MAX_ROWS,
                )
            for bar in bars:
                key = str(bar.get("date") or "")[:10]
                if key:
                    # Windows do not overlap, but dedup keeps a vendor-side
                    # boundary repeat from double-counting.
                    by_date[key] = bar
            cursor = window_end + timedelta(days=1)
        return [by_date[k] for k in sorted(by_date)]

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


def _row_fingerprint(row: Any, key_fields: Sequence[str]) -> Optional[str]:
    """Identify a payload row by the fields that name it on its endpoint.

    Falls back to the row's whole sorted content when it carries none of them, so
    the pagination repeat-guard still has something to compare rather than
    silently comparing None to None.
    """
    if not isinstance(row, dict):
        return None
    values = [str(row[f]) for f in key_fields if row.get(f) is not None]
    if values:
        return "|".join(values)
    return json.dumps(row, sort_keys=True, default=str)


def _as_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


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
