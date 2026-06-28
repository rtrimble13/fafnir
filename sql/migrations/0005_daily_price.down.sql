-- 0005_daily_price.down.sql
-- Dropping the partitioned parent drops all partitions.
BEGIN;
DROP TABLE IF EXISTS core.daily_price CASCADE;
COMMIT;
