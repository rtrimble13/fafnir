-- 0001_schemas_and_roles.up.sql
-- Foundation: medallion schemas and least-privilege roles.
--
-- Layers (provenance flows one direction; downstream is always rebuildable):
--   landing -> core -> mart, with ref / ops / meta as cross-cutting support.
--
-- Roles are created idempotently so this migration is safe to re-run on a host
-- where the roles already exist (e.g. shared cluster). Passwords are NOT set
-- here -- assign them out-of-band (ALTER ROLE ... PASSWORD) or via your secrets
-- manager. Login is granted so the ingest/read roles can connect.
--
-- This migration is designed to run as the (non-superuser) database owner, which
-- is what a least-privilege install uses: the owner must own these objects so that
-- routine maintenance (`fafnir db ensure-horizon` attaching yearly partitions to
-- core.daily_price) keeps working. Two statements would otherwise demand more
-- privilege than that, so both are handled explicitly below:
--   * CREATE ROLE      -- needs CREATEROLE; skipped when the roles already exist,
--                         and raises an actionable error when they do not.
--   * COMMENT ON ROLE  -- needs superuser (CREATEROLE is not enough: it also wants
--                         ADMIN OPTION on the role, and a role can never hold that
--                         on itself). Applied best-effort; these comments are
--                         catalog documentation, not structure.

BEGIN;

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS landing;  -- raw, immutable source payloads
CREATE SCHEMA IF NOT EXISTS core;     -- modeled source of truth, constrained
CREATE SCHEMA IF NOT EXISTS mart;     -- denormalized read views / matviews
CREATE SCHEMA IF NOT EXISTS ref;      -- reference / lookup data
CREATE SCHEMA IF NOT EXISTS ops;      -- ingestion lineage, DQ, watermarks
CREATE SCHEMA IF NOT EXISTS meta;     -- migration bookkeeping

COMMENT ON SCHEMA landing IS 'Raw immutable source payloads (FMP/FRED/BLS/BEA) as received.';
COMMENT ON SCHEMA core    IS 'Modeled, constrained source of truth. Join facts on security_id.';
COMMENT ON SCHEMA mart    IS 'Denormalized, derived read marts (views/matviews) for apps, duk-db and MCP.';
COMMENT ON SCHEMA ref     IS 'Reference / lookup data: exchanges, sectors, industries, trading calendar.';
COMMENT ON SCHEMA ops     IS 'Operational metadata: ingestion runs, data-quality flags, load watermarks.';
COMMENT ON SCHEMA meta    IS 'Schema-migration bookkeeping.';

-- ---------------------------------------------------------------------------
-- Roles (created only if absent; DO block keeps CREATE ROLE idempotent)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_ingest') THEN
        CREATE ROLE fafnir_ingest LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_read') THEN
        CREATE ROLE fafnir_read LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_app') THEN
        CREATE ROLE fafnir_app LOGIN;
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    -- The roles are referenced by the GRANTs below, so this is fatal -- but say
    -- exactly how to fix it instead of surfacing a bare "permission denied".
    RAISE EXCEPTION
        'the fafnir roles do not exist and the current user (%) lacks CREATEROLE. '
        'Create them once as a superuser, then re-run the migration: '
        'CREATE ROLE fafnir_ingest LOGIN; CREATE ROLE fafnir_read LOGIN; '
        'CREATE ROLE fafnir_app LOGIN;', current_user;
END
$$;

-- Role documentation. Best-effort: COMMENT ON ROLE requires superuser, which a
-- least-privilege migrator does not have, and losing a catalog comment is not a
-- reason to fail an install. To set them, run these three statements as a
-- superuser (e.g. `sudo -u postgres psql -d fafnir`) at any time.
DO $$
BEGIN
    COMMENT ON ROLE fafnir_ingest IS 'Write path: loaders. Writes landing/core/ref/ops. No DROP/ALTER in prod.';
    COMMENT ON ROLE fafnir_read   IS 'Read path: research/notebooks. Reads core/mart/ref.';
    COMMENT ON ROLE fafnir_app    IS 'Least-privilege app/MCP/duk-db role. Reads mart (+ ref) only.';
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE
        'Skipped COMMENT ON ROLE: % is not a superuser. Role comments are '
        'documentation only -- the schema and grants are unaffected.', current_user;
END
$$;

-- ---------------------------------------------------------------------------
-- Grants: usage on schemas
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA landing, core, ref, ops, mart TO fafnir_ingest;
GRANT USAGE ON SCHEMA core, mart, ref               TO fafnir_read;
GRANT USAGE ON SCHEMA mart, ref                     TO fafnir_app;

-- Ingest owns the write path.
GRANT CREATE ON SCHEMA landing, core, ref, ops TO fafnir_ingest;

-- Default privileges so tables created later by the migrator are readable by
-- the read/app roles without re-granting per table. These apply to objects
-- created by the role running migrations (the DB owner / ingest role).
ALTER DEFAULT PRIVILEGES IN SCHEMA core, ref
    GRANT SELECT ON TABLES TO fafnir_read, fafnir_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA mart
    GRANT SELECT ON TABLES TO fafnir_read, fafnir_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA landing, core, ref, ops
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fafnir_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA landing, core, ref, ops
    GRANT USAGE, SELECT ON SEQUENCES TO fafnir_ingest;

COMMIT;
