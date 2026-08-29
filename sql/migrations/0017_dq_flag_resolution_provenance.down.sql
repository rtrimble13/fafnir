-- 0017_dq_flag_resolution_provenance.down.sql
-- Drop resolution provenance. This LOSES the recorded notes and resolvers; the
-- flags themselves, and their resolved_at, are untouched, so the queue counts the
-- same problems before and after.

BEGIN;

DROP INDEX IF EXISTS ops.ix_dq_flag_resolved;

ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_resolution_provenance;

ALTER TABLE ops.data_quality_flag
    DROP COLUMN IF EXISTS resolution_note,
    DROP COLUMN IF EXISTS resolved_by;

COMMIT;
