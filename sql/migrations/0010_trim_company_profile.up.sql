-- 0010_trim_company_profile.up.sql
-- Reduce core.company_profile to the one attribute that is genuinely per-company
-- descriptive text, and re-source the two screening attributes from the security
-- master, where they arrive for free.
--
-- Why. 0003 put market_cap_usd and beta on core.company_profile, so the only way
-- to populate them was `fafnir ingest securities --enrich` -- one profile request
-- per symbol, ~75 minutes across a 21k universe. But FMP's company-screener,
-- which the security-master loader already calls for every symbol, returns both.
-- Moving them onto core.security means screening data costs nothing extra, and
-- --enrich becomes optional (it now only supplies `description`).
--
-- mart.security_latest keeps emitting the same column names from the new source,
-- so duk's screener (market_cap_usd / beta filters) is unaffected.

BEGIN;

-- 1. New home for the screener-sourced attributes.
ALTER TABLE core.security
    ADD COLUMN IF NOT EXISTS market_cap_usd NUMERIC(24, 2),
    ADD COLUMN IF NOT EXISTS beta           NUMERIC(12, 6);

COMMENT ON COLUMN core.security.market_cap_usd IS
    'From company-screener, refreshed each security-master load. Point-in-time '
    'snapshot, not history -- do not use for backtests.';
COMMENT ON COLUMN core.security.beta IS
    'From company-screener, refreshed each security-master load. Vendor-computed.';

-- 2. Carry across whatever enrichment already collected, so trimming the profile
--    table costs no data. (No-op on a fresh install.)
UPDATE core.security s
   SET market_cap_usd = cp.market_cap_usd,
       beta           = cp.beta
  FROM core.company_profile cp
 WHERE cp.security_id = s.security_id
   AND (cp.market_cap_usd IS NOT NULL OR cp.beta IS NOT NULL);

-- 3. The matview reads the columns being dropped, so it has to go first.
--    CREATE OR REPLACE cannot remove or re-source a column.
DROP MATERIALIZED VIEW IF EXISTS mart.security_latest;

-- 4. Trim the profile table to security_id + description (+ bookkeeping).
ALTER TABLE core.company_profile
    DROP COLUMN IF EXISTS ceo,
    DROP COLUMN IF EXISTS full_time_employees,
    DROP COLUMN IF EXISTS website,
    DROP COLUMN IF EXISTS beta,
    DROP COLUMN IF EXISTS market_cap_usd,
    DROP COLUMN IF EXISTS last_dividend,
    DROP COLUMN IF EXISTS price_range,
    DROP COLUMN IF EXISTS image_url;

COMMENT ON TABLE core.company_profile IS
    'Long-form company description per security. Grain: one row per security_id. '
    'Populated only by `ingest securities --enrich`; raw history lives in landing.';

-- 5. Rebuild the screening snapshot, identical in shape -- only the source of
--    market_cap_usd and beta changed.
CREATE MATERIALIZED VIEW mart.security_latest AS
SELECT
    s.security_id,
    s.primary_symbol               AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    sec.sector_name,
    ind.industry_name,
    s.currency,
    s.country,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    s.market_cap_usd,
    s.beta,
    lp.trade_date                  AS last_trade_date,
    lp.close                       AS last_close,
    lp.volume                      AS last_volume
FROM core.security s
LEFT JOIN ref.sector  sec ON sec.sector_id   = s.sector_id
LEFT JOIN ref.industry ind ON ind.industry_id = s.industry_id
LEFT JOIN LATERAL (
    SELECT dp.trade_date, dp.close, dp.volume
    FROM core.daily_price dp
    WHERE dp.security_id = s.security_id
    ORDER BY dp.trade_date DESC
    LIMIT 1
) lp ON TRUE;

COMMENT ON MATERIALIZED VIEW mart.security_latest IS
    'Latest screening attributes + last price per security. Derived snapshot; refresh on schedule.';

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_security_latest_id
    ON mart.security_latest (security_id);
CREATE INDEX IF NOT EXISTS ix_security_latest_symbol
    ON mart.security_latest (symbol);

COMMIT;
