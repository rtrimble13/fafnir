-- 0044_dq_label_statement_scope.up.sql
-- A stated label about financial statements names what a statement finding names
-- (DR-0913; dorq's doc/labels.md, "Labels about statements"): the statement, the
-- period type, the version and the line item. Each is optional: an unset field
-- covers every value. With any of them, first_date and last_date are fiscal dates,
-- not trade dates.
--
-- `fafnir dq label add --statement --period-type --fiscal-date --version-no --item`
-- writes them, and `dq export-labels` exports them as dorq's `statement`,
-- `period_type`, `version_no` and `item`. A label with none of them is about bars,
-- as every label before this migration is.
--
-- Columns on an existing table: the grants 0028 made on ops.dq_label cover them.
-- Nothing here changes a row that exists.

BEGIN;

ALTER TABLE ops.dq_label
    ADD COLUMN IF NOT EXISTS statement   TEXT,
    ADD COLUMN IF NOT EXISTS period_type TEXT,
    ADD COLUMN IF NOT EXISTS version_no  INTEGER,
    ADD COLUMN IF NOT EXISTS item        TEXT;

ALTER TABLE ops.dq_label
    DROP CONSTRAINT IF EXISTS ck_dq_label_statement,
    ADD CONSTRAINT ck_dq_label_statement
        CHECK (statement IS NULL OR statement IN ('income', 'balance_sheet', 'cash_flow')),
    DROP CONSTRAINT IF EXISTS ck_dq_label_period_type,
    ADD CONSTRAINT ck_dq_label_period_type
        CHECK (period_type IS NULL OR period_type IN ('annual', 'quarter')),
    DROP CONSTRAINT IF EXISTS ck_dq_label_version_no,
    ADD CONSTRAINT ck_dq_label_version_no CHECK (version_no IS NULL OR version_no >= 1),
    DROP CONSTRAINT IF EXISTS ck_dq_label_item,
    ADD CONSTRAINT ck_dq_label_item CHECK (item IS NULL OR btrim(item) <> '');

COMMENT ON COLUMN ops.dq_label.statement IS
    'For a label about statements: income, balance_sheet or cash_flow. NULL: every '
    'statement, or a label about bars.';
COMMENT ON COLUMN ops.dq_label.period_type IS
    'For a label about statements: quarter or annual. NULL: either.';
COMMENT ON COLUMN ops.dq_label.version_no IS
    'For a label about statements: the version (1 as first reported). NULL: any.';
COMMENT ON COLUMN ops.dq_label.item IS
    'For a label about statements: the line item (a column of the statement table). '
    'NULL: any.';

COMMIT;
