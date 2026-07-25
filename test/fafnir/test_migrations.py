"""Integration tests for the migration runner (needs FAFNIR_TEST_DSN)."""

from __future__ import annotations

import os

import pytest

from fafnir.db import migrate as m

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


def test_all_migrations_apply(migrated_dsn):
    states = m.status(migrated_dsn)
    assert states, "no migrations discovered"
    assert all(state in ("applied",) for _, _, state in states), states
    versions = [v for v, _, _ in states]
    assert versions == sorted(versions)


def test_rollback_then_remigrate(migrated_dsn):
    # Roll back the last migration (marts) and re-apply; should converge.
    rolled = m.rollback(migrated_dsn, steps=1)
    assert rolled, "nothing rolled back"
    after = dict((v, s) for v, _, s in m.status(migrated_dsn))
    assert after[rolled[0]] == "pending"
    applied = m.migrate(migrated_dsn)
    assert rolled[0] in applied
    final = dict((v, s) for v, _, s in m.status(migrated_dsn))
    assert final[rolled[0]] == "applied"


def _recorded_checksum(dsn: str, version: str) -> str:
    from fafnir.db.connection import Database

    with Database(dsn, autocommit=True) as db:
        return db.fetchval(
            "SELECT checksum FROM meta.schema_migration WHERE version = %s", (version,)
        )


def _set_checksum(dsn: str, version: str, checksum: str) -> None:
    from fafnir.db.connection import Database

    with Database(dsn, autocommit=True) as db:
        db.execute(
            "UPDATE meta.schema_migration SET checksum = %s WHERE version = %s",
            (checksum, version),
        )


def test_superseded_checksum_is_restamped_not_reported_as_drift(migrated_dsn):
    """A database applied from a listed earlier revision converges silently."""
    version = "0001"
    legacy = next(iter(m.SUPERSEDED_CHECKSUMS[version]))
    original = _recorded_checksum(migrated_dsn, version)
    _set_checksum(migrated_dsn, version, legacy)
    try:
        # status() must not cry drift ...
        states = dict((v, s) for v, _, s in m.status(migrated_dsn))
        assert states[version] == "applied"

        # ... and migrate() must re-stamp rather than raise.
        m.migrate(migrated_dsn)
        expected = {mig.version: mig.checksum for mig in m.discover_migrations()}
        assert _recorded_checksum(migrated_dsn, version) == expected[version]
    finally:
        _set_checksum(migrated_dsn, version, original)


def test_unknown_checksum_still_raises_drift(migrated_dsn):
    """The escape hatch must not weaken the guard for genuine edits."""
    version = "0002"
    original = _recorded_checksum(migrated_dsn, version)
    _set_checksum(migrated_dsn, version, "0" * 64)
    try:
        states = dict((v, s) for v, _, s in m.status(migrated_dsn))
        assert states[version] == "DRIFT"
        with pytest.raises(RuntimeError, match="drifted"):
            m.migrate(migrated_dsn)
    finally:
        _set_checksum(migrated_dsn, version, original)


def test_target_stops_even_when_target_is_already_applied(migrated_dsn):
    """
    `migrate --target N` must be a no-op once N is applied. It used to fall
    through and apply every later migration on the second run.
    """
    assert m.migrate(migrated_dsn, target="0003") == []
