-- 0011_symbol_change.down.sql
-- Drops the rename audit trail. The renames themselves are NOT reverted: they
-- live in core.security.primary_symbol and core.symbol_xref, which is where they
-- belong. Rolling this back only loses the record of which sweep applied them.

BEGIN;

DROP INDEX IF EXISTS core.ix_security_first_seen;
DROP TABLE IF EXISTS core.symbol_change;

COMMIT;
