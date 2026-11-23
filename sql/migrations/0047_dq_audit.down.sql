-- 0047_dq_audit.down.sql
-- Drops the audit cycles (0047). Every cycle, its units and the second
-- verification go with them, and the labels lose their sample and weight: they
-- stay, as ordinary stated labels. To keep a cycle, export it first:
--   psql -c "\copy (SELECT * FROM ops.dq_audit_item) TO 'dq_audit_item.csv' CSV HEADER"
--   fafnir dq export-labels --out labels.jsonl      # carries sample and weight

BEGIN;

DROP INDEX IF EXISTS ops.ix_dq_label_sample;

ALTER TABLE ops.dq_label
    DROP CONSTRAINT IF EXISTS fk_dq_label_sample_item,
    DROP CONSTRAINT IF EXISTS ck_dq_label_sample,
    DROP COLUMN IF EXISTS weight,
    DROP COLUMN IF EXISTS sample_item,
    DROP COLUMN IF EXISTS sample_id;

DROP TABLE IF EXISTS ops.dq_audit_item;
DROP TABLE IF EXISTS ops.dq_audit_cycle;

COMMIT;
