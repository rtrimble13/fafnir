-- 0011_symbol_change.up.sql
-- Make the universe self-maintaining: give ticker renames a place to land, and
-- make newly listed securities cheap to find.
--
-- The problem this fixes. Up to 0010 the security master was a *build* step, not
-- an upkeep step: `fafnir ingest securities` ran during the initial backfill and
-- never again, so an IPO after that day never entered scope. Worse, a rename was
-- indistinguishable from a new listing. When FB became META the screener returned
-- META, no active row matched (source, 'META', 'NASDAQ'), and the upsert minted a
-- *second* security_id -- leaving the company's price history stranded on the FB
-- row, which stayed is_actively_trading forever (a rename is not a delisting, so
-- the delisted sweep never touched it) and was re-polled nightly for bars that
-- will never come.
--
-- The fix is a reconciliation step that applies a rename to the security that
-- already exists: close the old ticker's period in core.symbol_xref, open one for
-- the new ticker against the SAME security_id, and move primary_symbol across.
-- Identity, price history, corporate actions and the incremental watermark all
-- stay on one security_id, which is exactly what core.symbol_xref was designed
-- for (doc/adr/0002).
--
-- core.symbol_change is the audit trail of that step. It exists so the nightly
-- sweep is idempotent (a rename already applied is skipped, not re-applied
-- against whatever ticker now sits there) and so a rename that could NOT be
-- applied automatically is durable evidence rather than a line in a log file.

BEGIN;

CREATE TABLE IF NOT EXISTS core.symbol_change (
    symbol_change_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    old_symbol        TEXT NOT NULL,
    new_symbol        TEXT NOT NULL,
    change_date       DATE NOT NULL,
    -- The security the rename was applied to. NULL when it could not be applied.
    security_id       BIGINT REFERENCES core.security (security_id),
    company_name      TEXT,
    status            TEXT NOT NULL
                        CHECK (status IN ('applied', 'conflict', 'ignored')),
    detail            JSONB,
    source            TEXT NOT NULL DEFAULT 'fmp',
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (old_symbol <> new_symbol)
);

COMMENT ON TABLE core.symbol_change IS
    'Ticker renames observed from the source and what fafnir did with each. '
    'Grain: one row per (source, old_symbol, new_symbol, change_date).';
COMMENT ON COLUMN core.symbol_change.status IS
    'applied  = the rename was carried onto an existing security_id (terminal). '
    'conflict = the new ticker already belongs to a different security that '
    'carries history, so a human must decide (retried each sweep). '
    'ignored  = nothing to do: the old ticker belongs to a delisted issuer, so '
    'this is ticker REUSE, not a rename (terminal).';
COMMENT ON COLUMN core.symbol_change.security_id IS
    'Security the rename was applied to; NULL while unapplied. A renamed security '
    'keeps its id -- that is the point of the step.';

-- Idempotency key for the nightly sweep: the same feed row is re-read every night
-- (the tail is small and cheap), and an already-applied rename must be recognized
-- rather than re-applied against whatever ticker currently sits there.
CREATE UNIQUE INDEX IF NOT EXISTS ux_symbol_change_natural
    ON core.symbol_change (source, old_symbol, new_symbol, change_date);

-- Research access: "what was this company called before?" / "what happened to
-- this ticker?" -- both directions, without a scan.
CREATE INDEX IF NOT EXISTS ix_symbol_change_old ON core.symbol_change (old_symbol);
CREATE INDEX IF NOT EXISTS ix_symbol_change_new ON core.symbol_change (new_symbol);
-- Unapplied renames are the review queue; keep them cheap to list.
CREATE INDEX IF NOT EXISTS ix_symbol_change_unapplied
    ON core.symbol_change (change_date DESC) WHERE status <> 'applied';

-- New-listing reporting ("N securities entered scope last night") reads the tail
-- of first_seen_at on every nightly run, over a 20k+ row table.
CREATE INDEX IF NOT EXISTS ix_security_first_seen
    ON core.security (first_seen_at DESC);

COMMIT;
