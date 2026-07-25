"""
Integration tests for the privileges the migrations actually require.

These exist because the ordinary suite cannot catch the failure they guard: the
CI Postgres service role is a superuser, so migrations always take the privileged
path there. Migration 0001 was for a long time unappliable by a non-superuser
(COMMENT ON ROLE requires it) and the suite stayed green throughout. Each test
below provisions its own unprivileged role and migrates a scratch database as
that role, which is the arrangement a least-privilege install actually uses.

Skipped automatically when FAFNIR_TEST_DSN's role cannot create roles/databases.
"""

from __future__ import annotations

import os

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fafnir.db import migrate as m
from fafnir.db.connection import Database

pytestmark = pytest.mark.integration

TEST_DSN = os.environ.get("FAFNIR_TEST_DSN", "")

LP_ROLE = "fafnir_lp_test_owner"
LP_PASSWORD = "lp_test_password"
LP_DB = "fafnir_lp_test"
FAFNIR_ROLES = ("fafnir_ingest", "fafnir_read", "fafnir_app")


def _admin() -> Database:
    return Database(TEST_DSN, autocommit=True)


def _can_provision() -> bool:
    """True when the test DSN's role may create roles and databases."""
    with _admin() as db:
        row = db.fetchone(
            "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname = current_user"
        )
    if row is None:
        return False
    return bool(row["rolsuper"] or (row["rolcreaterole"] and row["rolcreatedb"]))


def _lp_dsn() -> str:
    """The test DSN rewritten to connect as the unprivileged role to its own db."""
    parts = conninfo_to_dict(TEST_DSN)
    parts.update(user=LP_ROLE, password=LP_PASSWORD, dbname=LP_DB)
    return make_conninfo(**parts)


@pytest.fixture()
def least_privilege_dsn():
    """A scratch database owned by a NOSUPERUSER / NOCREATEROLE role."""
    if not TEST_DSN or not _can_provision():
        pytest.skip("test DSN role cannot create roles/databases")

    with _admin() as db:
        # The migration grants to these three and will not create them itself
        # without CREATEROLE, exactly as a real install pre-creates them.
        for role in FAFNIR_ROLES:
            if not db.fetchval("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
                db.execute(f"CREATE ROLE {role} LOGIN")
        db.execute(f"DROP DATABASE IF EXISTS {LP_DB}")
        if not db.fetchval("SELECT 1 FROM pg_roles WHERE rolname = %s", (LP_ROLE,)):
            db.execute(
                f"CREATE ROLE {LP_ROLE} LOGIN PASSWORD '{LP_PASSWORD}' "
                f"NOSUPERUSER NOCREATEROLE NOCREATEDB"
            )
        else:
            db.execute(f"ALTER ROLE {LP_ROLE} NOSUPERUSER NOCREATEROLE NOCREATEDB")
        db.execute(f"CREATE DATABASE {LP_DB} OWNER {LP_ROLE}")

    try:
        yield _lp_dsn()
    finally:
        with _admin() as db:
            db.execute(f"DROP DATABASE IF EXISTS {LP_DB}")


def test_migrations_apply_as_a_non_superuser_owner(least_privilege_dsn):
    """The whole schema must be installable without granting superuser."""
    with Database(least_privilege_dsn, autocommit=True) as db:
        row = db.fetchone(
            "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
    assert row == {"rolsuper": False, "rolcreaterole": False}, row

    applied = m.migrate(least_privilege_dsn)

    assert applied, "no migrations applied"
    states = m.status(least_privilege_dsn)
    assert all(state == "applied" for _, _, state in states), states


def test_non_superuser_owner_can_create_partitions(least_privilege_dsn):
    """
    Ownership is the reason migrations run as this role rather than as postgres:
    the nightly `fafnir db ensure-horizon` attaches partitions to
    core.daily_price, which only the owner of the parent table may do.
    """
    from fafnir.db import maintenance

    m.migrate(least_privilege_dsn)
    with Database(least_privilege_dsn, autocommit=True) as db:
        created = maintenance.ensure_year_partition(db, 2035)
        assert created
        assert db.fetchval("SELECT 1 FROM pg_class WHERE relname = 'daily_price_y2035'")


def test_skipped_role_comments_are_reported(least_privilege_dsn, caplog):
    """
    The role comments cannot be set without superuser, so they are skipped -- but
    the operator has to be told. psycopg drops server messages unless a handler is
    registered, so this asserts the warning actually reaches the log.
    """
    with caplog.at_level("WARNING"):
        m.migrate(least_privilege_dsn)

    assert any(
        "skipped COMMENT ON ROLE" in record.getMessage() for record in caplog.records
    ), [r.getMessage() for r in caplog.records]
