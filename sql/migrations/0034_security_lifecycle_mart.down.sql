-- 0034_security_lifecycle_mart.down.sql
-- Reverse of 0034: drop mart.v_security_lifecycle.
--
-- A view only: no data is lost. What is lost is a mart-only reader's way to read
-- why a security stopped trading and who acquired it.

BEGIN;

DROP VIEW IF EXISTS mart.v_security_lifecycle;

COMMIT;
