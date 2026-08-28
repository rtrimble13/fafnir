-- 0014_dq_flag_open_condition_index.up.sql
-- Make "is this problem already in the review queue?" a cheap lookup.
--
-- The queue only means something if one unresolved problem is one unresolved
-- flag. Every check that writes here runs on a schedule over the same data --
-- `fafnir adjust` recomputes every security with actions nightly, `fafnir dq run`
-- re-scans the whole universe -- so an unguarded INSERT turns one standing
-- problem into one new row per night. `fafnir status` reports
-- count(*) WHERE resolved_at IS NULL, so the number an operator triages on grows
-- without bound while the number of actual problems stays flat, until it says
-- nothing at all.
--
-- The fix is in the write path (repository.add_dq_flag_once, and the set-based
-- equivalent in fafnir.dq.checks): skip the insert when an unresolved flag with
-- the same (security_id, check_name, record_key) already exists. That guard runs
-- once per candidate row -- 21,000 of them on a universe-wide gap sweep -- and the
-- indexes from 0004 do not serve it: ix_dq_flag_open leads on check_name and then
-- detected_at, so a probe for one security scans every open flag of that check,
-- and ix_dq_flag_security leads on security_id but covers resolved rows too. This
-- index is exactly the guard's predicate.
--
-- Deliberately NOT unique. A unique constraint would enforce the rule in the
-- schema rather than in the write path, which is stronger -- but it cannot be
-- added to a warehouse that has been running: every existing duplicate would have
-- to be deleted or resolved first, and CREATE UNIQUE INDEX would simply fail on
-- the very inflation this issue is about. It would also have to carve out the
-- price_* checks, whose repetition is load-bearing (count_price_quarantines counts
-- those rows to bound the watermark hold on a persistently-bad bar), and handle
-- the nullable record_key and security_id. Enforcing it in the schema is worth
-- doing alongside a migration that first collapses the existing duplicates; that
-- is a separate, data-changing change.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_dq_flag_open_condition
    ON ops.data_quality_flag (check_name, security_id, record_key)
    WHERE resolved_at IS NULL;

COMMENT ON INDEX ops.ix_dq_flag_open_condition IS
    'Serves the "already flagged and unresolved?" probe that keeps a standing '
    'condition to one open flag (repository.add_dq_flag_once, fafnir.dq.checks). '
    'Not unique: see migration 0014 for why.';

COMMIT;
