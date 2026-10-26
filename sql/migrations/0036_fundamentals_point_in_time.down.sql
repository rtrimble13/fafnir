-- 0036_fundamentals_point_in_time.down.sql
-- Drops what 0036 added, dependents first. Nothing here held data.

BEGIN;

DROP VIEW IF EXISTS mart.v_fundamentals_ttm;
DROP VIEW IF EXISTS mart.v_statements_current;
DROP FUNCTION IF EXISTS mart.fn_fundamentals_ttm(date);
DROP FUNCTION IF EXISTS mart.fn_statements_as_of(date, timestamptz);
DROP FUNCTION IF EXISTS mart.fn_statements_as_of(date);
DROP VIEW IF EXISTS mart.v_cash_flow_statement_versions;
DROP VIEW IF EXISTS mart.v_balance_sheet_versions;
DROP VIEW IF EXISTS mart.v_income_statement_versions;

COMMIT;
