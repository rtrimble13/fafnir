-- 0028_labels_and_override_reasons.down.sql
-- Reverse of 0028: drop the stated labels, the judgement basis and the override
-- reasons.
--
-- This DESTROYS every label stated with `fafnir dq label add|import` or `--label`,
-- and the record of which closures were judged per flag. Every override reads as a
-- data error again, so CELG-RI's scope removal exports as 770 bar_deleted labels.
-- Save them first if they are worth keeping:
--   psql -c "\copy ops.dq_label TO 'dq_label.csv' CSV HEADER"

BEGIN;

DROP TABLE IF EXISTS ops.dq_label;

ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_judgement_basis;
ALTER TABLE ops.data_quality_flag
    DROP COLUMN IF EXISTS judgement_basis;

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_reason;
ALTER TABLE ops.operator_override
    DROP COLUMN IF EXISTS reason;

COMMIT;
