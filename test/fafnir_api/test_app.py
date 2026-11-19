"""The HTTP API's application (ADR 0021), with no database and no server.

The reads here are stand-ins: what is under test is who gets in, what a request
may name, and what a failure tells the caller.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
from decimal import Decimal

import pytest

from duk import identifiers
from duk.datasource import wire
from duk.datasource.base import DataSourceError
from fafnir_api import __version__, keys
from fafnir_api.app import MAX_BODY, RateLimiter, make_app

KEY_ID, TOKEN = keys.generate()
RECORD = keys.KeyRecord(KEY_ID, "rob-laptop", keys.digest(TOKEN))


def _lookup(key_id):
    return RECORD if key_id == KEY_ID else None


def echo(*, dsn, symbol, limit=None):
    return {"dsn": dsn, "symbol": symbol, "limit": limit, "price": Decimal("1.50")}


def refuses(*, dsn):
    raise DataSourceError("No security with security_id 9")


def bad_identifier(*, dsn):
    raise identifiers.IdentifierError("'cik:x' is not a CIK")


def explodes(*, dsn):
    raise RuntimeError("secret detail: /etc/fafnir/env")


READS = {f.__name__: f for f in (echo, refuses, bad_identifier, explodes)}


@pytest.fixture()
def app():
    return make_app(dsn="dbname=fafnir user=fafnir_api", lookup=_lookup, reads=READS)


def call(app, method, path, *, token=TOKEN, args=None, body=None, headers=None):
    raw = body if body is not None else json.dumps({"args": wire.encode(args or {})})
    raw = raw.encode() if isinstance(raw, str) else raw
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "REMOTE_ADDR": "127.0.0.1",
        **(headers or {}),
    }
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    seen = {}

    def start_response(status, response_headers):
        seen["status"] = int(status.split()[0])
        seen["headers"] = dict(response_headers)

    payload = json.loads(b"".join(app(environ, start_response)))
    return seen["status"], payload, seen["headers"]


# -- who gets in -------------------------------------------------------------


def test_health_needs_no_key_and_says_nothing(app):
    status, payload, _ = call(app, "GET", "/v1/health", token=None)
    assert (status, payload) == (200, {"status": "ok"})


def test_a_read_without_a_key_is_refused(app):
    status, payload, headers = call(app, "POST", "/v1/reads/echo", token=None)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize("token", ["nope", keys.generate()[1]])
def test_a_wrong_key_is_refused(app, token):
    status, payload, _ = call(app, "POST", "/v1/reads/echo", token=token)
    assert status == 401
    assert "rob-laptop" not in json.dumps(payload)


def test_a_key_in_the_query_string_is_not_a_key(app):
    status, _, _ = call(
        app,
        "POST",
        "/v1/reads/echo",
        token=None,
        headers={"QUERY_STRING": f"api_key={TOKEN}"},
    )
    assert status == 401


def test_a_revoked_key_is_refused_and_told_why():
    revoked = keys.KeyRecord(
        KEY_ID,
        "rob-laptop",
        keys.digest(TOKEN),
        revoked_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    app = make_app(dsn="dbname=d", lookup=lambda _: revoked, reads=READS)
    status, payload, _ = call(app, "POST", "/v1/reads/echo", args={"symbol": "A"})
    assert status == 401
    assert "revoked" in payload["error"]["message"]


def test_the_listing_names_the_key_and_the_reads(app):
    status, payload, _ = call(app, "GET", "/v1/reads")
    assert status == 200
    assert payload == {
        "version": __version__,
        "key": "rob-laptop",
        "reads": sorted(READS),
    }


# -- what a request may name ---------------------------------------------------


def test_a_read_runs_with_its_arguments_and_typed_result(app):
    status, payload, headers = call(
        app, "POST", "/v1/reads/echo", args={"symbol": "AAPL", "limit": 5}
    )
    assert status == 200
    result = wire.decode(payload["result"])
    assert result["symbol"] == "AAPL" and result["limit"] == 5
    assert result["price"] == Decimal("1.50")
    assert headers["Cache-Control"] == "no-store"


def test_the_query_is_attributed_to_the_key_in_application_name(app):
    _, payload, _ = call(app, "POST", "/v1/reads/echo", args={"symbol": "A"})
    assert "application_name=fafnir-api:rob-laptop" in payload["result"]["dsn"]
    assert "user=fafnir_api" in payload["result"]["dsn"]


def test_a_request_cannot_choose_the_connection(app):
    status, payload, _ = call(
        app,
        "POST",
        "/v1/reads/echo",
        args={"symbol": "A", "dsn": "dbname=fafnir user=fafnir_ingest"},
    )
    assert status == 400
    assert "dsn" in payload["error"]["message"]


def test_only_registered_reads_exist(app):
    for name in ("_connect", "__init__", "os.system", "echo/../refuses"):
        status, payload, _ = call(app, "POST", f"/v1/reads/{name}")
        assert status == 404, name
        assert payload["error"]["code"] == "unknown_read"


def test_the_default_surface_is_what_duk_marks_as_served():
    from duk.datasource import db

    app = make_app(dsn="dbname=d", lookup=_lookup)
    _, payload, _ = call(app, "GET", "/v1/reads")
    assert payload["reads"] == sorted(db.SERVED_READS)
    assert all(not name.startswith("_") for name in payload["reads"])


@pytest.mark.parametrize(
    "args", [{}, {"symbol": "A", "nonsense": 1}], ids=["missing", "unexpected"]
)
def test_arguments_the_read_does_not_take_are_a_bad_request(app, args):
    status, payload, _ = call(app, "POST", "/v1/reads/echo", args=args)
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '{"args": [1]}'])
def test_a_body_that_is_not_arguments_is_a_bad_request(app, body):
    status, _, _ = call(app, "POST", "/v1/reads/echo", body=body)
    assert status == 400


def test_an_oversized_body_is_refused_unread(app):
    status, _, _ = call(app, "POST", "/v1/reads/echo", body=b"x" * (MAX_BODY + 1))
    assert status == 413


def test_other_paths_and_methods(app):
    assert call(app, "GET", "/")[0] == 404
    assert call(app, "GET", "/v1/reads/echo")[0] == 405
    assert call(app, "POST", "/v1/reads")[0] == 405


# -- what a failure tells the caller -------------------------------------------


def test_a_reads_own_refusal_carries_its_message(app):
    status, payload, _ = call(app, "POST", "/v1/reads/refuses")
    assert status == 422
    assert payload["error"] == {
        "code": "datasource",
        "message": "No security with security_id 9",
    }


def test_a_malformed_identifier_is_reported_as_one(app):
    status, payload, _ = call(app, "POST", "/v1/reads/bad_identifier")
    assert (status, payload["error"]["code"]) == (422, "identifier")


def test_an_unexpected_failure_says_nothing_but_a_request_id(app, caplog):
    with caplog.at_level(logging.ERROR, logger="fafnir_api"):
        status, payload, _ = call(app, "POST", "/v1/reads/explodes")
    assert status == 500
    message = payload["error"]["message"]
    assert "secret detail" not in message and "/etc" not in message
    request_id = message.split("request ")[1].rstrip(")")
    # ...and the log has the whole of it, under that id.
    assert request_id in caplog.text and "secret detail" in caplog.text


def test_a_database_outage_and_a_timeout_are_told_apart():
    import psycopg

    def down(*, dsn):
        raise psycopg.OperationalError("connection refused")

    def slow(*, dsn):
        raise psycopg.errors.QueryCanceled("statement timeout")

    app = make_app(dsn="dbname=d", lookup=_lookup, reads={"down": down, "slow": slow})
    assert call(app, "POST", "/v1/reads/down")[0] == 503
    assert call(app, "POST", "/v1/reads/slow")[0] == 504


def test_the_access_log_carries_the_key_name_and_never_the_key(app, caplog):
    with caplog.at_level(logging.INFO, logger="fafnir_api"):
        call(app, "POST", "/v1/reads/refuses")
        call(app, "POST", "/v1/reads/echo", args={"symbol": "SECRETPOSITION"})
    assert "key=rob-laptop read=refuses" in caplog.text
    assert "status=422" in caplog.text
    assert TOKEN not in caplog.text and "SECRETPOSITION" not in caplog.text


# -- rate limit ----------------------------------------------------------------


def test_a_key_over_its_rate_is_told_when_to_come_back():
    app = make_app(dsn="dbname=d", lookup=_lookup, reads=READS, rate_per_minute=2)
    statuses = [call(app, "GET", "/v1/reads")[0] for _ in range(3)]
    assert statuses == [200, 200, 429]
    _, payload, headers = call(app, "GET", "/v1/reads")
    assert payload["error"]["code"] == "rate_limited"
    assert int(headers["Retry-After"]) >= 1


def test_the_bucket_refills_and_keys_do_not_share_one():
    now = [0.0]
    limiter = RateLimiter(60, clock=lambda: now[0])
    assert all(limiter.wait_for("a") == 0 for _ in range(60))
    assert limiter.wait_for("a") > 0
    assert limiter.wait_for("b") == 0
    now[0] += 1.0  # one second at 60/min is one request
    assert limiter.wait_for("a") == 0
    assert limiter.wait_for("a") > 0


def test_a_rate_of_zero_is_no_limit():
    limiter = RateLimiter(0)
    assert all(limiter.wait_for("a") == 0 for _ in range(1000))
