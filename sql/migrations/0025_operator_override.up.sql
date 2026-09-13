-- 0025_operator_override.up.sql
-- Give an operator a way to correct a corporate action or a bar the vendor has
-- wrong, and to make the correction survive the next load.
--
-- Until now the warehouse had no write path for either. The loaders upsert and
-- never delete (the one exception is a re-dated dividend, see
-- fafnir.ingest.corporate_actions._redated_dividends), so every repair the DQ
-- playbooks call for on these rows -- delete a duplicate split, correct a
-- misdated ex-date, add a split the feed never reported, drop a bar dated on a
-- non-session day -- had to be done by hand in SQL as the owning role. On the
-- production warehouse in 2026-09 that left ~200 outlier flags and the adjusted
-- series of DAMD, STSM, KEEX and LNOK waiting on a command that did not exist.
--
-- A hand-written DELETE is also not durable. The feed that produced the row still
-- carries it, so the next calendar sweep or reconciliation re-inserts it, with no
-- flag, and the adjusted series silently reverts. This table is what the loaders
-- consult to stop that, and it is the audit record of who changed what and why.
--
-- One row per edit
-- ----------------
--   operation = 'delete'  the row at this key was removed. While the override is
--                         active the loaders skip the key: a vendor row for it is
--                         set aside instead of upserted. `detail` holds the row as
--                         it stood, so the removal can be read back exactly.
--   operation = 'add'     the operator wrote the row at this key (a corporate
--                         action with source = 'operator'). The loaders do not
--                         overwrite it and the reconciliation does not report it
--                         as withdrawn by the feed. `detail` holds the values.
--
-- A re-date is a 'delete' at the old ex-date plus an 'add' at the new one; each
-- names the other in `detail`.
--
-- `revoked_*` undoes an edit without erasing its record: a revoked 'delete' stops
-- suppressing its key (the next load brings back whatever the vendor serves), a
-- revoked 'add' removes the operator's row.
--
-- No foreign key to core.security, deliberately: fold_empty_security and the merge
-- commands delete security rows, and an override must not block that or vanish with
-- it. merge_security retargets a victim's overrides onto the survivor.

BEGIN;

CREATE TABLE IF NOT EXISTS ops.operator_override (
    override_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id    BIGINT NOT NULL,
    target         TEXT   NOT NULL,
    action_type    TEXT,
    key_date       DATE   NOT NULL,
    operation      TEXT   NOT NULL,
    detail         JSONB  NOT NULL DEFAULT '{}'::jsonb,
    note           TEXT   NOT NULL,
    created_by     TEXT   NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ,
    revoked_by     TEXT,
    revoked_note   TEXT,
    CONSTRAINT ck_operator_override_target
        CHECK (target IN ('corporate_action', 'daily_price')),
    CONSTRAINT ck_operator_override_operation
        CHECK (operation IN ('delete', 'add')),
    -- A corporate action is keyed on its type as well as its date; a bar is not.
    CONSTRAINT ck_operator_override_action_type CHECK (
        (target = 'corporate_action' AND action_type IN ('split', 'dividend'))
        OR (target = 'daily_price' AND action_type IS NULL)
    ),
    -- Bars are only ever removed: a price the operator typed in is not a price.
    CONSTRAINT ck_operator_override_price_delete_only
        CHECK (target <> 'daily_price' OR operation = 'delete'),
    CONSTRAINT ck_operator_override_note CHECK (btrim(note) <> ''),
    CONSTRAINT ck_operator_override_revoked CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revoked_note IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
    )
);

COMMENT ON TABLE ops.operator_override IS
    'Operator corrections to vendor corporate actions and bars, and the keys the '
    'loaders must leave alone because of them. Grain: override_id. Active = '
    'revoked_at IS NULL. Written by `fafnir actions add|delete|redate` and '
    '`fafnir prices delete`; undone by `fafnir override revoke`.';
COMMENT ON COLUMN ops.operator_override.key_date IS
    'ex_date for a corporate action, trade_date for a bar.';
COMMENT ON COLUMN ops.operator_override.operation IS
    'delete = the row was removed and the loaders skip its key while active. '
    'add = the operator wrote the row (source = operator); the loaders do not '
    'overwrite it and the reconciliation does not report it as withdrawn.';
COMMENT ON COLUMN ops.operator_override.detail IS
    'delete: the row as it stood when removed. add: the values written. A re-date '
    'pair names the other half (redated_to / redated_from, override ids).';

-- One active edit of each kind per key. A 'delete' and an 'add' may share a key --
-- that is how a wrong vendor row is replaced with a corrected one -- but two active
-- suppressions of one key would make "revoke" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS ux_operator_override_active
    ON ops.operator_override
       (security_id, target, COALESCE(action_type, ''), key_date, operation)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_operator_override_security
    ON ops.operator_override (security_id);

COMMENT ON COLUMN core.corporate_action.source IS
    'Where the row came from: fmp for the vendor feed, operator for a row written by '
    '`fafnir actions add|redate` (see ops.operator_override). The loaders never '
    'overwrite an operator row.';

COMMIT;
