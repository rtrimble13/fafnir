-- 0044_dq_label_statement_scope.down.sql
-- Drops the statement scope of ops.dq_label (0044). A stated label about statements
-- then loses its statement, period type, version and item, and covers every one;
-- its fiscal dates read as dates. To keep them, export the rows first:
--   psql -c "\copy (SELECT * FROM ops.dq_label WHERE statement IS NOT NULL
--            OR period_type IS NOT NULL OR version_no IS NOT NULL
--            OR item IS NOT NULL) TO 'dq_label_statements.csv' CSV HEADER"

BEGIN;

ALTER TABLE ops.dq_label
    DROP CONSTRAINT IF EXISTS ck_dq_label_statement,
    DROP CONSTRAINT IF EXISTS ck_dq_label_period_type,
    DROP CONSTRAINT IF EXISTS ck_dq_label_version_no,
    DROP CONSTRAINT IF EXISTS ck_dq_label_item,
    DROP COLUMN IF EXISTS statement,
    DROP COLUMN IF EXISTS period_type,
    DROP COLUMN IF EXISTS version_no,
    DROP COLUMN IF EXISTS item;

COMMIT;
