"""
The WSGI application (ADR 0021).

Three routes, all JSON:

``GET /v1/health``
    ``{"status": "ok"}``, unauthenticated: something for the reverse proxy and a
    monitor to ask. It says nothing about the warehouse, the version or the keys.
``GET /v1/reads``
    The reads this server serves, its version, and the name of the key asking.
``POST /v1/reads/<name>``
    Run one read. The body is ``{"args": {...}}`` in :mod:`duk.datasource.wire`
    form; the answer is ``{"result": ...}`` in the same form.

Every route but health needs ``Authorization: Bearer <key>``. A key in the query
string is not accepted: a URL is written to access logs, proxies and shell
history, and a header is not.

What a request can do is fixed in three places, none of them here to be got
wrong: the reads are ``duk.datasource.db.SERVED_READS`` and nothing else (there
is no SQL, table or column in a request); the SQL those reads run is
parameterised; and the connection is a PostgreSQL role that can read ``mart`` and
``ref`` and write nothing (``fafnir-api`` refuses to serve as one that can). The
last is the boundary. The first two are why it is not expected to be tested.

A failure is ``{"error": {"code": ..., "message": ...}}``. A read's own refusal
(``DataSourceError``, ``IdentifierError``) carries its message, since that is
what ``duk`` prints. Anything unexpected carries a request id and nothing else,
and the traceback goes to the log under that id.
"""

from __future__ import annotations

import inspect
import json
import logging
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Optional

from duk import identifiers
from duk.datasource import wire
from duk.datasource.base import DataSourceError
from fafnir_api import __version__, keys

log = logging.getLogger("fafnir_api")

#: Largest request body read. A read's arguments are a ticker and some dates; the
#: largest legitimate one is a constituent list of a few thousand symbols.
MAX_BODY = 1 << 20

#: Requests per key per minute, by default. A `duk` command is a handful of
#: reads; a script walking a few hundred tickers is a few hundred. This bounds a
#: runaway loop, not ordinary use.
DEFAULT_RATE = 600


class _Refusal(Exception):
    def __init__(self, status: int, code: str, message: str, headers=()):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers = list(headers)


class RateLimiter:
    """A token bucket per key: ``per_minute`` requests, refilled continuously.
    ``per_minute`` of 0 or less turns it off."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self._capacity = float(per_minute)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def wait_for(self, key_id: str) -> float:
        """0 when the request may proceed (and is counted), else the seconds
        until the key's next request would."""
        if self._capacity <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            tokens, seen = self._buckets.get(key_id, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - seen) * self._capacity / 60.0)
            if tokens >= 1.0:
                self._buckets[key_id] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key_id] = (tokens, now)
            return (1.0 - tokens) * 60.0 / self._capacity


_STATUS = {
    200: "200 OK",
    400: "400 Bad Request",
    401: "401 Unauthorized",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    413: "413 Content Too Large",
    422: "422 Unprocessable Content",
    429: "429 Too Many Requests",
    500: "500 Internal Server Error",
    503: "503 Service Unavailable",
    504: "504 Gateway Timeout",
}


def _application_name(dsn: str, key_name: str) -> str:
    """``dsn`` with the key's name in ``application_name``, so
    ``pg_stat_activity`` and the server log say whose request a query is."""
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, application_name=f"fafnir-api:{key_name}")


def _bearer(environ: dict) -> str:
    header = environ.get("HTTP_AUTHORIZATION", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _Refusal(
            401,
            "unauthorized",
            "send the API key as 'Authorization: Bearer <key>'",
            [("WWW-Authenticate", 'Bearer realm="fafnir"')],
        )
    return token.strip()


def _arguments(environ: dict) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = -1
    if length < 0:
        raise _Refusal(400, "bad_request", "Content-Length is not a number")
    if length > MAX_BODY:
        raise _Refusal(413, "too_large", f"the request is over {MAX_BODY} bytes")
    raw = environ["wsgi.input"].read(length) if length else b""
    try:
        body = json.loads(raw or b"{}")
        args = wire.decode(body.get("args") or {})
    except (ValueError, AttributeError, wire.WireError) as exc:
        raise _Refusal(400, "bad_request", f"the body is not a read's arguments: {exc}")
    if not isinstance(args, dict) or not all(isinstance(k, str) for k in args):
        raise _Refusal(400, "bad_request", "'args' must be an object")
    if "dsn" in args:
        raise _Refusal(400, "bad_request", "'dsn' is not an argument of any read")
    return args


def _client(environ: dict) -> str:
    """Who asked, for the log: the proxy's ``X-Forwarded-For`` when the peer is
    the proxy (loopback), else the peer."""
    peer = environ.get("REMOTE_ADDR", "-")
    forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and peer in ("127.0.0.1", "::1"):
        return forwarded.split(",")[-1].strip()[:64]
    return peer


def make_app(
    *,
    dsn: str,
    lookup: Callable[[str], Optional[keys.KeyRecord]],
    reads: Optional[dict[str, Callable]] = None,
    rate_per_minute: int = DEFAULT_RATE,
) -> Callable:
    """The application: reads run with ``dsn``, keys are found by ``lookup``.

    ``reads`` defaults to every read ``duk.datasource.db`` serves; a test passes
    its own.
    """
    if reads is None:
        from duk.datasource import db

        reads = db.SERVED_READS
    limiter = RateLimiter(rate_per_minute)
    # Fails here, at start, for a DSN libpq cannot parse -- not on the first
    # request, as a 500 with nothing to say.
    _application_name(dsn, "startup")

    def run(name: str, key: keys.KeyRecord, environ: dict) -> Any:
        fn = reads.get(name)
        if fn is None:
            raise _Refusal(404, "unknown_read", f"this server has no read '{name}'")
        args = _arguments(environ)
        try:
            inspect.signature(fn).bind(dsn=dsn, **args)
        except TypeError as exc:
            raise _Refusal(400, "bad_request", f"{name}: {exc}")
        return fn(dsn=_application_name(dsn, key.name), **args)

    def route(environ: dict, seen: dict) -> Any:
        """The response payload, or a :class:`_Refusal`. ``seen`` is filled with
        the key and the read as each becomes known, so a request that fails after
        authenticating is still logged under its key."""
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "") or "/"
        if path == "/v1/health":
            return {"status": "ok"}
        if path != "/v1/reads" and not path.startswith("/v1/reads/"):
            raise _Refusal(404, "not_found", "no such path")
        try:
            key = keys.authenticate(_bearer(environ), lookup)
        except keys.KeyRefused as exc:
            raise _Refusal(
                401,
                "unauthorized",
                str(exc),
                [("WWW-Authenticate", 'Bearer realm="fafnir"')],
            )
        seen["key"] = key.name
        wait = limiter.wait_for(key.key_id)
        if wait > 0:
            raise _Refusal(
                429,
                "rate_limited",
                "too many requests for this key",
                [("Retry-After", str(int(wait) + 1))],
            )
        if path == "/v1/reads":
            if method != "GET":
                raise _Refusal(405, "method_not_allowed", "GET /v1/reads")
            return {"version": __version__, "key": key.name, "reads": sorted(reads)}
        name = path[len("/v1/reads/") :]
        if method != "POST":
            raise _Refusal(405, "method_not_allowed", f"POST /v1/reads/{name}")
        seen["read"] = name
        return {"result": wire.encode(run(name, key, environ))}

    def answer(environ: dict, seen: dict) -> tuple[int, dict, list]:
        try:
            return 200, route(environ, seen), []
        except _Refusal as exc:
            return exc.status, _error(exc.code, exc.message), exc.headers
        except DataSourceError as exc:
            return 422, _error("datasource", str(exc)), []
        except identifiers.IdentifierError as exc:
            return 422, _error("identifier", str(exc)), []
        except Exception as exc:  # noqa: BLE001 -- the last line of the service
            status, code, message = _classify(exc)
            request_id = secrets.token_hex(4)
            log.error("request %s failed: %s", request_id, exc, exc_info=status == 500)
            return status, _error(code, f"{message} (request {request_id})"), []

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        started = time.monotonic()
        seen = {"key": "-", "read": "-"}
        status, payload, headers = answer(environ, seen)
        try:
            body = json.dumps(payload, allow_nan=False).encode()
        except (TypeError, ValueError):  # pragma: no cover -- wire.encode's contract
            log.exception("a result could not be written as JSON")
            status = 500
            body = json.dumps(_error("internal", "internal error")).encode()
        start_response(
            _STATUS[status],
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                *headers,
            ],
        )
        # One line per request, and nothing of the request in it but its path: no
        # header, no argument, so no key and no position can reach the journal.
        log.info(
            "%s %s %s status=%d key=%s read=%s ms=%d bytes=%d",
            _client(environ),
            environ.get("REQUEST_METHOD", "-"),
            (environ.get("PATH_INFO") or "/")[:120],
            status,
            seen["key"],
            seen["read"],
            (time.monotonic() - started) * 1000,
            len(body),
        )
        return [body]

    return app


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _classify(exc: Exception) -> tuple[int, str, str]:
    """An unexpected failure as ``(status, code, message)``. The message names the
    kind of failure and never quotes the exception: that text can hold a DSN, a
    constraint name or a path, and belongs in the log."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover -- psycopg is a hard dependency
        return 500, "internal", "internal error"
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return 504, "timeout", "the warehouse query ran past the API's time limit"
    if isinstance(exc, psycopg.OperationalError):
        return 503, "unavailable", "the warehouse database is not available"
    return 500, "internal", "internal error"
