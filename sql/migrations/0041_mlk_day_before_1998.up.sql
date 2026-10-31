-- 0041_mlk_day_before_1998.up.sql
-- Add the eight Martin Luther King Jr. Day sessions of 1990-1997 that the seed left
-- out of ref.trading_calendar.
--
-- What was wrong
-- --------------
-- fafnir.db.seed.us_market_holidays closed MLK Day (the third Monday of January)
-- in every year. NYSE first closed for it in 1998, and traded through it before:
-- the warehouse holds 1,247-2,511 bars on each of these eight Mondays (14,183 in
-- all on 2026-10-31), the same as the Friday before, and 2 on 1998-01-19. With the
-- days missing from the calendar:
--   * both price loaders' non-session set-aside (`_drop_non_session`) dropped the
--     real bars of any history loaded on those dates since the guard existed
--     (AKO-A lost 1995-01-16, 1996-01-15 and 1997-01-20);
--   * `prices delete --non-session` counted the 14,183 bars as deletable, and
--     `prices shift` refused to move a bar onto one of them;
--   * dorq reported each stored one as `dorq_non_session_bar` (DQ105), "a bar on
--     a Monday, which is not a session": 27 on the 2026-10-19 history pass, 8 on
--     CECO on 2026-10-29;
--   * the gap check never flagged a security with no bar on those days.
--
-- Why INSERT, and only into the calendars that span the year
-- ----------------------------------------------------------
-- Recurring holidays are represented by ABSENCE (seed_calendar inserts open days
-- only), so the fix is the eight rows the seed should have written, open. Each
-- exchange gets them only where its calendar already spans the date, the span
-- `repository.open_sessions` reads: inside it a missing row means closed, outside it
-- unknown, and this migration must not move a calendar's edges. ON CONFLICT leaves
-- a row someone added by hand alone.
--
-- One half of the fix
-- -------------------
-- On a FRESH install migrations run before `fafnir db seed`, so this INSERT finds no
-- calendar and adds nothing; the seed then generates the days itself, since
-- us_market_holidays now closes MLK Day only from MLK_DAY_FIRST_CLOSED (1998). The
-- dates are written out here rather than computed, as 0022's are:
-- test_mlk_sessions_match_the_migration pins this list to the seed's rule.
--
-- After it runs: the next `fafnir dq run` flags a gap on each of these dates for a
-- security whose history spans it and holds no bar on it (about 80 on the host on
-- 2026-10-31, most of them 1996-1997 bars the loaders dropped); a re-fetch or
-- `ingest sharadar-prices --backfill` fills those. dorq's run hash changes, since
-- the calendar it is given changes.

BEGIN;

INSERT INTO ref.trading_calendar (exchange_code, trade_date, is_open)
SELECT span.exchange_code, mlk.trade_date, TRUE
  FROM (VALUES
        (DATE '1990-01-15'),
        (DATE '1991-01-21'),
        (DATE '1992-01-20'),
        (DATE '1993-01-18'),
        (DATE '1994-01-17'),
        (DATE '1995-01-16'),
        (DATE '1996-01-15'),
        (DATE '1997-01-20')
       ) AS mlk (trade_date)
  JOIN (SELECT exchange_code, min(trade_date) AS first, max(trade_date) AS last
          FROM ref.trading_calendar
         GROUP BY exchange_code) AS span
    ON mlk.trade_date BETWEEN span.first AND span.last
ON CONFLICT (exchange_code, trade_date) DO NOTHING;

COMMIT;
