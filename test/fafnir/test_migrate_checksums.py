"""
Unit tests for migration checksum classification (no database required).

The runner refuses to re-apply an edited migration (drift). The one sanctioned
exception is a schema-neutral revision whose previous checksum is recorded in
``SUPERSEDED_CHECKSUMS``, so operators who migrated before the edit are not
blocked by a drift error they cannot act on.
"""

from __future__ import annotations

import re

from fafnir.db import migrate as m

LEGACY_0001 = "2d0c8a20afe1c6b3deae69dcec310f1266dd1ba94c7ff96ed2fa2cfeb7139d4e"


def test_matching_checksum_is_current():
    assert m.checksum_state("0005", "abc123", "abc123") == m.CURRENT


def test_unknown_mismatch_is_drift():
    assert m.checksum_state("0005", "abc123", "def456") == m.DRIFT


def test_every_recorded_legacy_revision_is_superseded():
    for version, legacy_checksums in m.SUPERSEDED_CHECKSUMS.items():
        current = _migration(version).checksum
        for legacy in legacy_checksums:
            assert m.checksum_state(version, legacy, current) == m.SUPERSEDED


def test_original_released_revision_of_0001_is_superseded():
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

    # Strip every DO $$ ... $$ block and then the -- comments (the file header
    # discusses COMMENT ON ROLE in prose). Whatever survives is executable SQL at
    # the top level, where a COMMENT ON ROLE would abort the migration. This
    # catches an unguarded statement wherever it sits, not just at column zero.
    outside_do_blocks = re.sub(r"DO \$\$.*?\$\$;", "", sql, flags=re.DOTALL)
    outside_do_blocks = re.sub(r"--[^\n]*", "", outside_do_blocks)
    assert "COMMENT ON ROLE" not in outside_do_blocks, (
        "COMMENT ON ROLE outside a DO block would abort the migration for a "
        "non-superuser; keep it inside the insufficient_privilege handler."
    )


def _migration(version: str) -> m.Migration:
    found = [mig for mig in m.discover_migrations() if mig.version == version]
    assert found, f"migration {version} not discovered"
    return found[0]
