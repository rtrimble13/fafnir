-- 0028_labels_and_override_reasons.up.sql
-- Let the warehouse's decisions be read as labels that can judge dorq (v1.5.0).
--
-- `fafnir dq export-labels` turns the record of repairs and judgements into dorq's
-- label format, and `fafnir dq compare --labels` measures dorq's precision against
-- it during the shadow period. After three shadow nights it judged 17 of 350
-- reports, and of the 17, nine were scored on a repair made after dorq ran. Three
-- things the record could not say are added here.
--
-- 1. Why an override was made: ops.operator_override.reason
-- ---------------------------------------------------------
-- Every override has been read as a confirmed data error. Most are, but not all:
-- CELG-RI's 770 bars were deleted on 2026-10-14 because a contingent value right
-- does not belong in the universe, not because a bar was wrong, and a merge's
-- leftovers are bookkeeping. Only `data_error` overrides become labels.
--
--   data_error    the row was wrong (the default, and what every existing row was)
--   out_of_scope  the security does not belong; its data was removed, not repaired
--   cleanup       bookkeeping, such as a merge's leftovers
--
-- 2. How a flag was judged: ops.data_quality_flag.judgement_basis
-- ---------------------------------------------------------------
-- 23,903 outlier acceptances of 2026-09-10/11 say in their note that they were
-- closed by rule, without looking at each bar. They are not the evidence a
-- per-security judgement is, and a label set that cannot tell them apart cannot be
-- filtered to what was checked.
--
--   per_flag   the closer looked at this flag's own evidence (`dq accept --per-flag`)
--   by_rule    closed as one of a class, by a rule (`--by-rule`)
--   NULL       not said
--
-- The backfill below marks the bulk acceptances by_rule from their notes.
--
-- 3. Labels nobody could derive: ops.dq_label
-- -------------------------------------------
-- A label is derived automatically only where the disposition is unambiguous: a
-- repair, an outlier accepted or resolved, a dorq flag accepted or resolved. An
-- accepted `gap` can mean "no trades that session" (a market fact) or "the vendor
-- has no bar and never will" (a data error), and acceptance alone cannot say which.
-- Those labels -- feed gaps, odd-lot-only staleness, real distributions, cohort
-- facts -- are stated by a person, with `fafnir dq label add|import`, and kept here.
-- A label changes no data and no flag.
--
-- Grants: none here. ALTER DEFAULT PRIVILEGES in 0001 and 0021 give fafnir_ingest
-- write and fafnir_ops read on every ops table the migrator creates, as they did
-- for ops.operator_override (0025).

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. ops.operator_override.reason
-- ---------------------------------------------------------------------------
ALTER TABLE ops.operator_override
    ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT 'data_error';

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_reason;
ALTER TABLE ops.operator_override
    ADD CONSTRAINT ck_operator_override_reason
    CHECK (reason IN ('data_error', 'out_of_scope', 'cleanup'));

COMMENT ON COLUMN ops.operator_override.reason IS
    'Why the edit was made. data_error: the row was wrong. out_of_scope: the '
    'security does not belong, so its data was removed rather than repaired. '
    'cleanup: bookkeeping. Only data_error overrides are exported as labels. '
    'Changed with `fafnir override set-reason`, which keeps the history in detail.';

-- ---------------------------------------------------------------------------
-- 2. ops.data_quality_flag.judgement_basis
-- ---------------------------------------------------------------------------
ALTER TABLE ops.data_quality_flag
    ADD COLUMN IF NOT EXISTS judgement_basis TEXT;

-- The closures of 2026-09-10/11 that say so in their own words. Matched on the
-- resolution and the acceptance notes alike: an acceptance writes both.
UPDATE ops.data_quality_flag
   SET judgement_basis = 'by_rule'
 WHERE resolved_at IS NOT NULL
   AND judgement_basis IS NULL
   AND (   resolution_note ILIKE '%without per-flag verification%'
        OR resolution_note ILIKE '%classified by rule%'
        OR resolution_note ILIKE '%not individually verified%'
        OR accepted_note   ILIKE '%without per-flag verification%'
        OR accepted_note   ILIKE '%classified by rule%'
        OR accepted_note   ILIKE '%not individually verified%');

ALTER TABLE ops.data_quality_flag
    DROP CONSTRAINT IF EXISTS ck_dq_flag_judgement_basis;
-- A basis belongs to a judgement, so only to a closed flag. `dq reopen` clears it
-- with the rest of the resolution.
ALTER TABLE ops.data_quality_flag
    ADD CONSTRAINT ck_dq_flag_judgement_basis
    CHECK (judgement_basis IS NULL
           OR (judgement_basis IN ('per_flag', 'by_rule') AND resolved_at IS NOT NULL));

COMMENT ON COLUMN ops.data_quality_flag.judgement_basis IS
    'How the closure was judged: per_flag (this flag''s own evidence was looked '
    'at), by_rule (closed as one of a class), or NULL (not said). Exported as a '
    'label''s `verified` field.';

-- ---------------------------------------------------------------------------
-- 3. ops.dq_label
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.dq_label (
    label_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- NULL for a cross-sectional label (dorq's series "*"): a cohort, or the market.
    security_id    BIGINT,
    -- For a cohort label: the securities it is about. A cohort fact then covers only
    -- the cross-sectional rows that name one of them, not every security.
    members        BIGINT[],
    first_date     DATE   NOT NULL,
    last_date      DATE   NOT NULL,
    class          TEXT   NOT NULL,
    kind           TEXT   NOT NULL,
    -- A fault's expected codes ("DQ202|DQ205"); a market fact's code prefix.
    expect         TEXT,
    codes          TEXT,
    note           TEXT   NOT NULL,
    verified       BOOLEAN NOT NULL DEFAULT false,
    source         TEXT   NOT NULL,
    decided_by     TEXT   NOT NULL,
    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- After this, the data dorq reads no longer shows what the label says (it was
    -- repaired). NULL: always applies.
    applies_until  TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    revoked_by     TEXT,
    revoked_note   TEXT,
    CONSTRAINT ck_dq_label_class
        CHECK (class IN ('data_error', 'context_gap', 'market_fact')),
    CONSTRAINT ck_dq_label_dates CHECK (last_date >= first_date),
    CONSTRAINT ck_dq_label_kind CHECK (btrim(kind) <> ''),
    CONSTRAINT ck_dq_label_note CHECK (btrim(note) <> ''),
    CONSTRAINT ck_dq_label_members
        CHECK (members IS NULL OR security_id IS NULL),
    CONSTRAINT ck_dq_label_revoked CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revoked_note IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
    )
);

COMMENT ON TABLE ops.dq_label IS
    'Labels a person stated: what was true about a stretch of one series (or a '
    'cohort), for measuring dorq. Grain: label_id. Active = revoked_at IS NULL. '
    'Written by `fafnir dq label add|import` and `dq accept|resolve --label`; '
    'merged into `dq export-labels`, where a row here wins over a derived label '
    'on the same security, dates and class. Changes no data and no flag.';
COMMENT ON COLUMN ops.dq_label.security_id IS
    'NULL for a cross-sectional label (dorq series "*").';
COMMENT ON COLUMN ops.dq_label.members IS
    'For a cohort label: the securities it is about.';
COMMENT ON COLUMN ops.dq_label.source IS
    'Where it came from, free text: dq_flag:<id>, hand-2026-10-15, ...';
COMMENT ON COLUMN ops.dq_label.applies_until IS
    'When the data stopped showing what the label says (a repair). A shadow night '
    'that started after it is not judged by this label. NULL: always applies.';

-- No foreign key to core.security, as for ops.operator_override (0025): a merge or
-- a purge must not be blocked by a label, nor take it with it.
CREATE INDEX IF NOT EXISTS ix_dq_label_security_first
    ON ops.dq_label (security_id, first_date);

COMMIT;
