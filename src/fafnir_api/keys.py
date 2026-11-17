"""
API keys (ADR 0021): making one, storing it, checking one.

A key is ``ffn_<key_id>_<secret>``. The ``key_id`` (12 hex characters) is public:
it names the row, appears in ``fafnir api key list`` and in the access log, and
lets the server find the one candidate row without scanning. The ``secret`` is 32
random bytes. ``ops.api_key`` stores the SHA-256 of the whole key and nothing
else of it, so neither the table, a backup of it, nor anything that can read it
yields a key that works.

A plain SHA-256 and not a password hash, deliberately. A slow, salted hash exists
to protect a secret a person chose, which can be guessed; this secret is 256
random bits, which cannot, and the check runs on every request.

The functions here take what they need (a token, a row, a clock) and touch no
database, so the rules are testable as rules. :class:`DatabaseKeyStore` is the one
place that reads ``ops.api_key``, and the ``fafnir api key`` commands the one
place that writes it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

PREFIX = "ffn"
_TOKEN_RE = re.compile(r"^ffn_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
#: What a key may be called: it becomes part of ``application_name`` in
#: ``pg_stat_activity`` and of every access-log line, so it is kept to characters
#: that need no quoting in either.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


class KeyRefused(Exception):
    """Why a presented key was refused. The message is safe to return to the
    caller: it says which rule failed, never anything about another key."""


@dataclass(frozen=True)
class KeyRecord:
    """A row of ``ops.api_key``, as much as authentication needs."""

    key_id: str
    name: str
    key_sha256: str
    expires_at: Optional[dt.datetime] = None
    revoked_at: Optional[dt.datetime] = None


def generate() -> tuple[str, str]:
    """A new key: ``(key_id, token)``. The token is shown once and never stored."""
    key_id = secrets.token_hex(6)
    return key_id, f"{PREFIX}_{key_id}_{secrets.token_urlsafe(32)}"


def key_id_of(token: str) -> Optional[str]:
    """The ``key_id`` a well-formed token names, else None."""
    match = _TOKEN_RE.match(token or "")
    return match.group(1) if match else None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def authenticate(
    token: str,
    lookup: Callable[[str], Optional[KeyRecord]],
    now: Optional[dt.datetime] = None,
) -> KeyRecord:
    """The key ``token`` is, or :class:`KeyRefused` saying why it is not one.

    An unknown ``key_id`` and a wrong secret get the same answer, and the digests
    are compared in constant time, so a refusal tells a guesser nothing about how
    close the guess was.
    """
    key_id = key_id_of(token)
    if key_id is None:
        raise KeyRefused("not a fafnir API key")
    record = lookup(key_id)
    if record is None or not hmac.compare_digest(digest(token), record.key_sha256):
        raise KeyRefused("unknown key")
    if record.revoked_at is not None:
        raise KeyRefused("this key was revoked")
    now = now or dt.datetime.now(dt.timezone.utc)
    if record.expires_at is not None and record.expires_at <= now:
        raise KeyRefused("this key has expired")
    return record


_LOOKUP_SQL = (
    "SELECT key_id, name, key_sha256, expires_at, revoked_at "
    "FROM ops.api_key WHERE key_id = %s"
)


class DatabaseKeyStore:
    """``ops.api_key``, read through a short cache.

    Every request authenticates, and a ``duk`` command makes several, so the row
    is remembered for ``ttl`` seconds. That is also how long a revocation can take
    to bite on a running server: the price of not opening a second connection per
    request, and bounded. A miss is remembered too, so a stream of made-up
    ``key_id`` values costs one query each and no more; the cache is emptied when
    it grows past ``max_entries``, so that stream cannot grow it without limit.
    """

    def __init__(self, dsn: str, *, ttl: float = 30.0, max_entries: int = 2048):
        self._dsn = dsn
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Optional[KeyRecord]]] = {}

    def lookup(self, key_id: str) -> Optional[KeyRecord]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key_id)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        record = self._read(key_id)
        with self._lock:
            if len(self._cache) >= self._max:
                self._cache.clear()
            self._cache[key_id] = (now, record)
        return record

    def _read(self, key_id: str) -> Optional[KeyRecord]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            row = conn.execute(_LOOKUP_SQL, (key_id,)).fetchone()
        return KeyRecord(**row) if row else None
