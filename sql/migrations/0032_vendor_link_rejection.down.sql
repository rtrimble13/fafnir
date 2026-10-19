-- 0032_vendor_link_rejection.down.sql
-- Reverse of 0032: drop the rejection table.
--
-- This DESTROYS every recorded "this security is not that key" decision, with who
-- made it and why: the next `fafnir vendor link` may propose those pairs again.
-- Save them first if they matter:
--   psql -c "\copy core.security_vendor_rejection TO 'security_vendor_rejection.csv' CSV HEADER"

BEGIN;

DROP TABLE IF EXISTS core.security_vendor_rejection;

COMMIT;
