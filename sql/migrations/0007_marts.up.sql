-- 0007_marts.up.sql
-- Read marts. This is the seam external apps, MCP, and duk-db mode read from.
--
--  * mart.v_daily_price_adjusted -- derives split/dividend-adjusted OHLCV ON READ
--                                   from raw prices x core.adjustment_factor. Because
--                                   factors are derived deterministically from corporate
--                                   actions, the adjusted series is point-in-time stable.
--  * mart.security_latest        -- materialized snapshot for screening (refresh on schedule).

BEGIN;

-- ---------------------------------------------------------------------------
-- Adjusted-price view.
-- For a price at trade_date t, the applicable cumulative factor is the row with
-- the smallest effective_date strictly greater than t (i.e. the product of all
-- corporate-action factors that occurred AFTER t). If none exists (t is on/after
-- the latest ex-date), the factor is 1.0 -> raw == adjusted for recent prices.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_daily_price_adjusted AS
SELECT
    p.security_id,
    p.trade_date,
    ROUND(p.open  * COALESCE(af.cumulative_price_factor, 1.0), 6)::numeric(20, 6) AS open,
    ROUND(p.high  * COALESCE(af.cumulative_price_factor, 1.0), 6)::numeric(20, 6) AS high,
    ROUND(p.low   * COALESCE(af.cumulative_price_factor, 1.0), 6)::numeric(20, 6) AS low,
    ROUND(p.close * COALESCE(af.cumulative_price_factor, 1.0), 6)::numeric(20, 6) AS close,
    (p.volume * COALESCE(af.cumulative_volume_factor, 1.0))::bigint               AS volume,
    p.close                                                                       AS close_raw,
    COALESCE(af.cumulative_price_factor, 1.0)                                     AS price_factor,
    COALESCE(af.cumulative_volume_factor, 1.0)                                    AS volume_factor
FROM core.daily_price p
LEFT JOIN LATERAL (
    SELECT a.cumulative_price_factor, a.cumulative_volume_factor
    FROM core.adjustment_factor a
    WHERE a.security_id = p.security_id
      AND a.effective_date > p.trade_date
    ORDER BY a.effective_date ASC
    LIMIT 1
) af ON TRUE;

COMMENT ON VIEW mart.v_daily_price_adjusted IS
    'Split/dividend-adjusted OHLCV derived on read. Point-in-time stable. '
    'close_raw exposes the unadjusted close; price_factor/volume_factor expose the applied factors.';

-- ---------------------------------------------------------------------------
-- Latest snapshot per security for screening. Materialized; refresh on schedule
-- via `fafnir db refresh-marts` (REFRESH MATERIALIZED VIEW CONCURRENTLY).
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.security_latest AS
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

COMMENT ON MATERIALIZED VIEW mart.security_latest IS
    'Latest profile + last price per security for screening. Derived snapshot; refresh on schedule.';

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_security_latest_id
    ON mart.security_latest (security_id);
CREATE INDEX IF NOT EXISTS ix_security_latest_symbol
    ON mart.security_latest (symbol);

COMMIT;
