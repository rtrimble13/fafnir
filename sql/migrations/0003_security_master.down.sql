-- 0003_security_master.down.sql
BEGIN;
DROP TABLE IF EXISTS core.company_profile;
DROP TABLE IF EXISTS core.symbol_xref;
DROP TABLE IF EXISTS core.security;
COMMIT;
