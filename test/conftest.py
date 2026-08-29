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
                     ops.load_watermark, landing.fmp_raw,
                     -- ref is otherwise kept (it is seeded reference data), but the
                     -- declared universe is per-test state, not reference data.
                     ref.tracked_symbol
            RESTART IDENTITY CASCADE
            """)
        yield database
