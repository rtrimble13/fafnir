-- 0018_symbol_change_dismissed.down.sql
-- Narrow core.symbol_change.status back to the 0011 domain.
--
-- Dismissed rows are demoted to `conflict` first, because the constraint could not
-- otherwise be re-added. That is the honest rollback: the status they go back to is
-- the one the 0011 schema has for "not applied, a human must decide", and the
-- operator's reasoning is not lost -- detail->>dismissed_note stays on the row.
-- The cost is that the nightly sweep starts retrying them again, which is exactly
-- the behaviour this migration was written to stop.

BEGIN;

UPDATE core.symbol_change SET status = 'conflict', updated_at = now()
 WHERE status = 'dismissed';

ALTER TABLE core.symbol_change
    DROP CONSTRAINT IF EXISTS ck_symbol_change_status;
ALTER TABLE core.symbol_change
    ADD CONSTRAINT ck_symbol_change_status
    CHECK (status IN ('applied', 'conflict', 'ignored'));

COMMENT ON COLUMN core.symbol_change.status IS
    'applied  = the rename was carried onto an existing security_id (terminal). '
    'conflict = the new ticker already belongs to a different security that '
    'carries history, so a human must decide (retried each sweep). '
    'ignored  = nothing to do: the old ticker belongs to a delisted issuer, so '
    'this is ticker REUSE, not a rename (terminal).';

COMMIT;
