-- 0021_ops_reader_role.down.sql
-- Reverse of 0021: revoke everything fafnir_ops was granted.
--
-- The ROLE is intentionally not dropped, for the same reason 0001 does not drop
-- the three it creates: roles are cluster-global and may be a member of something,
-- or have members, that this database knows nothing about. Dropping one out from
-- under a login role that inherits it produces a confusing failure at connect time
-- rather than here, where it could be explained.
--
-- Revoking is enough to close the tier: a member of fafnir_ops that keeps the
-- membership but loses every grant reads nothing. To remove it entirely, once you
-- are certain no login role inherits it:
--   DROP ROLE IF EXISTS fafnir_ops;
--
-- REVOKE of a privilege that was never granted is not an error, so this is safe to
-- run against a partially-applied 0021.

BEGIN;

-- Undo the forward-looking grant first. Order matters for readability only, but
-- ALTER DEFAULT PRIVILEGES is the half most easily forgotten -- leave it in place
-- and every table a LATER migration creates is still granted to a role this
-- rollback was supposed to have cut off.
ALTER DEFAULT PRIVILEGES IN SCHEMA core, mart, ref, ops, landing, meta
    REVOKE SELECT ON TABLES FROM fafnir_ops;

REVOKE SELECT ON ALL TABLES IN SCHEMA core, mart, ref, ops, landing, meta
    FROM fafnir_ops;

REVOKE USAGE ON SCHEMA core, mart, ref, ops, landing, meta FROM fafnir_ops;

COMMIT;
