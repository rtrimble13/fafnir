-- 0006_corporate_actions.up.sql
-- Corporate actions (splits, cash dividends) and the DERIVED adjustment factors.
--
--  * core.corporate_action  -- the source events. Grain: (security_id, action_type, ex_date).
--  * core.adjustment_factor -- cumulative price/volume factors derived deterministically
--                              from corporate_action by `fafnir adjust`. Point-in-time stable.
--
-- Adjusted OHLCV is produced on read in mart.v_daily_price_adjusted (migration 0007)
-- by multiplying raw prices by the cumulative price factor and dividing volume by
-- the cumulative volume factor.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.corporate_action
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.corporate_action (
    corporate_action_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id          BIGINT NOT NULL REFERENCES core.security (security_id),
    action_type          TEXT   NOT NULL CHECK (action_type IN ('split', 'dividend')),
    ex_date              DATE   NOT NULL,
    record_date          DATE,
    payment_date         DATE,
    declaration_date     DATE,
    -- Splits: a 2-for-1 split is numerator=2, denominator=1.
    split_numerator      NUMERIC(20, 6),
    split_denominator    NUMERIC(20, 6),
    -- Dividends: cash amount per share in `currency`.
    dividend_amount      NUMERIC(20, 6),
    currency             TEXT NOT NULL DEFAULT 'USD',
    source               TEXT NOT NULL DEFAULT 'fmp',
    ingestion_run_id     BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    loaded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (security_id, action_type, ex_date),
    CONSTRAINT ck_corp_action_split CHECK (
        action_type <> 'split'
        OR (split_numerator > 0 AND split_denominator > 0)
    ),
    CONSTRAINT ck_corp_action_dividend CHECK (
        action_type <> 'dividend'
        OR (dividend_amount IS NOT NULL AND dividend_amount >= 0)
    )
);
COMMENT ON TABLE core.corporate_action IS
    'Split and cash-dividend events. Grain: (security_id, action_type, ex_date). '
    'Feeds core.adjustment_factor.';

CREATE INDEX IF NOT EXISTS ix_corp_action_security_exdate
    ON core.corporate_action (security_id, ex_date);

-- ---------------------------------------------------------------------------
-- core.adjustment_factor (derived, recomputable)
-- effective_date is the first trade_date on/after which the factor applies, i.e.
-- prices STRICTLY BEFORE the ex_date are scaled by the cumulative factor.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.adjustment_factor (
    security_id              BIGINT NOT NULL REFERENCES core.security (security_id),
    effective_date           DATE   NOT NULL,         -- ex_date boundary
    cumulative_price_factor  NUMERIC(20, 10) NOT NULL DEFAULT 1.0,
    cumulative_volume_factor NUMERIC(20, 10) NOT NULL DEFAULT 1.0,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, effective_date),
    CONSTRAINT ck_adj_factor_positive
        CHECK (cumulative_price_factor > 0 AND cumulative_volume_factor > 0)
);
COMMENT ON TABLE core.adjustment_factor IS
    'Derived cumulative adjustment factors per security at each ex-date boundary. '
    'Recomputed by `fafnir adjust`; deterministic from core.corporate_action.';
COMMENT ON COLUMN core.adjustment_factor.cumulative_price_factor IS
    'Multiply RAW price by this factor for trade_date < effective_date to get the back-adjusted price.';

COMMIT;
