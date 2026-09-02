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


# ---------------------------------------------------------------------------
# The mart read seam (migration 0020, ADR 0008 §4 / ADR 0009)
#
# These are the tests the `ph`-as-fafnir_app breakage survived for want of. The
# grants alone do not tell you whether duk can run: `fafnir_app` held SELECT on
# core's TABLES via default privileges the whole time and still could not read
# them, because it has no USAGE on the schema. Only an actual SELECT as that role
# answers the question, so that is what these do.
# ---------------------------------------------------------------------------

# Every relation duk's db datasource reads. If duk grows a read, it goes here --
# that is the point of the list.
DUK_MART_RELATIONS = (
    "mart.v_symbol_lookup",
    "mart.v_daily_price_raw",
    "mart.v_daily_price_adjusted",
    "mart.v_security_profile",
    "mart.v_security_price_coverage",
    "mart.v_security_action_summary",
    "mart.v_security_dq_open",
    "mart.security_latest",
    "ref.sector",
    "ref.industry",
)

# What the seam must NOT reach. core.daily_price is the one that matters most: it
# is readable through mart.v_daily_price_raw and must not be readable directly, or
# the view is decoration rather than a boundary.
FORBIDDEN_TO_APP = (
    "core.daily_price",
    "core.symbol_xref",
    "core.security",
    "ops.data_quality_flag",
    "landing.fmp_raw",
)


def _as_app_dsn(dsn: str) -> str:
    parts = conninfo_to_dict(dsn)
    parts.update(user="fafnir_app")
    parts.pop("password", None)
    return make_conninfo(**parts)


def test_fafnir_app_can_read_every_relation_duk_uses(least_privilege_dsn):
    """duk connects as a mart-only role (ADR 0008), so every relation it names
    must be readable as `fafnir_app` -- including the views 0020 added."""
    m.migrate(least_privilege_dsn)
    with Database(least_privilege_dsn, autocommit=True) as owner:
        owner.execute("GRANT CONNECT ON DATABASE %s TO fafnir_app" % LP_DB)

    failures = []
    with Database(_as_app_dsn(least_privilege_dsn), autocommit=True) as app:
        for relation in DUK_MART_RELATIONS:
            try:
                app.fetchval(f"SELECT count(*) FROM {relation}")
            except Exception as exc:  # noqa: BLE001 -- the message is the report
                failures.append(f"{relation}: {type(exc).__name__}: {exc}")
    assert not failures, "fafnir_app cannot read:\n  " + "\n  ".join(failures)


def test_fafnir_app_still_cannot_reach_core_ops_or_landing(least_privilege_dsn):
    """The other half of the argument for definer-rights views.

    A mart view readable by `fafnir_app` is only a boundary if the underlying
    table is not. If this test ever fails, the seam has been widened by a stray
    GRANT and mart.v_security_dq_open stopped being a narrowing of the DQ queue.
    """
    m.migrate(least_privilege_dsn)
    with Database(least_privilege_dsn, autocommit=True) as owner:
        owner.execute("GRANT CONNECT ON DATABASE %s TO fafnir_app" % LP_DB)

    reachable = []
    with Database(_as_app_dsn(least_privilege_dsn), autocommit=True) as app:
        for relation in FORBIDDEN_TO_APP:
            try:
                app.fetchval(f"SELECT count(*) FROM {relation}")
                reachable.append(relation)
            except Exception:  # noqa: BLE001 -- denial is the expected outcome
                pass
    assert not reachable, f"fafnir_app should not reach: {reachable}"


def test_dq_seam_exposes_no_resolution_provenance(least_privilege_dsn):
    """`mart.v_security_dq_open` must not carry `detail` or the human judgements.

    `detail` is excluded because `adjustment_failed` writes a raw Python exception
    string into it. `resolved_by`/`resolution_note` are excluded structurally by
    the open-only filter (0017's CHECK forces them NULL while `resolved_at` is) --
    this asserts the columns are absent too, so relaxing the WHERE cannot quietly
    put human-written text on an agent-readable seam.
    """
    m.migrate(least_privilege_dsn)
    with Database(least_privilege_dsn, autocommit=True) as db:
        columns = {
            r["column_name"]
            for r in db.fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'mart' AND table_name = 'v_security_dq_open'"
            )
        }
    assert "record_key" in columns, columns
    assert not columns & {"detail", "resolved_by", "resolution_note"}, columns
