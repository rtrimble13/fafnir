"""
Sharadar source client (plan SA-0106, #47).

Everything Sharadar-facing goes through :class:`SharadarClient`, which inherits
throttling, retry/backoff, byte metering and secret redaction from
:class:`~fafnir.sources.base.BaseSource`, as :class:`~fafnir.sources.fmp.FMPClient`
does. ``duk -S sharadar`` (``duk.sharadar_api``) takes its base URL, price tables
and payload parser from here, so a correction is made once.

API MECHANICS -- what this module relies on, and on what evidence
-----------------------------------------------------------------

The repository adopts an endpoint on a probe's evidence, not its documentation
(ADR 0007). The items marked DOCUMENTED come from Sharadar's own agent guide
(supplied by the operator on 2026-10-19); none has yet been measured from this
repository, because the build host's network could not reach the vendor.
``fafnir source probe-sharadar`` (SA-0201, #49) measures every item on the paid
key and corrects this docstring, the constants and ``duk.sharadar_api`` together.

1. **Base URL** -- ``https://api.sharadar.com/v1.0``, tables at
   ``/data/{table}``. DOCUMENTED. It is the direct Sharadar API, *not* Nasdaq Data
   Link: ``data.nasdaq.com`` and its ``qopts.*`` / ``datatable`` conventions do not
   apply. Overridable without a release: ``SHARADAR_BASE_URL`` or
   ``[api].sharadar_base_url``, shared with duk.
2. **Authentication** -- ``api_key`` (or ``apiKey``) on the query string, or the
   ``x-api-key`` header. DOCUMENTED. This client sends the header, so the key is in
   no URL, and therefore in no requests exception, log line or traceback that
   renders one; it is also masked by value in everything this client logs or
   raises. The header is sent to Sharadar only: a bulk download's redirect target
   is fetched without it (item 8). The vendor's ``test-api-key`` serves the free
   sample universe (roughly the DJIA names); a 403 ``Exceeds free tier`` means the
   request ran as free-tier. MEASURED 2026-10-19: a missing or invalid key is
   *not* refused -- it is served as free tier (AAPL's prices from five years back;
   ``TWO``'s refused) -- so a 200 proves nothing about the key.
   :meth:`SharadarClient.tier` asks for what only a subscription serves.
3. **Tables** -- a modern name (``stocks``, ``tickers``) or a legacy code (``SEP``,
   ``SF1``); both resolve. DOCUMENTED. This client uses the legacy code where one
   exists (SEP, SFP, SF1: the names the plan and duk use) and the modern name
   otherwise.
4. **Format** -- ``format=json`` (or ``csv``). DOCUMENTED. MEASURED 2026-10-19 on
   ``tickers``, ``SEP`` and ``actions``: ``{"count": n, "data": [{...}, ...]}``,
   ``count`` the rows returned; an error is ``{"error": ..., "description": ...}``.
   :func:`rows_from_payload` also accepts a bare list and the datatable shape.
5. **Slices** -- ``ticker``, ``from``, ``to`` (calendar dates, ``YYYY-MM-DD``),
   ``fields`` (the columns) and ``sort``. DOCUMENTED. MEASURED 2026-10-19 (paid key,
   ``fafnir source probe-sharadar``): ``ticker`` takes a comma-separated list;
   ``date``, ``lastupdated`` and ``lastupdated.gte`` filter too; on SF1, ``from`` /
   ``to`` filter ``date`` (the filing date), not ``calendardate``. TICKERS' own
   ``table`` filter takes the modern names (``stocks``, ``fundamentals``, ``funds``):
   ``table=SEP`` matches nothing. **An unknown parameter is ignored, not refused**,
   so a misspelt filter returns everything it should have narrowed; a loader
   asserts its filters in the rows it gets back.
6. **Paging** -- ``limit`` (default 10,000) and ``offset`` / ``skip``. DOCUMENTED.
   MEASURED: a JSON ``limit`` above 10,000 is an error (``limit too large``), not a
   silent cap; ``offset`` is honoured; ``SEP`` answers newest date first by
   default and ``sort=date`` reverses it. TICKERS has no useful default order, so
   paging it sorts explicitly. The vendor says not to page a whole table: that is
   what bulk is for (item 8).
7. **Rate limits** -- not documented. MEASURED: ``X-Ratelimit-Limit: 5000`` requests
   and ``X-Ratelimit-Weighted-Limit: 25000`` per window, with ``-Remaining``,
   ``-Reset`` (epoch seconds) and ``-Cost`` (1 a request) headers. ``[sharadar]
   rate_per_min`` (default 60) throttles proactively; a 429 is retried with backoff
   by the base class.
8. **Bulk** -- the same ``GET /data/{table}`` with ``years=5|10|full`` answers
   HTTP 302 to a pre-signed zip, subscription-gated. DOCUMENTED. MEASURED: the link
   is a DigitalOcean Spaces URL (``static-sharadar.nyc3.digitaloceanspaces.com``,
   ``/bulk-sharadar/<table>.csv.zip``), so a host's egress must allow that host as
   well as ``api.sharadar.com``. ``tickers`` and ``descriptions`` publish a full
   snapshot only. MEASURED (first full download, 2026-10-19): each zip holds one
   CSV named for the table's modern name (``stocks.csv`` when ``SEP`` was asked
   for), whose header matches the table's ``/v1.0/schema``. Full history totals
   1.9 GB of zips across tickers, actions, SEP, SFP and SF1.
9. **Schemas** -- ``GET /schema/{table}?format=postgres`` serves a table's schema
   without a key. DOCUMENTED; an input for the bulk reader's header check (#52).

The field contract, as measured (``probe-sharadar``, doc/ingestion.md): the price
tables carry the raw close (``closeunadj``), split-adjusted ``open/high/low/close/
volume`` and a split-and-dividend-adjusted ``closeadj``, all to **3 decimal
places**; a split's ``value`` is new shares per old; a dividend's is
split-adjusted; history is keyed by the *current* ticker; and SEP/SF1 rows carry no
``permaticker`` (TICKERS does). Nothing here interprets rows: this client fetches
them.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

import requests

from fafnir.logging_config import get_logger
from fafnir.sources.base import BaseSource, SourceError

logger = get_logger("source.sharadar")

#: Base of Sharadar's table API (item 1).
DEFAULT_BASE_URL = "https://api.sharadar.com/v1.0/data"

#: The equity and fund price tables. A ticker is looked up in SEP first, then
#: SFP, which holds ETFs and closed-end funds.
PRICE_TABLES = ("SEP", "SFP")

#: The ``years`` values a bulk download takes (item 8).
BULK_YEARS = ("5", "10", "full")

__all__ = [
    "BULK_YEARS",
    "BulkExport",
    "DEFAULT_BASE_URL",
    "PRICE_TABLES",
    "SharadarClient",
    "SourceError",
    "rows_from_payload",
]


def rows_from_payload(data: Any) -> list[dict]:
    """Normalise the payload shapes item 4 allows for to a list of row dicts.

    Raises :class:`SourceError` for an error payload; the caller redacts.
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("datatable"), dict):
            table = data["datatable"]
            names = [c.get("name") for c in table.get("columns", [])]
            return [dict(zip(names, row)) for row in table.get("data", [])]
        for key in ("data", "rows", "results"):
            if isinstance(data.get(key), list):
                return rows_from_payload(data[key])
        if "error" in data or "message" in data:
            raise SourceError(
                f"Sharadar API error: {data.get('error') or data.get('message')}"
            )
    return []


def _no_query(text: str) -> str:
    """Drop every URL query string from ``text``. A pre-signed link's query is its
    credentials plus noise; the host and path say which file failed."""
    return re.sub(r"\?[^\s'\")]*", "", text)


@dataclass(frozen=True)
class BulkExport:
    """One downloaded bulk file, named by its content."""

    table: str
    path: Path
    bytes: int
    sha256: str


class SharadarClient(BaseSource):
    name = "sharadar"

    # Tables (item 3), centralized for easy correction (the FMPClient pattern).
    T_TICKERS = "tickers"
    T_ACTIONS = "actions"
    T_SEP = "SEP"
    T_SFP = "SFP"
    T_SF1 = "SF1"
    T_DAILY = "daily"
    T_METRICS = "metrics"
    T_DESCRIPTIONS = "descriptions"
    #: What the plan reads, of the Bundle's tables.
    TABLES = (
        T_TICKERS,
        T_ACTIONS,
        T_SEP,
        T_SFP,
        T_SF1,
        T_DAILY,
        T_METRICS,
        T_DESCRIPTIONS,
    )

    # Item 6: rows per page (the documented default), and a bound on a runaway.
    # 1,000 pages of 10,000 rows is ten million: a slice that long belongs in a
    # bulk download, so hitting it means the paging is not advancing.
    PAGE_ROWS = 10_000
    MAX_PAGES = 1_000

    DOWNLOAD_CHUNK_BYTES = 1 << 20

    # A ticker outside the free sample: a delisted issuer the full-history
    # subscription serves and the free tier refuses (item 2).
    TIER_PROBE_TABLE = T_SEP
    TIER_PROBE_TICKER = "TWO"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        rate_per_min: int = 60,
        **kwargs,
    ):
        if not api_key:
            raise ValueError("Sharadar API key is required")
        super().__init__(rate_per_min=rate_per_min, **kwargs)
        self._secrets = (api_key,)
        self._key_header = {"x-api-key": api_key}  # item 2
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _url(self, table: str) -> str:
        return f"{self.base_url}/{table}"

    def _call(self, table: str, params: Optional[dict] = None) -> tuple[Any, int, int]:
        merged = dict(params or {})
        merged.setdefault("format", "json")
        return self._get(self._url(table), merged, headers=self._key_header)

    def _rows(self, data: Any) -> list[dict]:
        try:
            return rows_from_payload(data)
        except SourceError as exc:
            raise SourceError(self._redact(str(exc))) from None

    # -- discovery ----------------------------------------------------------
    def tables(self) -> tuple[str, ...]:
        """The tables fafnir reads (see :attr:`TABLES`)."""
        return self.TABLES

    def ping(self, *, table: str = T_TICKERS, ticker: str = "AAPL") -> dict:
        """One small request: proof the key, the base URL and the payload parser
        work together. Returns what an operator needs to see; never the key."""
        started = time.monotonic()
        data, status, nbytes = self._call(table, {"ticker": ticker.upper()})
        rows = self._rows(data)
        return {
            "url": self._url(table),
            "table": table,
            "ticker": ticker.upper(),
            "status": status,
            "rows": len(rows),
            "bytes": nbytes,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "columns": sorted(rows[0]) if rows else [],
        }

    def response(
        self, table: str, params: Optional[dict] = None, *, allow_redirects: bool = True
    ) -> requests.Response:
        """One throttled, metered GET of ``table``, returned as it came: any status,
        headers included, redirects followed only if asked. For diagnostics (the
        tier check, the probe, the bulk redirect), which need what :meth:`_get`
        turns into an exception or discards. Only a transport failure raises, and
        that redacted."""
        url = self._url(table)
        self.limiter.acquire()
        try:
            resp = self._session.get(
                url,
                params=dict(params or {}),
                headers=self._key_header,
                allow_redirects=allow_redirects,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SourceError(
                f"GET {self._redact(url)} failed: "
                f"{type(exc).__name__}: {self._redact(str(exc))}"
            ) from None
        self.request_count += 1
        self.bytes_downloaded += len(resp.content or b"")
        return resp

    def tier(self) -> str:
        """``paid`` or ``free``: which tier Sharadar serves this key as, or
        ``unknown (...)`` when the answer is neither.

        One request, for a ticker the free sample does not hold. Needed because
        an invalid key is not refused (item 2): only a refusal of what a
        subscription would serve shows the key is not working.
        """
        resp = self.response(
            self.TIER_PROBE_TABLE,
            {"ticker": self.TIER_PROBE_TICKER, "limit": 1, "format": "json"},
        )
        body = self._redact(resp.text or "")
        if resp.status_code == 200:
            return "paid"
        if resp.status_code == 403 and "free tier" in body.lower():
            return "free"
        return f"unknown (HTTP {resp.status_code}: {body[:120]})"

    # -- tables -------------------------------------------------------------
    def query(
        self,
        table: str,
        filters: Optional[dict] = None,
        fields: Optional[Sequence[str]] = None,
        *,
        max_pages: Optional[int] = None,
    ) -> list[dict]:
        """Every row of a slice of ``table`` (item 5), all pages (item 6).

        ``filters`` are passed as given (``{"ticker": "AAPL", "from":
        "2020-01-01"}``). Stops on a short page, or on a page whose first row
        repeats the previous page's -- a server that ignored ``offset`` would
        otherwise be read ``max_pages`` times. For a whole table use
        :meth:`bulk_export`.
        """
        params = dict(filters or {})
        if fields:
            params["fields"] = ",".join(fields)
        params["limit"] = self.PAGE_ROWS
        limit = max_pages or self.MAX_PAGES
        out: list[dict] = []
        first_of_last: Optional[dict] = None
        for page in range(limit):
            params["offset"] = page * self.PAGE_ROWS
            data, _, _ = self._call(table, params)
            rows = self._rows(data)
            if rows and rows[0] == first_of_last:
                logger.warning(
                    "Sharadar %s: offset %d repeated the previous page; stopping at "
                    "%d rows",
                    table,
                    params["offset"],
                    len(out),
                )
                return out
            out.extend(rows)
            if len(rows) < self.PAGE_ROWS:
                return out
            first_of_last = rows[0]
        raise SourceError(
            f"Sharadar {table}: more than {limit} pages of {self.PAGE_ROWS} rows; "
            "a slice that long belongs in a bulk download"
        )

    # -- bulk ---------------------------------------------------------------
    def bulk_export(
        self, table: str, dest_dir: str | Path, *, years: str = "full"
    ) -> BulkExport:
        """Download ``table``'s pre-built zip (item 8) to
        ``dest_dir/<sha256><suffix>``.

        The redirect is followed by hand: the pre-signed link is fetched without
        the key header, which would otherwise go to whichever host the link names.
        The file is written under a temporary name and renamed once complete, so a
        failed download leaves nothing that looks like a file, and the same
        content twice lands on the same name.
        """
        years = str(years)
        if years not in BULK_YEARS:
            raise ValueError(f"years must be one of {BULK_YEARS}, not {years!r}")
        link = self._bulk_link(table, years)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        suffix = "".join(Path(urlsplit(link).path).suffixes) or ".zip"
        digest = hashlib.sha256()
        nbytes = 0
        fd, tmp_name = tempfile.mkstemp(prefix=f".{table}-", suffix=".part", dir=dest)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in self._stream(link):
                    fh.write(chunk)
                    digest.update(chunk)
                    nbytes += len(chunk)
            final = dest / f"{digest.hexdigest()}{suffix}"
            tmp.replace(final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        logger.info("Sharadar %s bulk: %d bytes -> %s", table, nbytes, final.name)
        return BulkExport(table, final, nbytes, digest.hexdigest())

    def _bulk_link(self, table: str, years: str) -> str:
        """The pre-signed link the bulk request redirects to."""
        resp = self.response(table, {"years": years}, allow_redirects=False)
        location = resp.headers.get("Location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            return location
        detail = ""
        try:
            self._rows(resp.json())
        except SourceError as exc:
            detail = f": {exc}"
        except ValueError:
            pass
        raise SourceError(
            f"Sharadar {table} bulk (years={years}) answered HTTP "
            f"{resp.status_code} without a redirect{detail}. Bulk is "
            "subscription-gated; a 403 'Exceeds free tier' is the free key."
        )

    def _stream(self, url: str):
        """Yield the body of ``url`` in chunks, metered, without the key header.
        The link carries its own credentials, so it is redacted like a key."""
        self.limiter.acquire()
        try:
            resp = requests.get(url, stream=True, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(
                f"GET {_no_query(url)} failed: "
                f"{type(exc).__name__}: {_no_query(self._redact(str(exc)))}"
            ) from None
        self.request_count += 1
        with resp:
            try:
                for chunk in resp.iter_content(chunk_size=self.DOWNLOAD_CHUNK_BYTES):
                    if chunk:
                        self.bytes_downloaded += len(chunk)
                        yield chunk
            except requests.RequestException as exc:
                raise SourceError(
                    f"GET {_no_query(url)} broke off: "
                    f"{type(exc).__name__}: {_no_query(self._redact(str(exc)))}"
                ) from None
