-- 0031_vendor_identity.down.sql
-- Reverse of 0031: drop the vendor identity tables.
--
-- This DESTROYS every link between fafnir's securities and a vendor's keys, with
-- the evidence and the person behind each manual one (#63), and with them the feed
-- assignment: a Sharadar-fed security is fed by nobody afterwards. The vendor
-- master and events reload from the vendor's files (#58); the links do not.
-- Save them first if they matter:
--   psql -c "\copy core.security_vendor_xref TO 'security_vendor_xref.csv' CSV HEADER"

BEGIN;

DROP TABLE IF EXISTS core.vendor_security_event;
DROP TABLE IF EXISTS core.security_vendor_xref;
DROP TABLE IF EXISTS core.vendor_security;

COMMIT;
