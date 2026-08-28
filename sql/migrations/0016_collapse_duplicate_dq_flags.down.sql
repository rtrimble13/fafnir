-- 0016_collapse_duplicate_dq_flags.down.sql
-- Drop the one-open-flag-per-condition constraint.
--
-- The collapsed duplicates do not come back, and nothing is lost by that: each was
-- a restatement of a condition whose surviving row still carries the earliest
-- detected_at. Rolling the schema back is not a reason to re-inflate the queue.

BEGIN;

DROP INDEX IF EXISTS ops.ux_dq_flag_open_condition;

COMMIT;
