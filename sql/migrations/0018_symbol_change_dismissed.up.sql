-- 0018_symbol_change_dismissed.up.sql
-- Give a reported rename that is NOT a rename a way to leave the queue.
--
-- 0011 gave `core.symbol_change` three statuses, two of them terminal: `applied`
-- and `ignored` are decisions, `conflict` is a retry. That is the right shape for
-- a conflict a human can act on -- the sweep re-tries it every night, and the day
-- the obstruction clears it applies itself. It has no shape at all for a rename
-- that is simply wrong.
--
-- The feed does emit wrong ones, and they are not rare. Two observed families:
--
--   * A pre-launch ticker assignment reported as a change. Five ETFs minted
--     together on 2026-08-04 arrived with a mutual "rename" dated 2026-08-03 --
--     the day before any of them had a single bar, and all five kept trading
--     concurrently afterwards. Nothing renamed; the issuer shuffled tickers before
--     listing and the vendor recorded the shuffle.
--   * The same rename emitted in both directions. VBX -> USSX dated 2026-08-21 and
--     USSX -> VBX dated 2026-08-20, both securities still printing a week later.
--
-- Neither can ever reach a terminal status on its own: `apply_symbol_change` finds
-- the target ticker held by a live security with history, refuses (correctly --
-- merging two price histories is not a loader's call), and writes `conflict`
-- again. Seven of the fourteen open `symbol_change_conflict` flags on the
-- production warehouse were of this kind, re-conflicting nightly, in a queue whose
-- whole value is that an `error` in it means something.
--
-- `dismissed` is the operator saying "this rename is not real, stop trying". It is
-- terminal, so the sweep skips the row (TERMINAL_CHANGE_STATUSES) and
-- `record_symbol_change` cannot downgrade it back to `conflict` on the next pass.
-- It is deliberately NOT a way to close a rename that IS real but inconvenient:
-- that one needs `fafnir security merge-rename`, which reaches `applied`.
--
-- Provenance goes in the existing `detail` JSONB (`dismissed_by`, `dismissed_note`,
-- `dismissed_at`) rather than in new columns. Unlike 0017's resolution provenance,
-- which had to be queryable across a 900k-row queue, this table holds a handful of
-- dismissals and every reader of them is already reading `detail`.

BEGIN;

-- The 0011 constraint is an inline column CHECK, so its name is whatever
-- PostgreSQL generated. Find it by its definition rather than trusting the
-- generated name, and skip the replacement this migration itself installs so a
-- re-run is a no-op instead of a drop/re-add cycle.
DO $$
DECLARE cname text;
BEGIN
    SELECT con.conname INTO cname
      FROM pg_constraint con
      JOIN pg_class     c ON c.oid = con.conrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'core'
       AND c.relname = 'symbol_change'
       AND con.contype = 'c'
       AND con.conname <> 'ck_symbol_change_status'
       AND pg_get_constraintdef(con.oid) LIKE '%status%'
       AND pg_get_constraintdef(con.oid) LIKE '%conflict%';
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE core.symbol_change DROP CONSTRAINT %I', cname);
    END IF;
END $$;

ALTER TABLE core.symbol_change
    DROP CONSTRAINT IF EXISTS ck_symbol_change_status;
ALTER TABLE core.symbol_change
    ADD CONSTRAINT ck_symbol_change_status
    CHECK (status IN ('applied', 'conflict', 'ignored', 'dismissed'));

COMMENT ON COLUMN core.symbol_change.status IS
    'applied   = the rename was carried onto an existing security_id (terminal). '
    'conflict  = the new ticker already belongs to a different security that '
    'carries history, so a human must decide (retried each sweep). '
    'ignored   = nothing to do: the old ticker belongs to a delisted issuer, so '
    'this is ticker REUSE, not a rename (terminal). '
    'dismissed = an operator judged the reported rename not to be a rename at all '
    '(bad feed row); the sweep stops retrying it (terminal). Provenance is in '
    'detail->>dismissed_by / dismissed_note / dismissed_at.';

-- ix_symbol_change_unapplied (0011) is partial on `status <> 'applied'`, so it
-- still indexes dismissed rows. That is correct and wants no change: the index
-- serves "renames that did not apply", and a dismissal is one of those. The review
-- queue itself (count_unapplied_symbol_changes) filters on status = 'conflict'
-- exactly, so dismissed rows leave it without any index change.

COMMIT;
