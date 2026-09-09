-- 0024_dq_flag_accepted.down.sql
-- Reverse of 0024: drop acceptance.
--
-- This DESTROYS the record of every accepted condition -- who accepted it and
-- why -- and the next `fafnir dq run` will write all of them back into the queue,
-- because acceptance was the only thing stopping re-detection. On a warehouse
-- that has accepted a historical vendor-absence cohort that is six figures of
-- flags returning overnight.
--
-- Run `fafnir dq list --state accepted --detail --json` first if the decisions
-- are worth keeping. There is nowhere else they are written down.

BEGIN;

DROP INDEX IF EXISTS ops.ix_dq_flag_accepted_condition;

ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_accepted_is_resolved;

ALTER TABLE ops.data_quality_flag
    DROP COLUMN IF EXISTS accepted_note,
    DROP COLUMN IF EXISTS accepted_by,
    DROP COLUMN IF EXISTS accepted_at;

COMMIT;
