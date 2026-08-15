-- 0010_trim_company_profile.down.sql
-- Restore 0003's wider core.company_profile and 0007's matview.
--
-- NOTE: the dropped columns come back empty apart from market_cap_usd and beta,
-- which are carried back from core.security. ceo, website, full_time_employees,
-- last_dividend, price_range and image_url are gone -- and, because
-- enrich_profiles never landed its raw payloads, they are not recoverable from
-- landing either. Re-run `fafnir ingest securities --enrich` to repopulate them.

BEGIN;

ALTER TABLE core.company_profile
    ADD COLUMN IF NOT EXISTS ceo                 TEXT,
    ADD COLUMN IF NOT EXISTS full_time_employees BIGINT,
    ADD COLUMN IF NOT EXISTS website             TEXT,
    ADD COLUMN IF NOT EXISTS beta                NUMERIC(12, 6),
    ADD COLUMN IF NOT EXISTS market_cap_usd      NUMERIC(24, 2),
    ADD COLUMN IF NOT EXISTS last_dividend       NUMERIC(20, 6),
    ADD COLUMN IF NOT EXISTS price_range         TEXT,
    ADD COLUMN IF NOT EXISTS image_url           TEXT;

UPDATE core.company_profile cp
   SET market_cap_usd = s.market_cap_usd,
       beta           = s.beta
  FROM core.security s
 WHERE s.security_id = cp.security_id;

DROP MATERIALIZED VIEW IF EXISTS mart.security_latest;

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
    cp.market_cap_usd,
    cp.beta,
    lp.trade_date                  AS last_trade_date,
    lp.close                       AS last_close,
    lp.volume                      AS last_volume
FROM core.security s
LEFT JOIN ref.sector  sec ON sec.sector_id   = s.sector_id
LEFT JOIN ref.industry ind ON ind.industry_id = s.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id
LEFT JOIN LATERAL (
    SELECT dp.trade_date, dp.close, dp.volume
    FROM core.daily_price dp
    WHERE dp.security_id = s.security_id
    ORDER BY dp.trade_date DESC
    LIMIT 1
) lp ON TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_latest_id
    ON mart.security_latest (security_id);
CREATE INDEX IF NOT EXISTS ix_security_latest_symbol
    ON mart.security_latest (symbol);

ALTER TABLE core.security
    DROP COLUMN IF EXISTS market_cap_usd,
    DROP COLUMN IF EXISTS beta;

COMMIT;
