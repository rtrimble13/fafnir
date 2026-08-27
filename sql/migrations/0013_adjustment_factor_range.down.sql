-- 0013_adjustment_factor_range.down.sql
-- Restore the NUMERIC(20, 10) factors and the 0008 view.
--
-- Narrowing cannot represent every value the wide type accepted, so the rows outside
-- [1e-10, 1e10) have to go first -- the ALTER would otherwise fail and leave the
-- rollback stuck. Every affected security is cleared ENTIRELY, not just its
-- out-of-range rows: a factor set is a chain, and the mart reads it by taking the
-- smallest effective_date greater than the bar's date. Deleting only the rows that
-- do not fit removes the earliest boundaries -- the ones carrying the largest
-- cumulative factors -- so old bars would silently fall through to a later, smaller
-- factor and read UNDER-adjusted, which is worse than unadjusted because nothing
-- about the series looks wrong. Clearing the security instead makes it read plainly
-- unadjusted, which is the pre-0013 behaviour for exactly these securities.
--
-- That is safe in the sense that matters here: core.adjustment_factor is derived,
-- recomputable state (`fafnir adjust` rebuilds it from core.corporate_action). It is
-- NOT lossless in the sense that matters to research -- those securities lose their
-- adjustment until the schema is rolled forward again.

BEGIN;

-- The view has to go first: its columns depend on the ones being narrowed.
DROP VIEW IF EXISTS mart.v_daily_price_adjusted;

DELETE FROM core.adjustment_factor
 WHERE security_id IN (
        SELECT security_id FROM core.adjustment_factor
         WHERE cumulative_price_factor  >= 1e10 OR cumulative_price_factor  < 1e-10
            OR cumulative_volume_factor >= 1e10 OR cumulative_volume_factor < 1e-10
       );

ALTER TABLE core.adjustment_factor
    ALTER COLUMN cumulative_price_factor  TYPE NUMERIC(20, 10),
    ALTER COLUMN cumulative_volume_factor TYPE NUMERIC(20, 10);

COMMENT ON COLUMN core.adjustment_factor.cumulative_price_factor IS
    'Multiply RAW price by this factor for trade_date < effective_date to get the back-adjusted price.';
COMMENT ON COLUMN core.adjustment_factor.cumulative_volume_factor IS NULL;

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
