-- 0025_operator_override.down.sql
-- Reverse of 0025: drop the operator override record.
--
-- This DESTROYS the record of every operator correction -- what was removed, what
-- was written, by whom and why -- and releases every suppressed key: the next
-- corporate-actions load re-inserts the vendor rows an operator deleted, and the
-- next price load re-inserts deleted bars the vendor still serves. Rows written with
-- source = 'operator' stay in core.corporate_action, but nothing protects them from
-- being overwritten any more.
--
-- Save the record first if the decisions are worth keeping:
--   psql -c "\copy ops.operator_override TO 'operator_override.csv' CSV HEADER"

BEGIN;

COMMENT ON COLUMN core.corporate_action.source IS NULL;

DROP TABLE IF EXISTS ops.operator_override;

COMMIT;
