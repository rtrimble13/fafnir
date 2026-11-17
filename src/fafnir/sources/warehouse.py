"""
The warehouse's own HTTP API (ADR 0021): the request, and nothing else.

Not a vendor, but the same rule applies to it (ADR 0017 §2): ``duk`` keeps no HTTP
code, so the one place a request is made is this package. ``duk.datasource.remote``
decides what to ask and what an answer means; this module sends the bytes.

Like the rest of ``fafnir.sources`` it imports only the standard library and
``requests``, so it knows nothing of the wire format, the reads or the warehouse:
it posts a JSON body with a bearer key and hands back the status, the headers and
the decoded body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from fafnir.sources.base import SourceError, redact_secrets

#: Seconds to wait for the connection, then for the response. The read timeout is
#: longer than any statement the API's role may run, so the server's own "that
#: took too long" arrives before the client gives up on it.
TIMEOUT = (10, 120)

_session: Optional[requests.Session] = None


class Unreachable(SourceError):
    """No HTTP answer was had. ``kind`` is ``tls``, ``timeout`` or ``connect``; the
    message has the key redacted."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Answer:
    status: int
    headers: Any
    #: The decoded JSON body, or None when the body was not JSON (a proxy's error
    #: page, an empty 502).
    body: Any


def _http() -> requests.Session:
    """One session per process: a ``duk`` command makes several reads, and each
    would otherwise pay a TLS handshake."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def post(url: str, *, key: str, payload: Any, user_agent: str) -> Answer:
    """POST ``payload`` as JSON to ``url`` with ``key`` as the bearer credential.

    The key goes in the ``Authorization`` header and nowhere else. Raises
    :class:`Unreachable` when there is no response to return.
    """
    try:
        response = _http().post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "User-Agent": user_agent},
            timeout=TIMEOUT,
        )
    except requests.exceptions.SSLError as exc:
        raise Unreachable("tls", redact_secrets(str(exc), (key,))) from exc
    except requests.exceptions.Timeout as exc:
        raise Unreachable("timeout", redact_secrets(str(exc), (key,))) from exc
    except requests.exceptions.RequestException as exc:
        raise Unreachable("connect", type(exc).__name__) from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    return Answer(response.status_code, response.headers, body)
