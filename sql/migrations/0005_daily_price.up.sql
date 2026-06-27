-- 0005_daily_price.up.sql
-- The primary fact table: raw daily OHLCV.
--
-- Design commitments:
--  * RAW prices only (immutable once the trading day closes). Adjusted series are
--    DERIVED on read from core.adjustment_factor (migration 0006) -> point-in-time stable.
--  * Money is exact NUMERIC, never float. Volume is BIGINT (can exceed 32-bit).
--  * Grain (security_id, trade_date), enforced by the PK -> idempotent upserts.
--  * Range-partitioned by trade_date (yearly). Single time-partitioned table with the
--    time column in the PK => can be converted to a TimescaleDB hypertable later
--    WITHOUT a grain change.
--  * Cross-field CHECKs reject impossible bars at the boundary.

BEGIN;

CREATE TABLE IF NOT EXISTS core.daily_price (
    security_id       BIGINT NOT NULL REFERENCES core.security (security_id),
    trade_date        DATE   NOT NULL,
    open              NUMERIC(20, 6) NOT NULL,
    high              NUMERIC(20, 6) NOT NULL,
    low               NUMERIC(20, 6) NOT NULL,
    close             NUMERIC(20, 6) NOT NULL,
    volume            BIGINT NOT NULL DEFAULT 0,
    vwap              NUMERIC(20, 6),
    source            TEXT   NOT NULL DEFAULT 'fmp',
    ingestion_run_id  BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, trade_date),
    CONSTRAINT ck_daily_price_high_low   CHECK (high >= low),
    CONSTRAINT ck_daily_price_high_open  CHECK (high >= open),
    CONSTRAINT ck_daily_price_high_close CHECK (high >= close),
    CONSTRAINT ck_daily_price_low_open   CHECK (low  <= open),
    CONSTRAINT ck_daily_price_low_close  CHECK (low  <= close),
    CONSTRAINT ck_daily_price_volume     CHECK (volume >= 0),
    CONSTRAINT ck_daily_price_positive   CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0)
) PARTITION BY RANGE (trade_date);

COMMENT ON TABLE core.daily_price IS
    'Raw daily OHLCV. Grain: (security_id, trade_date). Immutable once the day closes; '
    'adjusted series derived on read via mart.v_daily_price_adjusted.';
COMMENT ON COLUMN core.daily_price.close IS 'RAW close (unadjusted). Use mart for split/dividend-adjusted close.';
COMMENT ON COLUMN core.daily_price.volume IS 'RAW (unadjusted) volume.';

-- Index serving contiguous-series reads by symbol over a date range.
-- (The PK already covers (security_id, trade_date); this is a no-op placeholder
--  kept explicit for clarity and future covering-index tuning.)

-- ---------------------------------------------------------------------------
-- Partitions. A DEFAULT partition catches any out-of-range dates so loads never
-- fail on an unexpected year; the maintenance job (fafnir db ensure-partitions)
-- creates dedicated yearly partitions ahead of time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.daily_price_default
    PARTITION OF core.daily_price DEFAULT;

CREATE TABLE IF NOT EXISTS core.daily_price_y2018 PARTITION OF core.daily_price
    FOR VALUES FROM ('2018-01-01') TO ('2019-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2019 PARTITION OF core.daily_price
    FOR VALUES FROM ('2019-01-01') TO ('2020-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2020 PARTITION OF core.daily_price
    FOR VALUES FROM ('2020-01-01') TO ('2021-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2021 PARTITION OF core.daily_price
    FOR VALUES FROM ('2021-01-01') TO ('2022-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2022 PARTITION OF core.daily_price
    FOR VALUES FROM ('2022-01-01') TO ('2023-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2023 PARTITION OF core.daily_price
    FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2024 PARTITION OF core.daily_price
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2025 PARTITION OF core.daily_price
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2026 PARTITION OF core.daily_price
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE IF NOT EXISTS core.daily_price_y2027 PARTITION OF core.daily_price
    FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');

COMMIT;
