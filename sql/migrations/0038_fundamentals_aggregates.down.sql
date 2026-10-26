-- 0038_fundamentals_aggregates.down.sql
-- Drops what 0038 added. Both are derived: nothing is lost.

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS mart.security_fundamentals_latest;
DROP VIEW IF EXISTS mart.v_industry_fundamentals;

COMMIT;
