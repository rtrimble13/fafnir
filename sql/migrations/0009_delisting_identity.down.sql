-- 0009_delisting_identity.down.sql
-- Restore 0003's unconditional uniqueness.
--
-- NOTE: this will fail if the data now holds a delisted row and a live row that
-- share (source, primary_symbol, exchange_code) -- exactly the ticker reuse that
-- 0009 exists to permit. That is intentional: rolling back is only safe while no
-- ticker has been reused, and a hard error beats silently deleting a security.

BEGIN;

DROP INDEX IF EXISTS core.ix_security_delisted_symbol;
DROP INDEX IF EXISTS core.ux_security_active_source_symbol_exchange;

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_source_symbol_exchange
    ON core.security (source, primary_symbol, COALESCE(exchange_code, ''));

COMMIT;
