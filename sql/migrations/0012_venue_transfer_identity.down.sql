-- 0012_venue_transfer_identity.down.sql
-- Restores the exchange to the identity key. Note that the duplicate rows folded
-- by the up-migration are NOT restored: they were stubs with no bars, actions or
-- factors, and the securities they duplicated are still here holding the history.

BEGIN;

DROP INDEX IF EXISTS core.ux_security_active_source_symbol;

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_active_source_symbol_exchange
    ON core.security (source, primary_symbol, COALESCE(exchange_code, ''))
    WHERE delisted_date IS NULL;

COMMENT ON INDEX core.ux_security_active_source_symbol_exchange IS
    'At most one LISTED security per (source, symbol, exchange). Delisted rows are '
    'excluded so a reused ticker mints a new security_id instead of overwriting history.';

COMMIT;
