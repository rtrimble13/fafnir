"""
Sharadar source client (plan SA-0106, #47).

Everything Sharadar-facing goes through :class:`SharadarClient`, which inherits
throttling, retry/backoff, byte metering and secret redaction from
:class:`~fafnir.sources.base.BaseSource`, as :class:`~fafnir.sources.fmp.FMPClient`
does. ``duk -S sharadar`` (``duk.sharadar_api``) takes its base URL and price tables
from here, so a correction to either is made once.

API MECHANICS -- what this module assumes, and how sure it is
--------------------------------------------------------------

The repository does not adopt an endpoint on the strength of its documentation
(ADR 0007), and Sharadar's documentation could not be read from where this module
was written: ``sharadar.com/docs`` is rendered by JavaScript, and the build host
could not reach it at all. Every item below is therefore **UNVERIFIED** until
``fafnir source probe-sharadar`` (SA-0201, #49) measures it against the paid key
and corrects this docstring, the constants and ``duk.sharadar_api`` in one PR.

1. **Base URL** -- ``https://api.sharadar.com/v1.0/data``, one path per table
   (``.../SEP``). UNVERIFIED. Overridable without a release:
   ``SHARADAR_BASE_URL`` or ``[api].sharadar_base_url``, shared with duk.
2. **Authentication** -- the key as an ``api_key`` query parameter. UNVERIFIED.
   If it turns out to be a header, only :meth:`SharadarClient._call` changes; the
   key is masked by value in everything this client logs or raises either way.
3. **Format** -- ``format=json``. The payload is either a list of row objects or
   the datatable shape, ``{"datatable": {"columns": [{"name": ...}], "data":
   [[...]]}, "meta": {...}}``. :func:`rows_from_payload` accepts both. UNVERIFIED
   which one Sharadar sends.
4. **Filters** -- a column name with an operator suffix: ``ticker=AAPL``,
   ``date.gte=2020-01-01``, ``date.lte=...``, ``lastupdated.gte=...`` (the
   nightly-delta filter). A comma-separated value is a list (``ticker=A,B``).
   UNVERIFIED.
5. **Columns** -- ``qopts.columns=ticker,date,close``. UNVERIFIED.
6. **Pagination** -- a page holds at most :attr:`SharadarClient.PAGE_ROWS` rows
   (10,000, UNVERIFIED); a longer result carries ``meta.next_cursor_id`` and the
   next page is requested with ``qopts.cursor_id``. A response without a cursor is
   the last page. UNVERIFIED -- and a row cap that truncates silently instead
   (FMP's habit, see ``FMPClient.EOD_MAX_ROWS``) is exactly what the probe checks.
7. **Rate limits** -- unknown. ``[sharadar] rate_per_min`` (default 60) throttles
   proactively; a 429 is retried with backoff by the base class.
8. **Bulk export** -- a table request with ``qopts.export=true`` returns
   ``{"datatable_bulk_download": {"file": {"status": ..., "link": ...}}}``. The
   status is ``creating`` or ``regenerating`` while the vendor builds the file and
   ``fresh`` once ``link`` (a short-lived, pre-signed URL) can be downloaded.
   UNVERIFIED, including the file's format (zip or gzip): :meth:`bulk_export`
   keeps whatever arrives and names it by content.

What is *not* in doubt is the field contract duk already relies on: SEP and SFP's
``open/high/low/close/volume`` are split-adjusted, ``closeadj`` is adjusted for
splits and dividends, and ``closeunadj`` is the close as traded (ADR 0015 and
``duk.sharadar_api``). Nothing here interprets rows: this client fetches them.
"""

from __future__ import annotations

import hashlib
import os
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

#: Default base of Sharadar's table API (UNVERIFIED -- module docstring, item 1).
DEFAULT_BASE_URL = "https://api.sharadar.com/v1.0/data"

#: The equity and fund price tables. A ticker is looked up in SEP first, then
#: SFP, which holds ETFs and closed-end funds.
PRICE_TABLES = ("SEP", "SFP")

__all__ = [
    "BulkExport",
    "DEFAULT_BASE_URL",
    "PRICE_TABLES",
    "SharadarClient",
    "SourceError",
    "rows_from_payload",
]


def rows_from_payload(data: Any) -> list[dict]:
    """Normalise either payload shape (item 3) to a list of row dicts.

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


def _next_cursor(data: Any) -> Optional[str]:
    if isinstance(data, dict) and isinstance(data.get("meta"), dict):
        cursor = data["meta"].get("next_cursor_id")
        return str(cursor) if cursor else None
    return None


@dataclass(frozen=True)
class BulkExport:
    """One downloaded export file, named by its content."""

    table: str
    path: Path
    bytes: int
    sha256: str


class SharadarClient(BaseSource):
    name = "sharadar"

    # Tables, centralized for easy correction (the FMPClient pattern).
    T_TICKERS = "TICKERS"
    T_ACTIONS = "ACTIONS"
    T_SEP = "SEP"
    T_SFP = "SFP"
    T_SF1 = "SF1"
    T_DAILY = "DAILY"
    T_INDICATORS = "INDICATORS"
    #: What the plan reads. Not an API listing: whether the API can list its
    #: tables is unknown (item 1), and the subscription decides which answer.
    TABLES = (T_TICKERS, T_ACTIONS, T_SEP, T_SFP, T_SF1, T_DAILY, T_INDICATORS)

    # Item 6: rows per page, and a bound on a runaway cursor. 1,000 pages of
    # 10,000 rows is ten million: past SEP's whole history, so hitting it means the
    # cursor is not advancing, not that the table is that long.
    PAGE_ROWS = 10_000
    MAX_PAGES = 1_000

    # Item 8: how long to wait for the vendor to build an export, and how often to
    # ask. SEP's full history is the largest file; minutes, not hours, is the
    # expectation the probe will confirm or correct.
    EXPORT_POLL_SECONDS = 15
    EXPORT_MAX_WAIT_SECONDS = 1800
    DOWNLOAD_CHUNK_BYTES = 1 << 20

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
        self._api_key = api_key
        self._secrets = (api_key,)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _call(self, table: str, params: Optional[dict] = None) -> tuple[Any, int, int]:
        merged = dict(params or {})
        merged.setdefault("format", "json")
        merged["api_key"] = self._api_key  # item 2
        return self._get(f"{self.base_url}/{table}", merged)

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
            "url": f"{self.base_url}/{table}",
            "table": table,
            "ticker": ticker.upper(),
            "status": status,
            "rows": len(rows),
            "bytes": nbytes,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "columns": sorted(rows[0]) if rows else [],
            "paged": _next_cursor(data) is not None,
        }

    # -- tables -------------------------------------------------------------
    def query(
        self,
        table: str,
        filters: Optional[dict] = None,
        columns: Optional[Sequence[str]] = None,
        *,
        max_pages: Optional[int] = None,
    ) -> list[dict]:
        """Every row of ``table`` matching ``filters`` (item 4), all pages.

        ``filters`` are passed as given (``{"ticker": "AAPL", "date.gte":
        "2020-01-01"}``). Stops on a page without a cursor, or on a cursor that
        repeats -- a server that ignores it would otherwise be read ``max_pages``
        times.
        """
        params = dict(filters or {})
        if columns:
            params["qopts.columns"] = ",".join(columns)  # item 5
        limit = max_pages or self.MAX_PAGES
        out: list[dict] = []
        seen: set[str] = set()
        for _ in range(limit):
            data, _, _ = self._call(table, params)
            out.extend(self._rows(data))
            cursor = _next_cursor(data)
            if cursor is None:
                return out
            if cursor in seen:
                logger.warning(
                    "Sharadar %s: cursor %s repeated; stopping at %d rows",
                    table,
                    cursor,
                    len(out),
                )
                return out
            seen.add(cursor)
            params["qopts.cursor_id"] = cursor
        raise SourceError(
            f"Sharadar {table}: more than {limit} pages; the cursor is not "
            "advancing, or the filter is far wider than intended"
        )

    # -- bulk export --------------------------------------------------------
    def bulk_export(self, table: str, dest_dir: str | Path, **filters) -> BulkExport:
        """Request ``table``'s export (item 8), wait for it, and stream it to
        ``dest_dir/<sha256><suffix>``.

        The file is written under a temporary name and renamed once complete, so
        a failed download leaves nothing that looks like a file. Downloading the
        same content twice lands on the same name.
        """
        link = self._export_link(table, filters)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        suffix = "".join(Path(urlsplit(link).path).suffixes) or ".bin"
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
        logger.info("Sharadar %s export: %d bytes -> %s", table, nbytes, final.name)
        return BulkExport(table, final, nbytes, digest.hexdigest())

    def _export_link(self, table: str, filters: dict) -> str:
        params = {**filters, "qopts.export": "true"}
        deadline = time.monotonic() + self.EXPORT_MAX_WAIT_SECONDS
        while True:
            data, _, _ = self._call(table, params)
            file = (
                (data.get("datatable_bulk_download") or {}).get("file") or {}
                if isinstance(data, dict)
                else {}
            )
            status = str(file.get("status") or "").lower()
            if status == "fresh" and file.get("link"):
                return str(file["link"])
            if not file:
                self._rows(data)  # an error payload raises here, redacted
                raise SourceError(
                    f"Sharadar {table} export: no datatable_bulk_download in the "
                    "response; the export flow is UNVERIFIED (see "
                    "fafnir.sources.sharadar, item 8)"
                )
            if time.monotonic() >= deadline:
                raise SourceError(
                    f"Sharadar {table} export still {status or 'unknown'} after "
                    f"{self.EXPORT_MAX_WAIT_SECONDS}s"
                )
            logger.info("Sharadar %s export is %s; waiting", table, status)
            time.sleep(self.EXPORT_POLL_SECONDS)

    def _stream(self, url: str):
        """Yield the body of ``url`` in chunks, metered. The link carries its own
        credentials (a pre-signed URL), so it is redacted like a key."""
        self.limiter.acquire()
        try:
            resp = self._session.get(url, stream=True, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(
                f"GET {self._redact(url)} failed: "
                f"{type(exc).__name__}: {self._redact(str(exc))}"
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
                    f"GET {self._redact(url)} broke off: "
                    f"{type(exc).__name__}: {self._redact(str(exc))}"
                ) from None
