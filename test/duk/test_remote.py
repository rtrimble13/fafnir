"""duk reading the warehouse through its HTTP API (ADR 0021): the client half.

Real HTTP, to a server on a loopback port, with stand-in reads behind it. The
database half -- that the same command prints the same thing over both
transports -- is test/fafnir_api/test_api_integration.py.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from duk import identifiers
from duk.config import DukConfig
from duk.datasource import db as ds_db
from duk.datasource import remote
from duk.datasource.base import ApiAddress, DataSourceError
from fafnir_api import keys
from fafnir_api.app import make_app

KEY_ID, TOKEN = keys.generate()
RECORD = keys.KeyRecord(KEY_ID, "test", keys.digest(TOKEN))
CALLS = []


def price_history(*, dsn, symbol, start_date, end_date, adjusted=False):
    CALLS.append({"symbol": symbol, "start_date": start_date, "adjusted": adjusted})
    frame = pd.DataFrame(
        {"close": [1.5, 2.5]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]).rename("date"),
    )
    return frame


def exact_bars(*, dsn, symbol):
    return [{"date": dt.date(2024, 1, 2), "close": Decimal("189.8400")}]


def resolve_identifier(*, dsn, identifier):
    return [{"scheme": identifier.scheme, "value": identifier.value}]


def resolve_company(*, dsn, query):
    return identifiers.parse(query) and []


def company_summary(*, dsn, security_id):
    raise DataSourceError(f"No security with security_id {security_id}")


def calendars(*, dsn):
    raise RuntimeError("boom")


READS = {
    f.__name__: f
    for f in (
        price_history,
        exact_bars,
        resolve_identifier,
        resolve_company,
        company_summary,
        calendars,
    )
}


@pytest.fixture()
def address(serve_wsgi):
    CALLS.clear()
    app = make_app(
        dsn="dbname=fafnir",
        lookup=lambda key_id: RECORD if key_id == KEY_ID else None,
        reads=READS,
    )
    return ApiAddress(serve_wsgi(app), TOKEN)


# -- dispatch: the same call, either transport ---------------------------------


def test_a_dsn_runs_the_read_here_and_an_address_runs_it_there(address, monkeypatch):
    def no_database(dsn):
        raise AssertionError("an API address must not open a database connection")

    monkeypatch.setattr(ds_db, "_connect", no_database)
    frame = ds_db.price_history(
        dsn=address,
        symbol="AAPL",
        start_date="2024-01-01",
        end_date=None,
        adjusted=True,
    )
    assert CALLS == [{"symbol": "AAPL", "start_date": "2024-01-01", "adjusted": True}]
    assert list(frame["close"]) == [1.5, 2.5]
    assert isinstance(frame.index, pd.DatetimeIndex) and frame.index.name == "date"


def test_exact_values_arrive_exact(address):
    (bar,) = ds_db.exact_bars(dsn=address, symbol="AAPL")
    assert bar == {"date": dt.date(2024, 1, 2), "close": Decimal("189.8400")}
    assert str(bar["close"]) == "189.8400"


def test_an_identifier_argument_crosses_the_wire(address):
    found = ds_db.resolve_identifier(
        dsn=address, identifier=identifiers.parse("cik:0000051143")
    )
    assert found == [{"scheme": "cik", "value": "51143"}]


def test_every_whole_read_in_the_datasource_is_served():
    # A read added to duk.datasource.db without @_served works over a DSN and
    # fails over the API with psycopg trying to parse a URL. Decorating it is the
    # whole of serving it, so the omission is caught here.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ds_db))
    public = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert public == set(ds_db.SERVED_READS)


def test_served_reads_take_the_connection_by_keyword_only():
    import inspect

    for name, fn in ds_db.SERVED_READS.items():
        params = inspect.signature(fn).parameters
        assert params["dsn"].kind is inspect.Parameter.KEYWORD_ONLY, name
        positional = [
            p
            for p in params.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert not positional, name


# -- failures arrive as what the CLI already reports ----------------------------


def test_the_reads_own_error_is_raised_with_its_message(address):
    with pytest.raises(DataSourceError, match="No security with security_id 9"):
        ds_db.company_summary(dsn=address, security_id=9)


def test_a_malformed_identifier_is_still_an_identifier_error(address):
    with pytest.raises(identifiers.IdentifierError, match="not a CIK"):
        ds_db.resolve_company(dsn=address, query="cik:abc")


def test_a_server_fault_is_one_line_with_a_request_id(address):
    with pytest.raises(DataSourceError, match=r"HTTP 500: internal error \(request"):
        ds_db.calendars(dsn=address)


def test_a_refused_key_says_where_to_look(address):
    wrong = ApiAddress(str(address), keys.generate()[1])
    with pytest.raises(DataSourceError, match="refused the key.*fafnir api key list"):
        ds_db.calendars(dsn=wrong)


def test_an_older_host_is_named_as_the_cause(address):
    with pytest.raises(DataSourceError, match="older than this duk"):
        remote.call(address, "a_read_from_the_future", {})


def test_a_missing_key_is_configuration_advice(address):
    with pytest.raises(DataSourceError, match="FAFNIR_API_KEY"):
        ds_db.calendars(dsn=ApiAddress(str(address), ""))


def test_nothing_listening_is_a_datasource_error():
    with pytest.raises(DataSourceError, match="cannot reach the fafnir API"):
        ds_db.calendars(dsn=ApiAddress("http://127.0.0.1:9", TOKEN))


def test_something_that_is_not_the_api_is_recognised(serve_wsgi):
    def not_fafnir(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<html>welcome to nginx</html>"]

    with pytest.raises(DataSourceError, match="not as a fafnir API"):
        ds_db.calendars(dsn=ApiAddress(serve_wsgi(not_fafnir), TOKEN))


def test_the_key_is_never_sent_over_plain_http_to_another_host(monkeypatch):
    monkeypatch.setattr(
        "fafnir.sources.warehouse.post",
        lambda *a, **k: pytest.fail("no request may be attempted"),
    )
    with pytest.raises(DataSourceError, match="plain HTTP"):
        ds_db.calendars(dsn=ApiAddress("http://fafnir.example.com", TOKEN))


def test_an_address_that_is_not_a_url_is_refused():
    with pytest.raises(DataSourceError, match="not a fafnir API address"):
        ds_db.calendars(dsn=ApiAddress("fafnir.example.com", TOKEN))


def test_the_key_is_not_part_of_the_address_text():
    address = ApiAddress("https://fafnir.example.com/", TOKEN)
    assert str(address) == "https://fafnir.example.com"
    assert TOKEN not in repr(address) and TOKEN not in f"{address}"
    assert address.api_key == TOKEN


# -- configuration ---------------------------------------------------------------


def _config(tmp_path, text, monkeypatch, **env):
    for name in ("FAFNIR_DSN", "FAFNIR_API_URL", "FAFNIR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "dukrc"
    path.write_text(text)
    return DukConfig(str(path))


API_FILE = '[api]\nfafnir_url = "https://fafnir.example.com"\nfafnir_key = "ffn_file"\n'
DSN_FILE = '[database]\ndsn = "dbname=fafnir user=rob"\n'


def test_an_api_address_in_the_file_is_the_warehouse(tmp_path, monkeypatch):
    where = _config(tmp_path, API_FILE, monkeypatch).dsn
    assert isinstance(where, ApiAddress)
    assert (where, where.api_key) == ("https://fafnir.example.com", "ffn_file")


def test_the_key_can_come_from_the_environment(tmp_path, monkeypatch):
    where = _config(tmp_path, API_FILE, monkeypatch, FAFNIR_API_KEY="ffn_env").dsn
    assert where.api_key == "ffn_env"


def test_a_dsn_beats_an_address_at_the_same_level(tmp_path, monkeypatch):
    where = _config(tmp_path, DSN_FILE + API_FILE, monkeypatch).dsn
    assert where == "dbname=fafnir user=rob" and not isinstance(where, ApiAddress)


def test_the_environment_beats_the_file_either_way(tmp_path, monkeypatch):
    over_dsn = _config(
        tmp_path, DSN_FILE, monkeypatch, FAFNIR_API_URL="https://other.example.com"
    ).dsn
    assert isinstance(over_dsn, ApiAddress) and over_dsn == "https://other.example.com"
    over_api = _config(tmp_path, API_FILE, monkeypatch, FAFNIR_DSN="dbname=x").dsn
    assert over_api == "dbname=x" and not isinstance(over_api, ApiAddress)


def test_nothing_configured_is_still_empty(tmp_path, monkeypatch):
    assert _config(tmp_path, "", monkeypatch).dsn == ""
