-- 0038_fundamentals_aggregates.up.sql
-- The panel's industry aggregates and the latest snapshot (#101; build prompt §5.4-5.5,
-- plan doc/plans/fundamentals-sf1.md §5.4-5.6).
--
-- GENERATED, like 0035-0037. Tests hold the metric list and the columns.
--
-- mart.v_industry_fundamentals (VIEW)
--     Over mart.fundamental_panel, per month-end: by sector, and by sector and
--     industry. n_securities, and per metric its count of non-NULL values (_n) and
--     the 25th, 50th and 75th percentiles (percentile_cont over the non-NULLs). A
--     group of fewer than 3 securities has no row; a metric with fewer than 3
--     values has NULL percentiles, its _n still shown. A security without a sector
--     (or industry) is in no group at that level. The groups are the security
--     master's current classification, as the panel's are. A filter on as_of_date,
--     sector_name or group_level reaches the panel's indexes.
--
-- mart.security_fundamentals_latest (MATERIALIZED)
--     The panel function at current_date, with the security's symbol and name: one
--     row per security that traded in the last week. For screens; mart.security_latest
--     is untouched (duk's screener reads it). Created WITH NO DATA: `fafnir db
--     refresh-marts` fills it, after the panel (maintenance.MATVIEWS).
--
-- Privileges: SELECT for the read roles, as 0036 and 0037.

BEGIN;

-- ---------------------------------------------------------------------------
-- mart.v_industry_fundamentals
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_industry_fundamentals AS
WITH g AS (
    SELECT 'sector'::text AS group_level,
           p.as_of_date,
           p.sector_name,
           NULL::text AS industry_name,
           count(*) AS n_securities,
           count(p.market_cap) AS market_cap_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.market_cap::double precision) AS market_cap_q,
           count(p.earnings_yield) AS earnings_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.earnings_yield::double precision) AS earnings_yield_q,
           count(p.book_to_price) AS book_to_price_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.book_to_price::double precision) AS book_to_price_q,
           count(p.sales_to_price) AS sales_to_price_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.sales_to_price::double precision) AS sales_to_price_q,
           count(p.fcf_yield) AS fcf_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.fcf_yield::double precision) AS fcf_yield_q,
           count(p.ev_to_ebitda) AS ev_to_ebitda_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.ev_to_ebitda::double precision) AS ev_to_ebitda_q,
           count(p.roe) AS roe_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.roe::double precision) AS roe_q,
           count(p.gross_profitability) AS gross_profitability_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.gross_profitability::double precision) AS gross_profitability_q,
           count(p.operating_margin) AS operating_margin_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.operating_margin::double precision) AS operating_margin_q,
           count(p.accruals) AS accruals_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.accruals::double precision) AS accruals_q,
           count(p.debt_to_equity) AS debt_to_equity_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.debt_to_equity::double precision) AS debt_to_equity_q,
           count(p.interest_coverage) AS interest_coverage_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.interest_coverage::double precision) AS interest_coverage_q,
           count(p.revenue_growth_yoy) AS revenue_growth_yoy_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.revenue_growth_yoy::double precision) AS revenue_growth_yoy_q,
           count(p.asset_growth_1y) AS asset_growth_1y_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.asset_growth_1y::double precision) AS asset_growth_1y_q,
           count(p.dividend_yield_ttm) AS dividend_yield_ttm_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.dividend_yield_ttm::double precision) AS dividend_yield_ttm_q,
           count(p.shareholder_yield) AS shareholder_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.shareholder_yield::double precision) AS shareholder_yield_q
      FROM mart.fundamental_panel p
     WHERE p.sector_name IS NOT NULL
     GROUP BY p.as_of_date, p.sector_name
    HAVING count(*) >= 3
    UNION ALL
    SELECT 'industry'::text AS group_level,
           p.as_of_date,
           p.sector_name,
           p.industry_name,
           count(*) AS n_securities,
           count(p.market_cap) AS market_cap_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.market_cap::double precision) AS market_cap_q,
           count(p.earnings_yield) AS earnings_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.earnings_yield::double precision) AS earnings_yield_q,
           count(p.book_to_price) AS book_to_price_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.book_to_price::double precision) AS book_to_price_q,
           count(p.sales_to_price) AS sales_to_price_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.sales_to_price::double precision) AS sales_to_price_q,
           count(p.fcf_yield) AS fcf_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.fcf_yield::double precision) AS fcf_yield_q,
           count(p.ev_to_ebitda) AS ev_to_ebitda_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.ev_to_ebitda::double precision) AS ev_to_ebitda_q,
           count(p.roe) AS roe_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.roe::double precision) AS roe_q,
           count(p.gross_profitability) AS gross_profitability_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.gross_profitability::double precision) AS gross_profitability_q,
           count(p.operating_margin) AS operating_margin_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.operating_margin::double precision) AS operating_margin_q,
           count(p.accruals) AS accruals_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.accruals::double precision) AS accruals_q,
           count(p.debt_to_equity) AS debt_to_equity_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.debt_to_equity::double precision) AS debt_to_equity_q,
           count(p.interest_coverage) AS interest_coverage_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.interest_coverage::double precision) AS interest_coverage_q,
           count(p.revenue_growth_yoy) AS revenue_growth_yoy_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.revenue_growth_yoy::double precision) AS revenue_growth_yoy_q,
           count(p.asset_growth_1y) AS asset_growth_1y_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.asset_growth_1y::double precision) AS asset_growth_1y_q,
           count(p.dividend_yield_ttm) AS dividend_yield_ttm_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.dividend_yield_ttm::double precision) AS dividend_yield_ttm_q,
           count(p.shareholder_yield) AS shareholder_yield_n,
           percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY p.shareholder_yield::double precision) AS shareholder_yield_q
      FROM mart.fundamental_panel p
     WHERE p.industry_name IS NOT NULL
     GROUP BY p.as_of_date, p.sector_name, p.industry_name
    HAVING count(*) >= 3
)
SELECT g.group_level,
       g.as_of_date,
       g.sector_name,
       g.industry_name,
       g.n_securities,
       g.market_cap_n,
       CASE WHEN g.market_cap_n >= 3 THEN g.market_cap_q[1] END AS market_cap_p25,
       CASE WHEN g.market_cap_n >= 3 THEN g.market_cap_q[2] END AS market_cap_median,
       CASE WHEN g.market_cap_n >= 3 THEN g.market_cap_q[3] END AS market_cap_p75,
       g.earnings_yield_n,
       CASE WHEN g.earnings_yield_n >= 3 THEN g.earnings_yield_q[1] END AS earnings_yield_p25,
       CASE WHEN g.earnings_yield_n >= 3 THEN g.earnings_yield_q[2] END AS earnings_yield_median,
       CASE WHEN g.earnings_yield_n >= 3 THEN g.earnings_yield_q[3] END AS earnings_yield_p75,
       g.book_to_price_n,
       CASE WHEN g.book_to_price_n >= 3 THEN g.book_to_price_q[1] END AS book_to_price_p25,
       CASE WHEN g.book_to_price_n >= 3 THEN g.book_to_price_q[2] END AS book_to_price_median,
       CASE WHEN g.book_to_price_n >= 3 THEN g.book_to_price_q[3] END AS book_to_price_p75,
       g.sales_to_price_n,
       CASE WHEN g.sales_to_price_n >= 3 THEN g.sales_to_price_q[1] END AS sales_to_price_p25,
       CASE WHEN g.sales_to_price_n >= 3 THEN g.sales_to_price_q[2] END AS sales_to_price_median,
       CASE WHEN g.sales_to_price_n >= 3 THEN g.sales_to_price_q[3] END AS sales_to_price_p75,
       g.fcf_yield_n,
       CASE WHEN g.fcf_yield_n >= 3 THEN g.fcf_yield_q[1] END AS fcf_yield_p25,
       CASE WHEN g.fcf_yield_n >= 3 THEN g.fcf_yield_q[2] END AS fcf_yield_median,
       CASE WHEN g.fcf_yield_n >= 3 THEN g.fcf_yield_q[3] END AS fcf_yield_p75,
       g.ev_to_ebitda_n,
       CASE WHEN g.ev_to_ebitda_n >= 3 THEN g.ev_to_ebitda_q[1] END AS ev_to_ebitda_p25,
       CASE WHEN g.ev_to_ebitda_n >= 3 THEN g.ev_to_ebitda_q[2] END AS ev_to_ebitda_median,
       CASE WHEN g.ev_to_ebitda_n >= 3 THEN g.ev_to_ebitda_q[3] END AS ev_to_ebitda_p75,
       g.roe_n,
       CASE WHEN g.roe_n >= 3 THEN g.roe_q[1] END AS roe_p25,
       CASE WHEN g.roe_n >= 3 THEN g.roe_q[2] END AS roe_median,
       CASE WHEN g.roe_n >= 3 THEN g.roe_q[3] END AS roe_p75,
       g.gross_profitability_n,
       CASE WHEN g.gross_profitability_n >= 3 THEN g.gross_profitability_q[1] END AS gross_profitability_p25,
       CASE WHEN g.gross_profitability_n >= 3 THEN g.gross_profitability_q[2] END AS gross_profitability_median,
       CASE WHEN g.gross_profitability_n >= 3 THEN g.gross_profitability_q[3] END AS gross_profitability_p75,
       g.operating_margin_n,
       CASE WHEN g.operating_margin_n >= 3 THEN g.operating_margin_q[1] END AS operating_margin_p25,
       CASE WHEN g.operating_margin_n >= 3 THEN g.operating_margin_q[2] END AS operating_margin_median,
       CASE WHEN g.operating_margin_n >= 3 THEN g.operating_margin_q[3] END AS operating_margin_p75,
       g.accruals_n,
       CASE WHEN g.accruals_n >= 3 THEN g.accruals_q[1] END AS accruals_p25,
       CASE WHEN g.accruals_n >= 3 THEN g.accruals_q[2] END AS accruals_median,
       CASE WHEN g.accruals_n >= 3 THEN g.accruals_q[3] END AS accruals_p75,
       g.debt_to_equity_n,
       CASE WHEN g.debt_to_equity_n >= 3 THEN g.debt_to_equity_q[1] END AS debt_to_equity_p25,
       CASE WHEN g.debt_to_equity_n >= 3 THEN g.debt_to_equity_q[2] END AS debt_to_equity_median,
       CASE WHEN g.debt_to_equity_n >= 3 THEN g.debt_to_equity_q[3] END AS debt_to_equity_p75,
       g.interest_coverage_n,
       CASE WHEN g.interest_coverage_n >= 3 THEN g.interest_coverage_q[1] END AS interest_coverage_p25,
       CASE WHEN g.interest_coverage_n >= 3 THEN g.interest_coverage_q[2] END AS interest_coverage_median,
       CASE WHEN g.interest_coverage_n >= 3 THEN g.interest_coverage_q[3] END AS interest_coverage_p75,
       g.revenue_growth_yoy_n,
       CASE WHEN g.revenue_growth_yoy_n >= 3 THEN g.revenue_growth_yoy_q[1] END AS revenue_growth_yoy_p25,
       CASE WHEN g.revenue_growth_yoy_n >= 3 THEN g.revenue_growth_yoy_q[2] END AS revenue_growth_yoy_median,
       CASE WHEN g.revenue_growth_yoy_n >= 3 THEN g.revenue_growth_yoy_q[3] END AS revenue_growth_yoy_p75,
       g.asset_growth_1y_n,
       CASE WHEN g.asset_growth_1y_n >= 3 THEN g.asset_growth_1y_q[1] END AS asset_growth_1y_p25,
       CASE WHEN g.asset_growth_1y_n >= 3 THEN g.asset_growth_1y_q[2] END AS asset_growth_1y_median,
       CASE WHEN g.asset_growth_1y_n >= 3 THEN g.asset_growth_1y_q[3] END AS asset_growth_1y_p75,
       g.dividend_yield_ttm_n,
       CASE WHEN g.dividend_yield_ttm_n >= 3 THEN g.dividend_yield_ttm_q[1] END AS dividend_yield_ttm_p25,
       CASE WHEN g.dividend_yield_ttm_n >= 3 THEN g.dividend_yield_ttm_q[2] END AS dividend_yield_ttm_median,
       CASE WHEN g.dividend_yield_ttm_n >= 3 THEN g.dividend_yield_ttm_q[3] END AS dividend_yield_ttm_p75,
       g.shareholder_yield_n,
       CASE WHEN g.shareholder_yield_n >= 3 THEN g.shareholder_yield_q[1] END AS shareholder_yield_p25,
       CASE WHEN g.shareholder_yield_n >= 3 THEN g.shareholder_yield_q[2] END AS shareholder_yield_median,
       CASE WHEN g.shareholder_yield_n >= 3 THEN g.shareholder_yield_q[3] END AS shareholder_yield_p75
  FROM g;

COMMENT ON VIEW mart.v_industry_fundamentals IS
    'The factor panel summarized per month-end, by sector and by sector and '
    'industry (#101): n_securities, and per metric its non-NULL count and '
    'quartiles. A group of fewer than three securities has no row; a metric '
    'with fewer than three values has NULL quartiles. Classification is '
    'today''s, as in the panel.';

-- ---------------------------------------------------------------------------
-- mart.security_fundamentals_latest
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.security_fundamentals_latest AS
SELECT f.security_id,
       s.primary_symbol AS symbol,
       s.company_name,
       f.as_of_date,
       f.exchange_code,
       f.sector_name,
       f.industry_name,
       f.trading_currency,
       f.reported_currency,
       f.currency_mismatch,
       f.basis,
       f.fiscal_date,
       f.fiscal_year,
       f.fiscal_period,
       f.known_from,
       f.knowledge_source,
       f.days_since_filing,
       f.ttm_quarters_used,
       f.price_date,
       f.close_raw,
       f.close_adjusted,
       f.shares_outstanding_as_of,
       f.market_cap,
       f.enterprise_value,
       f.earnings_yield,
       f.book_to_price,
       f.sales_to_price,
       f.cash_flow_to_price,
       f.fcf_yield,
       f.ev_to_ebitda,
       f.ev_to_sales,
       f.ev_to_ebit,
       f.ev_to_fcf,
       f.roe,
       f.roa,
       f.roic,
       f.gross_profitability,
       f.gross_margin,
       f.operating_margin,
       f.net_margin,
       f.asset_turnover,
       f.accruals,
       f.net_operating_assets,
       f.debt_to_equity,
       f.net_debt_to_ebitda,
       f.interest_coverage,
       f.current_ratio,
       f.quick_ratio,
       f.altman_z,
       f.piotroski_f,
       f.revenue_growth_yoy,
       f.eps_growth_yoy,
       f.revenue_cagr_3y,
       f.net_income_growth_yoy,
       f.asset_growth_1y,
       f.capex_to_sales,
       f.net_share_issuance_1y,
       f.dividend_yield_ttm,
       f.buyback_yield_ttm,
       f.shareholder_yield
  FROM mart.fn_fundamental_snapshot(current_date) f
  JOIN core.security s ON s.security_id = f.security_id
WITH NO DATA;

COMMENT ON MATERIALIZED VIEW mart.security_fundamentals_latest IS
    'Each security''s factors as of the last refresh: mart.fn_fundamental_snapshot '
    'at current_date, with symbol and name (#101). For screens, not backtests. '
    'Refreshed by `fafnir db refresh-marts`, after the panel.';

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_fundamentals_latest
    ON mart.security_fundamentals_latest (security_id);

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
GRANT SELECT ON mart.v_industry_fundamentals TO fafnir_read, fafnir_app, fafnir_ops;
GRANT SELECT ON mart.security_fundamentals_latest TO fafnir_read, fafnir_app, fafnir_ops;

COMMIT;
