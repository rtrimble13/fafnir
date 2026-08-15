-- 0009_delisting_identity.up.sql
-- Make delisted securities immutable history, so ticker reuse cannot corrupt
-- identity and backtests stay free of survivorship bias.
--
-- The problem this fixes. 0003 made the security upsert key
-- (source, primary_symbol, COALESCE(exchange_code, '')). Circuit City (CC, NYSE,
-- delisted 2009) and Chemours (CC, NYSE, listed 2015) collide on that key, so the
-- security-master loader would UPDATE the dead company's row into the live one --
-- silently re-pointing years of price history at the wrong issuer, and flipping
-- is_actively_trading back on.
--
-- The fix is to scope uniqueness to *currently listed* securities. At most one
-- active row per (source, symbol, exchange); delisted rows are retained forever
-- and are invisible to the upsert's conflict arbiter, so they are never
-- resurrected or overwritten. A reused ticker mints a new security_id, which is
-- what core.symbol_xref exists to disambiguate.

BEGIN;

DROP INDEX IF EXISTS core.ux_security_source_symbol_exchange;

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_active_source_symbol_exchange
    ON core.security (source, primary_symbol, COALESCE(exchange_code, ''))
    WHERE delisted_date IS NULL;

COMMENT ON INDEX core.ux_security_active_source_symbol_exchange IS
    'At most one LISTED security per (source, symbol, exchange). Delisted rows are '
    'excluded so a reused ticker mints a new security_id instead of overwriting history.';

-- Delisted rows are still worth finding by ticker (reuse detection, research on
-- dead names), just not via a unique constraint.
CREATE INDEX IF NOT EXISTS ix_security_delisted_symbol
    ON core.security (primary_symbol, delisted_date)
    WHERE delisted_date IS NOT NULL;

COMMIT;
