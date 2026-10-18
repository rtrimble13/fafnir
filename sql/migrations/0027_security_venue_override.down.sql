-- 0027_security_venue_override.down.sql
-- Reverse of 0027: drop the operator's venue override.
--
-- A security moved with `fafnir security venue-transfer` keeps its exchange_code
-- and activity as they stand, but the next `ingest securities` writes the vendor's
-- view back over them (FMP's NYSE, and inactive, for the TXSE moves of 2026-10).
--
-- The TXSE row in ref.exchange is left in place: it is reference data the seed also
-- creates, and securities and calendar rows may refer to it.

BEGIN;

ALTER TABLE core.security
    DROP COLUMN IF EXISTS venue_override,
    DROP COLUMN IF EXISTS venue_override_left;

COMMIT;
