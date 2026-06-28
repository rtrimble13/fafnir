-- 0001_schemas_and_roles.down.sql
-- Reverse of 0001. Drops the schemas (CASCADE) but intentionally does NOT drop
-- the roles: roles are cluster-global and may be shared with other databases.
-- Drop them manually if you are certain they are unused:
--   DROP ROLE IF EXISTS fafnir_app, fafnir_read, fafnir_ingest;

BEGIN;

DROP SCHEMA IF EXISTS meta    CASCADE;
DROP SCHEMA IF EXISTS ops     CASCADE;
DROP SCHEMA IF EXISTS ref     CASCADE;
DROP SCHEMA IF EXISTS mart    CASCADE;
DROP SCHEMA IF EXISTS core    CASCADE;
DROP SCHEMA IF EXISTS landing CASCADE;

COMMIT;
