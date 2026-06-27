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
