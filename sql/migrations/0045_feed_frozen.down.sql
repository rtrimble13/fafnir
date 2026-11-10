-- 0045_feed_frozen.down.sql
-- Drops the frozen-feed marker (0045). A frozen security then reads as stale
-- again from the next `dq run`. To keep the markers, export them first:
--   psql -c "\copy (SELECT security_id, feed_frozen_on, feed_frozen_note
--            FROM core.security WHERE feed_frozen_on IS NOT NULL)
--            TO 'feed_frozen.csv' CSV HEADER"
--

BEGIN;

-- mart.fundamental_panel (0037) reads this view, so it cannot be dropped and
-- re-created; CREATE OR REPLACE keeps the two columns, reading NULL. The next up
-- migration that redefines the view may drop them.
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
    cp.loaded_at                AS description_loaded_at,
    NULL::date                  AS feed_frozen_on,
    NULL::text                  AS feed_frozen_note
FROM core.security s
LEFT JOIN ref.exchange      x   ON x.exchange_code = s.exchange_code
LEFT JOIN core.v_security_classification cl ON cl.security_id = s.security_id
LEFT JOIN ref.sector        sec ON sec.sector_id   = cl.sector_id
LEFT JOIN ref.industry      ind ON ind.industry_id = cl.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_profile IS
    'Descriptive profile per security, read live. sector_name and industry_name are '
    'the feeding vendor''s (core.v_security_classification, #109). market_cap_usd and '
    'beta are a company-screener snapshot refreshed with the security master -- not '
    'history; mart.security_latest derives its own.';

DROP INDEX IF EXISTS core.ix_security_feed_frozen;

ALTER TABLE core.security
    DROP CONSTRAINT IF EXISTS ck_security_feed_frozen,
    DROP COLUMN IF EXISTS feed_frozen_on,
    DROP COLUMN IF EXISTS feed_frozen_note;

COMMIT;
