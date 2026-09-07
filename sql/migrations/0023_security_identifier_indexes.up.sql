-- 0023_security_identifier_indexes.up.sql
-- Make "which security carries this CIK/ISIN/CUSIP?" an index lookup.
--
-- `duk ph cik:51143` and `duk ls cusip:459200101` resolve a security by an
-- identifier instead of a ticker. The predicates behind them compare NORMALISED
-- forms on both sides, because the vendor's spelling and the user's differ in ways
-- that carry no information: the SEC writes CIK 0000051143 and people type 51143,
-- and an ISIN or CUSIP arrives in whatever case and grouping it was copied from.
-- duk.identifiers normalises what was typed; these expressions normalise what is
-- stored.
--
-- Normalising the stored side is exactly what makes a plain index on `cik` useless
-- here, so these are EXPRESSION indexes and they must match
-- duk/datasource/db.py's identifier SQL character for character -- an index on
-- ltrim(btrim(cik),'0') does not serve a query written ltrim(cik,'0'). Change one
-- and change the other. (test_identifiers.py::test_migration_indexes_match_the_sql
-- fails when they drift.)
--
-- Every function used is IMMUTABLE, which index expressions require: btrim, ltrim,
-- upper, left and substring over (text, int, int) all are.
--
-- Why index at all, when the name search two rungs above happily sequential-scans
-- the same ~21k rows? Because these are equality lookups on a key, which is what
-- an index is for, and because they are the resolution step of every subsequent
-- query -- unlike the name search, which IS the answer. The three indexes cost a
-- few hundred KB on a 21k-row table.
--
-- The two FALLBACK rungs are deliberately unindexed: an 8-character CUSIP
-- (left(...,8)) and the CUSIP embedded in a North American ISIN are only reached
-- when the direct match found nothing, so they cost a sequential scan on the rare
-- lookup that needs them rather than a third and fourth index on every write.
--
-- The indexes are partial (WHERE <col> IS NOT NULL) and still serve a query that
-- does not say so: the predicate is strict in the column, from which PostgreSQL
-- proves IS NOT NULL. That keeps out the majority of rows -- funds and most
-- non-US listings carry none of the three.
--
-- Deliberately NOT unique. Two rows legitimately share a CIK (one issuer, several
-- listed share classes), and a venue transfer can leave the same CUSIP on more
-- than one row for as long as it takes the nightly reconciliation to merge them
-- (migration 0012). The read path returns candidates and asks; the schema does not
-- get to decide the vendor is wrong.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_security_cik_normalised
    ON core.security ((ltrim(btrim(cik), '0')))
    WHERE cik IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_security_isin_normalised
    ON core.security ((upper(btrim(isin))))
    WHERE isin IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_security_cusip_normalised
    ON core.security ((upper(btrim(cusip))))
    WHERE cusip IS NOT NULL;

COMMENT ON INDEX core.ix_security_cik_normalised IS
    'Serves duk''s cik: lookup. Expression must match duk/datasource/db.py exactly.';
COMMENT ON INDEX core.ix_security_isin_normalised IS
    'Serves duk''s isin: lookup. Expression must match duk/datasource/db.py exactly.';
COMMENT ON INDEX core.ix_security_cusip_normalised IS
    'Serves duk''s cusip: lookup. Expression must match duk/datasource/db.py exactly.';

COMMIT;
