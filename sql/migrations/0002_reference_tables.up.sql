-- 0002_reference_tables.up.sql
-- Reference / lookup data: exchanges, sectors, industries, trading calendar.
-- These are small, slowly-changing dimensions referenced by the security master
-- and used for gap detection (trading_calendar).

BEGIN;

-- ---------------------------------------------------------------------------
-- ref.exchange
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.exchange (
    exchange_code   TEXT PRIMARY KEY,          -- e.g. 'NASDAQ', 'NYSE', 'AMEX'
    exchange_name   TEXT,
    country         TEXT,                       -- ISO-ish country code, e.g. 'US'
    timezone        TEXT,                       -- IANA tz, e.g. 'America/New_York'
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  ref.exchange IS 'Trading venues. Grain: one row per exchange_code.';
COMMENT ON COLUMN ref.exchange.timezone IS 'IANA timezone of the exchange; used to recover local market time from UTC.';

-- ---------------------------------------------------------------------------
-- ref.sector / ref.industry  (FMP taxonomy)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.sector (
    sector_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sector_name  TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ref.sector IS 'FMP sector taxonomy. Grain: one row per sector_name.';

CREATE TABLE IF NOT EXISTS ref.industry (
    industry_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    industry_name  TEXT NOT NULL UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ref.industry IS 'FMP industry taxonomy. Grain: one row per industry_name.';

-- ---------------------------------------------------------------------------
-- ref.trading_calendar
-- Used to validate EOD DATE semantics and detect gaps in daily_price.
-- Grain: one row per (exchange_code, trade_date).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.trading_calendar (
    exchange_code  TEXT NOT NULL REFERENCES ref.exchange (exchange_code),
    trade_date     DATE NOT NULL,
    is_open        BOOLEAN NOT NULL DEFAULT TRUE,
    session_note   TEXT,                         -- e.g. 'half-day', 'holiday'
    PRIMARY KEY (exchange_code, trade_date)
);
COMMENT ON TABLE ref.trading_calendar IS
    'Exchange trading days for gap detection. is_open=false marks holidays/closures.';

CREATE INDEX IF NOT EXISTS ix_trading_calendar_open
    ON ref.trading_calendar (exchange_code, trade_date)
    WHERE is_open;

COMMIT;
