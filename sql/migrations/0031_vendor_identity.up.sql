-- 0031_vendor_identity.up.sql
-- Vendor identity: each vendor's own security master, the link from fafnir's
-- security_id to the vendor's key over time, and the vendor's lifecycle events
-- (plan SA-0306, #55; ADR 0015).
--
-- ADR 0015 keeps vendor attributes out of the columns FMP fills and makes every
-- vendor removable. So Sharadar's view of a security lives here, keyed by its own
-- stable id (permaticker), and never overwrites core.security. What joins the two
-- is core.security_vendor_xref: a dated link, made by a rule (#62) or a person
-- (#63), with its evidence.
--
-- The link also carries feed assignment (ADR 0015 §2): the Sharadar loaders feed a
-- security only through an open link whose is_feed is true, and a security has at
-- most one feeding vendor at a time.
--
-- What the paid feed showed (#49) shapes the keys:
--   * permaticker is the stable id, the same across a security's TICKERS rows
--     (stocks, fundamentals, insiders); vendor_table keeps those rows apart;
--   * SEP, SF1 and ACTIONS rows carry only the ticker, and history is filed under
--     the *current* ticker, so a loader resolves rows to permaticker through
--     TICKERS before writing here;
--   * table names are Sharadar's modern ones (stocks, not SEP), as in
--     landing.sharadar_file.
--
-- No mart view exposes these tables yet; a mart view is a grant (SA-0701, #79).
-- Read access comes from the default privileges: fafnir_read and fafnir_ops read
-- core; fafnir_app has no USAGE on core.
--
-- Empty on arrival: nothing here changes a row that exists.

BEGIN;

-- ---------------------------------------------------------------------------
-- core.vendor_security: the vendor's security master, as the vendor states it
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.vendor_security (
    vendor              TEXT NOT NULL,
    vendor_table        TEXT NOT NULL,
    vendor_key          TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    name                TEXT,
    exchange            TEXT,
    category            TEXT,
    is_delisted         BOOLEAN,
    cusips              TEXT[],
    figi                TEXT,
    cik                 TEXT,
    sic_code            TEXT,
    sector              TEXT,
    industry            TEXT,
    fama_industry       TEXT,
    currency            TEXT,
    location            TEXT,
    first_price_date    DATE,
    last_price_date     DATE,
    first_quarter       DATE,
    last_quarter        DATE,
    related_tickers     TEXT[],
    attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,
    vendor_last_updated DATE,
    ingestion_run_id    BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vendor, vendor_table, vendor_key),
    CONSTRAINT ck_vendor_security_vendor CHECK (vendor IN ('fmp', 'sharadar', 'tiingo')),
    CONSTRAINT ck_vendor_security_dates CHECK (
        first_price_date IS NULL OR last_price_date IS NULL
        OR last_price_date >= first_price_date
    )
);

COMMENT ON TABLE core.vendor_security IS
    'A vendor''s own security master, as the vendor states it (ADR 0015): one row per '
    'vendor, vendor table and vendor key. Never written into core.security. Grain: '
    '(vendor, vendor_table, vendor_key). Source: Sharadar TICKERS (#58). Cadence: the '
    'bulk load, then the nightly delta.';
COMMENT ON COLUMN core.vendor_security.vendor_table IS
    'The vendor''s table the row describes, by its modern name (stocks, funds, '
    'fundamentals, insiders): TICKERS has one row per table a security appears in.';
COMMENT ON COLUMN core.vendor_security.vendor_key IS
    'The vendor''s stable id for the security: Sharadar''s permaticker, as text.';
COMMENT ON COLUMN core.vendor_security.ticker IS
    'The vendor''s ticker for it today. A dead issuer''s reused ticker carries a '
    'numeric suffix (CLA1); history is filed under this ticker, not the one of the day.';
COMMENT ON COLUMN core.vendor_security.cusips IS
    'Every CUSIP the vendor lists (space-separated in TICKERS).';
COMMENT ON COLUMN core.vendor_security.cik IS
    'SEC CIK, when the vendor gives one (Sharadar: from the secfilings URL).';
COMMENT ON COLUMN core.vendor_security.first_quarter IS
    'First fiscal quarter with fundamentals; last_quarter the last.';
COMMENT ON COLUMN core.vendor_security.related_tickers IS
    'Other tickers the vendor associates with the security (former tickers, share '
    'classes).';
COMMENT ON COLUMN core.vendor_security.attributes IS
    'Vendor fields with no column of their own (Sharadar: scalemarketcap, '
    'scalerevenue, sicsector, sicindustry, firstadded, secfilings, companysite).';
COMMENT ON COLUMN core.vendor_security.vendor_last_updated IS
    'The vendor''s own lastupdated for the row.';

CREATE INDEX IF NOT EXISTS ix_vendor_security_ticker
    ON core.vendor_security (vendor, ticker);

-- ---------------------------------------------------------------------------
-- core.security_vendor_xref: fafnir's security_id <-> the vendor's key, over time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.security_vendor_xref (
    security_id   BIGINT NOT NULL REFERENCES core.security (security_id),
    vendor        TEXT NOT NULL,
    vendor_key    TEXT NOT NULL,
    valid_from    DATE NOT NULL,
    valid_to      DATE,
    is_feed       BOOLEAN NOT NULL DEFAULT FALSE,
    match_method  TEXT NOT NULL,
    confidence    NUMERIC(4, 3),
    evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT,
    PRIMARY KEY (vendor, vendor_key, valid_from),
    CONSTRAINT ck_svx_vendor CHECK (vendor IN ('fmp', 'sharadar', 'tiingo')),
    CONSTRAINT ck_svx_method
        CHECK (match_method IN ('minted', 'ticker_span_price', 'identifier', 'manual')),
    CONSTRAINT ck_svx_range CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CONSTRAINT ck_svx_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_svx_created_by CHECK (btrim(created_by) <> ''),
    -- A closed link feeds nothing.
    CONSTRAINT ck_svx_feed_open CHECK (NOT is_feed OR valid_to IS NULL)
);

COMMENT ON TABLE core.security_vendor_xref IS
    'Which vendor key a fafnir security is, from when to when (ADR 0015). Grain: '
    '(vendor, vendor_key, valid_from). Open = valid_to IS NULL. Written by the linking '
    'engine (#62), manual review (#63) and minting (#71).';
COMMENT ON COLUMN core.security_vendor_xref.is_feed IS
    'This vendor loads the security''s new rows (ADR 0015 §2). Only on an open link; '
    'at most one feeding vendor per security at a time.';
COMMENT ON COLUMN core.security_vendor_xref.match_method IS
    'minted: fafnir created the security from this vendor key; ticker_span_price: '
    'the ticker''s span and prices agree; identifier: a shared CUSIP/FIGI/CIK; '
    'manual: a person decided.';
COMMENT ON COLUMN core.security_vendor_xref.evidence IS
    'What the match rests on (overlap sessions, price agreement, identifiers), so a '
    'link can be reviewed without re-running the matcher.';

-- One open link per vendor key, and per (security, vendor).
CREATE UNIQUE INDEX IF NOT EXISTS ux_svx_open_key
    ON core.security_vendor_xref (vendor, vendor_key) WHERE valid_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_svx_open_sec
    ON core.security_vendor_xref (security_id, vendor) WHERE valid_to IS NULL;
-- One feeding vendor per security.
CREATE UNIQUE INDEX IF NOT EXISTS ux_svx_one_feed
    ON core.security_vendor_xref (security_id) WHERE is_feed;
CREATE INDEX IF NOT EXISTS ix_svx_security
    ON core.security_vendor_xref (security_id);

-- ---------------------------------------------------------------------------
-- core.vendor_security_event: the vendor's lifecycle events for a security
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.vendor_security_event (
    vendor            TEXT NOT NULL,
    vendor_key        TEXT NOT NULL,
    event_date        DATE NOT NULL,
    action            TEXT NOT NULL,
    ticker            TEXT,
    value             NUMERIC,
    contra_ticker     TEXT,
    contra_name       TEXT,
    contra_vendor_key TEXT,
    ingestion_run_id  BIGINT REFERENCES ops.ingestion_run (ingestion_run_id),
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_vse_vendor CHECK (vendor IN ('fmp', 'sharadar', 'tiingo'))
);

COMMENT ON TABLE core.vendor_security_event IS
    'A vendor''s lifecycle events for a security (Sharadar ACTIONS other than splits '
    'and dividends: listed, delisted, tickerchangefrom/to, acquisitionby, mergerfrom, '
    'relation, ...). Grain: (vendor, vendor_key, event_date, action, contra_ticker). '
    'Source: ACTIONS, resolved to the vendor key through TICKERS (#58). Splits and '
    'dividends go to core.corporate_action (#73).';
COMMENT ON COLUMN core.vendor_security_event.value IS
    'As the vendor gives it: a price, a deal value (millions), or NULL.';
COMMENT ON COLUMN core.vendor_security_event.contra_vendor_key IS
    'The other party''s vendor key, when it resolves; contra_ticker as the vendor '
    'spells it either way.';

-- The grain. An expression cannot be a primary key, so it is a unique index:
-- many events have no contra party.
CREATE UNIQUE INDEX IF NOT EXISTS ux_vendor_security_event
    ON core.vendor_security_event
       (vendor, vendor_key, event_date, action, COALESCE(contra_ticker, ''));
CREATE INDEX IF NOT EXISTS ix_vendor_security_event_date
    ON core.vendor_security_event (event_date);

COMMIT;
