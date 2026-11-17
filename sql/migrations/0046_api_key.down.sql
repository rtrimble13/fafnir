-- 0046_api_key.down.sql
-- Reverse of 0046: drop ops.api_key.
--
-- Every key stops working, since nothing is left to check one against, and a
-- running fafnir-api fails every request until it is stopped. The login role the API
-- served as is not dropped: it was created by the install, not by 0046, and its
-- grant on this table goes with the table.

BEGIN;

DROP TABLE IF EXISTS ops.api_key;

COMMIT;
