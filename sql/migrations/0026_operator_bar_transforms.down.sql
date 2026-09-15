-- 0026_operator_bar_transforms.down.sql
-- Reverse of 0026: bars may only be deleted again.
--
-- Refuses while ANY daily_price 'add' override exists, active or revoked. The old
-- constraint cannot be restored over them, and the two ways around that both destroy
-- something this table exists to keep: dropping the records erases who changed which
-- bars and why, and leaving an active one without its rule would leave operator bars
-- the price loader no longer protects. Undo the edits first:
--
--   fafnir override list --json      # find the shift/rescale edits
--   fafnir override revoke <id> -m "rolling back 0026"
--
-- and, if the records of revoked edits are not worth keeping, save and delete them:
--
--   psql -c "\copy (SELECT * FROM ops.operator_override WHERE target = 'daily_price'
--            AND operation = 'add') TO 'bar_transforms.csv' CSV HEADER"
--   psql -c "DELETE FROM ops.operator_override WHERE target = 'daily_price'
--            AND operation = 'add' AND revoked_at IS NOT NULL"

BEGIN;

DO $$
DECLARE
    n_active  bigint;
    n_revoked bigint;
BEGIN
    SELECT count(*) FILTER (WHERE revoked_at IS NULL),
           count(*) FILTER (WHERE revoked_at IS NOT NULL)
      INTO n_active, n_revoked
      FROM ops.operator_override
     WHERE target = 'daily_price' AND operation = 'add';
    IF n_active + n_revoked > 0 THEN
        RAISE EXCEPTION
            'cannot roll back 0026: % active and % revoked bar transform override(s) '
            'exist. Revoke the active ones with `fafnir override revoke`, then save '
            'and delete the records (see this file''s header).', n_active, n_revoked;
    END IF;
END
$$;

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_price_delete_or_transform;

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_price_delete_only;

ALTER TABLE ops.operator_override
    ADD CONSTRAINT ck_operator_override_price_delete_only
        CHECK (target <> 'daily_price' OR operation = 'delete');

COMMENT ON TABLE ops.operator_override IS
    'Operator corrections to vendor corporate actions and bars, and the keys the '
    'loaders must leave alone because of them. Grain: override_id. Active = '
    'revoked_at IS NULL. Written by `fafnir actions add|delete|redate` and '
    '`fafnir prices delete`; undone by `fafnir override revoke`.';
COMMENT ON COLUMN ops.operator_override.operation IS
    'delete = the row was removed and the loaders skip its key while active. '
    'add = the operator wrote the row (source = operator); the loaders do not '
    'overwrite it and the reconciliation does not report it as withdrawn.';
COMMENT ON COLUMN ops.operator_override.detail IS
    'delete: the row as it stood when removed. add: the values written. A re-date '
    'pair names the other half (redated_to / redated_from, override ids).';
COMMENT ON COLUMN core.daily_price.source IS NULL;

COMMIT;
