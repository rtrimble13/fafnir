-- attributes_report.sql -- the screener's attributes, frozen FMP values against the
-- precedence view's (#109), in aggregates.
--
-- Since the cutover (2026-10-22) FMP's screener no longer runs, so core.security holds
-- FMP's last sector, industry, market cap and beta for every security it minted.
-- mart.v_security_attributes shows the feeding vendor's classification and a derived
-- market cap and beta. This compares the two for the FMP-minted securities, by
-- column:
--
--   1. who supplies each attribute now, by feed;
--   2. the classification: same, a new sector, a new industry only, still FMP's
--      (Sharadar's did not map), or now missing;
--   3. the sector moves, as counts of (FMP sector -> shown sector);
--   4. market cap: how many are derived, and how far they sit from FMP's last value;
--   5. beta: the same.
--
-- Every figure is a count, a share or a quantile of a ratio: no vendor value, so the
-- output can be posted on an issue as it is.
--
-- Usage:
--   psql "${FAFNIR_DSN}" -X -f scripts/attributes_report.sql
-- Read-only; any role that reads core and mart (fafnir_ops, fafnir_read). It reads
-- mart.v_security_attributes, which computes every beta: allow a minute.

\echo '-- 0. the attributes, once'
CREATE TEMP TABLE attr AS
SELECT a.*, s.source AS minted_by,
       s.sector_id AS fmp_sector_id, s.industry_id AS fmp_industry_id,
       s.market_cap_usd AS fmp_market_cap, s.beta AS fmp_beta,
       c.sector_id, c.industry_id,
       CASE WHEN EXISTS (SELECT 1 FROM core.security_vendor_xref x
                          WHERE x.security_id = s.security_id AND x.vendor = 'sharadar'
                            AND x.is_feed AND x.valid_to IS NULL)
            THEN 'sharadar' ELSE 'fmp or other' END AS fed_by
  FROM mart.v_security_attributes a
  JOIN core.security s ON s.security_id = a.security_id
  JOIN core.v_security_classification c ON c.security_id = a.security_id;
SELECT count(*) AS securities FROM attr;

\echo '-- 1. who supplies each attribute, by feed (listed securities)'
SELECT fed_by, minted_by,
       count(*)                                                   AS securities,
       count(*) FILTER (WHERE attributes_source = 'sharadar')     AS class_sharadar,
       count(*) FILTER (WHERE market_cap_source = 'derived')      AS mcap_derived,
       count(*) FILTER (WHERE market_cap_source IS NULL)          AS mcap_none,
       count(*) FILTER (WHERE beta_source = 'derived')            AS beta_derived,
       count(*) FILTER (WHERE beta_source IS NULL)                AS beta_none
  FROM attr
 WHERE is_actively_trading
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo '-- 2. classification, FMP-minted listed securities'
SELECT fed_by,
       count(*) FILTER (WHERE sector_id IS NOT DISTINCT FROM fmp_sector_id
                          AND industry_id IS NOT DISTINCT FROM fmp_industry_id) AS same,
       count(*) FILTER (WHERE sector_id IS DISTINCT FROM fmp_sector_id
                          AND sector_id IS NOT NULL)                           AS new_sector,
       count(*) FILTER (WHERE sector_id IS NOT DISTINCT FROM fmp_sector_id
                          AND industry_id IS DISTINCT FROM fmp_industry_id)     AS new_industry_only,
       count(*) FILTER (WHERE fed_by = 'sharadar' AND attributes_source <> 'sharadar') AS still_fmp,
       count(*) FILTER (WHERE (sector_id IS NULL OR industry_id IS NULL)
                          AND fmp_sector_id IS NOT NULL
                          AND fmp_industry_id IS NOT NULL)                     AS now_missing
  FROM attr
 WHERE minted_by = 'fmp' AND is_actively_trading
 GROUP BY 1
 ORDER BY 1;

\echo '-- 3. sector moves, FMP-minted listed securities (counts of FMP -> shown)'
SELECT coalesce(o.sector_name, '(none)') AS fmp_sector,
       coalesce(n.sector_name, '(none)') AS shown_sector,
       count(*)                          AS securities
  FROM attr a
  LEFT JOIN ref.sector o ON o.sector_id = a.fmp_sector_id
  LEFT JOIN ref.sector n ON n.sector_id = a.sector_id
 WHERE a.minted_by = 'fmp' AND a.is_actively_trading
   AND a.sector_id IS DISTINCT FROM a.fmp_sector_id
 GROUP BY 1, 2
 ORDER BY 3 DESC, 1, 2;

\echo '-- 4. market cap: derived against FMP''s last value'
SELECT count(*) FILTER (WHERE market_cap_source = 'derived')               AS derived,
       count(*) FILTER (WHERE market_cap_source = 'derived'
                          AND fmp_market_cap > 0)                           AS compared,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(ln(market_cap_usd / fmp_market_cap)))
             FILTER (WHERE market_cap_source = 'derived' AND fmp_market_cap > 0
                       AND market_cap_usd > 0)::numeric, 4)                AS median_abs_log_ratio,
       round(avg((abs(market_cap_usd / fmp_market_cap - 1) <= 0.10)::int)
             FILTER (WHERE market_cap_source = 'derived' AND fmp_market_cap > 0)::numeric, 3)
                                                                            AS share_within_10pct,
       round(avg((market_cap_usd / fmp_market_cap NOT BETWEEN 0.5 AND 2)::int)
             FILTER (WHERE market_cap_source = 'derived' AND fmp_market_cap > 0)::numeric, 3)
                                                                            AS share_off_by_2x
  FROM attr
 WHERE minted_by = 'fmp' AND is_actively_trading;

\echo '-- 5. beta: derived against FMP''s last value'
SELECT count(*) FILTER (WHERE beta_source = 'derived')                        AS derived,
       count(*) FILTER (WHERE beta_source = 'derived' AND fmp_beta IS NOT NULL) AS compared,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(beta - fmp_beta))
             FILTER (WHERE beta_source = 'derived' AND fmp_beta IS NOT NULL)::numeric, 3)
                                                                                AS median_abs_diff,
       round(corr(beta::float8, fmp_beta::float8)
             FILTER (WHERE beta_source = 'derived')::numeric, 3)                AS correlation
  FROM attr
 WHERE minted_by = 'fmp' AND is_actively_trading;

DROP TABLE attr;
