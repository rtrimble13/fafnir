-- 0030_sharadar_file_ledger.up.sql
-- The Sharadar file ledger: one row per file downloaded from Sharadar (plan v2,
-- SA-0203, #50).
--
-- FMP's responses land in landing.fmp_raw as JSONB, one row per request. Sharadar
-- arrives as whole-table bulk files and nightly slices, and JSONB rows of those would
-- add tens of GB to a host sized at 80 GB. Plan v2 also dropped typed landing tables
-- (one warehouse of market data, not two). So the files stay on disk, under
-- `[sharadar] bulk_dir`, named by their content, and this table records each one:
-- which table it holds, what was asked for, its hash, size and row count, and the
-- run that fetched it. Every later load can then say which file a row came from, and
-- a file is never fetched twice (sha256 is unique).
--
-- `path` is relative to `bulk_dir`, so the directory can move (onto a Hetzner
-- Volume, install_hetzner.md §3.3) without rewriting the ledger. A bulk file is a
-- zip (measured 2026-10-19: `/bulk-sharadar/<table>.csv.zip`), and `member` names
-- the CSV inside it. A slice saved from the API may be a plain or gzipped CSV, with
-- `member` NULL.
--
-- `status` is the file's lifecycle, not a loader's progress: `downloaded`, then
-- `superseded` by a newer full file of the same table, then `deleted` from disk
-- (retention, or the licence). Which files a loader has applied is that loader's
-- record (ops.ingestion_run, ops.load_watermark), since several loaders read one
-- file.
--
-- Size, projected from the paid feed on 2026-10-19:
--   * TICKERS date ranges give about 45.5M bars for stocks (55% delisted), 15.8M
--     for funds, and about 3.5M fundamentals rows;
--   * a CSV row is about 70 bytes for prices and 1 KB for fundamentals, and zip
--     compresses CSV about 3.3x.
--   One full download of stocks, funds, fundamentals, actions and tickers is about
--   2.4 GB on disk: stocks ~1.0, funds ~0.35, fundamentals ~1.1, the rest < 0.1.
--   doc/backfill.md › "Disk budget with Sharadar" has the arithmetic and what core
--   gains. This table itself stays small: one row per file.

BEGIN;

CREATE TABLE IF NOT EXISTS landing.sharadar_file (
    file_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_table      TEXT   NOT NULL,
    kind              TEXT   NOT NULL,
    filters           JSONB  NOT NULL DEFAULT '{}'::jsonb,
    path              TEXT   NOT NULL,
    member            TEXT,
    sha256            TEXT   NOT NULL,
    bytes             BIGINT NOT NULL,
    rows              BIGINT,
    columns           TEXT[],
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingestion_run_id  BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    status            TEXT   NOT NULL DEFAULT 'downloaded',
    status_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The modern names Sharadar's /v1.0/schema lists. A legacy code (SEP, SF1) is
    -- stored as its modern name, so one table never appears under two spellings.
    CONSTRAINT ck_sharadar_file_table CHECK (vendor_table IN (
        'actions', 'daily', 'descriptions', 'events', 'fundamentals', 'funds',
        'holdings', 'holdings_investor', 'holdings_ticker', 'insiders', 'metrics',
        'sp500', 'stocks', 'tickers'
    )),
    CONSTRAINT ck_sharadar_file_kind CHECK (kind IN ('full', 'delta')),
    CONSTRAINT ck_sharadar_file_status
        CHECK (status IN ('downloaded', 'superseded', 'deleted')),
    CONSTRAINT ck_sharadar_file_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_sharadar_file_sizes CHECK (bytes >= 0 AND (rows IS NULL OR rows >= 0)),
    -- Relative to bulk_dir, and inside it.
    CONSTRAINT ck_sharadar_file_path CHECK (
        path <> '' AND path !~ '^/' AND path !~ '(^|/)\.\.(/|$)'
    )
);

COMMENT ON TABLE landing.sharadar_file IS
    'One row per file downloaded from Sharadar; the file itself is on disk under '
    '[sharadar] bulk_dir (plan v2: no typed landing tables). Grain: file_id; one '
    'row per content (sha256 unique).';
COMMENT ON COLUMN landing.sharadar_file.vendor_table IS
    'Sharadar''s modern table name (stocks, not SEP).';
COMMENT ON COLUMN landing.sharadar_file.kind IS
    'full: a whole-table bulk file (years=5|10|full); delta: a slice (a date, a '
    'lastupdated window).';
COMMENT ON COLUMN landing.sharadar_file.filters IS
    'What was asked for, as sent: {"years": "full"}, {"lastupdated.gte": ...}.';
COMMENT ON COLUMN landing.sharadar_file.path IS
    'Relative to [sharadar] bulk_dir: <sha256><suffix>, e.g. <hash>.csv.zip.';
COMMENT ON COLUMN landing.sharadar_file.member IS
    'The CSV inside a zip; NULL for a plain or gzipped CSV.';
COMMENT ON COLUMN landing.sharadar_file.rows IS
    'Data rows (header excluded), counted when the file is first read; NULL until.';
COMMENT ON COLUMN landing.sharadar_file.columns IS
    'The CSV header as found, for header-drift checks against the next file.';
COMMENT ON COLUMN landing.sharadar_file.status IS
    'The file''s lifecycle: downloaded, superseded (a newer full of the table), '
    'deleted (gone from disk). Not a loader''s progress.';

CREATE UNIQUE INDEX IF NOT EXISTS ux_sharadar_file_sha256
    ON landing.sharadar_file (sha256);
CREATE INDEX IF NOT EXISTS ix_sharadar_file_table
    ON landing.sharadar_file (vendor_table, kind, fetched_at DESC);

COMMIT;
