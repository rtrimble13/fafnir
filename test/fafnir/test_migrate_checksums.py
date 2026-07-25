"""
Unit tests for migration checksum classification (no database required).

The runner refuses to re-apply an edited migration (drift). The one sanctioned
exception is a schema-neutral revision whose previous checksum is recorded in
``SUPERSEDED_CHECKSUMS``, so operators who migrated before the edit are not
blocked by a drift error they cannot act on.
"""

from __future__ import annotations

from fafnir.db import migrate as m

LEGACY_0001 = "2d0c8a20afe1c6b3deae69dcec310f1266dd1ba94c7ff96ed2fa2cfeb7139d4e"


def test_matching_checksum_is_current():
    assert m.checksum_state("0005", "abc123", "abc123") == m.CURRENT


def test_unknown_mismatch_is_drift():
    assert m.checksum_state("0005", "abc123", "def456") == m.DRIFT


def test_recorded_legacy_revision_is_superseded():
    current = _migration("0001").checksum
    assert m.checksum_state("0001", LEGACY_0001, current) == m.SUPERSEDED


def test_superseded_list_is_scoped_per_version():
    # The same checksum recorded against a different version is still drift.
    assert m.checksum_state("0002", LEGACY_0001, "whatever") == m.DRIFT


def test_current_files_are_not_listed_as_superseded():
    # A current checksum in the allowlist would permanently disable the drift
    # guard for that migration.
    for mig in m.discover_migrations():
        assert mig.checksum not in m.SUPERSEDED_CHECKSUMS.get(mig.version, frozenset())


def test_superseded_entries_reference_real_migrations():
    versions = {mig.version for mig in m.discover_migrations()}
    assert set(m.SUPERSEDED_CHECKSUMS) <= versions


def test_role_comments_cannot_fail_migration_0001():
    """
    COMMENT ON ROLE requires superuser, which a least-privilege migrator does not
    have, so 0001 must keep those statements inside a handler rather than letting
    them abort the install.
    """
    sql = _migration("0001").up_path.read_text()
    assert "EXCEPTION WHEN insufficient_privilege" in sql
    unguarded = [
        line
        for line in sql.splitlines()
        if line.startswith("COMMENT ON ROLE")  # i.e. not indented inside a DO block
    ]
    assert not unguarded, f"unguarded COMMENT ON ROLE statements: {unguarded}"


def _migration(version: str) -> m.Migration:
    found = [mig for mig in m.discover_migrations() if mig.version == version]
    assert found, f"migration {version} not discovered"
    return found[0]
