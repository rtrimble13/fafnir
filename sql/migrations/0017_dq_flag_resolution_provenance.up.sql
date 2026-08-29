-- 0017_dq_flag_resolution_provenance.up.sql
-- Record WHO closed a data-quality flag and WHY, not just that it is closed.
--
-- `resolved_at` (0004) is the whole of what the queue remembers about a decision.
-- That is enough to stop a flag being counted, and not enough to review the
-- decision later: a `gap` on a security whose exchange simply did not trade that
-- day and a `gap` somebody closed to quiet the count look identical afterwards,
-- and the second is exactly the one an operator needs to find again. A flag is a
-- judgement call -- "this outlier is a real 60% move, not bad data" -- and the
-- judgement is the part worth keeping.
--
-- Two nullable columns, no new table: resolution is a single terminal event per
-- flag, so it belongs on the row. A flag that is reopened loses its note, which
-- is the honest thing for a schema with no history table to do -- it does not
-- leave a stale "resolved because X" sitting on a row that is open again. The
-- CHECK enforces exactly that: provenance exists only where a resolution does.
--
-- Nothing about the open-flag rules changes. ux_dq_flag_open_condition (0016) is
-- partial on `resolved_at IS NULL` and indexes neither column, so one unresolved
-- problem is still one unresolved row, and reopening a flag still has to find the
-- condition's slot free.

BEGIN;

ALTER TABLE ops.data_quality_flag
    ADD COLUMN IF NOT EXISTS resolved_by      TEXT,
    ADD COLUMN IF NOT EXISTS resolution_note  TEXT;

COMMENT ON COLUMN ops.data_quality_flag.resolved_by IS
    'Who closed the flag. Defaults to the OS user running `fafnir dq resolve`; '
    'set explicitly with --by. NULL on an open flag.';
COMMENT ON COLUMN ops.data_quality_flag.resolution_note IS
    'Why the flag was closed -- the judgement, which resolved_at alone does not '
    'keep. NULL on an open flag.';

-- Provenance only where there is a resolution. Without this, `fafnir dq reopen`
-- could leave a "resolved because X" note on a row that is open again, and the
-- next operator would read a decision that no longer stands.
ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_resolution_provenance;
ALTER TABLE ops.data_quality_flag
    ADD CONSTRAINT ck_dq_flag_resolution_provenance
    CHECK (resolved_at IS NOT NULL
           OR (resolved_by IS NULL AND resolution_note IS NULL));

-- Serves `fafnir dq list --state resolved`, which reads the closed tail newest
-- first to answer "what was triaged this week, and by whom?". ix_dq_flag_open
-- (0004) is partial on the open side and cannot serve it.
CREATE INDEX IF NOT EXISTS ix_dq_flag_resolved
    ON ops.data_quality_flag (resolved_at DESC)
    WHERE resolved_at IS NOT NULL;

COMMENT ON INDEX ops.ix_dq_flag_resolved IS
    'Reads the resolved tail newest first for `fafnir dq list --state resolved`.';

COMMIT;
