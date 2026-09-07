-- 0023_security_identifier_indexes.down.sql
-- Drop the identifier indexes. Identifier lookups stay CORRECT without them -- the
-- predicates are unchanged and simply sequential-scan core.security -- so this is
-- a performance rollback, not a behaviour one.

BEGIN;

DROP INDEX IF EXISTS core.ix_security_cik_normalised;
DROP INDEX IF EXISTS core.ix_security_isin_normalised;
DROP INDEX IF EXISTS core.ix_security_cusip_normalised;

COMMIT;
