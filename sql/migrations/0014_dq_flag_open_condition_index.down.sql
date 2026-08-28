-- 0014_dq_flag_open_condition_index.down.sql
-- Drop the open-condition index. Losing it makes the dedupe probe slower, not
-- wrong: the guard is in the write path, so the queue stays correct either way.

BEGIN;

DROP INDEX IF EXISTS ops.ix_dq_flag_open_condition;

COMMIT;
