-- 0027_security_venue_override.up.sql
-- Let an operator record that a security moved to another venue, and make it
-- survive the nightly security-master load (ADR 0014).
--
-- On 2026-10-05 six NYSE securities (ET, SUN, SUNC, USAC, DDS, DDT) moved their
-- primary listing to the Texas Stock Exchange (TXSE) and kept their tickers. FMP has
-- no venue code for TXSE: its profile and screener still name the NYSE, and call
-- each of them inactive while quoting today's prices. The nightly
-- `ingest securities` writes that view onto every listed row -- exchange_code by
-- COALESCE, is_actively_trading outright -- so a venue fafnir sets is overwritten
-- the next night, and the security drops out of the active universe.
--
-- `fafnir security venue-transfer` writes the venue here instead. While it is set,
-- the upsert keeps it and keeps the row's activity, until FMP reports a venue other
-- than the one the security left (venue_override_left): then FMP has caught up, and
-- the override clears itself.

BEGIN;

-- TXSE is fafnir's own code; FMP has none yet. The seed (seeds/0001_exchanges.sql)
-- creates it on a fresh install, and this creates it on a deployed one, so the
-- reference below can be satisfied without re-seeding.
INSERT INTO ref.exchange (exchange_code, exchange_name, country, timezone)
VALUES ('TXSE', 'Texas Stock Exchange', 'US', 'America/Chicago')
ON CONFLICT (exchange_code) DO NOTHING;

ALTER TABLE core.security
    ADD COLUMN IF NOT EXISTS venue_override TEXT
        REFERENCES ref.exchange (exchange_code),
    ADD COLUMN IF NOT EXISTS venue_override_left TEXT;

COMMENT ON COLUMN core.security.venue_override IS
    'Operator-recorded venue (fafnir security venue-transfer). While set, the '
    'nightly upsert keeps it as exchange_code, and keeps is_actively_trading, '
    'until the vendor reports a venue other than venue_override_left. ADR 0014.';
COMMENT ON COLUMN core.security.venue_override_left IS
    'The venue the security left when venue_override was recorded. The vendor '
    'still reporting it means the vendor has not caught up with the move.';

COMMIT;
