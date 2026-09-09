-- 0024_dq_flag_accepted.up.sql
-- Give a DQ condition that is real, permanent and unfixable a way to leave the
-- queue.
--
-- What is missing
-- ---------------
-- ops.data_quality_flag has two dispositions and needs three. `open` is "not yet
-- judged"; `resolved` is "the condition went away". Neither describes a condition
-- that is correctly detected, will never change, and has no repair -- and that
-- case is not rare:
--
--   * A vendor that has no bars for a security's first decade. FMP will not
--     produce them tomorrow. `gap` is right that the sessions are missing.
--   * A security whose stored bar is a measurement rather than a defect --
--     price_scale_collapse, say: real, permanent, and nothing to repair.
--
-- Note what acceptance does NOT reach: the price_<reason> quarantine flags the
-- loader writes on a REJECTED bar (price_subresolution_price,
-- price_price_out_of_range). Those go through add_dq_flag, which has no dedupe
-- probe by design -- count_price_quarantines counts their repeats to decide when a
-- persistently-bad bar has held the watermark long enough, so a probe there would
-- freeze that counter behind the bar forever. Accepting them clears the backlog,
-- but a re-read of the same bar writes a new row. Suppression covers what goes
-- through add_dq_flag_once and the checks in fafnir.dq.checks.
--
-- Resolving one of these is not terminal, because resolution is judged against
-- the data: closing the flag frees its slot in ux_dq_flag_open_condition (0016)
-- and the next `fafnir dq run` writes it again, because the condition is still
-- there. On this warehouse that is 166,903 `gap` flags asking the same
-- unanswerable question every night, in a queue whose whole value is that
-- something being in it means something.
--
-- The shape of the fix is not new
-- ------------------------------
-- 0018 gave core.symbol_change exactly this, for exactly this reason: `conflict`
-- was a retry, right for an obstruction that may clear, and had "no shape at all
-- for a rename that is simply wrong". `dismissed` is terminal and the sweep skips
-- it. `accepted` is that disposition for the DQ queue.
--
-- Why three columns rather than a status enum
-- -------------------------------------------
-- The table already carries resolution provenance as columns (0017:
-- resolved_at/resolved_by/resolution_note) rather than in `detail`, because those
-- have to be queryable across a 900k-row queue. Acceptance is the same: it is the
-- disposition an operator will most want to audit -- "what have we agreed to stop
-- looking at, who agreed, and why" -- so it gets the same treatment. An accepted
-- row is also resolved: acceptance is a resolution, and leaving resolved_at NULL
-- would make an accepted flag count as open everywhere that does not know about
-- this migration.
--
-- Why a second partial index rather than a wider predicate
-- -------------------------------------------------------
-- add_dq_flag_once probes for an existing open flag once per candidate -- 21,000
-- times on a universe-wide `fafnir adjust` -- and its predicate is written to
-- match ix_dq_flag_open_condition (0014). Widening that probe to
-- `(resolved_at IS NULL OR accepted_at IS NOT NULL)` cannot use a partial index
-- and would degrade every one of those probes to a filter over the whole check.
-- So acceptance gets its own partial index and its own probe: two index-backed
-- lookups instead of one that cannot be.

BEGIN;

ALTER TABLE ops.data_quality_flag
    ADD COLUMN IF NOT EXISTS accepted_at   timestamptz,
    ADD COLUMN IF NOT EXISTS accepted_by   text,
    ADD COLUMN IF NOT EXISTS accepted_note text;

COMMENT ON COLUMN ops.data_quality_flag.accepted_at IS
    'When this condition was accepted as real, permanent and unfixable. Set '
    'alongside resolved_at. The checks skip a condition with an accepted row, so '
    'unlike a resolution this does not free the slot for re-detection.';
COMMENT ON COLUMN ops.data_quality_flag.accepted_by IS
    'Who accepted it. Never a sweep: acceptance is always a human judgement.';
COMMENT ON COLUMN ops.data_quality_flag.accepted_note IS
    'Why there is nothing to do. Required -- this is the whole record of a '
    'decision to stop asking, and the next reader has only this.';

-- An accepted row must be a resolved row. Anything reading `resolved_at IS NULL`
-- as "in the queue" -- which is everything written before this migration -- stays
-- correct without knowing acceptance exists.
ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_accepted_is_resolved;
ALTER TABLE ops.data_quality_flag
    ADD CONSTRAINT ck_dq_flag_accepted_is_resolved
    CHECK (accepted_at IS NULL OR resolved_at IS NOT NULL);

-- Mirrors ix_dq_flag_open_condition (0014), for the second probe. Same column
-- order, so the same (check_name, security_id, record_key) lookup is served.
CREATE INDEX IF NOT EXISTS ix_dq_flag_accepted_condition
    ON ops.data_quality_flag (check_name, security_id, record_key)
    WHERE accepted_at IS NOT NULL;

COMMENT ON INDEX ops.ix_dq_flag_accepted_condition IS
    'Serves the acceptance probe in add_dq_flag_once and the NOT EXISTS guards in '
    'fafnir.dq.checks. Partial on accepted_at so it stays small: acceptance is '
    'rare relative to the queue.';

COMMIT;
