-- 0004_ops_and_landing.up.sql
-- Provenance and landing infrastructure. Created before the fact tables so they
-- can reference ops.ingestion_run for lineage.
--
--  * landing.fmp_raw      -- raw immutable payloads as received from FMP
--  * ops.ingestion_run    -- one row per load: params, window, counts, hash, bytes, status
--  * ops.data_quality_flag-- quarantined rows / detected anomalies awaiting review
--  * ops.load_watermark   -- per source/endpoint/symbol incremental high-water marks

BEGIN;

-- ---------------------------------------------------------------------------
-- ops.ingestion_run  -- lineage for every load
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.ingestion_run (
    ingestion_run_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT 'fmp',     -- fmp / fred / bls / bea
    endpoint            TEXT NOT NULL,
    params              JSONB NOT NULL DEFAULT '{}'::jsonb,
    window_from         DATE,
    window_to           DATE,
    symbols_requested   INTEGER NOT NULL DEFAULT 0,
    rows_inserted       INTEGER NOT NULL DEFAULT 0,
    rows_updated        INTEGER NOT NULL DEFAULT 0,
    rows_quarantined    INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded    BIGINT  NOT NULL DEFAULT 0,      -- vs 50GB/mo FMP budget
    response_hash       TEXT,
    status              TEXT NOT NULL DEFAULT 'started'
                          CHECK (status IN ('started', 'success', 'partial', 'failed')),
    error_message       TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
COMMENT ON TABLE ops.ingestion_run IS
    'Provenance/lineage log. One row per load. Grain: ingestion_run_id.';
CREATE INDEX IF NOT EXISTS ix_ingestion_run_source_time
    ON ops.ingestion_run (source, endpoint, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_ingestion_run_status
    ON ops.ingestion_run (status) WHERE status IN ('failed', 'partial');

-- ---------------------------------------------------------------------------
-- ops.data_quality_flag  -- quarantine + anomaly review queue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.data_quality_flag (
    dq_flag_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id  BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    security_id       BIGINT,                            -- nullable; soft ref to core.security
    table_name        TEXT,
    record_key        JSONB,                             -- natural key of offending row
    check_name        TEXT NOT NULL,                     -- e.g. 'gap', 'outlier', 'cross_field'
    severity          TEXT NOT NULL DEFAULT 'warn'
                        CHECK (severity IN ('info', 'warn', 'error')),
    detail            JSONB,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ
);
COMMENT ON TABLE ops.data_quality_flag IS
    'Quarantined rows and detected anomalies awaiting review. Quarantine, never silently drop.';
CREATE INDEX IF NOT EXISTS ix_dq_flag_open
    ON ops.data_quality_flag (check_name, detected_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_dq_flag_security
    ON ops.data_quality_flag (security_id) WHERE security_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- ops.load_watermark  -- incremental high-water marks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.load_watermark (
    source             TEXT NOT NULL DEFAULT 'fmp',
    endpoint           TEXT NOT NULL,
    security_id        BIGINT NOT NULL DEFAULT 0,        -- 0 = whole-endpoint (non per-symbol)
    last_loaded_date   DATE,
    last_run_at        TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, endpoint, security_id)
);
COMMENT ON TABLE ops.load_watermark IS
    'Per source/endpoint/security incremental high-water mark. Daily loads fetch only the new tail.';

-- ---------------------------------------------------------------------------
-- landing.fmp_raw  -- immutable raw payloads
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS landing.fmp_raw (
    raw_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id  BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    endpoint          TEXT NOT NULL,
    params            JSONB NOT NULL DEFAULT '{}'::jsonb,
    symbol            TEXT,
    http_status       INTEGER,
    payload           JSONB,
    payload_hash      TEXT,
    bytes             BIGINT NOT NULL DEFAULT 0,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE landing.fmp_raw IS
    'Raw immutable FMP responses. Ground truth when a field meaning surprises you later.';
CREATE INDEX IF NOT EXISTS ix_fmp_raw_endpoint_symbol
    ON landing.fmp_raw (endpoint, symbol, fetched_at DESC);
CREATE INDEX IF NOT EXISTS ix_fmp_raw_hash ON landing.fmp_raw (payload_hash);

COMMIT;
