-- 0046_api_key.up.sql
-- ops.api_key: the keys of the warehouse's HTTP API (ADR 0021).
--
-- `duk` can reach the warehouse with no database connection, through an HTTP
-- service on the host (`fafnir-api`), authenticating with an API key in place of
-- an SSH key. This is where the service looks a key up.
--
-- What is stored
-- --------------
-- A key is `ffn_<key_id>_<secret>`. `key_id` is public and names the row; the
-- secret is 32 random bytes. `key_sha256` is the SHA-256 of the whole key, and no
-- other part of the key is kept, so this table (and every dump of it) holds
-- nothing that authenticates. A plain SHA-256 is deliberate: a slow salted hash
-- protects a secret a person chose, and this one is 256 random bits.
--
-- A key is never deleted. Revoking stamps `revoked_at`, so the access log's
-- `key=<name>` can always be traced to the row that issued it. The live keys'
-- names are unique; a revoked name can be issued again.
--
-- Who may read it
-- ---------------
-- Nobody, by this migration, beyond what 0001's default privileges already give
-- the write path (fafnir_ingest: the `fafnir api key` commands). In particular:
--
--   * fafnir_ops is REVOKED. 0021 gave that tier SELECT on everything in ops,
--     present and future, and it is the tier an agent reads with -- one whose
--     input includes text nobody on this side wrote. Hashes of random keys are
--     not a secret, but who holds a key and when it expires is nothing the DQ
--     queue needs, so the door stays shut.
--   * The role the API serves as is NOT created or granted here. Which login
--     role that is, is a deployment fact and not schema (ADR 0008): the install
--     creates it `IN ROLE fafnir_app` and grants it USAGE on `ops` and SELECT on
--     this one table (doc/install_hetzner.md §14). `fafnir-api --check` reports
--     whether that was done.

BEGIN;

CREATE TABLE ops.api_key (
    key_id      TEXT        PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{12}$'),
    name        TEXT        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,39}$'),
    key_sha256  TEXT        NOT NULL CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT        NOT NULL,
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    revoked_by  TEXT,
    note        TEXT,
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);

-- One live key per name: the name is how a key is revoked and how the access log
-- and pg_stat_activity (application_name = 'fafnir-api:<name>') attribute a query.
CREATE UNIQUE INDEX api_key_live_name ON ops.api_key (name) WHERE revoked_at IS NULL;

COMMENT ON TABLE ops.api_key IS
    'Keys of the fafnir HTTP API (ADR 0021). Holds the SHA-256 of each key, never '
    'the key. Written by `fafnir api key`; read by the role fafnir-api serves as.';
COMMENT ON COLUMN ops.api_key.key_id IS
    'Public half of the key (ffn_<key_id>_<secret>): names the row, safe to log.';
COMMENT ON COLUMN ops.api_key.name IS
    'What the key is called: who or what holds it. Unique among live keys.';
COMMENT ON COLUMN ops.api_key.key_sha256 IS
    'SHA-256 (hex) of the whole key. The key itself is shown once, at creation.';
COMMENT ON COLUMN ops.api_key.revoked_at IS
    'When the key stopped working. Rows are kept so the log stays traceable.';

REVOKE ALL ON ops.api_key FROM fafnir_ops;

COMMIT;
