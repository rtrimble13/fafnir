-- 0021_ops_reader_role.up.sql
-- fafnir_ops: a fourth functional role that can READ the operational record.
--
-- Why a role and not a mart view (ADR 0009, ADR 0010)
-- ---------------------------------------------------
-- ADR 0009 made `mart` the whole read seam, and its second rule is that adding a
-- mart view is a GRANT to every mart reader -- every person, every agent. That is
-- exactly what must NOT happen to ops.data_quality_flag.detail, which carries raw
-- Python exception strings for `adjustment_failed` (psycopg internals, constraint
-- names, filesystem paths), or to landing.fmp_raw.payload, which is unbounded
-- vendor JSON. mart.v_security_dq_open already draws that line deliberately: open
-- flags only, record_key in, detail out.
--
-- An on-host operations agent needs precisely the columns that view withholds. So
-- this migration opens a SEPARATE door for a SEPARATE role rather than widening
-- the shared one. If you find yourself about to add `detail` to a mart view, this
-- role is the thing you actually wanted.
--
-- Privileges, and their boundaries
-- --------------------------------
-- fafnir_ops gets SELECT on core, mart, ref (what fafnir_read has) plus ops,
-- landing and meta. It gets no write privilege of any kind, no CREATE on any
-- schema, and no sequence access -- a reader that can write has no tier boundary
-- left to enforce, and a reader that can CREATE can materialise its way around a
-- row cap.
--
-- meta is included because "is this host running the schema the repo expects?" is
-- an operational question and meta.schema_migration is its answer. It holds
-- versions, names and checksums -- bookkeeping, no market data, no secrets.
--
-- Membership is deliberately NOT granted here
-- -------------------------------------------
-- No login role is created and no GRANT fafnir_ops TO <someone> is issued. Who may
-- become an ops reader is a deployment fact, not schema (ADR 0008), and belongs in
-- the runbook -- see doc/agent.md.
--
-- Nor is `GRANT fafnir_read TO fafnir_ops` used to inherit the core/mart/ref half.
-- Granting one role to another needs ADMIN OPTION on the grantee, which the
-- migrator holds only when it created the role itself. On a least-privilege
-- install where a superuser pre-created the roles (install_hetzner.md §3.5) it does
-- not, so that statement would fail on exactly the deployments this project
-- targets. Granting the object privileges directly needs only ownership, which the
-- migrator always has.
--
-- Like 0001, CREATE ROLE is guarded: it needs CREATEROLE, which a least-privilege
-- migrator may not have, so an absent role raises an actionable error rather than
-- a bare "permission denied".

BEGIN;

-- ---------------------------------------------------------------------------
-- The role
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_ops') THEN
        CREATE ROLE fafnir_ops NOLOGIN;
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    -- Same shape and same SQLSTATE as 0001's guard, so a caller matching on the
    -- error code sees insufficient_privilege rather than raise_exception (P0001).
    RAISE EXCEPTION
        'role fafnir_ops is missing and the current user (%) lacks CREATEROLE. '
        'Create it once, as a superuser, then re-run the migration: '
        'CREATE ROLE fafnir_ops NOLOGIN;', current_user
        USING ERRCODE = '42501';
END
$$;

-- NOLOGIN on purpose: fafnir_ops is a GROUP that holds grants, never a principal
-- that connects. Deployments add per-agent login roles as members of it
-- (`CREATE ROLE claude_ops LOGIN IN ROLE fafnir_ops`), which is what makes
-- pg_stat_activity, pg_stat_statements and the server log name the agent rather
-- than a shared identity -- and what makes `ALTER ROLE claude_ops NOLOGIN` a
-- revocation that touches nothing else. This is ADR 0008's per-agent role model,
-- applied to the ops tier.
--
-- If the role already existed with LOGIN (created by hand before this migration),
-- it is left alone: taking LOGIN away from a role that may be authenticating
-- something is not a migration's decision to make.

DO $$
BEGIN
    COMMENT ON ROLE fafnir_ops IS
        'Ops read tier: SELECT on core/mart/ref + ops/landing/meta. Group role, '
        'no login. Members read the operational record; nothing writes.';
EXCEPTION WHEN insufficient_privilege THEN
    -- Best-effort, exactly as 0001: COMMENT ON ROLE needs superuser, and losing a
    -- catalog comment is not a reason to fail an install. WARNING not NOTICE so it
    -- is visible (fafnir.db.connection keeps routine notices at debug).
    RAISE WARNING
        'skipped COMMENT ON ROLE fafnir_ops: % is not a superuser. The comment is '
        'documentation only -- the grants below are unaffected.', current_user;
END
$$;

-- ---------------------------------------------------------------------------
-- Grants: usage on schemas
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA core, mart, ref, ops, landing, meta TO fafnir_ops;

-- ---------------------------------------------------------------------------
-- Grants: SELECT on what already exists
--
-- Both halves are needed and they are not redundant. ALTER DEFAULT PRIVILEGES
-- below applies only to objects created AFTER it runs; every table and view
-- migrations 0002-0020 created already exists by the time this migration executes,
-- so those need an explicit grant here. Forget this half and fafnir_ops can read
-- nothing that matters; forget the other and it silently stops seeing new
-- relations, which is worse because it looks like it works.
--
-- ALL TABLES covers ordinary tables, views and materialized views, which is what
-- mart is made of.
-- ---------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA core, mart, ref, ops, landing, meta
    TO fafnir_ops;

-- ---------------------------------------------------------------------------
-- Grants: SELECT on what migrations add later
-- ---------------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA core, mart, ref, ops, landing, meta
    GRANT SELECT ON TABLES TO fafnir_ops;

COMMIT;
