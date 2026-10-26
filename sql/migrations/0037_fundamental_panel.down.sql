-- 0037_fundamental_panel.down.sql
-- Drops what 0037 added, dependents first. The panel is derived: nothing is lost.

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS mart.fundamental_panel;
DROP FUNCTION IF EXISTS mart.fn_fundamental_snapshot(date, integer);
DROP FUNCTION IF EXISTS mart.fn_fundamental_panel(date[], integer);
DROP FUNCTION IF EXISTS mart.fn_growth(numeric, numeric);
DROP FUNCTION IF EXISTS mart.fn_ratio(numeric, numeric);

COMMIT;
