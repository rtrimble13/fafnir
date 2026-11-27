"""
Shared pytest fixtures.

Integration tests require a PostgreSQL database identified by the
``FAFNIR_TEST_DSN`` environment variable. When it is unset, integration-marked
tests are skipped automatically so the unit suite stays runnable anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DSN = os.environ.get("FAFNIR_TEST_DSN")

# Ensure the migration runner can find sql/ when tests run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FAFNIR_SQL_DIR", str(_REPO_ROOT / "sql"))


@pytest.fixture(autouse=True)
def _no_ambient_warehouse_api(monkeypatch):
    """No test reads the warehouse of whoever runs it, over its HTTP API.

    duk sends its warehouse reads to ``FAFNIR_API_URL``, with ``FAFNIR_API_KEY``,
    ahead of any DSN its config file names (ADR 0021). On a machine set up to read
    the warehouse that way (a laptop, or a cloud session with the API connected)
    every duk test would otherwise be pointed at the real warehouse: a test that
    replaces some reads with fakes fails, and the reads it did not replace go to
    production. A test that wants an API address sets one (test/duk/test_remote.py,
    test/fafnir_api/test_api_integration.py).
    """
    monkeypatch.delenv("FAFNIR_API_URL", raising=False)
    monkeypatch.delenv("FAFNIR_API_KEY", raising=False)


def pytest_collection_modifyitems(config, items):
    if TEST_DSN:
        return
    skip = pytest.mark.skip(reason="set FAFNIR_TEST_DSN to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def migrated_dsn():
    """Apply migrations + a small seed once per session; return the DSN."""
    from fafnir.db import migrate as m
    from fafnir.db import seed as s
    from fafnir.db.connection import Database

    m.migrate(TEST_DSN)
    with Database(TEST_DSN) as db:
        s.seed(db, 2023, 2024)
    return TEST_DSN


@pytest.fixture()
def db(migrated_dsn):
    """A clean Database per test: core/ops/landing data truncated, ref/meta kept.

    Uses autocommit so that data written through this connection is visible to the
    separate connections opened by the duk db datasource within the same test.
    """
    from fafnir.db.connection import Database

    with Database(migrated_dsn, autocommit=True) as database:
        database.execute("""
            TRUNCATE core.daily_price, core.corporate_action, core.adjustment_factor,
                     core.symbol_xref, core.symbol_change, core.company_profile,
                     core.security, ops.data_quality_flag, ops.ingestion_run,
                     ops.load_watermark, ops.operator_override, ops.dq_label,
                     ops.dq_audit_item, ops.dq_audit_cycle,
                     ops.vendor_correction, ops.split_kind_change,
                     core.security_vendor_xref, core.security_vendor_rejection,
                     core.vendor_security,
                     core.vendor_security_event,
                     landing.fmp_raw, landing.sharadar_file,
                     -- ref is otherwise kept (it is seeded reference data), but the
                     -- declared universe is per-test state, not reference data.
                     ref.tracked_symbol
            RESTART IDENTITY CASCADE
            """)
        yield database


@pytest.fixture()
def serve_wsgi():
    """Serve a WSGI app over real HTTP on a loopback port; returns its base URL.

    For the HTTP API (ADR 0021): the client half is only tested once a request has
    actually crossed a socket, headers and all. ``wsgiref`` is the standard
    library's server, so this needs nothing the unit suite does not have.
    """
    import threading
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    class _Quiet(WSGIRequestHandler):
        def log_message(self, *args):  # the suite's output is not an access log
            pass

    servers = []

    def start(app) -> str:
        server = make_server("127.0.0.1", 0, app, handler_class=_Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
