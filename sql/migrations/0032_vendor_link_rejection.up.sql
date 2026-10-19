-- 0032_vendor_link_rejection.up.sql
-- A person's "this security is not that vendor key" (plan SA-0402, #63).
--
-- `fafnir vendor link reject` records a negative decision so the linking engine
-- (#62) never proposes the pair again, whatever its prices say. It is not a row of
-- core.security_vendor_xref: a link is "this key was this security from valid_from
-- to valid_to", and a rejection is not a span of identity. Kept there, a rejected
-- pair would answer "who was this key on date D" wrongly.
--
-- A rejection is about one security. When the security goes away (merge, fold) the
-- decision goes with it: ON DELETE CASCADE. The worst case is that the engine
-- proposes the pair again for the survivor, and a person rejects it again.
--
-- Read access comes from the default privileges, as for 0031: fafnir_read and
-- fafnir_ops read it; fafnir_app has no USAGE on core.
--
-- Empty on arrival: nothing here changes a row that exists.

BEGIN;

CREATE TABLE IF NOT EXISTS core.security_vendor_rejection (
    security_id  BIGINT NOT NULL
                 REFERENCES core.security (security_id) ON DELETE CASCADE,
    vendor       TEXT NOT NULL,
    vendor_key   TEXT NOT NULL,
    rejected_by  TEXT NOT NULL,
    rejected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT NOT NULL,
    evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (security_id, vendor, vendor_key),
    CONSTRAINT ck_svr_vendor CHECK (vendor IN ('fmp', 'sharadar', 'tiingo')),
    CONSTRAINT ck_svr_rejected_by CHECK (btrim(rejected_by) <> ''),
    CONSTRAINT ck_svr_note CHECK (btrim(note) <> '')
);

COMMENT ON TABLE core.security_vendor_rejection IS
    'A person''s decision that a security is NOT a vendor key (#63): the linking '
    'engine never proposes the pair again. Grain: (security_id, vendor, vendor_key). '
    'Written by `fafnir vendor link reject`; removed by `accept` of the same pair.';
COMMENT ON COLUMN core.security_vendor_rejection.evidence IS
    'What was rejected: the link it closed, if any.';

CREATE INDEX IF NOT EXISTS ix_svr_key
    ON core.security_vendor_rejection (vendor, vendor_key);

COMMIT;
