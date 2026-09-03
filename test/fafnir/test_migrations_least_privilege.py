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
# 0021 adds fafnir_ops, and like the other three it will not create itself without
# CREATEROLE -- so the fixture pre-creates it exactly as install_hetzner.md §3.5
# does. Its NOLOGIN default is overridden here only for the member role below;
# fafnir_ops itself stays a group that holds grants.
FAFNIR_ROLES = ("fafnir_ingest", "fafnir_read", "fafnir_app", "fafnir_ops")

# A login role that is a MEMBER of fafnir_app, rather than fafnir_app itself.
#
# Two reasons. Practically, fafnir_app has no password, and a test DSN pointed at
# a password-authenticated server (CI's, and any real install) cannot log in as
# it -- giving the shared role a password to suit a test would be worse. But it is
# also the more faithful test: ADR 0008 deploys exactly this shape, per person and
# per agent (`CREATE ROLE rob LOGIN IN ROLE fafnir_app`), so what is asserted here
# is what a laptop or an MCP server actually connects as.
APP_MEMBER_ROLE = "fafnir_app_member_test"
APP_MEMBER_PASSWORD = "app_member_test_password"

# The same shape one tier up: what an on-host operations agent connects as
# (`CREATE ROLE claude_ops LOGIN IN ROLE fafnir_ops`, ADR 0010). Kept separate from
# APP_MEMBER_ROLE because the whole point of the tier is that the two see different
# things, which only two live connections can demonstrate.
OPS_MEMBER_ROLE = "fafnir_ops_member_test"
OPS_MEMBER_PASSWORD = "ops_member_test_password"


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
                # fafnir_ops is a group that holds grants, never a principal that
                # connects (ADR 0010) -- per-agent roles are members of it. The
                # other three keep LOGIN because deployments do connect as them.
                login = "NOLOGIN" if role == "fafnir_ops" else "LOGIN"
                db.execute(f"CREATE ROLE {role} {login}")
        # Inherits fafnir_app's grants and nothing else; password so it can log
        # in over TCP the way CI and every real install authenticate.
        if not db.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_MEMBER_ROLE,)
        ):
            db.execute(
                f"CREATE ROLE {APP_MEMBER_ROLE} LOGIN PASSWORD "
                f"'{APP_MEMBER_PASSWORD}' IN ROLE fafnir_app"
            )
        else:
            db.execute(
                f"ALTER ROLE {APP_MEMBER_ROLE} LOGIN PASSWORD "
                f"'{APP_MEMBER_PASSWORD}'"
            )
            db.execute(f"GRANT fafnir_app TO {APP_MEMBER_ROLE}")
        if not db.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (OPS_MEMBER_ROLE,)
        ):
            db.execute(
                f"CREATE ROLE {OPS_MEMBER_ROLE} LOGIN PASSWORD "
                f"'{OPS_MEMBER_PASSWORD}' IN ROLE fafnir_ops"
            )
        else:
            db.execute(
                f"ALTER ROLE {OPS_MEMBER_ROLE} LOGIN PASSWORD "
                f"'{OPS_MEMBER_PASSWORD}'"
            )
            db.execute(f"GRANT fafnir_ops TO {OPS_MEMBER_ROLE}")

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
    """The LP database, connected to as a member of fafnir_app."""
    parts = conninfo_to_dict(dsn)
    parts.update(user=APP_MEMBER_ROLE, password=APP_MEMBER_PASSWORD)
    return make_conninfo(**parts)


def _migrate_and_open_as_app(dsn: str):
    """Migrate the LP database, then open it as the fafnir_app member.

    CONNECT is granted explicitly rather than relying on PUBLIC's default: an
    installation that has revoked it should still be able to run this test, and
    the grant says out loud what the role needs.
    """
    m.migrate(dsn)
    with Database(dsn, autocommit=True) as owner:
        owner.execute(f"GRANT CONNECT ON DATABASE {LP_DB} TO fafnir_app")
    return Database(_as_app_dsn(dsn), autocommit=True)


def test_fafnir_app_can_read_every_relation_duk_uses(least_privilege_dsn):
    """duk connects as a mart-only role (ADR 0008), so every relation it names
    must be readable by a member of `fafnir_app` -- including the views 0020
    added."""
    failures = []
    with _migrate_and_open_as_app(least_privilege_dsn) as app:
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
    reachable = []
    with _migrate_and_open_as_app(least_privilege_dsn) as app:
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


# ---------------------------------------------------------------------------
# The ops read tier (migration 0021, ADR 0010)
#
# Three properties, and the third is the one that decays silently. The tier is
# only a tier while `fafnir_ops` reads MORE than `fafnir_app` and writes NOTHING,
# and while adding it has not widened `fafnir_app` as a side effect -- which is
# precisely what would happen if someone served the agent's need for `detail` by
# relaxing mart.v_security_dq_open instead of using this role.
# ---------------------------------------------------------------------------

# The operational record an agent triaging the DQ queue has to be able to read.
# meta.schema_migration is here because "is this host running the schema the repo
# expects?" is an operations question with an operations answer.
OPS_READABLE = (
    "ops.data_quality_flag",
    "ops.ingestion_run",
    "ops.load_watermark",
    "landing.fmp_raw",
    "meta.schema_migration",
    # Everything fafnir_read reaches is included by design: triage starts from a
    # flag and ends in the bars and actions that explain it.
    "core.daily_price",
    "core.security",
    "core.corporate_action",
    "core.symbol_change",
    "mart.v_daily_price_adjusted",
    "mart.security_latest",
    "ref.exchange",
)

# One write of each shape, against a relation the role can definitely SELECT --
# so a failure here means the privilege is missing, never that the table is.
OPS_FORBIDDEN_WRITES = (
    ("INSERT", "INSERT INTO ops.data_quality_flag (check_name) VALUES ('probe')"),
    ("UPDATE", "UPDATE ops.data_quality_flag SET severity = 'error'"),
    ("DELETE", "DELETE FROM ops.data_quality_flag"),
    ("INSERT core", "INSERT INTO core.security (primary_symbol) VALUES ('PROBE')"),
    ("DELETE core", "DELETE FROM core.daily_price"),
    ("CREATE ops", "CREATE TABLE ops.privilege_probe (i int)"),
    ("CREATE core", "CREATE TABLE core.privilege_probe (i int)"),
    ("CREATE mart", "CREATE TABLE mart.privilege_probe (i int)"),
)


def _as_ops_dsn(dsn: str) -> str:
    """The LP database, connected to as a member of fafnir_ops."""
    parts = conninfo_to_dict(dsn)
    parts.update(user=OPS_MEMBER_ROLE, password=OPS_MEMBER_PASSWORD)
    return make_conninfo(**parts)


def _migrate_and_open_as_ops(dsn: str):
    m.migrate(dsn)
    with Database(dsn, autocommit=True) as owner:
        owner.execute(f"GRANT CONNECT ON DATABASE {LP_DB} TO fafnir_ops")
    return Database(_as_ops_dsn(dsn), autocommit=True)


def test_fafnir_ops_can_read_the_operational_record(least_privilege_dsn):
    """The reason the tier exists: ops, landing and meta are readable by it.

    Grants alone would not prove this -- `fafnir_app` held SELECT on core's tables
    via default privileges for months while being unable to read one of them, for
    want of USAGE on the schema (ADR 0009). Only a real SELECT as the role answers
    it, so that is what this does.
    """
    failures = []
    with _migrate_and_open_as_ops(least_privilege_dsn) as ops:
        for relation in OPS_READABLE:
            try:
                ops.fetchval(f"SELECT count(*) FROM {relation}")
            except Exception as exc:  # noqa: BLE001 -- the message is the report
                failures.append(f"{relation}: {type(exc).__name__}: {exc}")
    assert not failures, "fafnir_ops cannot read:\n  " + "\n  ".join(failures)


def test_fafnir_ops_cannot_write_anything(least_privilege_dsn):
    """The tier boundary. A reader that can write is not a read tier.

    This is the assertion that matters most, because the ops role is the one an
    agent connects as: ADR 0010 puts every mutation on the `fafnir` CLI as
    `fafnir_ingest` precisely so that the agent's own credential cannot change
    data even if every tool argument goes wrong at once.
    """
    succeeded = []
    with _migrate_and_open_as_ops(least_privilege_dsn) as ops:
        for label, statement in OPS_FORBIDDEN_WRITES:
            try:
                ops.execute(statement)
                succeeded.append(label)
            except Exception:  # noqa: BLE001 -- denial is the expected outcome
                pass
    assert not succeeded, f"fafnir_ops should not be able to: {succeeded}"


def test_ops_tier_did_not_widen_the_app_tier(least_privilege_dsn):
    """0021 must open a second door, not widen the first one.

    ADR 0009's rule is that a `mart` view is a grant to every mart reader, so the
    tempting way to give an agent `ops.data_quality_flag.detail` -- relaxing
    mart.v_security_dq_open -- would hand it to every laptop and app as well. The
    two roles are compared directly here so that mistake fails a test rather than
    passing review.
    """
    m.migrate(least_privilege_dsn)
    with Database(least_privilege_dsn, autocommit=True) as owner:
        owner.execute(f"GRANT CONNECT ON DATABASE {LP_DB} TO fafnir_app")
        owner.execute(f"GRANT CONNECT ON DATABASE {LP_DB} TO fafnir_ops")

    ops_only = ("ops.data_quality_flag", "landing.fmp_raw")

    reachable_by_app = []
    with Database(_as_app_dsn(least_privilege_dsn), autocommit=True) as app:
        for relation in ops_only:
            try:
                app.fetchval(f"SELECT count(*) FROM {relation}")
                reachable_by_app.append(relation)
            except Exception:  # noqa: BLE001
                pass
    assert (
        not reachable_by_app
    ), f"0021 widened fafnir_app, which it must not: {reachable_by_app}"

    # And the same relations ARE reachable one tier up -- otherwise this test
    # would pass just as well if 0021 had never run.
    with Database(_as_ops_dsn(least_privilege_dsn), autocommit=True) as ops:
        for relation in ops_only:
            assert ops.fetchval(f"SELECT count(*) FROM {relation}") is not None
