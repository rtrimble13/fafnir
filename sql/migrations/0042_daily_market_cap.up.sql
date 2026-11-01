-- 0042_daily_market_cap.up.sql
-- Daily market cap, derived on read (ADR 0019 §5 and its 2026-11 amendment; ADR 0001).
--
-- Market cap on day t is the raw close on t x the shares outstanding of the latest
-- balance sheet in force on t (ended within 730 days), put in t's share terms with the
-- warehouse's splits, x SF1's share factor for that period. Nothing is stored: like the
-- adjusted prices, the series is a function of core, so a split, a vendor correction
-- or a restatement reaches every reader on the next read.
--
--   mart.v_share_factor_versions (VIEW)
--       SF1's sharefactor for every income-statement version, from that row's extra
--       (vendor_metric.sharefactor), on the version's own knowledge and load
--       timelines. The factor turns SF1's count into the traded line's: an ADR's
--       ratio (TSM 0.2: five ordinary shares to one ADR), or a multi-class issuer's
--       conversion into the primary class (BRK.B about 1.52). It is a share-terms
--       conversion like a split, not a vendor metric: it is not measured on a date or
--       a price, and the vendor does not recompute it from either (the amendment).
--       This is the one key of extra that mart reads. A value that is not a positive
--       number is NULL.
--
--   mart.fn_daily_market_cap(security_ids bigint[], start_date date, end_date date)
--       One row per raw bar of each security in [start_date, end_date] (NULL: open),
--       with the count, the factor, the cap, and the balance sheet the count came
--       from. market_cap is NULL where no count is in force: no SF1 shares (funds,
--       ETFs, a secondary share class, a security Sharadar does not feed), before the
--       first filing, or a latest balance sheet ended more than 730 days before t.
--       - The count steps on its filing's known_from, and between filings moves only
--         with the warehouse's splits, from known_from (exclusive) to t (inclusive):
--         the expression the panel and mart.v_security_attributes use.
--       - The factor is the same period's income version in force on t; 1 where SF1
--         gave none.
--       - In the trading currency (currency), whatever the reporting currency.
--       How: what is in force changes only on a version's known_from or known_to, the
--       day a balance sheet goes stale, or a split's ex-date. Those days cut each
--       security's span into segments; the count, factor and split product are found
--       once per segment, and each bar takes its segment's by a running count, not a
--       lookup per bar. So a cross-section of hundreds of securities over decades
--       costs a sort of their bars.
--       A plain SQL function reading mart views only (ADR 0009's status note),
--       STABLE, never SECURITY DEFINER; EXECUTE for the read roles.
--
--   mart.v_security_attributes (VIEW, re-defined)
--       The same columns. market_cap_usd now applies the share factor too, so an
--       ADR's or a multi-class issuer's derived cap is no longer off by it (TSM's was
--       five times too high). It keeps its own lookup (a plain join per security, so
--       the screener's refresh does not run the function 34,000 times); a test holds
--       it equal to mart.fn_daily_market_cap on the last bar. mart.security_latest
--       reads it and takes the change on its next refresh.
--
-- Not changed here: the factor panel (0037) still omits the share factor. Its
-- market_cap and value yields for a security whose factor is not 1 differ from this
-- function until the panel is regenerated (a follow-up).
--
-- Privileges: SELECT on the view from the default privileges (0001, 0021), repeated
-- below; EXECUTE on the function revoked from PUBLIC and granted to the read roles, as
-- 0036 and 0039.

BEGIN;

-- ---------------------------------------------------------------------------
-- mart.v_share_factor_versions
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_share_factor_versions AS
SELECT
    i.security_id,
    i.period_type,
    i.fiscal_date,
    i.version_no,
    i.known_from,
    i.known_to,
    i.knowledge_source,
    i.loaded_at,
    i.superseded_at,
    -- Cast only what is a number: a vendor's text is never trusted to be one.
    CASE WHEN btrim(i.extra #>> '{vendor_metric,sharefactor}')
              ~ '^[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?$'
         THEN NULLIF(btrim(i.extra #>> '{vendor_metric,sharefactor}')::numeric, 0)
    END AS share_factor
FROM core.income_statement i;

COMMENT ON VIEW mart.v_share_factor_versions IS
    'SF1''s sharefactor per income-statement version (0042; ADR 0019 §5 amendment): '
    'the conversion of SF1''s share count to the traded line''s (an ADR''s ratio, a '
    'multi-class issuer''s primary class). From extra.vendor_metric.sharefactor, the '
    'one key of extra mart reads; NULL unless a positive number. Versions and '
    'timelines as mart.v_income_statement_versions.';

-- ---------------------------------------------------------------------------
-- mart.fn_daily_market_cap
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.fn_daily_market_cap(
    security_ids bigint[], start_date date DEFAULT NULL, end_date date DEFAULT NULL
)
RETURNS TABLE (
    security_id bigint,
    trade_date date,
    currency text,
    close_raw numeric,
    shares_outstanding numeric,
    share_factor numeric,
    market_cap numeric,
    fiscal_date date,
    known_from date
)
LANGUAGE sql STABLE PARALLEL SAFE
AS $fn$
WITH
span AS (
    SELECT coalesce(fn_daily_market_cap.start_date, '-infinity'::date) AS lo,
           coalesce(fn_daily_market_cap.end_date, 'infinity'::date) AS hi
),
bars AS MATERIALIZED (
    SELECT p.security_id AS sid, p.trade_date AS d, p.close
      FROM mart.v_daily_price_raw p
     WHERE p.security_id = ANY (fn_daily_market_cap.security_ids)
       AND p.trade_date >= coalesce(fn_daily_market_cap.start_date, '-infinity'::date)
       AND p.trade_date <= coalesce(fn_daily_market_cap.end_date, 'infinity'::date)
),
bs AS MATERIALIZED (
    -- Every balance sheet with a count that can be in force somewhere in the span.
    SELECT b.security_id AS sid, b.known_from AS kf, b.known_to AS kt,
           b.fiscal_date AS fd
      FROM mart.v_balance_sheet_versions b, span
     WHERE b.security_id = ANY (fn_daily_market_cap.security_ids)
       AND b.superseded_at IS NULL AND b.shares_outstanding > 0
       AND b.known_from <= span.hi
       AND (b.known_to IS NULL OR b.known_to > span.lo)
       AND b.fiscal_date >= span.lo - 730
),
sf AS MATERIALIZED (
    -- The income versions whose factor can apply in the span.
    SELECT f.security_id AS sid, f.known_from AS kf, f.known_to AS kt
      FROM mart.v_share_factor_versions f, span
     WHERE f.security_id = ANY (fn_daily_market_cap.security_ids)
       AND f.superseded_at IS NULL
       AND f.known_from <= span.hi
       AND (f.known_to IS NULL OR f.known_to > span.lo)
       AND f.fiscal_date >= span.lo - 730
),
splits AS MATERIALIZED (
    -- The warehouse's splits, the list the raw prices follow (ADR 0019 §3).
    SELECT a.security_id AS sid,
           array_agg(a.ex_date ORDER BY a.ex_date) AS ex,
           array_agg(ln(a.split_numerator / a.split_denominator) ORDER BY a.ex_date) AS l
      FROM mart.v_corporate_action a
     WHERE a.action_type = 'split' AND a.split_numerator > 0 AND a.split_denominator > 0
       AND a.security_id = ANY (fn_daily_market_cap.security_ids)
     GROUP BY a.security_id
),
cuts AS MATERIALIZED (
    -- The days anything in force can change, clamped to the span.
    SELECT DISTINCT c.sid, greatest(c.d, span.lo) AS d
      FROM (
            SELECT sid, kf AS d FROM bs
            UNION ALL SELECT sid, kt FROM bs WHERE kt IS NOT NULL
            UNION ALL SELECT sid, fd + 731 FROM bs     -- the first day it is stale
            UNION ALL SELECT sid, kf FROM sf
            UNION ALL SELECT sid, kt FROM sf WHERE kt IS NOT NULL
            UNION ALL SELECT s.sid, u.e FROM splits s, unnest(s.ex) AS u(e)
           ) c, span
     WHERE c.d <= span.hi
),
seg AS MATERIALIZED (
    -- Per segment: the count in force on its first day, the same period's factor,
    -- and the splits since the count was known.
    SELECT c.sid, c.d, row_number() OVER (PARTITION BY c.sid ORDER BY c.d) AS n,
           b.fiscal_date, b.known_from,
           round(b.shares_outstanding
                 * (CASE WHEN sp.ex IS NULL THEN 1 ELSE (SELECT exp(coalesce(sum(u.l), 0))
                      FROM unnest(sp.ex, sp.l) AS u(e, l)
                     WHERE u.e > b.known_from AND u.e <= c.d) END)) AS so_as_of,
           coalesce(f.share_factor, 1) AS share_factor
      FROM cuts c
      LEFT JOIN LATERAL (
            SELECT v.period_type, v.fiscal_date, v.known_from, v.shares_outstanding
              FROM mart.v_balance_sheet_versions v
             WHERE v.security_id = c.sid
               AND v.superseded_at IS NULL AND v.shares_outstanding > 0
               AND v.known_from <= c.d AND (v.known_to IS NULL OR c.d < v.known_to)
               AND v.fiscal_date >= c.d - 730
             ORDER BY v.fiscal_date DESC, v.known_from DESC, v.period_type
             LIMIT 1) b ON true
      LEFT JOIN LATERAL (
            SELECT w.share_factor
              FROM mart.v_share_factor_versions w
             WHERE w.security_id = c.sid AND w.period_type = b.period_type
               AND w.fiscal_date = b.fiscal_date
               AND w.superseded_at IS NULL
               AND w.known_from <= c.d AND (w.known_to IS NULL OR c.d < w.known_to)
             ORDER BY w.known_from DESC
             LIMIT 1) f ON true
      LEFT JOIN splits sp ON sp.sid = c.sid
),
placed AS MATERIALIZED (
    -- Each bar's segment: how many segments have begun by its day (a segment
    -- beginning on the bar's own day counts).
    SELECT x.sid, x.d, x.close, x.n
      FROM (
            SELECT u.sid, u.d, u.close, u.k,
                   count(*) FILTER (WHERE u.k = 0) OVER (
                       PARTITION BY u.sid ORDER BY u.d, u.k
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS n
              FROM (SELECT sid, d, close, 1 AS k FROM bars
                    UNION ALL SELECT sid, d, NULL, 0 FROM cuts) u
           ) x
     WHERE x.k = 1
)
SELECT p.sid,
       p.d,
       coalesce(sp.currency, 'USD'),
       p.close,
       s.so_as_of,
       CASE WHEN s.so_as_of IS NOT NULL THEN s.share_factor END,
       p.close * s.so_as_of * s.share_factor,
       s.fiscal_date,
       s.known_from
  FROM placed p
  LEFT JOIN seg s ON s.sid = p.sid AND s.n = p.n
  LEFT JOIN mart.v_security_profile sp ON sp.security_id = p.sid
 ORDER BY p.sid, p.d
$fn$;

COMMENT ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date) IS
    'Daily market cap per raw bar (0042; ADR 0019 §5): the raw close x the shares of '
    'the latest balance sheet in force that day (ended within 730 days), in that '
    'day''s share terms with the warehouse''s splits, x SF1''s share factor for the '
    'period (mart.v_share_factor_versions; 1 where SF1 gave none). In the trading '
    'currency. NULL where no count is in force: funds, ETFs, secondary share classes, '
    'unfed securities, stale statements. Derived on read; NULL dates leave the span '
    'open.';

-- ---------------------------------------------------------------------------
-- mart.v_security_attributes: the share factor applied
-- ---------------------------------------------------------------------------
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
    -- ADR 0019 §5, mart.fn_daily_market_cap's expression on the last bar: the raw
    -- close x the shares of the latest balance sheet in force that day, in that day's
    -- terms, x the same period's share factor in force that day (1 where none).
    SELECT b.fiscal_date,
           lp.close * round(b.shares_outstanding * (
               SELECT exp(coalesce(sum(ln(a.split_numerator::numeric
                                          / a.split_denominator)), 0))
                 FROM mart.v_corporate_action a
                WHERE a.security_id = s.security_id AND a.action_type = 'split'
                  AND a.split_numerator > 0 AND a.split_denominator > 0
                  AND a.ex_date > b.known_from AND a.ex_date <= lp.trade_date))
               * coalesce((
                   SELECT f.share_factor
                     FROM mart.v_share_factor_versions f
                    WHERE f.security_id = s.security_id
                      AND f.period_type = b.period_type
                      AND f.fiscal_date = b.fiscal_date
                      AND f.superseded_at IS NULL
                      AND f.known_from <= lp.trade_date
                      AND (f.known_to IS NULL OR lp.trade_date < f.known_to)
                    ORDER BY f.known_from DESC
                    LIMIT 1), 1)
               AS market_cap
      FROM core.balance_sheet b
     WHERE b.security_id = s.security_id
       AND coalesce(s.currency, 'USD') = 'USD'
       AND b.superseded_at IS NULL AND b.shares_outstanding > 0
       AND b.known_from <= lp.trade_date
       AND (b.known_to IS NULL OR lp.trade_date < b.known_to)
       AND b.fiscal_date >= lp.trade_date - 730
     ORDER BY b.fiscal_date DESC, b.known_from DESC, b.period_type
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
    'that day, in its terms, x the period''s share factor (0042); USD-traded) where '
    'SF1 holds the shares, else core.security''s vendor snapshot. beta derived (365 '
    'days of daily adjusted returns on SPY''s, at least 120) else core.security''s. '
    'Computes the whole universe''s betas when read: read mart.security_latest, '
    'refreshed nightly.';

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
GRANT SELECT ON mart.v_share_factor_versions TO fafnir_read, fafnir_app, fafnir_ops;
GRANT SELECT ON mart.v_security_attributes TO fafnir_read, fafnir_app, fafnir_ops;
REVOKE ALL ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mart.fn_daily_market_cap(bigint[], date, date)
    TO fafnir_read, fafnir_app, fafnir_ops;

COMMIT;
