-- 0018_tracked_symbol.down.sql
-- Drop the declared universe.
--
-- This LOSES the declarations -- which symbols were tracked, why, and since when.
-- It does NOT touch core.security: securities already minted from those
-- declarations keep their security_id, their bars and their history, exactly as a
-- delisted security does. They simply stop being refreshed, because nothing
-- declares them any more.
--
-- The MUTF venue is left in ref.exchange. Dropping it would break the foreign key
-- on any core.security row still recorded under it, and an unused venue row costs
-- nothing.

BEGIN;

DROP INDEX IF EXISTS ref.ix_tracked_symbol_tracked;
DROP TABLE IF EXISTS ref.tracked_symbol;

COMMIT;
