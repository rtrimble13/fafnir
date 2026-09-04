-- 0022_ad_hoc_market_closures.down.sql
-- Reverse of 0022: restore the eleven ad-hoc closures to open sessions.
--
-- Safe to run because 0022 is the only thing that has ever written is_open = FALSE
-- to this table -- seed_calendar inserts open days and nothing else updates the
-- column -- so there is no other closure for this statement to trample.
--
-- Rolling this back re-creates the condition that produced 69,229 `gap` flags: the
-- next `fafnir dq run` will find eleven sessions with no bar for any security and
-- flag them again. That is the migration working in reverse, not a fault.

BEGIN;

UPDATE ref.trading_calendar
   SET is_open = TRUE
 WHERE NOT is_open
   AND trade_date IN (
        DATE '1994-04-27',
        DATE '2001-09-11',
        DATE '2001-09-12',
        DATE '2001-09-13',
        DATE '2001-09-14',
        DATE '2004-06-11',
        DATE '2007-01-02',
        DATE '2012-10-29',
        DATE '2012-10-30',
        DATE '2018-12-05',
        DATE '2025-01-09'
   );

COMMIT;
