-- 0039_attribute_precedence.down.sql
-- Restores mart.security_latest and mart.v_security_profile as 0010 and 0020 defined
-- them, reading core.security, and drops the two precedence views. Everything here
-- is derived: nothing is lost.

BEGIN;

CREATE OR REPLACE VIEW mart.v_security_profile AS
SELECT
    s.security_id,
    s.primary_symbol            AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    x.exchange_name,
    sec.sector_name,
    ind.industry_name,
    s.currency,
    s.country,
    s.cik,
    s.isin,
    s.cusip,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    s.ipo_date,
    s.delisted_date,
    s.market_cap_usd,
    s.beta,
    s.source,
    s.first_seen_at,
    s.updated_at,
    cp.description,
    cp.loaded_at                AS description_loaded_at
FROM core.security s
LEFT JOIN ref.exchange      x   ON x.exchange_code = s.exchange_code
LEFT JOIN ref.sector        sec ON sec.sector_id   = s.sector_id
LEFT JOIN ref.industry      ind ON ind.industry_id = s.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_profile IS
    'Descriptive profile per security, read live. market_cap_usd and beta are a '
    'company-screener snapshot refreshed with the security master -- not history.';

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

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_latest_id
    ON mart.security_latest (security_id);
CREATE INDEX IF NOT EXISTS ix_security_latest_symbol
    ON mart.security_latest (symbol);

GRANT SELECT ON mart.security_latest TO fafnir_read, fafnir_app, fafnir_ops;

DROP VIEW IF EXISTS mart.v_security_attributes;
DROP VIEW IF EXISTS core.v_security_classification;

COMMIT;
