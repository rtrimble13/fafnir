-- 0029_vendor_correction.down.sql
-- Reverse of 0029: drop the vendor correction record.
--
-- This DESTROYS the record of every row one vendor replaced in place of another's,
-- and with it the only copy of each replaced row: a correction can no longer be
-- reverted, and removing a vendor can no longer restore the rows it replaced. The
-- corrected rows themselves stay in core, under the correcting vendor's source.
--
-- Save the record first if it is worth keeping:
--   psql -c "\copy ops.vendor_correction TO 'vendor_correction.csv' CSV HEADER"

BEGIN;

-- Restore the comments 0026 and 0025 left on the two source columns.
COMMENT ON COLUMN core.daily_price.source IS
    'fmp for a vendor bar; operator for a vendor bar an operator re-dated or re-scaled '
    '(`fafnir prices shift|rescale`, see ops.operator_override). The price loader never '
    'overwrites an operator bar.';
COMMENT ON COLUMN core.corporate_action.source IS
    'Where the row came from: fmp for the vendor feed, operator for a row written by '
    '`fafnir actions add|redate` (see ops.operator_override). The loaders never '
    'overwrite an operator row.';

DROP TABLE IF EXISTS ops.vendor_correction;

COMMIT;
