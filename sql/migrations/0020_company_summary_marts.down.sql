-- 0020_company_summary_marts.down.sql
-- Drop the six views 0020 added.
--
-- Views only -- no data is lost. What IS lost is duk's ability to read as
-- fafnir_app: rolling this back returns `duk -S db ph` to the pre-0020 state where
-- symbol resolution needs core, so a laptop or MCP client on a mart-only role stops
-- working. Roll back only alongside the duk version that still names core
-- relations.

BEGIN;

DROP VIEW IF EXISTS mart.v_security_dq_open;
DROP VIEW IF EXISTS mart.v_security_action_summary;
DROP VIEW IF EXISTS mart.v_security_price_coverage;
DROP VIEW IF EXISTS mart.v_security_profile;
DROP VIEW IF EXISTS mart.v_daily_price_raw;
DROP VIEW IF EXISTS mart.v_symbol_lookup;

COMMIT;
