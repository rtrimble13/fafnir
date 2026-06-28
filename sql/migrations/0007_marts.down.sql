-- 0007_marts.down.sql
BEGIN;
DROP MATERIALIZED VIEW IF EXISTS mart.security_latest;
DROP VIEW IF EXISTS mart.v_daily_price_adjusted;
COMMIT;
