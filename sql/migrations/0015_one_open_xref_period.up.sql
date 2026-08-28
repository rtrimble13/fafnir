-- 0015_one_open_xref_period.up.sql
-- Collapse duplicate open ticker periods, then make a second one impossible.
--
-- The invariant core.symbol_xref has always assumed but never enforced: a ticker
-- serves at most ONE security at a time. XREF_RESOLVE_SQL reads only open periods
-- and takes the first by (is_primary DESC, valid_from DESC), so a second open period
-- for the same symbol is not a second answer -- it is a row the resolver silently
-- ignores while point-in-time queries still read it.
--
-- They exist. upsert_symbol_xref(valid_from=NULL) resolved "the period this ticker
-- serves" from CLOSED periods only, so a ticker whose open period does not start at
-- the '1900-01-01' fallback missed the (symbol, valid_from) arbiter and INSERTed a
-- second open row instead of updating the first. That is every ticker
-- retarget_symbol has just renamed -- it opens the new ticker's period at the change
-- date -- and `ingest symbol-changes` and `ingest securities` run back to back every
-- night. One rename plus one nightly run was enough:
--
--     FB     1900-01-01 -> 2024-06-09   security 1
--     META   1900-01-01 -> (open)       security 1   <- spurious, claims 1900
--     META   2024-06-10 -> (open)       security 1
--
-- erasing the rename boundary 0011 exists to record. The write path is fixed; this
-- repairs the warehouses that ran with it and closes the hole in the schema.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The pure duplicates: same ticker, same security, more than one open period.
--    These carry nothing the surviving row does not -- same security_id, and a
--    valid_from the resolver was already ignoring -- and what they do carry is
--    false: a claim on dates when the ticker belonged to someone else, or to
--    nobody. Deleted rather than closed, because closing one would PRESERVE that
--    false claim as history (META valid from 1900, when it was still FB) where
--    point-in-time queries would find it.
-- ---------------------------------------------------------------------------
WITH ranked AS (
    SELECT symbol, valid_from, security_id,
           first_value(valid_from)  OVER w AS keep_from,
           first_value(security_id) OVER w AS keep_security_id,
           count(*) OVER (PARTITION BY symbol) AS open_periods
      FROM core.symbol_xref
     WHERE valid_to IS NULL
    -- Exactly XREF_RESOLVE_SQL's ordering (and active_security_for_symbol's), so
    -- the row kept open is the row resolution already answers with. Ranking on
    -- valid_from alone would keep a later non-primary period instead and silently
    -- move the ticker to a different security_id.
    WINDOW w AS (PARTITION BY symbol ORDER BY is_primary DESC, valid_from DESC)
)
DELETE FROM core.symbol_xref x
 USING ranked r
 WHERE x.symbol     = r.symbol
   AND x.valid_from = r.valid_from      -- (symbol, valid_from) is the primary key
   AND x.valid_to IS NULL
   AND r.open_periods > 1
   AND r.valid_from  <> r.keep_from     -- never the row the resolver answers with
   AND r.security_id  = r.keep_security_id;

-- ---------------------------------------------------------------------------
-- 2. Anything still doubled is two DIFFERENT securities holding one ticker, which
--    is not this bug's signature and is not ours to delete -- a security_id is an
--    identity and the row is the record that it held the ticker. Closed instead,
--    exactly as retarget_symbol and mark_delisted close a period they supersede.
--
--    Closed the day before whichever comes first: the surviving period, or the next
--    superseded period along. With two rows those are the same date. With three or
--    more they are not, and closing them all against the survivor would produce
--    OVERLAPPING closed periods -- 1900-2023 and 2020-2023 both claiming 2020-2023 --
--    breaking the contiguity the rest of the table maintains. LEAST ignores a NULL
--    lead (nothing follows), so a period after the survivor still closes against it.
--
--    Resolution does not change: the period left open is the one the resolver
--    already answered with (same ordering, above). What changes is that a superseded
--    claim is bounded instead of open-ended. GREATEST covers a period that starts
--    on or after the survivor's: it closes on its own start date rather than
--    violating ck valid_to >= valid_from.
-- ---------------------------------------------------------------------------
WITH ranked AS (
    SELECT symbol, valid_from,
           first_value(valid_from) OVER w AS keep_from,
           lead(valid_from) OVER (PARTITION BY symbol ORDER BY valid_from) AS next_from,
           count(*) OVER (PARTITION BY symbol) AS open_periods
      FROM core.symbol_xref
     WHERE valid_to IS NULL
    -- Exactly XREF_RESOLVE_SQL's ordering (and active_security_for_symbol's), so
    -- the row kept open is the row resolution already answers with. Ranking on
    -- valid_from alone would keep a later non-primary period instead and silently
    -- move the ticker to a different security_id.
    WINDOW w AS (PARTITION BY symbol ORDER BY is_primary DESC, valid_from DESC)
)
UPDATE core.symbol_xref x
   SET valid_to = GREATEST(x.valid_from, LEAST(r.keep_from, r.next_from) - 1)
  FROM ranked r
 WHERE x.symbol     = r.symbol
   AND x.valid_from = r.valid_from
   AND x.valid_to IS NULL
   AND r.open_periods > 1
   AND r.valid_from <> r.keep_from;

-- ---------------------------------------------------------------------------
-- 3. Now it cannot come back. Unlike the DQ-flag queue -- where a redundant row is
--    noise and a constraint would turn it into an exception that aborts a nightly
--    load -- a second open period here is genuine corruption: the ticker resolves
--    to one security while another row says it belongs to a different one. Every
--    write goes through upsert_symbol_xref or retarget_symbol, both of which now
--    maintain this, so the constraint guards against a future call site rather
--    than against the loaders as they stand.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_symbol_xref_open
    ON core.symbol_xref (symbol) WHERE valid_to IS NULL;

COMMENT ON INDEX core.ux_symbol_xref_open IS
    'A ticker serves at most one security at a time. XREF_RESOLVE_SQL reads only '
    'open periods, so a second one is a row it ignores while point-in-time queries '
    'still read it (migration 0015).';

COMMIT;
