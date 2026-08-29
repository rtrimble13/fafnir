-- 0019_tracked_symbol.up.sql
-- A DECLARED universe alongside the screened one (ADR 0006).
--
-- The universe core.security holds is a *discovered* one: `ingest securities`
-- re-reads company-screener per venue and keeps what `_is_us` accepts. An open-end
-- mutual fund has no listing venue -- it is struck at NAV once a day, not traded on
-- an exchange -- so it is not in the screener at all and no amount of nightly upkeep
-- will ever mint it. A fund is a *declared* security, and until now fafnir had no
-- way to declare one.
--
-- This table is that declaration: the operator's curated list of symbols that must
-- be in the security master regardless of what any screener returns. It is an INPUT
-- to the security master, not a copy of it -- core.security stays the identity, and
-- `fafnir ingest tracked` is what turns a declaration into a security_id.
--
-- Grain: (source, symbol) -- deliberately the same soft key upsert_security
-- conflicts on (0009/0012), so a declared symbol and a screened one can never fork
-- into two security_ids.

BEGIN;

-- ---------------------------------------------------------------------------
-- The pseudo-venue funds are recorded under.
--
-- In the migration rather than sql/seeds/, because ref.tracked_symbol has a foreign
-- key on it: this row is a dependency of the schema, not the merely-informative
-- venue data that belongs in a seed.
--
-- It earns more than documentation. `ingest delisted` only considers feed rows whose
-- normalized venue is in SCREENER_EXCHANGES, so recording funds under MUTF puts them
-- structurally outside the delisting sweep -- an equity feed that has never heard of
-- a fund cannot mark one dead.
-- ---------------------------------------------------------------------------
INSERT INTO ref.exchange (exchange_code, exchange_name, country, timezone)
VALUES ('MUTF', 'US open-end mutual funds (NAV, no venue)', 'US', 'America/New_York')
ON CONFLICT (exchange_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- ref.tracked_symbol
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.tracked_symbol (
    source        TEXT NOT NULL DEFAULT 'fmp',
    symbol        TEXT NOT NULL,
    asset_type    TEXT NOT NULL DEFAULT 'fund'
                     CHECK (asset_type IN ('equity', 'etf', 'fund', 'other')),
    exchange_code TEXT REFERENCES ref.exchange (exchange_code),
    note          TEXT,                      -- why this symbol is tracked
    is_tracked    BOOLEAN NOT NULL DEFAULT TRUE,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    untracked_at  TIMESTAMPTZ,
    PRIMARY KEY (source, symbol),
    -- An untracked row must say when it stopped being tracked; a tracked one must
    -- not claim it has. Without this the table cannot answer "since when", which is
    -- the only question anyone asks of a symbol that quietly stopped loading.
    CONSTRAINT ck_tracked_symbol_untracked CHECK (
        (is_tracked AND untracked_at IS NULL)
        OR (NOT is_tracked AND untracked_at IS NOT NULL)
    )
);

COMMENT ON TABLE ref.tracked_symbol IS
    'Operator-declared symbols the security master must hold regardless of the '
    'screener. Grain: (source, symbol) -- the same soft key upsert_security uses. '
    'Loaded by `fafnir ingest tracked`.';
COMMENT ON COLUMN ref.tracked_symbol.asset_type IS
    'What this symbol IS, per the operator. Authoritative over the vendor profile: '
    'the declaration is the reason the row exists.';
COMMENT ON COLUMN ref.tracked_symbol.exchange_code IS
    'Venue to record on core.security. MUTF for open-end funds (no venue).';
COMMENT ON COLUMN ref.tracked_symbol.note IS
    'Why this symbol is tracked. The one thing a hand-inserted core.security row '
    'could never carry.';
COMMENT ON COLUMN ref.tracked_symbol.is_tracked IS
    'FALSE stops the nightly pulls. It does NOT delist: retiring a security is '
    'mark_delisted''s job, and the history is retained either way.';

-- The loader reads only the tracked rows, every night, and the untracked ones are
-- kept forever as the record of what used to be declared.
CREATE INDEX IF NOT EXISTS ix_tracked_symbol_tracked
    ON ref.tracked_symbol (symbol)
    WHERE is_tracked;

COMMIT;
