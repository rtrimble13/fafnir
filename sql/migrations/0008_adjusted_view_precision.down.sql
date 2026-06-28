-- 0008_adjusted_view_precision.down.sql
-- Restore the 0007 definition (6-dp prices, bigint volume).
BEGIN;

DROP VIEW IF EXISTS mart.v_daily_price_adjusted;

CREATE VIEW mart.v_daily_price_adjusted AS
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

COMMIT;
