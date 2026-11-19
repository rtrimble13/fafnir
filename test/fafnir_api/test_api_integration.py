"""The HTTP API against a real warehouse (ADR 0021; needs FAFNIR_TEST_DSN).

The claim under test is the one the design rests on: a read answers the same over
the API as over a DSN, value for value and type for type, while the service is
connected as a role that can write nothing. So the warehouse is seeded once per
test, each read is run both ways, and the two answers are compared.

The API side is the whole path: a key issued by `fafnir api key create`, looked up
in ops.api_key by a login role created the way the install creates it, over real
HTTP.
"""

from __future__ import annotations

import datetime as dt
import os
from datetime import date, timedelta
from test.duk.test_db_fundamentals import _history, _price, _security, _year

import pandas as pd
import psycopg
import pytest
from click.testing import CliRunner
from psycopg.conninfo import make_conninfo

from duk import identifiers
from duk.cli import main as duk_main
from duk.datasource import db as ds_db
from duk.datasource.base import ApiAddress, DataSourceError
from fafnir.cli import main as fafnir_main
from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir_api import cli as api_cli
from fafnir_api.app import make_app
from fafnir_api.keys import DatabaseKeyStore

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")
API_ROLE = "fafnir_api_test"
API_PASSWORD = "fafnir_api_test"


def _fafnir(*args) -> str:
    """Run the fafnir CLI against the test database; return its stdout."""
    result = CliRunner().invoke(fafnir_main, list(args), env={"FAFNIR_DSN": DSN})
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.fixture()
def api_role_dsn(db):
    """A login role as doc/install_hetzner.md §13 creates one: in fafnir_app, able
    to read ops.api_key and nothing else of ops, read-only, with a timeout."""
    (
        db.execute(f"DROP OWNED BY {API_ROLE}")
        if db.fetchone("SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (API_ROLE,))
        else None
    )
    db.execute(f"DROP ROLE IF EXISTS {API_ROLE}")
    db.execute(
        f"CREATE ROLE {API_ROLE} LOGIN PASSWORD '{API_PASSWORD}' IN ROLE fafnir_app"
    )
    db.execute(f"ALTER ROLE {API_ROLE} SET default_transaction_read_only = on")
    db.execute(f"ALTER ROLE {API_ROLE} SET statement_timeout = '30s'")
    db.execute(f"GRANT USAGE ON SCHEMA ops TO {API_ROLE}")
    db.execute(f"GRANT SELECT ON ops.api_key TO {API_ROLE}")
    db.execute("TRUNCATE ops.api_key")
    yield make_conninfo(DSN, user=API_ROLE, password=API_PASSWORD)
    db.execute("TRUNCATE ops.api_key")
    db.execute(f"DROP OWNED BY {API_ROLE}")
    db.execute(f"DROP ROLE {API_ROLE}")


@pytest.fixture()
def address(api_role_dsn, serve_wsgi):
    """The API, serving as the read role, and a key issued for it by the CLI."""
    token = _fafnir("api", "key", "create", "test-laptop", "--note", "ci").strip()
    # ttl=0: a revocation in a test bites at once, not after the cache's 30s.
    app = make_app(
        dsn=api_role_dsn, lookup=DatabaseKeyStore(api_role_dsn, ttl=0).lookup
    )
    return ApiAddress(serve_wsgi(app), token)


@pytest.fixture()
def warehouse(db):
    """One issuer with two classes, statements, a split, a dividend and a second
    ticker period; every mart refreshed."""
    today = date.today()
    sid = _security(db, "ZZP", cik="0000900107")
    secondary = _security(db, "ZZPB", cik="900107")
    _history(db, sid)
    _year(db, sid, today.year - 1, today - timedelta(days=100), today - timedelta(200))
    for offset, close in ((400, 40), (300, 42), (20, 21), (3, 22), (1, 25)):
        _price(db, sid, today - timedelta(days=offset), close)
    _price(db, secondary, today - timedelta(days=1), 24)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=today - timedelta(days=30),
        split_numerator=2,
        split_denominator=1,
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=today - timedelta(days=10),
        dividend_amount=0.25,
    )
    adjustments.compute_for_security(db, sid)
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to, "
        "is_primary) VALUES (%s, 'ZZOLD', %s, %s, false)",
        (sid, date(2015, 1, 1), date(2019, 12, 31)),
    )
    maintenance.refresh_marts(db)
    return {"sid": sid, "secondary": secondary, "today": today}


def _same(over_dsn, over_api, where="result"):
    """Equal, and of the same type, all the way down."""
    assert type(over_api) is type(over_dsn), where
    if isinstance(over_dsn, pd.DataFrame):
        pd.testing.assert_frame_equal(over_api, over_dsn, obj=where)
        assert over_api.attrs == over_dsn.attrs, where
    elif isinstance(over_dsn, dict):
        assert list(over_api) == list(over_dsn), where
        for key in over_dsn:
            _same(over_dsn[key], over_api[key], f"{where}[{key!r}]")
    elif isinstance(over_dsn, list):
        assert len(over_api) == len(over_dsn), where
        for i, (a, b) in enumerate(zip(over_dsn, over_api)):
            _same(a, b, f"{where}[{i}]")
    else:
        assert over_api == over_dsn, where


def _cases(w) -> list[tuple[str, dict]]:
    sid, today = w["sid"], w["today"]
    year_ago = (today - timedelta(days=365)).isoformat()
    span = {"start_date": None, "end_date": None}
    return [
        ("ticker_periods", {"symbol": "ZZP"}),
        ("resolve_identifier", {"identifier": identifiers.parse("cik:900107")}),
        ("resolve_identifier", {"identifier": identifiers.parse("ZZOLD@2018-06-29")}),
        ("exact_bars", {"symbol": "ZZP", "adjusted": True, "market_cap": True}),
        ("price_history", {"symbol": "ZZP", **span}),
        ("price_history", {"symbol": "ZZP", **span, "adjusted": True, "exact": True}),
        (
            "price_history",
            {"security_id": sid, **span, "frequency": "month", "market_cap": True},
        ),
        ("price_history", {"symbol": "NOSUCH", **span}),
        ("resolve_symbols", {"symbols": ["ZZP", "ZZPB", "NOSUCH"]}),
        ("market_cap_rows", {"security_ids": [sid], "start_date": year_ago}),
        ("market_cap_history", {"security_ids": [sid], "frequency": "month"}),
        ("market_cap_history", {"security_ids": [w["secondary"]]}),
        ("issuer_class", {"security_id": w["secondary"]}),
        ("screen", {"limit": 10}),
        ("screen", {"includeDelisted": True, "exchange": "nasdaq"}),
        ("sector_names", {}),
        ("industry_names", {}),
        ("list_sectors", {}),
        ("list_industries", {"includeDelisted": True}),
        ("list_actively_trading", {"limit": 5}),
        ("resolve_company", {"query": "ZZP"}),
        ("resolve_company", {"query": "zzp inc"}),
        ("company_summary", {"security_id": sid}),
        ("statements", {"symbol": "ZZP", "statement": "income"}),
        (
            "statements",
            {"symbol": "ZZPB", "statement": "balance", "as_of": "2021-06-30"},
        ),
        ("factor_row", {"symbol": "ZZP"}),
        ("factor_row", {"security_id": sid, "as_of": date(2021, 7, 15)}),
        ("ticker_history", {"symbols": ["zzp", "ZZOLD"]}),
        ("ticker_history", {"security_ids": [sid], "start_date": "2016-01-01"}),
        ("corporate_actions", {"symbol": "ZZP"}),
        ("corporate_actions", {"security_id": sid, "action_type": "split"}),
        ("calendars", {}),
        ("calendar_days", {"exchange": "NYSE", "start_date": "2024-01-01"}),
    ]


def test_every_read_answers_the_same_over_the_api_as_over_a_dsn(warehouse, address):
    cases = _cases(warehouse)
    assert {name for name, _ in cases} == set(ds_db.SERVED_READS), (
        "a served read has no case here: add one, so its result is known to "
        "survive the wire"
    )
    empty = []
    for name, kwargs in cases:
        read = getattr(ds_db, name)
        over_dsn = read(dsn=DSN, **kwargs)
        over_api = read(dsn=address, **kwargs)
        _same(over_dsn, over_api, f"{name}({kwargs})")
        if over_dsn is None or (hasattr(over_dsn, "__len__") and len(over_dsn) == 0):
            empty.append(name)
    # The comparison means little if the seed left the reads with nothing to say.
    assert set(empty) <= {"price_history", "market_cap_history"}, empty


def test_a_reads_refusal_is_the_same_refusal(warehouse, address):
    for where in (DSN, address):
        with pytest.raises(DataSourceError, match="No security with security_id 0"):
            ds_db.company_summary(dsn=where, security_id=0)
        with pytest.raises(DataSourceError, match="Unknown statement 'nope'"):
            ds_db.statements(dsn=where, symbol="ZZP", statement="nope")
        with pytest.raises(identifiers.IdentifierError, match="not a CIK"):
            ds_db.resolve_company(dsn=where, query="cik:abc")


@pytest.mark.parametrize(
    "command",
    [
        ["ph", "ZZP", "--csv"],
        ["ph", "ZZP", "--adj", "--exact", "--csv"],
        ["ph", "cik:900107", "-f", "month", "--json"],
        ["mc", "ZZP", "--csv"],
        ["ls", "ZZP"],
        ["ls", "--sector-list"],
        ["fs", "ZZP", "--csv"],
        ["fa", "ZZP"],
        ["th", "ZZP", "--csv"],
        ["ca", "ZZP", "--csv"],
        ["tc", "--csv"],
        ["ph", "NOSUCH"],
    ],
    ids=lambda c: " ".join(c),
)
def test_a_duk_command_prints_the_same_over_both(
    warehouse, address, command, tmp_path, monkeypatch
):
    for name in ("FAFNIR_DSN", "FAFNIR_API_URL", "FAFNIR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    cfg = tmp_path / "dukrc"
    cfg.write_text(f'[general]\nlog_dir = "{tmp_path}/log"\n')

    def run(env):
        result = CliRunner().invoke(duk_main, ["-c", str(cfg), *command], env=env)
        return result.exit_code, result.output

    over_dsn = run({"FAFNIR_DSN": DSN})
    over_api = run({"FAFNIR_API_URL": str(address), "FAFNIR_API_KEY": address.api_key})
    assert over_api == over_dsn
    assert over_dsn[1].strip(), "the command printed nothing: it compared nothing"


# -- keys, end to end ------------------------------------------------------------


def test_a_revoked_key_stops_working(warehouse, address):
    assert ds_db.calendars(dsn=address)
    assert "test-laptop" in _fafnir("api", "key", "list")
    _fafnir("api", "key", "revoke", "test-laptop")
    with pytest.raises(DataSourceError, match="refused the key: this key was revoked"):
        ds_db.calendars(dsn=address)
    assert "test-laptop" not in _fafnir("api", "key", "list")
    assert "revoked" in _fafnir("api", "key", "list", "--all")


def test_an_expired_key_stops_working(db, address):
    db.execute("UPDATE ops.api_key SET expires_at = now() - interval '1 second'")
    with pytest.raises(DataSourceError, match="this key has expired"):
        ds_db.calendars(dsn=address)


def test_a_key_made_up_by_the_caller_is_refused(address):
    from fafnir_api import keys

    with pytest.raises(DataSourceError, match="refused the key: unknown key"):
        ds_db.calendars(dsn=ApiAddress(str(address), keys.generate()[1]))


def test_the_table_holds_no_key(db, address):
    row = db.fetchone("SELECT * FROM ops.api_key")
    assert address.api_key not in " ".join(str(v) for v in row.values())
    assert row["key_id"] in address.api_key and row["name"] == "test-laptop"


def test_a_live_name_is_issued_once_and_a_revoked_one_again(db, address):
    again = CliRunner().invoke(
        fafnir_main, ["api", "key", "create", "test-laptop"], env={"FAFNIR_DSN": DSN}
    )
    assert again.exit_code != 0 and "a live key named" in again.output
    _fafnir("api", "key", "revoke", "test-laptop")
    assert _fafnir("api", "key", "create", "test-laptop").startswith("ffn_")
    assert db.fetchval("SELECT count(*) FROM ops.api_key") == 2


# -- the role is the boundary ------------------------------------------------------


def test_the_serving_role_passes_the_check_and_the_owner_does_not(api_role_dsn):
    found = api_cli.probe(api_role_dsn)
    assert api_cli.problems(found) == []
    assert (found["read_only"], found["writable"]) == ("on", False)
    assert api_cli.main(["--dsn", api_role_dsn, "--check"]) == 0
    # The test DSN owns the schema: exactly the identity the API must never be.
    assert api_cli.main(["--dsn", DSN, "--check"]) == 1


def test_the_serving_role_can_write_nothing(api_role_dsn):
    attempts = [
        "INSERT INTO ops.api_key (key_id, name, key_sha256, created_by) "
        f"VALUES ('{'a' * 12}', 'x', '{'0' * 64}', 'x')",
        "UPDATE ops.api_key SET revoked_at = NULL, revoked_by = NULL",
        "DELETE FROM ops.api_key",
        "SELECT * FROM ops.data_quality_flag",
        "SELECT * FROM core.daily_price",
        "CREATE TABLE mart.x (i int)",
    ]
    for sql in attempts:
        with psycopg.connect(api_role_dsn) as conn:
            with pytest.raises(psycopg.Error):
                conn.execute(sql)


def test_the_key_table_is_closed_to_the_shared_read_tiers(db):
    # 0046 grants it to nobody: not to every mart reader (fafnir_app), not to
    # research (fafnir_read), and not to the tier an agent reads with (fafnir_ops),
    # which 0021's default privileges would otherwise have handed it.
    for role in ("fafnir_app", "fafnir_read", "fafnir_ops"):
        assert not db.fetchval(
            "SELECT has_table_privilege(%s, 'ops.api_key', 'SELECT')", (role,)
        ), role
    assert db.fetchval(
        "SELECT has_table_privilege('fafnir_ingest', 'ops.api_key', 'INSERT')"
    )


def test_a_query_past_the_roles_time_limit_is_a_timeout_not_a_fault(
    api_role_dsn, serve_wsgi, address
):
    def slow(*, dsn):
        with psycopg.connect(dsn, options="-c statement_timeout=50") as conn:
            conn.execute("SELECT pg_sleep(2)")

    app = make_app(
        dsn=api_role_dsn,
        lookup=DatabaseKeyStore(api_role_dsn, ttl=0).lookup,
        reads={"calendars": slow},
    )
    started = dt.datetime.now()
    with pytest.raises(DataSourceError, match="HTTP 504.*time limit"):
        ds_db.calendars(dsn=ApiAddress(serve_wsgi(app), address.api_key))
    assert (dt.datetime.now() - started).total_seconds() < 2
