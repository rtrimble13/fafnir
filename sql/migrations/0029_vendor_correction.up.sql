-- 0029_vendor_correction.up.sql
-- The audit record of every row one vendor replaced in place of another's
-- (ADR 0015 §3).
--
-- Until now one vendor wrote core.daily_price and core.corporate_action, and the
-- price upsert overwrote whatever it found, relabelling the row as its own. ADR 0015
-- replaces that with precedence (operator > sharadar > tiingo > fmp):
--
--   * a vendor revises its own rows;
--   * it never writes over a row a higher-ranked party holds (the key is skipped
--     and counted);
--   * it replaces a lower-ranked party's row only when the caller asks for a
--     correction explicitly, and every replaced row lands here first, as it stood,
--     in the same transaction.
--
-- So a correction is reviewable (what changed, from whom, in which run, why) and
-- reversible: `fafnir vendor corrections revert` writes `old_row` back. It is also
-- what makes removing Sharadar possible without rebuilding FMP's history: the FMP
-- rows it replaced are restored from here (ADR 0015 §5).
--
-- `old_row` holds the replaced vendor's data, so it is that vendor's to keep or
-- delete under its licence, like landing.fmp_raw.
--
-- No foreign key to core.security, deliberately, as for ops.operator_override:
-- the merge commands delete security rows, and a correction record must neither
-- block that nor vanish with it. merge_security retargets a victim's corrections
-- onto the survivor.

BEGIN;

CREATE TABLE IF NOT EXISTS ops.vendor_correction (
    correction_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id       BIGINT NOT NULL,
    target            TEXT   NOT NULL,
    action_type       TEXT,
    key_date          DATE   NOT NULL,
    old_source        TEXT   NOT NULL,
    new_source        TEXT   NOT NULL,
    old_row           JSONB  NOT NULL,
    new_row           JSONB  NOT NULL,
    reason            TEXT   NOT NULL,
    ingestion_run_id  BIGINT,
    corrected_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at       TIMESTAMPTZ,
    reverted_by       TEXT,
    revert_note       TEXT,
    CONSTRAINT ck_vendor_correction_target
        CHECK (target IN ('daily_price', 'corporate_action')),
    -- A corporate action is keyed on its type as well as its date; a bar is not.
    CONSTRAINT ck_vendor_correction_action_type CHECK (
        (target = 'corporate_action' AND action_type IN ('split', 'dividend'))
        OR (target = 'daily_price' AND action_type IS NULL)
    ),
    CONSTRAINT ck_vendor_correction_reason
        CHECK (reason IN ('reconcile_divergence', 'gap_fill', 'restatement')),
    -- A correction moves a key between two parties; an operator row is never
    -- replaced, and a vendor revising its own row is not a correction.
    CONSTRAINT ck_vendor_correction_parties CHECK (
        old_source <> new_source AND old_source <> 'operator'
    ),
    CONSTRAINT ck_vendor_correction_reverted CHECK (
        (reverted_at IS NULL AND reverted_by IS NULL AND revert_note IS NULL)
        OR (reverted_at IS NOT NULL AND reverted_by IS NOT NULL)
    )
);

COMMENT ON TABLE ops.vendor_correction IS
    'Every core.daily_price / core.corporate_action row a higher-ranked vendor '
    'replaced (ADR 0015 §3), as it stood before. Grain: correction_id. Active = '
    'reverted_at IS NULL. Written by the precedence write path (correct=True); '
    'undone by `fafnir vendor corrections revert`.';
COMMENT ON COLUMN ops.vendor_correction.key_date IS
    'trade_date for a bar, ex_date for a corporate action.';
COMMENT ON COLUMN ops.vendor_correction.old_row IS
    'The replaced row''s value columns as they stood; what a revert writes back.';
COMMENT ON COLUMN ops.vendor_correction.new_row IS
    'The values the correction wrote; a revert refuses if the row no longer holds '
    'them (a later load or correction changed it since).';
COMMENT ON COLUMN ops.vendor_correction.reason IS
    'reconcile_divergence: the two vendors disagreed and the category''s policy is '
    'to correct. gap_fill / restatement: reserved for the loaders that will use them.';

-- One active correction per key: a second correction of the same key while the
-- first is active would make "revert" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS ux_vendor_correction_active
    ON ops.vendor_correction
       (security_id, target, COALESCE(action_type, ''), key_date)
    WHERE reverted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_vendor_correction_security
    ON ops.vendor_correction (security_id);

COMMENT ON COLUMN core.daily_price.source IS
    'Who supplied the bar: fmp, sharadar, tiingo, or operator for a bar an operator '
    're-dated or re-scaled. Writes follow ADR 0015 precedence; a row another vendor '
    'replaced is kept in ops.vendor_correction.';
COMMENT ON COLUMN core.corporate_action.source IS
    'Who supplied the row: fmp, sharadar, tiingo, or operator for a row written by '
    '`fafnir actions add|redate` (see ops.operator_override). Writes follow ADR 0015 '
    'precedence: no vendor overwrites an operator row, and a row another vendor '
    'replaced is kept in ops.vendor_correction.';

COMMIT;
