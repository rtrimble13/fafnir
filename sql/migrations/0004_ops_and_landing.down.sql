-- 0004_ops_and_landing.down.sql
BEGIN;
DROP TABLE IF EXISTS landing.fmp_raw;
DROP TABLE IF EXISTS ops.load_watermark;
DROP TABLE IF EXISTS ops.data_quality_flag;
DROP TABLE IF EXISTS ops.ingestion_run;
COMMIT;
