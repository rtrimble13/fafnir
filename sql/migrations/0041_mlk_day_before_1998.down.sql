-- 0041_mlk_day_before_1998.down.sql
-- Reverse of 0041: take the eight 1990-1997 MLK Day sessions out of the calendar.
--
-- Safe because nothing else writes these dates: seed_calendar never generated them
-- before 0041, and the seed that ships with 0041 generates them only on a fresh
-- install, where rolling 0041 back is not how you would undo it. Rolling back
-- re-creates the defect 0041 describes: the loaders drop real bars on these days
-- again, and dorq reports the ones stored as non-session.

BEGIN;

DELETE FROM ref.trading_calendar
 WHERE is_open
   AND trade_date IN (
        DATE '1990-01-15',
        DATE '1991-01-21',
        DATE '1992-01-20',
        DATE '1993-01-18',
        DATE '1994-01-17',
        DATE '1995-01-16',
        DATE '1996-01-15',
        DATE '1997-01-20'
   );

COMMIT;
