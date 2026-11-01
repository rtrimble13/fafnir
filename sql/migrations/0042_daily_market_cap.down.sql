-- 0042_daily_market_cap.down.sql
-- Drops mart.fn_daily_market_cap and mart.v_share_factor_versions, and restores
-- mart.v_security_attributes as 0039 defined it (no share factor). Everything here is
-- derived: nothing is lost. mart.security_latest keeps its columns and reads the
-- restored view on its next refresh.

BEGIN;

CREATE OR REPLACE VIEW mart.v_security_attributes AS
SELECT
    s.security_id,
    s.primary_symbol AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    sec.sector_name,
    ind.industry_name,
    c.attributes_source,
    s.currency,
    s.country,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    CASE WHEN mc.market_cap IS NOT NULL THEN mc.market_cap ELSE s.market_cap_usd END
        AS market_cap_usd,
    CASE WHEN mc.market_cap IS NOT NULL THEN 'derived'
         WHEN s.market_cap_usd IS NOT NULL THEN s.source END
        AS market_cap_source,
    CASE WHEN mc.market_cap IS NOT NULL THEN lp.trade_date END AS market_cap_as_of,
    mc.fiscal_date AS market_cap_fiscal_date,
    CASE WHEN bt.beta IS NOT NULL THEN bt.beta ELSE s.beta END AS beta,
    CASE WHEN bt.beta IS NOT NULL THEN 'derived'
         WHEN s.beta IS NOT NULL THEN s.source END
        AS beta_source,
    bt.as_of AS beta_as_of,
    bt.returns AS beta_returns,
    lp.trade_date AS last_trade_date,
    lp.close AS last_close,
    lp.volume AS last_volume
FROM core.security s
JOIN core.v_security_classification c ON c.security_id = s.security_id
LEFT JOIN ref.sector sec ON sec.sector_id = c.sector_id
LEFT JOIN ref.industry ind ON ind.industry_id = c.industry_id
LEFT JOIN LATERAL (
    SELECT dp.trade_date, dp.close, dp.volume
      FROM core.daily_price dp
     WHERE dp.security_id = s.security_id
     ORDER BY dp.trade_date DESC
     LIMIT 1
) lp ON true
LEFT JOIN LATERAL (
    -- ADR 0019 §5, the panel's expression: the raw close x the shares of the latest
    -- balance sheet in force that day, in that day's terms.
    SELECT b.fiscal_date,
           lp.close * round(b.shares_outstanding * (
               SELECT exp(coalesce(sum(ln(a.split_numerator::numeric
                                          / a.split_denominator)), 0))
                 FROM mart.v_corporate_action a
                WHERE a.security_id = s.security_id AND a.action_type = 'split'
                  AND a.split_numerator > 0 AND a.split_denominator > 0
                  AND a.ex_date > b.known_from AND a.ex_date <= lp.trade_date))
               AS market_cap
      FROM core.balance_sheet b
     WHERE b.security_id = s.security_id
       AND coalesce(s.currency, 'USD') = 'USD'
       AND b.superseded_at IS NULL AND b.shares_outstanding > 0
       AND b.known_from <= lp.trade_date
       AND (b.known_to IS NULL OR lp.trade_date < b.known_to)
       AND b.fiscal_date >= lp.trade_date - 730
     ORDER BY b.fiscal_date DESC, b.known_from DESC
     LIMIT 1
) mc ON true
LEFT JOIN (
    -- A slope too steep for the screener's numeric(12,6) is a degenerate series,
    -- not a beta.
    SELECT f.security_id, f.beta, f.returns, f.as_of_date AS as_of
      FROM mart.fn_security_betas(current_date) f
     WHERE abs(f.beta) < 1000000
) bt ON bt.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_attributes IS
    'Per security, the screener''s attributes by precedence (ADR 0015 §4, #109), with '
    'their sources and dates. Classification from core.v_security_classification. '
    'market_cap_usd derived (ADR 0019 §5: the last raw close x SF1 shares in force '
    'that day, in its terms; USD-traded) where SF1 holds the shares, else '
    'core.security''s vendor snapshot. beta derived (365 days of daily adjusted '
    'returns on SPY''s, at least 120) else core.security''s. Computes the whole '
    'universe''s betas when read: read mart.security_latest, refreshed nightly.';

GRANT SELECT ON mart.v_security_attributes TO fafnir_read, fafnir_app, fafnir_ops;

DROP FUNCTION IF EXISTS mart.fn_daily_market_cap(bigint[], date, date);
DROP VIEW IF EXISTS mart.v_share_factor_versions;

COMMIT;
