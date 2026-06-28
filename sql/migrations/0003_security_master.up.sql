-- 0003_security_master.up.sql
-- The conformed security dimension. Everything joins to security_id.
--
-- Design commitments (per the Database Engineer brief):
--  * Surrogate security_id is the identity -- tickers are NEVER keys (reused/renamed).
--  * symbol_xref maps external tickers to security_id over time (renames/relists).
--  * Delisted / inactive securities are RETAINED (delisted_date set), never deleted,
--    so backtests are free of survivorship bias.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.security  -- one row per security_id
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.security (
    security_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    primary_symbol       TEXT NOT NULL,                 -- current/best-known ticker
    company_name         TEXT,
    asset_type           TEXT NOT NULL DEFAULT 'equity'
                            CHECK (asset_type IN ('equity', 'etf', 'fund', 'other')),
    exchange_code        TEXT REFERENCES ref.exchange (exchange_code),
    sector_id            INTEGER REFERENCES ref.sector (sector_id),
    industry_id          INTEGER REFERENCES ref.industry (industry_id),
    currency             TEXT NOT NULL DEFAULT 'USD',
    country              TEXT,
    cik                  TEXT,
    isin                 TEXT,
    cusip                TEXT,
    is_actively_trading  BOOLEAN NOT NULL DEFAULT TRUE,
    is_etf               BOOLEAN NOT NULL DEFAULT FALSE,
    is_fund              BOOLEAN NOT NULL DEFAULT FALSE,
    ipo_date             DATE,
    delisted_date        DATE,                          -- NULL while listed; set on delist
    source               TEXT NOT NULL DEFAULT 'fmp',
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  core.security IS
    'Conformed security dimension. Grain: one row per minted security_id. Delisted names retained.';
COMMENT ON COLUMN core.security.primary_symbol IS
    'Current/best-known ticker. NOT an identifier -- resolve point-in-time via core.symbol_xref.';
COMMENT ON COLUMN core.security.delisted_date IS
    'NULL while listed. Set (never deleted) when a security stops trading -- avoids survivorship bias.';

-- A security is identified to the loader by its (source, primary_symbol, exchange).
-- This soft natural key makes the security-master upsert idempotent without
-- making the ticker a global key.
CREATE UNIQUE INDEX IF NOT EXISTS ux_security_source_symbol_exchange
    ON core.security (source, primary_symbol, COALESCE(exchange_code, ''));

CREATE INDEX IF NOT EXISTS ix_security_primary_symbol ON core.security (primary_symbol);
CREATE INDEX IF NOT EXISTS ix_security_active         ON core.security (is_actively_trading);

-- ---------------------------------------------------------------------------
-- core.symbol_xref  -- ticker history / cross-reference
-- Resolves a ticker to a security_id at any point in time (renames/relists).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.symbol_xref (
    security_id   BIGINT NOT NULL REFERENCES core.security (security_id),
    symbol        TEXT   NOT NULL,
    valid_from    DATE   NOT NULL DEFAULT '1900-01-01',
    valid_to      DATE,                                  -- NULL = currently valid
    is_primary    BOOLEAN NOT NULL DEFAULT TRUE,
    source        TEXT NOT NULL DEFAULT 'fmp',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, valid_from),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
COMMENT ON TABLE core.symbol_xref IS
    'Maps external tickers to security_id over time. Grain: (symbol, valid_from). NULL valid_to = current.';

CREATE INDEX IF NOT EXISTS ix_symbol_xref_security ON core.symbol_xref (security_id);

-- ---------------------------------------------------------------------------
-- core.company_profile  -- descriptive attributes (current snapshot)
-- Full history is preserved in landing; this holds the latest profile.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.company_profile (
    security_id        BIGINT PRIMARY KEY REFERENCES core.security (security_id),
    description        TEXT,
    ceo                TEXT,
    full_time_employees BIGINT,
    website            TEXT,
    beta               NUMERIC(12, 6),
    market_cap_usd     NUMERIC(24, 2),
    last_dividend      NUMERIC(20, 6),
    price_range        TEXT,
    image_url          TEXT,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source             TEXT NOT NULL DEFAULT 'fmp'
);
COMMENT ON TABLE core.company_profile IS
    'Current descriptive profile per security. Grain: one row per security_id. History lives in landing.';

COMMIT;
