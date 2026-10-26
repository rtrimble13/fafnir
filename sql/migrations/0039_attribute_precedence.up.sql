-- 0039_attribute_precedence.up.sql
-- Security attributes after the cutover (#109; ADR 0015 §4 and its addendum, ADR 0019
-- §5).
--
-- Since 2026-10-22 FMP's screener runs only in FMP's night, so nothing refreshed
-- core.security's sector_id, industry_id, market_cap_usd or beta for the securities
-- Sharadar feeds. ADR 0015 §4 keeps Sharadar from overwriting them: which vendor's
-- attributes a reader sees is decided here, by precedence, and no column of
-- core.security is written.
--
--   core.v_security_classification (VIEW)
--       Per security, its sector and industry: those of its feeding vendor. A security
--       Sharadar feeds (an open is_feed link) takes Sharadar's TICKERS classification
--       (core.vendor_security, the stocks row, else the funds row) mapped onto
--       fafnir's names as sharadar_map.map_classification maps them: case, commas and
--       spacing ignored, and the industry aliases below. The pair comes from Sharadar
--       when its sector maps, else from core.security (as FMP loaded it, or as the
--       security was minted); attributes_source names which. Plain joins, so a filter
--       on security_id reaches the indexes: the DQ check, mart.v_security_profile and
--       mart.v_security_attributes read it.
--
--   mart.v_security_attributes (VIEW)
--       What the screener shows, with where each value came from and as of when:
--       - the classification above;
--       - the last raw bar;
--       - market_cap_usd derived (ADR 0019 §5): the last raw close x shares_outstanding
--         of the latest balance sheet in force on that day, ended within 730 days, put
--         in that day's share terms with the warehouse's splits: the panel's
--         expression. USD-traded securities only. Where it cannot be derived (no SF1
--         shares: funds, ETFs, secondary share classes, unlinked names), the value
--         core.security holds, the vendor's last snapshot;
--       - beta derived: the slope of the daily adjusted returns of the last 365 days on
--         SPY's over the same intervals, from at least 120 of them. Otherwise
--         core.security's.
--       It computes beta for the whole universe whenever it is read: read
--       mart.security_latest, which holds it as of the last refresh.
--
--   mart.fn_security_betas(as_of date) (FUNCTION)
--       The betas as of a date, from mart relations only, so a mart reader may call
--       it. A function rather than CTEs in the view: the view then depends on the
--       function and not on mart.v_daily_price_adjusted, which 0013 drops and
--       re-creates and which nothing else may depend on (0013, 0020), as the panel's
--       function reads it too. EXECUTE for the read roles only, as 0036.
--
--   mart.security_latest (MATERIALIZED, re-defined)
--       The same seventeen columns, names, types and order (duk's screener contract),
--       now selected from mart.v_security_attributes. Created WITH DATA, as 0010 did,
--       so screens keep answering across the deploy.
--
--   mart.v_security_profile (VIEW, re-defined)
--       The same columns; sector_name and industry_name now from
--       core.v_security_classification, so duk's company summary, the factor panel and
--       the industry aggregates see the precedence classification too. Its
--       market_cap_usd and beta stay core.security's, the vendor snapshot, as its
--       comment says.
--
-- Privileges: SELECT on the new views comes from the default privileges (0001, 0021);
-- the grants below repeat them. mart.security_latest is re-created, so its grants are
-- repeated too.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.v_security_classification
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW core.v_security_classification AS
SELECT
    s.security_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN rs.sector_id ELSE s.sector_id END
        AS sector_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN ri.industry_id ELSE s.industry_id END
        AS industry_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN 'sharadar' ELSE s.source END
        AS attributes_source
FROM core.security s
LEFT JOIN core.security_vendor_xref fx
       ON fx.security_id = s.security_id AND fx.vendor = 'sharadar'
      AND fx.is_feed AND fx.valid_to IS NULL
LEFT JOIN core.vendor_security vst
       ON vst.vendor = 'sharadar' AND vst.vendor_table = 'stocks'
      AND vst.vendor_key = fx.vendor_key
LEFT JOIN core.vendor_security vfu
       ON vfu.vendor = 'sharadar' AND vfu.vendor_table = 'funds'
      AND vfu.vendor_key = fx.vendor_key
-- sharadar_map._norm: case, commas and spacing do not distinguish two names.
LEFT JOIN LATERAL (
    SELECT lower(btrim(regexp_replace(replace(
               CASE WHEN vst.vendor_key IS NOT NULL THEN vst.sector ELSE vfu.sector END,
               ',', ' '), '\s+', ' ', 'g'))) AS sector_norm,
           lower(btrim(regexp_replace(replace(
               CASE WHEN vst.vendor_key IS NOT NULL THEN vst.industry ELSE vfu.industry END,
               ',', ' '), '\s+', ' ', 'g'))) AS industry_norm
) n ON true
-- sharadar_map.INDUSTRY_ALIASES: Sharadar's name, normalized, and fafnir's.
LEFT JOIN (VALUES
    ('drug manufacturers - major', 'drug manufacturers - general'),
    ('furnishings', 'furnishings fixtures & appliances')
) al (alias_norm, target_norm) ON al.alias_norm = n.industry_norm
-- One id per normalized name; two names alike once normalized resolve to the later
-- one, as map_classification's dictionary does.
LEFT JOIN (
    SELECT DISTINCT ON (k) sector_id, k
      FROM (SELECT sector_id,
                   lower(btrim(regexp_replace(replace(sector_name, ',', ' '),
                                              '\s+', ' ', 'g'))) AS k
              FROM ref.sector) t
     ORDER BY k, sector_id DESC
) rs ON rs.k = n.sector_norm
LEFT JOIN (
    SELECT DISTINCT ON (k) industry_id, k
      FROM (SELECT industry_id,
                   lower(btrim(regexp_replace(replace(industry_name, ',', ' '),
                                              '\s+', ' ', 'g'))) AS k
              FROM ref.industry) t
     ORDER BY k, industry_id DESC
) ri ON ri.k = coalesce(al.target_norm, n.industry_norm);

COMMENT ON VIEW core.v_security_classification IS
    'Per security, the sector and industry of its feeding vendor (ADR 0015 §4, #109): '
    'Sharadar''s TICKERS classification mapped onto fafnir''s names for a security '
    'Sharadar feeds, when its sector maps; else core.security''s. attributes_source '
    'names which: ''sharadar'', or core.security.source.';

-- ---------------------------------------------------------------------------
-- mart.v_security_attributes
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.fn_security_betas(as_of date)
RETURNS TABLE (
    security_id bigint,
    beta double precision,
    returns bigint,
    as_of_date date
)
LANGUAGE sql STABLE
AS $fn$
WITH benchmark AS (
    -- SPY, by its current ticker: the market the betas are measured against.
    SELECT x.security_id
      FROM mart.v_symbol_lookup x
     WHERE x.symbol = 'SPY' AND x.valid_to IS NULL
     ORDER BY x.is_primary DESC, x.security_id
     LIMIT 1
),
bars AS MATERIALIZED (
    -- Every adjusted close of the 400 days to as_of, with the bar before it: the
    -- extra weeks give the first return of the year its starting close.
    SELECT a.security_id AS sid, a.trade_date AS d, a.close::float8 AS c,
           lag(a.trade_date) OVER w AS prev_d,
           lag(a.close::float8) OVER w AS prev_c
      FROM mart.v_daily_price_adjusted a
     WHERE a.trade_date > as_of - 400 AND a.trade_date <= as_of AND a.close > 0
    WINDOW w AS (PARTITION BY a.security_id ORDER BY a.trade_date)
),
market AS MATERIALIZED (
    SELECT b.d, b.c FROM bars b JOIN benchmark m ON m.security_id = b.sid
)
-- Each return on SPY's over the same interval, so a security that skipped days is
-- not matched against a one-day market move.
SELECT b.sid,
       regr_slope(b.c / b.prev_c - 1, m1.c / m0.c - 1),
       count(*),
       max(b.d)
  FROM bars b
  JOIN market m1 ON m1.d = b.d
  JOIN market m0 ON m0.d = b.prev_d
 WHERE b.d > as_of - 365 AND b.prev_c > 0
 GROUP BY b.sid
HAVING count(*) >= 120
$fn$;

COMMENT ON FUNCTION mart.fn_security_betas(date) IS
    'Per security, the slope of its daily adjusted returns over the 365 days to '
    'as_of on SPY''s over the same intervals, from at least 120 of them (#109): '
    'beta, how many returns, the last one''s date.';

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

-- ---------------------------------------------------------------------------
-- mart.security_latest: the same columns, from the precedence view
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mart.security_latest;

CREATE MATERIALIZED VIEW mart.security_latest AS
SELECT
    a.security_id,
    a.symbol,
    a.company_name,
    a.asset_type,
    a.exchange_code,
    a.sector_name,
    a.industry_name,
    a.currency,
    a.country,
    a.is_actively_trading,
    a.is_etf,
    a.is_fund,
    a.market_cap_usd::numeric(24, 2) AS market_cap_usd,
    a.beta::numeric(12, 6) AS beta,
    a.last_trade_date,
    a.last_close,
    a.last_volume
FROM mart.v_security_attributes a;

COMMENT ON MATERIALIZED VIEW mart.security_latest IS
    'Latest screening attributes + last price per security, from '
    'mart.v_security_attributes (#109): the feeding vendor''s classification, market '
    'cap and beta derived where they can be. Derived snapshot; refresh on schedule.';

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ux_security_latest_id
    ON mart.security_latest (security_id);
CREATE INDEX IF NOT EXISTS ix_security_latest_symbol
    ON mart.security_latest (symbol);

-- ---------------------------------------------------------------------------
-- mart.v_security_profile: the same columns, the precedence classification
-- ---------------------------------------------------------------------------
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
LEFT JOIN core.v_security_classification cl ON cl.security_id = s.security_id
LEFT JOIN ref.sector        sec ON sec.sector_id   = cl.sector_id
LEFT JOIN ref.industry      ind ON ind.industry_id = cl.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_profile IS
    'Descriptive profile per security, read live. sector_name and industry_name are '
    'the feeding vendor''s (core.v_security_classification, #109). market_cap_usd and '
    'beta are a company-screener snapshot refreshed with the security master -- not '
    'history; mart.security_latest derives its own.';

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
GRANT SELECT ON core.v_security_classification
    TO fafnir_read, fafnir_ingest, fafnir_ops;
GRANT SELECT ON mart.v_security_attributes, mart.security_latest
    TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_security_betas(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_security_betas(date)
    TO fafnir_read, fafnir_app, fafnir_ops;

COMMIT;
