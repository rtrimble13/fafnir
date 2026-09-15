-- 0026_operator_bar_transforms.up.sql
-- Let an operator re-date or re-scale a block of bars the vendor has wrong, and make
-- the correction survive the next load.
--
-- 0025 allowed a bar to be deleted and nothing else: "a price the operator typed in
-- is not a price". That stands. What it left without a repair are histories whose
-- every bar is a real price stored under the wrong key or at the wrong scale, where
-- deleting them throws the history away and a re-fetch returns the same thing:
--
--   * FVI and WLL (production, 2026-09): the whole history is dated one calendar day
--     early -- no Friday bars, and each Sunday bar is Monday's session. FMP's payload
--     carries the same dates, and `prices delete --non-session` would delete every
--     real Monday.
--   * EQC before 1997-10-17 is stored at 1/20 of the traded price (volume ~25x), and
--     AKR, HUN and JKL carry pre-split eras multiplied by a split the vendor applied
--     backwards -- in some cases next to a split row fabricated to match.
--
-- A bar written by `fafnir prices shift` or `fafnir prices rescale` is therefore not
-- typed in: it is a vendor bar moved to another date, or multiplied by a factor the
-- operator states, and the override that writes it records that transform and the
-- vendor bar it was derived from. Each transformed bar is the same pair a re-dated
-- corporate action is -- a 'delete' of the vendor's row (its row as it stood, so the
-- edit can be undone exactly) and an 'add' of the operator's -- and both halves carry
-- `detail.transform`, naming the kind, the parameters, the other half and the edit
-- (the batch) they belong to.
--
-- Loaders: a vendor bar on any key with an active daily_price override, of either
-- operation, is set aside. For a 'delete' that keeps a removed bar out; for an 'add'
-- it keeps the operator's bar from being overwritten by the vendor's copy of that
-- date (see fafnir.db.repository.suppressed_price_dates).

BEGIN;

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_price_delete_only;

ALTER TABLE ops.operator_override
    DROP CONSTRAINT IF EXISTS ck_operator_override_price_delete_or_transform;

-- A bar may be deleted, or added as a recorded transform of a vendor bar. Never a
-- bare add: the check is on the record, so a hand-written INSERT without the
-- transform is refused by the table, not only by the command.
--
-- The transform must name its `edit`, because that is what makes the record
-- undoable: `revoke_price_edit` finds an edit's halves by it, and a bar add without
-- one is a row the revoke path cannot resolve -- it would report the operator's bar
-- removed, delete nothing, and leave that bar in core.daily_price with no active
-- override recording or protecting it.
--
-- Written as `#> IS NOT NULL` rather than `detail->'transform' ? 'edit'`: `->` on a
-- missing key yields SQL NULL, `?` on NULL yields NULL, and a CHECK that evaluates to
-- NULL passes. The `#>` form is false for a missing key and for a NULL detail alike.
ALTER TABLE ops.operator_override
    ADD CONSTRAINT ck_operator_override_price_delete_or_transform CHECK (
        target <> 'daily_price'
        OR operation = 'delete'
        OR (operation = 'add' AND detail #> '{transform,edit}' IS NOT NULL)
    );

COMMENT ON TABLE ops.operator_override IS
    'Operator corrections to vendor corporate actions and bars, and the keys the '
    'loaders must leave alone because of them. Grain: override_id. Active = '
    'revoked_at IS NULL. Written by `fafnir actions add|delete|redate` and '
    '`fafnir prices delete|shift|rescale`; undone by `fafnir override revoke`.';
COMMENT ON COLUMN ops.operator_override.operation IS
    'delete = the row was removed and the loaders skip its key while active. '
    'add = the operator wrote the row (source = operator); the loaders do not '
    'overwrite it and the reconciliation does not report it as withdrawn. A bar is '
    'only ever added as a transform of a vendor bar (detail.transform), never typed in.';
COMMENT ON COLUMN ops.operator_override.detail IS
    'delete: the row as it stood when removed. add: the values written. A re-date '
    'pair names the other half (redated_to / redated_from, override ids). A bar '
    'shift or rescale carries transform = {kind, edit, days | price_factor + '
    'volume_factor, from_override | to_override}; edit is the override id that '
    'names the batch, and revoking any override of an edit revokes all of it.';
COMMENT ON COLUMN core.daily_price.source IS
    'fmp for a vendor bar; operator for a vendor bar an operator re-dated or re-scaled '
    '(`fafnir prices shift|rescale`, see ops.operator_override). The price loader never '
    'overwrites an operator bar.';

COMMIT;
