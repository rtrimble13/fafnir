-- 0002_reference_tables.down.sql
BEGIN;
DROP TABLE IF EXISTS ref.trading_calendar;
DROP TABLE IF EXISTS ref.industry;
DROP TABLE IF EXISTS ref.sector;
DROP TABLE IF EXISTS ref.exchange;
COMMIT;
