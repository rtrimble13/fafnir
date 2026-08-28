-- 0016_collapse_duplicate_dq_flags.up.sql
-- Collapse the duplicate open flags a running warehouse has already accumulated.
--
-- 0014 stopped new ones: a standing condition is now flagged once, not once per
-- run. It deliberately did NOT enforce that in the schema, because CREATE UNIQUE
-- INDEX would have failed on the duplicates already in the table -- the very
-- inflation the change was about. This is the migration 0014 said that needed.
--
-- `fafnir status` reports count(*) WHERE resolved_at IS NULL. On a warehouse that
-- has been running nightly, that number counted check RUNS, not problems: one
-- security with 40 missing days contributed 40 rows a night. Collapsing the
-- duplicates is what makes the standing count mean something for data already
-- written, not just for data written from here on.
--
-- The price_* checks are carved out of all of it. count_price_quarantines counts
-- those rows for a (security_id, date) to decide when a persistently-bad bar has
-- held the ingestion watermark long enough (MAX_QUARANTINE_HOLDS): their repetition
-- IS the counter. Collapsing them would reset it to 1 and hold the watermark behind
-- that bar forever, and constraining them would stop the counter advancing at all.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Keep the EARLIEST open flag for each (security_id, check_name, record_key).
--    Earliest, not latest: detected_at on the surviving row is then the moment the
--    problem was first seen, which is the one fact a triage queue must not lose.
--    The rows dropped are restatements of that same condition by a later run --
--    same key, and their detail is a re-measurement of a problem still open, not a
--    new observation. dq_flag_id breaks ties on identical detected_at.
-- ---------------------------------------------------------------------------
WITH ranked AS (
    SELECT dq_flag_id,
           row_number() OVER (
               PARTITION BY security_id, check_name, record_key
               ORDER BY detected_at, dq_flag_id
           ) AS n
      FROM ops.data_quality_flag
     WHERE resolved_at IS NULL
       AND check_name NOT LIKE 'price\_%'
)
DELETE FROM ops.data_quality_flag f
 USING ranked r
 WHERE f.dq_flag_id = r.dq_flag_id AND r.n > 1;

-- NOTE: PARTITION BY treats NULL as one group, which is what is wanted here --
-- security_id and record_key are both nullable and a keyless flag
-- (adjustment_failed) has to dedupe per security, matching add_dq_flag_once.

-- ---------------------------------------------------------------------------
-- 2. Enforce it. The expressions are what make the nullable columns work in a
--    btree unique index without depending on NULLS NOT DISTINCT (PG15+): -1 is
--    unreachable for an identity-generated security_id, and '{}' is unreachable
--    for record_key because add_dq_flag/add_dq_flag_once store NULL rather than an
--    empty object. jsonb equality is semantic, so a key written with its fields in
--    another order is the same condition here and in the write-path probe.
--
--    This is a backstop, not the mechanism: the guard stays in the write path
--    (add_dq_flag_once, and the set-based equivalents in fafnir.dq.checks), because
--    a constraint alone would turn a redundant flag -- harmless noise -- into an
--    exception that aborts whatever load raised it. It catches a future call site
--    that reaches for add_dq_flag when it wanted add_dq_flag_once.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_dq_flag_open_condition
    ON ops.data_quality_flag (
        check_name,
        COALESCE(security_id, -1),
        COALESCE(record_key, '{}'::jsonb)
    )
    WHERE resolved_at IS NULL AND check_name NOT LIKE 'price\_%';

COMMENT ON INDEX ops.ux_dq_flag_open_condition IS
    'One unresolved problem is one unresolved row. Excludes price_*, whose repeats '
    'count_price_quarantines counts to bound the watermark hold (migration 0016).';

COMMIT;
