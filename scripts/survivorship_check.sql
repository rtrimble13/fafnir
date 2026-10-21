-- survivorship_check.sql -- does fafnir hold every issuer that traded, alive or dead
-- (#79, ADR 0016)?
--
-- For each year from 1998 (ADR 0016's floor) to this one, the Sharadar securities
-- that traded in it, from TICKERS' first..last price dates, against the fafnir
-- securities holding them: an open link to the vendor key, with bars spanning the
-- year. Sharadar's side applies the inclusion rules #71 mints by
-- (fafnir.ingest.vendor_overlap.mintable, less "delisted"): a priced table, a stock
-- or exchange-traded category (mutual funds are fafnir's declared funds, not in
-- scope), a venue of NYSE, NASDAQ, AMEX (NYSEMKT, NYSEARCA) or BATS, and a last
-- price on or after 1998-01-01. Keep the rules in step with sharadar_map.
--
-- Each security missing from a year has one reason:
--   no_link       no fafnir security is linked to the vendor key (not minted: held
--                 back by a vendor_history_overlap flag, or a live name the
--                 linking engine left unlinked, #64)
--   no_bars       linked, and the security holds no bars
--   outside_span  linked, and its bars do not reach that year (a history that
--                 starts later or ends earlier than the vendor's)
--
-- Counts only, safe to post on #79. It writes nothing but a temporary view, gone
-- when the session ends. Reads core; run it as the owner or fafnir_ops.
--
-- Usage:
--   psql "${FAFNIR_DSN}" -X -f scripts/survivorship_check.sql

CREATE OR REPLACE TEMP VIEW survivorship_scope AS
WITH sharadar AS (
    SELECT v.vendor_key, v.is_delisted, v.first_price_date, v.last_price_date,
           CASE upper(btrim(v.exchange))
                WHEN 'NYSEMKT' THEN 'AMEX'
                WHEN 'NYSEARCA' THEN 'AMEX'
                ELSE upper(btrim(v.exchange))
           END AS venue,
           CASE WHEN regexp_replace(upper(btrim(v.category)), '\s+', ' ', 'g')
                     IN ('ETF', 'ETN', 'ETD', 'ETMF') THEN 'etf'
                WHEN regexp_replace(upper(btrim(v.category)), '\s+', ' ', 'g')
                     IN ('CEF', 'CEF PREFERRED') THEN 'cef'
                ELSE 'stock'
           END AS kind
      FROM core.vendor_security v
     WHERE v.vendor = 'sharadar'
       AND v.vendor_table IN ('stocks', 'funds')
       AND upper(btrim(v.exchange)) IN ('NYSE', 'NASDAQ', 'NYSEMKT', 'NYSEARCA', 'BATS')
       AND (regexp_replace(upper(btrim(v.category)), '\s+', ' ', 'g')
              ~ '^(DOMESTIC|ADR|CANADIAN) (COMMON|PREFERRED) STOCK( (PRIMARY|SECONDARY) CLASS)?$'
            OR regexp_replace(upper(btrim(v.category)), '\s+', ' ', 'g')
              IN ('ETF', 'ETN', 'ETD', 'ETMF', 'CEF', 'CEF PREFERRED'))
       AND v.first_price_date IS NOT NULL
       AND v.last_price_date >= DATE '1998-01-01'
),
held AS MATERIALIZED (
    SELECT x.vendor_key,
           (SELECT min(p.trade_date) FROM core.daily_price p
             WHERE p.security_id = x.security_id) AS first_d,
           (SELECT max(p.trade_date) FROM core.daily_price p
             WHERE p.security_id = x.security_id) AS last_d
      FROM core.security_vendor_xref x
     WHERE x.vendor = 'sharadar' AND x.valid_to IS NULL
)
SELECT y.year, s.vendor_key, s.venue, s.kind,
       coalesce(s.is_delisted, false)
           AND extract(YEAR FROM s.last_price_date) = y.year AS died_this_year,
       CASE WHEN h.vendor_key IS NULL THEN 'no_link'
            WHEN h.first_d IS NULL THEN 'no_bars'
            WHEN h.first_d > make_date(y.year, 12, 31)
              OR h.last_d < make_date(y.year, 1, 1) THEN 'outside_span'
       END AS missing
  FROM generate_series(1998, extract(YEAR FROM current_date)::int) AS y(year)
  JOIN sharadar s
    ON s.first_price_date <= make_date(y.year, 12, 31)
   AND s.last_price_date >= make_date(y.year, 1, 1)
  LEFT JOIN held h ON h.vendor_key = s.vendor_key;

-- 1. Per year: what traded, what fafnir holds, why the rest is missing, and the
--    deaths (Sharadar's delisted in that year, and fafnir's delisted_date).
SELECT year,
       count(*)                                            AS sharadar_alive,
       count(*) FILTER (WHERE missing IS NULL)             AS fafnir_holds,
       round(100.0 * count(*) FILTER (WHERE missing IS NULL) / count(*), 2)
                                                           AS pct_held,
       count(*) FILTER (WHERE missing = 'no_link')         AS no_link,
       count(*) FILTER (WHERE missing = 'no_bars')         AS no_bars,
       count(*) FILTER (WHERE missing = 'outside_span')    AS outside_span,
       count(*) FILTER (WHERE died_this_year)              AS sharadar_delisted,
       count(*) FILTER (WHERE died_this_year AND missing IS NULL)
                                                           AS delisted_held,
       (SELECT count(*) FROM core.security s
         WHERE extract(YEAR FROM s.delisted_date) = year)  AS fafnir_delisted
  FROM survivorship_scope
 GROUP BY year
 ORDER BY year;

-- 2. The 20 largest gaps, by year, venue, kind and reason.
SELECT year, venue, kind, missing AS reason, count(*) AS securities
  FROM survivorship_scope
 WHERE missing IS NOT NULL
 GROUP BY year, venue, kind, missing
 ORDER BY securities DESC, year, venue, kind, reason
 LIMIT 20;

-- To work one gap, list its securities. Their keys and tickers are vendor data:
-- keep the output on the host.
--   SELECT g.vendor_key, v.ticker, v.name, v.first_price_date, v.last_price_date
--     FROM survivorship_scope g
--     JOIN core.vendor_security v
--       ON v.vendor = 'sharadar' AND v.vendor_key = g.vendor_key
--    WHERE g.year = 2008 AND g.venue = 'NYSE' AND g.missing = 'no_link'
--    ORDER BY v.ticker
