-- 0047_dq_audit.up.sql
-- The audit sample that decides the dorq cutover (DE-03, #268; plan
-- doc/plans/dorq-efficacy.md).
--
-- To show that dorq is better than the SQL checks, precision and recall are
-- estimated on a sample that neither engine selected: an **audit cycle**. A
-- cycle is drawn from one night of one domain (bars or statements) by
-- `fafnir dq sample --domain --cycle --seed`. Its frame is every condition either
-- engine reported that night, plus units neither reported (random bars, missing
-- sessions and statement periods, with an oversample near the thresholds). Each
-- unit is drawn with a known probability (`inclusion_p`). A drawn unit is verified
-- against an outside source, and the verdict is stored as labels that carry the
-- cycle (`sample_id`), the unit (`sample_item`) and the weight (`1 / inclusion_p`).
--
-- ops.dq_audit_cycle  one row per cycle: its night, its seed and its design
--                     (strata, sizes, rates), and the second verification's result.
-- ops.dq_audit_item   one row per drawn unit: its key, its strata and inclusion
--                     probability, the data as it stood when drawn (what the
--                     verifier is shown), and what each engine said about it
--                     (never shown to the verifier).
-- ops.dq_label        gains sample_id, sample_item and weight.
--
-- New tables in ops: 0001's default privileges give fafnir_ingest the writes and
-- 0021's give fafnir_ops SELECT, as for every ops table. The columns on
-- ops.dq_label are covered by that table's grants. Nothing here changes a row
-- that exists.

BEGIN;

CREATE TABLE ops.dq_audit_cycle (
    cycle_id      TEXT        PRIMARY KEY CHECK (cycle_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    domain        TEXT        NOT NULL CHECK (domain IN ('bars', 'statements')),
    seed          BIGINT      NOT NULL,
    -- The night sampled: its shadow file's date, and the dorq run that wrote it.
    night         DATE        NOT NULL,
    shadow_file   TEXT        NOT NULL,
    dorq_run_id   BIGINT,
    dorq_version  TEXT,
    -- The moment the engines' outputs are read as of: the dorq run's start.
    state_at      TIMESTAMPTZ,
    -- Strata with their frame sizes, targets and rates; the volume as written.
    design        JSONB       NOT NULL,
    created_by    TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT
);

COMMENT ON TABLE ops.dq_audit_cycle IS
    'Audit cycles for the dorq cutover gate (DE-03, #268). One row per cycle: a '
    'night of one domain, sampled with a seed. Written by `fafnir dq sample '
    '--domain`; read by `fafnir dq compare --audit`.';
COMMENT ON COLUMN ops.dq_audit_cycle.design IS
    'The sampling design: per stratum its frame size N, target n and rate; the '
    'frame''s volume as written per engine (standing, new per night); the '
    'second-verification share.';

CREATE TABLE ops.dq_audit_item (
    cycle_id      TEXT        NOT NULL REFERENCES ops.dq_audit_cycle (cycle_id) ON DELETE CASCADE,
    item_no       INTEGER     NOT NULL CHECK (item_no >= 1),
    security_id   BIGINT      NOT NULL,
    -- The unit: a bar or missing session (trade date), or a statement period
    -- (fiscal date and period type; '-' when a finding is about no one period).
    unit_date     DATE        NOT NULL,
    period_type   TEXT        CHECK (period_type IS NULL OR period_type IN ('annual', 'quarter', '-')),
    strata        TEXT[]      NOT NULL,
    inclusion_p   DOUBLE PRECISION NOT NULL CHECK (inclusion_p > 0 AND inclusion_p <= 1),
    -- What the verifier is shown: the data as it stood when the unit was drawn.
    data          JSONB       NOT NULL,
    -- What the engines said about the unit. Never shown to the verifier.
    engines       JSONB       NOT NULL,
    -- Drawn for the blind second verification.
    second_check  BOOLEAN     NOT NULL DEFAULT false,
    -- pending | judged | unverifiable
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'judged', 'unverifiable')),
    status_note   TEXT,
    -- The second verifier's verdict, kept apart from the labels.
    second        JSONB,
    PRIMARY KEY (cycle_id, item_no),
    UNIQUE NULLS NOT DISTINCT (cycle_id, security_id, unit_date, period_type)
);

COMMENT ON TABLE ops.dq_audit_item IS
    'The units an audit cycle drew (DE-03, #268). `data` is what the verifier is '
    'shown; `engines` is what dorq and the SQL checks said, and is never shown to '
    'the verifier. A judged unit''s verdict is its labels (ops.dq_label with this '
    'sample_id and sample_item).';
COMMENT ON COLUMN ops.dq_audit_item.inclusion_p IS
    'The probability the unit was drawn with: the largest rate of the strata it '
    'is in. Its labels'' weight is 1 / inclusion_p.';

ALTER TABLE ops.dq_label
    ADD COLUMN IF NOT EXISTS sample_id   TEXT,
    ADD COLUMN IF NOT EXISTS sample_item INTEGER,
    ADD COLUMN IF NOT EXISTS weight      DOUBLE PRECISION;

ALTER TABLE ops.dq_label
    DROP CONSTRAINT IF EXISTS ck_dq_label_sample,
    ADD CONSTRAINT ck_dq_label_sample
        CHECK ((sample_item IS NULL OR sample_id IS NOT NULL)
               AND (weight IS NULL OR weight >= 1)),
    DROP CONSTRAINT IF EXISTS fk_dq_label_sample_item,
    ADD CONSTRAINT fk_dq_label_sample_item
        FOREIGN KEY (sample_id, sample_item)
        REFERENCES ops.dq_audit_item (cycle_id, item_no);

CREATE INDEX IF NOT EXISTS ix_dq_label_sample
    ON ops.dq_label (sample_id, sample_item) WHERE sample_id IS NOT NULL;

COMMENT ON COLUMN ops.dq_label.sample_id IS
    'The audit cycle the label was judged in (ops.dq_audit_cycle). NULL: not from '
    'an audit sample. Exported as dorq''s `sample`.';
COMMENT ON COLUMN ops.dq_label.sample_item IS
    'The audit unit the label judges (ops.dq_audit_item.item_no).';
COMMENT ON COLUMN ops.dq_label.weight IS
    'The label''s survey weight: 1 / the unit''s inclusion probability. NULL: 1. '
    'Exported as dorq''s `weight`.';

COMMIT;
