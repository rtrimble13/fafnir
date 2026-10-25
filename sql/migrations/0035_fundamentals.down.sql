-- 0035_fundamentals.down.sql
-- Reverse of 0035: drop the statement tables and the line-item taxonomy.
--
-- This DESTROYS every stored statement version, with the restatements fafnir
-- observed and dated. The first-reported rows reload from the SF1 file (`fafnir
-- ingest fundamentals --backfill`), but a restatement's known_from is the day fafnir
-- first loaded it: reloaded, every restatement is dated the reload day, and the
-- history of when they were seen is gone. Save the tables first if they matter:
--   psql -c "\copy core.income_statement TO 'income_statement.csv' CSV HEADER"

BEGIN;

DROP TABLE IF EXISTS core.cash_flow_statement;
DROP TABLE IF EXISTS core.balance_sheet;
DROP TABLE IF EXISTS core.income_statement;
DROP TABLE IF EXISTS ref.statement_line_item;

COMMIT;
