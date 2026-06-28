-- 0008_adjusted_view_precision.up.sql
-- Harden mart.v_daily_price_adjusted (review finding):
--   * Round adjusted prices to 10 decimals (numeric(20,10)) instead of 6, so a
--     heavily back-adjusted low-priced stock no longer rounds to 0.000000 and
--     break downstream log-returns / divisions.
--   * Round adjusted volume (instead of truncating toward zero) and expose it as
--     numeric(38,0) so it neither loses shares nor overflows BIGINT on read.
-- close_raw / price_factor / volume_factor are unchanged.

BEGIN;

-- DROP + CREATE (not CREATE OR REPLACE): replacing a view cannot change a
-- column's data type. Nothing depends on this view (mart.security_latest reads
-- core tables directly), so dropping it is safe.
DROP VIEW IF EXISTS mart.v_daily_price_adjusted;

CREATE VIEW mart.v_daily_price_adjusted AS
SELECT
    p.security_id,
    p.trade_date,
    ROUND(p.open  * COALESCE(af.cumulative_price_factor, 1.0), 10)::numeric(20, 10) AS open,
    ROUND(p.high  * COALESCE(af.cumulative_price_factor, 1.0), 10)::numeric(20, 10) AS high,
    ROUND(p.low   * COALESCE(af.cumulative_price_factor, 1.0), 10)::numeric(20, 10) AS low,
    ROUND(p.close * COALESCE(af.cumulative_price_factor, 1.0), 10)::numeric(20, 10) AS close,
    ROUND(p.volume * COALESCE(af.cumulative_volume_factor, 1.0))::numeric(38, 0)    AS volume,
    p.close                                                                          AS close_raw,
    COALESCE(af.cumulative_price_factor, 1.0)                                        AS price_factor,
    COALESCE(af.cumulative_volume_factor, 1.0)                                       AS volume_factor
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
    'Prices rounded to 10 dp (no vanish-to-zero); volume rounded, numeric(38,0) '
    '(no truncation/overflow). close_raw exposes the unadjusted close.';

COMMIT;
