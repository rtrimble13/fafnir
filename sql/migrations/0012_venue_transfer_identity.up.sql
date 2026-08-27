-- 0012_venue_transfer_identity.up.sql
-- Stop a venue transfer from forking a company's identity.
--
-- The problem. 0003 made the security key (source, primary_symbol, exchange), and
-- 0009 scoped it to listed rows. That treats the *listing venue* as part of a
-- company's identity, which it is not: when a company moves from NYSE to NASDAQ it
-- is the same issuer, the same CUSIP and the same price history. But the key
-- changes, so the security-master upsert INSERTs:
--
--   * a second listed security_id appears for one ticker;
--   * core.symbol_xref's row for that ticker is REPOINTED to the new, empty row
--     (upsert_symbol_xref's ON CONFLICT updates the open period), so the ticker
--     now resolves to a security with no bars -- `duk ph ABC` returns nothing
--     while years of history sit unreachable on the old security_id;
--   * the new row has no watermark, so the next price run re-downloads the whole
--     history into it, duplicating what the warehouse already had;
--   * the old row keeps is_actively_trading = TRUE, so it is polled forever.
--
-- This was latent while the security master ran once, at install. 0011 put it in
-- the nightly job, so every venue transfer now trips it automatically.
--
-- The fix is to key a listed security on (source, primary_symbol) and let the
-- exchange be what it actually is: a mutable attribute of the listing. A US ticker
-- is unique across the national market system -- NYSE, NASDAQ, AMEX, BATS and CBOE
-- do not assign one symbol to two issuers -- and FMP namespaces foreign venues with
-- a suffix (2958.HK, SAP.DE), so the symbol alone is already unambiguous in every
-- universe this warehouse loads. A transfer therefore UPDATEs the security that
-- already holds the history, exactly as a rename does since 0011.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Repair databases that already forked. The new index cannot be created while
--    two listed rows share (source, primary_symbol).
--
--    Same rule the rename sweep applies (repository.fold_empty_security): a
--    duplicate carrying no bars, no actions and no factors is a stub with nothing
--    to lose and is folded into the row that has the history. A duplicate that
--    *does* carry history is not merged silently -- that is a decision for a human,
--    so the migration stops and names both ids.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    grp      RECORD;
    survivor BIGINT;
    victim   BIGINT;
BEGIN
    FOR grp IN
        SELECT source, primary_symbol
          FROM core.security
         WHERE delisted_date IS NULL
         GROUP BY source, primary_symbol
        HAVING count(*) > 1
    LOOP
        -- The row with the most price history is the company; ties go to the
        -- oldest id, which is the one that was there first.
        SELECT s.security_id INTO survivor
          FROM core.security s
         WHERE s.delisted_date IS NULL
           AND s.source = grp.source
           AND s.primary_symbol = grp.primary_symbol
         ORDER BY (SELECT count(*) FROM core.daily_price p
                    WHERE p.security_id = s.security_id) DESC,
                  s.security_id ASC
         LIMIT 1;

        FOR victim IN
            SELECT s.security_id
              FROM core.security s
             WHERE s.delisted_date IS NULL
               AND s.source = grp.source
               AND s.primary_symbol = grp.primary_symbol
               AND s.security_id <> survivor
        LOOP
            IF EXISTS (SELECT 1 FROM core.daily_price       WHERE security_id = victim)
            OR EXISTS (SELECT 1 FROM core.corporate_action  WHERE security_id = victim)
            OR EXISTS (SELECT 1 FROM core.adjustment_factor WHERE security_id = victim)
            THEN
                RAISE EXCEPTION
                    'core.security holds two listed rows for %/% (security_id % and %), '
                    'and both carry history. Merging two price histories is not '
                    'something this migration will do silently: decide which is the '
                    'company, move or delete the other row''s bars, then re-run '
                    '`fafnir db migrate`.',
                    grp.source, grp.primary_symbol, survivor, victim;
            END IF;

            UPDATE ops.data_quality_flag SET security_id = survivor WHERE security_id = victim;
            DELETE FROM ops.load_watermark  WHERE security_id = victim;
            UPDATE core.symbol_change SET security_id = survivor WHERE security_id = victim;
            DELETE FROM core.symbol_xref    WHERE security_id = victim;
            DELETE FROM core.company_profile WHERE security_id = victim;
            DELETE FROM core.security       WHERE security_id = victim;

            RAISE NOTICE 'venue-transfer repair: folded empty security % into % (%/%)',
                victim, survivor, grp.source, grp.primary_symbol;
        END LOOP;
    END LOOP;
END
$$;

-- The fold above deletes the victim's xref rows, and the victim may have been
-- holding the ticker's only open period (that is the repointing bug). Give any
-- listed security left without an open period for its own ticker one back, so the
-- symbol resolves to the row that owns the history.
INSERT INTO core.symbol_xref (security_id, symbol, valid_from, is_primary, source)
SELECT s.security_id, s.primary_symbol, DATE '1900-01-01', TRUE, s.source
  FROM core.security s
 WHERE s.delisted_date IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM core.symbol_xref x
         WHERE x.security_id = s.security_id
           AND x.symbol = s.primary_symbol
           AND x.valid_to IS NULL)
ON CONFLICT (symbol, valid_from) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Swap the identity key.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS core.ux_security_active_source_symbol_exchange;

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_active_source_symbol
    ON core.security (source, primary_symbol)
    WHERE delisted_date IS NULL;

COMMENT ON INDEX core.ux_security_active_source_symbol IS
    'At most one LISTED security per (source, symbol). Delisted rows are excluded so '
    'a reused ticker mints a new security_id (0009); the exchange is deliberately NOT '
    'part of the key, so a venue transfer updates the security that holds the history '
    'instead of forking it (0012).';

COMMENT ON COLUMN core.security.exchange_code IS
    'Current listing venue. A mutable attribute, NOT part of identity: a company that '
    'transfers venue keeps its security_id, its history and its watermark.';

COMMIT;
