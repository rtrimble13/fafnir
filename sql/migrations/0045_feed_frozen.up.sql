-- 0045_feed_frozen.up.sql
-- A security nothing feeds is frozen, not stale (ADR 0020).
--
-- When [general] fmp_secondary is false, the FMP loaders no longer run, and the
-- securities only FMP fed (repo.fmp_fed: the mutual funds, and the names with no
-- Sharadar link) take no new bar. They have not stopped trading -- the market
-- prices them every day -- so is_actively_trading stays true, and delisting them
-- would be a lie the delisting sweep is built to refuse. What has stopped is the
-- feed. The freshness check (`stale`) measures a feed that should be reaching a
-- security and is not; it has nothing to measure here, and without this marker it
-- writes one stale flag per frozen security the third session after the last bar:
-- 3,937 on the production warehouse.
--
-- feed_frozen_on is the last session the warehouse holds a bar for when the feed
-- stopped: the day its history ends. feed_frozen_note says why, so the marker
-- carries its own evidence. `fafnir security freeze --unfed` stamps them;
-- `security thaw` clears them, and so does a feeding link to another vendor
-- (`vendor feed`, `vendor link accept`), because a fed security is not frozen.
--
-- mart.v_security_profile gains the two columns at the end (CREATE OR REPLACE VIEW
-- may append). mart.security_latest is not rebuilt: duk's screen joins
-- core.security for the marker instead, so this migration touches no snapshot.
--
-- Columns on an existing table: the grants on core.security cover them.
-- Nothing here changes a row that exists.

BEGIN;

ALTER TABLE core.security
    ADD COLUMN IF NOT EXISTS feed_frozen_on   DATE,
    ADD COLUMN IF NOT EXISTS feed_frozen_note TEXT;

ALTER TABLE core.security
    DROP CONSTRAINT IF EXISTS ck_security_feed_frozen,
    ADD CONSTRAINT ck_security_feed_frozen
        CHECK ((feed_frozen_on IS NULL) = (feed_frozen_note IS NULL)
               AND (feed_frozen_note IS NULL OR btrim(feed_frozen_note) <> ''));

-- The frozen set is small against the master; every reader asks "is it frozen".
CREATE INDEX IF NOT EXISTS ix_security_feed_frozen
    ON core.security (security_id) WHERE feed_frozen_on IS NOT NULL;

COMMENT ON COLUMN core.security.feed_frozen_on IS
    'Set when no vendor feeds this security any more (ADR 0020): the last session '
    'it holds a bar for. It still trades; the warehouse stops at this date. The '
    'freshness check skips it. Cleared by `security thaw` or a new feeding link.';
COMMENT ON COLUMN core.security.feed_frozen_note IS
    'Why the feed stopped. Set with feed_frozen_on, never alone.';

-- mart.v_security_profile: the 0039 definition, with the marker appended.
CREATE OR REPLACE VIEW mart.v_security_profile AS
SELECT
    s.security_id,
    s.primary_symbol            AS symbol,
    s.company_name,
    s.asset_type,
    s.exchange_code,
    x.exchange_name,
    sec.sector_name,
    ind.industry_name,
    s.currency,
    s.country,
    s.cik,
    s.isin,
    s.cusip,
    s.is_actively_trading,
    s.is_etf,
    s.is_fund,
    s.ipo_date,
    s.delisted_date,
    s.market_cap_usd,
    s.beta,
    s.source,
    s.first_seen_at,
    s.updated_at,
    cp.description,
    cp.loaded_at                AS description_loaded_at,
    s.feed_frozen_on,
    s.feed_frozen_note
FROM core.security s
LEFT JOIN ref.exchange      x   ON x.exchange_code = s.exchange_code
LEFT JOIN core.v_security_classification cl ON cl.security_id = s.security_id
LEFT JOIN ref.sector        sec ON sec.sector_id   = cl.sector_id
LEFT JOIN ref.industry      ind ON ind.industry_id = cl.industry_id
LEFT JOIN core.company_profile cp ON cp.security_id = s.security_id;

COMMENT ON VIEW mart.v_security_profile IS
    'Descriptive profile per security, read live. sector_name and industry_name are '
    'the feeding vendor''s (core.v_security_classification, #109). market_cap_usd and '
    'beta are a company-screener snapshot refreshed with the security master -- not '
    'history; mart.security_latest derives its own. feed_frozen_on marks a security '
    'no vendor feeds (ADR 0020): its history stops there while it still trades.';

COMMIT;
