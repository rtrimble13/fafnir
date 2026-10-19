-- 0030_sharadar_file_ledger.down.sql
-- Reverse of 0030: drop the Sharadar file ledger.
--
-- The files themselves stay on disk under [sharadar] bulk_dir; only the record of
-- them goes: which table each holds, what was asked for, and the run that fetched
-- it. Their names are their sha256, so a re-applied 0030 plus a re-download finds
-- them again. Save the ledger first if its history matters:
--   psql -c "\copy landing.sharadar_file TO 'sharadar_file.csv' CSV HEADER"

BEGIN;

DROP TABLE IF EXISTS landing.sharadar_file;

COMMIT;
