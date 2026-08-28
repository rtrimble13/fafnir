-- 0015_one_open_xref_period.down.sql
-- Drop the one-open-period constraint.
--
-- Only the index comes back off. The rows step 1 deleted were duplicates carrying a
-- false claim and the periods step 2 closed are still there, closed -- neither is
-- restorable, and neither should be: rolling back the schema is not a reason to
-- reintroduce an ambiguous ticker. The write path keeps the invariant on its own.

BEGIN;

DROP INDEX IF EXISTS core.ux_symbol_xref_open;

COMMIT;
