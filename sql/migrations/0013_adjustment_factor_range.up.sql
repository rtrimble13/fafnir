-- 0013_adjustment_factor_range.up.sql
-- Give cumulative adjustment factors the numeric range a *product* actually needs.
--
-- The problem. 0006 typed both factors NUMERIC(20, 10), which holds only
-- [1e-10, 1e10). But a cumulative factor is not a single ratio -- it is the product
-- of every split and dividend factor after a date, over an entire history. Reverse
-- splits multiply the price factor up (den/num > 1) and forward splits multiply it
-- down, and a long-lived penny stock that has reverse-split a handful of times
-- (1:10 x 1:50 x 1:100 x 1:200 x 1:100 = 1e9, then one more) walks straight out of
-- that window. `fafnir adjust` over the full universe then died on the first such
-- security with
--
--     psycopg.errors.NumericValueOutOfRange: numeric field overflow
--     DETAIL: A field with precision 20, scale 10 must round to an absolute value
--             less than 10^10.
--
-- taking the whole step-5 recompute down with it (initial_backfill.sh, 2026-08-27).
--
-- Both ends of the window are fatal, and both are reachable from the same history
-- read in either direction:
--   * price factor >= 1e10 (deep reverse-split history) -> overflow, as above;
--   * volume factor >= 1e10 (deep FORWARD-split history) -> the same overflow;
--   * price factor <  5e-11 (deeper forward-split history) -> rounds to 0.0000000000
--     at scale 10, which trips ck_adj_factor_positive -- and, without that guard,
--     would have zeroed every pre-split price in the mart.
--
-- The fix is to stop declaring a range at all: unconstrained NUMERIC is arbitrary
-- precision, so no product of positive ratios can overflow it and none rounds to
-- zero. The factor arithmetic is exact Decimal either way; this only stops the
-- storage type from truncating the result. mart.v_daily_price_adjusted is rebuilt to
-- match, so a factor that now stores fine cannot instead overflow -- or round silently
-- to zero -- on READ.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Drop the mart first: PostgreSQL refuses to alter the type of a column a view
--    depends on. It is recreated, widened, in step 3.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS mart.v_daily_price_adjusted;

-- ---------------------------------------------------------------------------
-- 2. Widen the stored factors. The DEFAULT 1.0 and ck_adj_factor_positive both
--    carry over unchanged; existing values are unaffected (every one of them fits,
--    since the old type was narrower).
-- ---------------------------------------------------------------------------
ALTER TABLE core.adjustment_factor
    ALTER COLUMN cumulative_price_factor  TYPE NUMERIC,
    ALTER COLUMN cumulative_volume_factor TYPE NUMERIC;

COMMENT ON COLUMN core.adjustment_factor.cumulative_price_factor IS
    'Multiply RAW price by this factor for trade_date < effective_date to get the back-adjusted price. '
    'Unconstrained NUMERIC: a cumulative factor is a product over a whole action history and '
    'legitimately spans many orders of magnitude in both directions (migration 0013).';
COMMENT ON COLUMN core.adjustment_factor.cumulative_volume_factor IS
    'Multiply RAW volume by this factor for trade_date < effective_date. Moves inversely to '
    'the price factor: a forward split scales volume up and price down.';

-- ---------------------------------------------------------------------------
-- 3. Recreate the mart so the read side cannot overflow or vanish either, for the
--    same reason: a *declared scale* is the wrong shape for a value that spans
--    orders of magnitude. 0008 rounded prices to numeric(20, 10), which carries the
--    same 1e10 ceiling the factors just escaped -- now reachable from the factor
--    side as well as the price side -- and still rounds a price back-adjusted below
--    5e-11 to 0.0000000000, which is exactly the vanish-to-zero 0008 set out to
--    stop. Both disappear if the view simply does not round: the product of two
--    exact NUMERICs is exact, so adjusted prices are now unconstrained NUMERIC.
--    Nothing is lost downstream -- duk casts prices to float on read and formats
--    for display (duk.format_utils), so a scale imposed here never survived anyway.
--    Volume stays integral (a share count), just without a declared ceiling.
--    Nothing else depends on this view (mart.security_latest reads core tables
--    directly), so dropping it is safe.
-- ---------------------------------------------------------------------------
CREATE VIEW mart.v_daily_price_adjusted AS
SELECT
    p.security_id,
    p.trade_date,
    p.open  * COALESCE(af.cumulative_price_factor, 1.0)         AS open,
    p.high  * COALESCE(af.cumulative_price_factor, 1.0)         AS high,
    p.low   * COALESCE(af.cumulative_price_factor, 1.0)         AS low,
    p.close * COALESCE(af.cumulative_price_factor, 1.0)         AS close,
    ROUND(p.volume * COALESCE(af.cumulative_volume_factor, 1.0)) AS volume,
    p.close                                                      AS close_raw,
    COALESCE(af.cumulative_price_factor, 1.0)                    AS price_factor,
    COALESCE(af.cumulative_volume_factor, 1.0)                   AS volume_factor
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
    'Prices are the exact unrounded product of the raw price and the cumulative factor '
    '(unconstrained NUMERIC), so no split history can round an adjusted price to zero or '
    'overflow it on read (migration 0013). Volume is rounded to whole shares. '
    'close_raw exposes the unadjusted close.';

COMMIT;
