"""
SharadarClient (plan SA-0106, #47): HTTP mocked, no network.

The requests follow what Sharadar documents (the client's docstring, item by
item); the payloads are synthetic. They pin what the client sends and what it does
with each answer, not that Sharadar answers so: that is the probe's job (#49).
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
import requests
from click.testing import CliRunner

from fafnir import cli
from fafnir.sources import base as base_mod
from fafnir.sources import sharadar as mod
from fafnir.sources.base import SourceError, redact_secrets
from fafnir.sources.sharadar import DEFAULT_BASE_URL, SharadarClient

KEY = "test-sharadar-key-not-a-secret"
SIGNED = (
    "https://bulk.example/exports/SEP.csv.gz?X-Amz-Credential=AKIA123%2Fus"
    "&X-Amz-Signature=deadbeefcafe&X-Amz-Expires=600"
)


class _Resp:
    def __init__(self, status=200, body=None, *, raw=None, headers=None, url=""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.content = raw if raw is not None else json.dumps(body).encode()
        self.url = url

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: for url: {self.url}"
            )

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    """Answers each GET from a queue; records what was asked, headers included."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []
        self.redirects: list[bool] = []

    def get(
        self,
        url,
        params=None,
        headers=None,
        timeout=None,
        stream=False,
        allow_redirects=True,
    ):
        self.calls.append((url, dict(params or {})))
        self.headers.append(dict(headers or {}))
        self.redirects.append(allow_redirects)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _client(*responses, **kw):
    c = SharadarClient(KEY, rate_per_min=10_000, **kw)
    c._session = _Session(*responses)
    return c


def _datatable(rows):
    names = list(rows[0]) if rows else ["ticker"]
    return {
        "datatable": {
            "columns": [{"name": n} for n in names],
            "data": [[r[n] for n in names] for r in rows],
        }
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# Requests and payloads
# ---------------------------------------------------------------------------


def test_ping_asks_one_table_with_the_key_in_a_header_only():
    c = _client(_Resp(body=_datatable([{"ticker": "AAPL", "name": "Apple"}])))
    r = c.ping(ticker="aapl")
    [(url, params)] = c._session.calls
    assert url == f"{DEFAULT_BASE_URL}/tickers"
    assert params == {"ticker": "AAPL", "format": "json"}  # no key in the URL
    assert c._session.headers == [{"x-api-key": KEY}]
    assert (r["rows"], r["columns"]) == (1, ["name", "ticker"])
    assert KEY not in json.dumps(r)
    assert c.request_count == 1 and c.bytes_downloaded > 0


def test_the_base_url_is_configurable():
    c = _client(_Resp(body=[]), base_url="https://other.example/v2/")
    c.ping(table="SEP")
    assert c._session.calls[0][0] == "https://other.example/v2/SEP"


def test_a_list_of_rows_is_read_as_is():
    c = _client(_Resp(body=[{"ticker": "A", "close": 1.5}, "junk"]))
    assert c.query("SEP", {"ticker": "A"}) == [{"ticker": "A", "close": 1.5}]


def _page(start, n):
    return _Resp(body=[{"ticker": "A", "i": i} for i in range(start, start + n)])


def test_query_pages_by_offset_and_passes_fields_and_filters(monkeypatch):
    monkeypatch.setattr(SharadarClient, "PAGE_ROWS", 2)
    c = _client(_page(0, 2), _page(2, 2), _page(4, 1))
    rows = c.query(
        "SEP", {"ticker": "A", "from": "2024-01-01"}, fields=["ticker", "date"]
    )
    assert [r["i"] for r in rows] == [0, 1, 2, 3, 4]
    params = [p for _, p in c._session.calls]
    assert [(p["limit"], p["offset"]) for p in params] == [(2, 0), (2, 2), (2, 4)]
    assert all(p["fields"] == "ticker,date" for p in params)
    assert all(p["from"] == "2024-01-01" for p in params)


def test_a_full_last_page_costs_one_empty_request(monkeypatch):
    monkeypatch.setattr(SharadarClient, "PAGE_ROWS", 2)
    c = _client(_page(0, 2), _Resp(body=[]))
    assert len(c.query("SEP")) == 2 and len(c._session.calls) == 2


def test_a_server_that_ignores_offset_stops_the_read(monkeypatch, caplog):
    monkeypatch.setattr(SharadarClient, "PAGE_ROWS", 2)
    c = _client(_page(0, 2), _page(0, 2))
    with caplog.at_level(logging.WARNING):
        assert len(c.query("SEP")) == 2
    assert "repeated the previous page" in caplog.text


def test_paging_that_never_ends_is_refused(monkeypatch):
    monkeypatch.setattr(SharadarClient, "PAGE_ROWS", 1)
    pages = [_page(i, 1) for i in range(3)]
    with pytest.raises(SourceError, match="belongs in a bulk download"):
        _client(*pages).query("SEP", max_pages=3)


def test_the_key_is_required():
    with pytest.raises(ValueError, match="key is required"):
        SharadarClient("")


def test_tables_are_the_ones_the_plan_reads():
    assert {"tickers", "actions", "SEP", "SFP", "SF1"} <= set(_client().tables())


# ---------------------------------------------------------------------------
# The key never leaves: not in an exception, a log line or a result
# ---------------------------------------------------------------------------


def _leaks(text: str) -> bool:
    return KEY in text


def test_an_http_error_is_redacted():
    # The client never puts the key in a URL; this one stands for a server or
    # proxy that echoes it, which the value-based mask still catches.
    url = f"{DEFAULT_BASE_URL}/SEP?ticker=A&api_key={KEY}"
    c = _client(_Resp(status=403, body={}, url=url))
    with pytest.raises(SourceError) as exc:
        c.query("SEP", {"ticker": "A"})
    assert not _leaks(str(exc.value)) and "403" in str(exc.value)
    # `from None`: a traceback never prints the HTTPError, whose text has the key.
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_a_transport_error_is_redacted():
    boom = requests.ConnectionError(
        f"Max retries exceeded with url: /SEP?api_key={KEY}"
    )
    c = _client(*[boom] * 4)
    with pytest.raises(SourceError) as exc:
        c.ping()
    assert not _leaks(str(exc.value))


def test_an_error_body_that_echoes_the_key_is_redacted():
    c = _client(_Resp(body={"error": f"invalid key {KEY}"}))
    with pytest.raises(SourceError) as exc:
        c.query("SEP")
    assert not _leaks(str(exc.value)) and "invalid key ***" in str(exc.value)


def test_nothing_logged_carries_the_key(caplog):
    url = f"{DEFAULT_BASE_URL}/SEP?api_key={KEY}"
    c = _client(
        _Resp(status=429, body={}, headers={"Retry-After": "1"}),
        _Resp(status=500, body={}),
        _Resp(body=_datatable([{"ticker": "A"}])),
        _Resp(status=401, body={}, url=url),
    )
    with caplog.at_level(logging.DEBUG):
        c.query("SEP")
        with pytest.raises(SourceError):
            c.query("SEP")
    assert caplog.records and not _leaks(caplog.text)


def test_redact_masks_a_literal_secret_and_signed_link_credentials():
    assert redact_secrets(f"{{'api_key': '{KEY}'}}", [KEY]) == "{'api_key': '***'}"
    out = redact_secrets(SIGNED)
    assert "deadbeefcafe" not in out and "AKIA123" not in out
    assert "X-Amz-Expires=600" in out


# ---------------------------------------------------------------------------
# Bulk export
# ---------------------------------------------------------------------------


def _redirect(link=SIGNED, status=302):
    return _Resp(status=status, body={}, headers={"Location": link})


@pytest.fixture()
def link_fetches(monkeypatch):
    """The pre-signed link is fetched with plain `requests.get`, not the client's
    session: what it was asked, and with which headers."""
    asked: list = []
    queue: list = []

    def get(url, **kw):
        asked.append((url, kw))
        return queue.pop(0)

    monkeypatch.setattr(mod.requests, "get", get)
    return asked, queue


def test_bulk_follows_the_redirect_without_the_key(tmp_path, link_fetches):
    asked, queue = link_fetches
    body = b"PK\x03\x04 synthetic zip bytes " * 1000
    queue.append(_Resp(raw=body))
    c = _client(_redirect())
    out = c.bulk_export("SEP", tmp_path, years="10")
    sha = hashlib.sha256(body).hexdigest()
    assert (out.table, out.bytes, out.sha256) == ("SEP", len(body), sha)
    assert out.path == tmp_path / f"{sha}.csv.gz"  # the link's own suffix
    assert out.path.read_bytes() == body
    [(url, params)] = c._session.calls
    assert (url, params) == (f"{DEFAULT_BASE_URL}/SEP", {"years": "10"})
    assert c._session.headers == [{"x-api-key": KEY}]
    assert c._session.redirects == [False]  # followed by hand, below
    [(link, kw)] = asked
    assert link == SIGNED and "headers" not in kw and kw["stream"] is True
    assert c.bytes_downloaded >= len(body)


def test_the_same_bulk_file_twice_is_one_file(tmp_path, link_fetches):
    _, queue = link_fetches
    queue.extend([_Resp(raw=b"same bytes"), _Resp(raw=b"same bytes")])
    c = _client(_redirect(), _redirect())
    first = c.bulk_export("SEP", tmp_path)
    second = c.bulk_export("SEP", tmp_path)
    assert first.path == second.path
    assert [p.name for p in tmp_path.iterdir()] == [first.path.name]


def test_a_broken_download_leaves_nothing_and_hides_the_link(tmp_path, link_fetches):
    class _Broken(_Resp):
        def iter_content(self, chunk_size=1):
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError(f"broke at {SIGNED}")

    _, queue = link_fetches
    queue.append(_Broken(raw=b"x"))
    with pytest.raises(SourceError) as exc:
        _client(_redirect()).bulk_export("SEP", tmp_path)
    assert "deadbeefcafe" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_bulk_without_a_redirect_says_why(tmp_path):
    c = _client(_Resp(status=403, body={"error": "Exceeds free tier"}))
    with pytest.raises(SourceError, match="HTTP 403 without a redirect: .*free tier"):
        c.bulk_export("SEP", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_bulk_years_are_checked(tmp_path):
    with pytest.raises(ValueError, match="years"):
        _client().bulk_export("SEP", tmp_path, years="3")


# ---------------------------------------------------------------------------
# fafnir source ping
# ---------------------------------------------------------------------------


@pytest.fixture()
def rc(tmp_path, monkeypatch):
    path = tmp_path / "fafnirrc"
    path.write_text('[api]\nsharadar_key = "rc-key-duk-only"\n')
    for var in ("SHARADAR_API_KEY", "SHARADAR_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return str(path)


def _ping(rc, monkeypatch, *responses):
    made = []

    def factory(key, **kw):
        made.append((key, kw))
        return _client(*responses, **{k: v for k, v in kw.items() if k == "base_url"})

    monkeypatch.setattr(mod, "SharadarClient", factory)
    out = CliRunner().invoke(cli.main, ["--config", rc, "source", "ping"])
    return out, made


def test_ping_without_a_key_names_the_env_var_and_ignores_duks(rc, monkeypatch):
    out, made = _ping(rc, monkeypatch)
    assert out.exit_code != 0 and made == []
    assert "SHARADAR_API_KEY" in out.output
    assert "[api].sharadar_key in the rc file is duk's" in out.output
    assert "rc-key-duk-only" not in out.output


def test_ping_reports_what_came_back(rc, monkeypatch):
    monkeypatch.setenv("SHARADAR_API_KEY", KEY)
    out, made = _ping(rc, monkeypatch, _Resp(body=_datatable([{"ticker": "AAPL"}])))
    assert out.exit_code == 0, out.output
    assert made[0][0] == KEY and made[0][1]["rate_per_min"] == 60
    assert "1 row(s)" in out.output and "sharadar: OK" in out.output
    assert not _leaks(out.output)


def test_ping_fails_on_no_rows_and_on_an_error(rc, monkeypatch):
    monkeypatch.setenv("SHARADAR_API_KEY", KEY)
    empty, _ = _ping(rc, monkeypatch, _Resp(body=[]))
    assert empty.exit_code != 0 and "no rows" in empty.output
    url = f"{DEFAULT_BASE_URL}/TICKERS?api_key={KEY}"
    denied, _ = _ping(rc, monkeypatch, _Resp(status=401, body={}, url=url))
    assert denied.exit_code != 0 and "ping FAILED" in denied.output
    assert not _leaks(denied.output)


# ---------------------------------------------------------------------------
# ops.ingestion_run: what a loader records about a Sharadar run holds no key
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_no_key_reaches_the_run_log(db):
    from fafnir.ingest.runlog import RunLog

    c = _client(
        _Resp(body=_datatable([{"ticker": "AAPL"}])),
        _Resp(status=403, body={}, url=f"{DEFAULT_BASE_URL}/SEP?api_key={KEY}"),
    )
    with RunLog(db, source="sharadar", endpoint="ping") as run:
        run.params["ping"] = c.ping()
        run.bytes_downloaded = c.bytes_downloaded
    with pytest.raises(SourceError):
        with RunLog(db, source="sharadar", endpoint="bulk:SEP") as run:
            run.params["base_url"] = c.base_url
            c.query("SEP")
    stored = db.fetchall(
        "SELECT params::text AS p, coalesce(error_message, '') AS e "
        "FROM ops.ingestion_run WHERE source = 'sharadar'"
    )
    assert len(stored) == 2
    assert not any(_leaks(r["p"] + r["e"]) for r in stored)
    assert any("403" in r["e"] for r in stored)
