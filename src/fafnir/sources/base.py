"""
Source-client contract.

A data source returns (parsed_payload, raw_bytes) tuples so callers can both use
the typed data and land the raw response for lineage, while metering bandwidth.
Subclasses (FMP now; FRED/BLS/BEA as fast-follows) implement domain methods on
top of a throttled, retrying ``_get``.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import deque
from typing import Any

import requests

from fafnir.logging_config import get_logger

logger = get_logger("source")


class SourceError(Exception):
    """Raised when a source request fails after retries."""


class RateLimiter:
    """Simple sliding-window limiter: at most ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max(1, max_calls)
        self.period = period
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_for = self.period - (now - self._calls[0]) + 0.01
            if sleep_for > 0:
                logger.debug(
                    "Throttle: sleeping %.2fs to respect rate limit", sleep_for
                )
                time.sleep(sleep_for)
            return self.acquire()
        self._calls.append(time.monotonic())


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class BaseSource:
    """Throttled, retrying HTTP source with bandwidth metering."""

    name = "base"

    def __init__(
        self, rate_per_min: int = 280, timeout: int = 30, max_retries: int = 4
    ):
        self.limiter = RateLimiter(rate_per_min, period=60.0)
        self.timeout = timeout
        self.max_retries = max_retries
        self.bytes_downloaded = 0
        self.request_count = 0
        self._session = requests.Session()

    def _get(self, url: str, params: dict | None = None) -> tuple[Any, int, int]:
        """GET with throttle + exponential backoff on 429/5xx/timeouts.

        Returns (parsed_json, http_status, nbytes). Raises SourceError on
        exhaustion. The API key in ``params`` is never logged.
        """
        params = dict(params or {})
        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries - 1:
                    raise SourceError(f"GET {url} failed: {exc}") from exc
                self._backoff(attempt)
                continue

            self.request_count += 1
            nbytes = len(resp.content or b"")
            self.bytes_downloaded += nbytes

            if resp.status_code == 429:
                logger.warning(
                    "HTTP 429 from %s (attempt %d); backing off", self.name, attempt + 1
                )
                self._backoff(attempt, base=2.0, floor=1.0)
                continue
            if resp.status_code >= 500:
                self._backoff(attempt)
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                raise SourceError(f"GET {url} returned {resp.status_code}") from exc

            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError(f"GET {url} returned non-JSON body") from exc

            if isinstance(data, dict) and "Error Message" in data:
                raise SourceError(f"{self.name} error: {data['Error Message']}")
            return data, resp.status_code, nbytes

        raise SourceError(f"GET {url} exhausted retries")

    def _backoff(self, attempt: int, base: float = 2.0, floor: float = 0.0) -> None:
        delay = floor + (base**attempt) + random.uniform(0, 0.5)
        time.sleep(delay)
