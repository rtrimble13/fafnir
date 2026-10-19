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
import re
import time
from collections import deque
from typing import Any, Sequence

import requests

from fafnir.logging_config import get_logger

logger = get_logger("source")

# Cap on a server-supplied Retry-After so a misbehaving header can't stall a load.
MAX_RETRY_AFTER_SECONDS = 120

# Query parameters whose values must never reach a log, an exception message or a
# traceback. requests renders the *full* URL -- key included -- into the string
# form of HTTPError and of the connection errors, so anything derived from an
# upstream exception has to be scrubbed before it is re-raised or logged.
#
# The X-Amz-* parameters are a pre-signed download link's credentials: a bulk
# export hands one back (Sharadar, UNVERIFIED -- see sources/sharadar.py), and
# anyone holding the link can fetch the file until it expires.
SECRET_QUERY_PARAMS = (
    "apikey",
    "api_key",
    "token",
    "access_key",
    "secret",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
)
_SECRET_RE = re.compile(r"(?i)\b(" + "|".join(SECRET_QUERY_PARAMS) + r")=([^&\s'\"]+)")


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    """Mask API keys in any text bound for a log or an exception message.

    ``secrets`` are literal values to mask wherever they appear, whatever surrounds
    them: a key echoed in an error body, a header, or a dict's repr has no
    ``param=`` in front of it for the pattern to find.
    """
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


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
    # Literal secret values a subclass holds (its API key), masked in everything
    # this class logs or raises on top of the ``param=`` pattern.
    _secrets: tuple[str, ...] = ()

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
        exhaustion. The API key in ``params`` never reaches a log, an exception
        message or a traceback -- see :func:`redact_secrets`.
        """
        params = dict(params or {})
        last_status: int | None = None
        for attempt in range(self.max_retries):
            is_last = attempt == self.max_retries - 1
            self.limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if is_last:
                    # `from None`, not `from exc`: the chained RequestException
                    # renders the full URL -- API key and all -- into the
                    # traceback. The redacted detail is carried over instead.
                    raise SourceError(
                        f"GET {self._redact(url)} failed: "
                        f"{type(exc).__name__}: {self._redact(str(exc))}"
                    ) from None
                self._backoff(attempt)
                continue

            self.request_count += 1
            nbytes = len(resp.content or b"")
            self.bytes_downloaded += nbytes

            if resp.status_code == 429:
                last_status = 429
                retry_after = self._retry_after_seconds(resp.headers.get("Retry-After"))
                logger.warning(
                    "HTTP 429 from %s (attempt %d)%s",
                    self.name,
                    attempt + 1,
                    f"; honoring Retry-After={retry_after}s" if retry_after else "",
                )
                if not is_last:
                    if retry_after is not None:
                        time.sleep(retry_after)
                    else:
                        self._backoff(attempt, base=2.0, floor=1.0)
                continue
            if resp.status_code >= 500:
                last_status = resp.status_code
                if not is_last:
                    self._backoff(attempt)
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                # Same reason as above -- HTTPError stringifies to
                # "<code> ... for url: <full url with apikey>".
                raise SourceError(
                    f"GET {self._redact(url)} returned {resp.status_code}: "
                    f"{self._redact(str(exc))}"
                ) from None

            try:
                data = resp.json()
            except ValueError as exc:
                # Safe to chain: a JSON decode error carries no URL.
                raise SourceError(
                    f"GET {self._redact(url)} returned non-JSON body"
                ) from exc

            if isinstance(data, dict) and "Error Message" in data:
                raise SourceError(
                    self._redact(f"{self.name} error: {data['Error Message']}")
                )
            return data, resp.status_code, nbytes

        raise SourceError(
            f"GET {self._redact(url)} exhausted retries"
            + (f" (last status {last_status})" if last_status else "")
        )

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._secrets)

    @staticmethod
    def _retry_after_seconds(value: str | None) -> int | None:
        """Parse a Retry-After header (delta-seconds form), capped. Returns None
        for an absent/HTTP-date value so the caller falls back to exponential."""
        if not value:
            return None
        try:
            secs = int(float(value))
        except (TypeError, ValueError):
            return None
        return max(0, min(secs, MAX_RETRY_AFTER_SECONDS))

    def _backoff(self, attempt: int, base: float = 2.0, floor: float = 0.0) -> None:
        delay = floor + (base**attempt) + random.uniform(0, 0.5)
        time.sleep(delay)
