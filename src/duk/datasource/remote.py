"""
The warehouse over its HTTP API (ADR 0021): the client half.

``duk.datasource.db`` sends a read here when the warehouse is configured as an
:class:`~duk.datasource.base.ApiAddress` instead of a libpq DSN. :func:`call`
posts the read's name and arguments to the host, which runs the same
``duk.datasource.db`` function next to PostgreSQL and returns what it returned,
in the format :mod:`duk.datasource.wire` defines. Nothing is computed here: no
SQL, no resolution, no shaping. That is the point -- the two transports cannot
disagree about a series, because only one of them has an implementation.

The HTTP request itself is ``fafnir.sources.warehouse``'s: ``duk`` keeps no HTTP
code of its own (ADR 0017 §2), so this module decides what to ask and what an
answer means, and that one sends it.

Every failure is a :class:`DataSourceError` (or the server's own
``IdentifierError``), which is what the CLI already reports as one line: this
module adds no exception a command has to learn.

The key travels in the ``Authorization`` header, never in the URL, and only over
HTTPS -- plain HTTP is accepted for a loopback address alone, which is a test
server or a forwarded port.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from duk import identifiers
from duk.datasource import wire
from duk.datasource.base import ApiAddress, DataSourceError

_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _endpoint(address: ApiAddress, name: str) -> str:
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DataSourceError(
            f"'{address}' is not a fafnir API address: expected a URL such as "
            "https://fafnir.example.com (FAFNIR_API_URL, or [api].fafnir_url in "
            "~/.dukrc)"
        )
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise DataSourceError(
            f"refusing to send the API key to {parts.hostname} over plain HTTP: "
            "use an https:// address"
        )
    if not address.api_key:
        raise DataSourceError(
            "no fafnir API key configured. Set FAFNIR_API_KEY, or [api].fafnir_key "
            "in ~/.dukrc; a key is issued on the host with `fafnir api key create`"
        )
    return f"{address}/v1/reads/{name}"


def _error_of(body: Any) -> tuple[str, str]:
    """``(code, message)`` from an error response, tolerating a body that is not
    this API's (a proxy's HTML error page, an empty 502)."""
    try:
        return str(body["error"]["code"]), str(body["error"]["message"])
    except (TypeError, KeyError):
        return "", ""


def call(address: ApiAddress, name: str, kwargs: dict) -> Any:
    """Run the warehouse read ``name`` with ``kwargs`` on the host and return its
    result, as ``duk.datasource.db`` would have returned it here."""
    # The request itself is fafnir.sources' (ADR 0017 §2: duk keeps no HTTP code);
    # imported here so that a command reading through a DSN never loads requests.
    from duk import __version__
    from fafnir.sources import warehouse

    url = _endpoint(address, name)
    host = urlsplit(address).hostname
    try:
        answer = warehouse.post(
            url,
            key=address.api_key,
            payload={"args": wire.encode(kwargs)},
            user_agent=f"duk/{__version__}",
        )
    except warehouse.Unreachable as exc:
        if exc.kind == "tls":
            raise DataSourceError(
                f"cannot verify the certificate of the fafnir API at {host}: {exc}"
            ) from exc
        if exc.kind == "timeout":
            raise DataSourceError(
                f"the fafnir API at {host} did not answer in time"
            ) from exc
        raise DataSourceError(f"cannot reach the fafnir API at {host}: {exc}") from exc

    if answer.status == 200:
        try:
            return wire.decode(answer.body["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DataSourceError(
                f"{host} answered, but not as a fafnir API this duk understands "
                f"({type(exc).__name__}). Check the address, and that the host's "
                "fafnir is not older than this duk."
            ) from exc

    code, message = _error_of(answer.body)
    if code == "identifier":
        raise identifiers.IdentifierError(message)
    if code == "datasource":
        raise DataSourceError(message)
    if answer.status == 401:
        raise DataSourceError(
            f"the fafnir API at {host} refused the key"
            + (f": {message}" if message else "")
            + ". Check FAFNIR_API_KEY / [api].fafnir_key; `fafnir api key list` on "
            "the host shows whether it was revoked or has expired"
        )
    if answer.status == 404 and code == "unknown_read":
        raise DataSourceError(
            f"the fafnir API at {host} does not serve '{name}': the host's fafnir "
            "is older than this duk. Upgrade the host, or read through a DSN"
        )
    if answer.status == 429:
        wait = answer.headers.get("Retry-After")
        raise DataSourceError(
            f"the fafnir API at {host} is rate-limiting this key"
            + (f"; retry in {wait}s" if wait else "")
        )
    raise DataSourceError(
        f"the fafnir API at {host} answered HTTP {answer.status}"
        + (f": {message}" if message else "")
    )
