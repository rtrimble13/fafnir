"""
Versioned SQL migration runner.

Migrations live in ``sql/migrations`` as ``NNNN_name.up.sql`` / ``.down.sql``
pairs. Each up-migration is applied in version order and recorded in
``meta.schema_migration`` with a checksum. Re-running is safe: applied versions
are skipped, and a checksum mismatch (someone edited an applied migration) is
reported as drift rather than silently re-applied.

Migration files manage their own ``BEGIN/COMMIT`` so they can also be run
directly with ``psql``. The runner therefore executes them on an autocommit
connection and records bookkeeping immediately afterwards. All DDL uses
``IF NOT EXISTS`` so an interrupted run is recoverable on retry.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("migrate")

_VERSION_RE = re.compile(r"^(\d{4})_(.+)\.up\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Optional[Path]

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up_path.read_bytes()).hexdigest()


def find_sql_dir() -> Path:
    """
    Locate the ``sql`` directory.

    Order: ``FAFNIR_SQL_DIR`` env var, then a walk upward from this file and the
    current working directory looking for a ``sql/migrations`` folder.
    """
    env = os.environ.get("FAFNIR_SQL_DIR")
    if env:
        p = Path(env)
        if (p / "migrations").is_dir():
            return p
        if p.name == "migrations":
            return p.parent
        raise FileNotFoundError(f"FAFNIR_SQL_DIR set but no migrations found at {env}")

    candidates = []
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    candidates.append(Path.cwd())
    for base in candidates:
        cand = base / "sql"
        if (cand / "migrations").is_dir():
            return cand
    raise FileNotFoundError(
        "Could not locate sql/migrations. Set FAFNIR_SQL_DIR or run from the repo root."
    )


def discover_migrations(sql_dir: Optional[Path] = None) -> list[Migration]:
    sql_dir = sql_dir or find_sql_dir()
    mig_dir = sql_dir / "migrations"
    migrations: list[Migration] = []
    for path in sorted(mig_dir.glob("*.up.sql")):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        version, name = m.group(1), m.group(2)
        down = mig_dir / f"{version}_{name}.down.sql"
        migrations.append(
            Migration(version, name, path, down if down.exists() else None)
        )
    return migrations


def _ensure_bookkeeping(db: Database) -> None:
    db.execute("CREATE SCHEMA IF NOT EXISTS meta")
    db.execute("""
        CREATE TABLE IF NOT EXISTS meta.schema_migration (
            version    TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)


def applied_versions(db: Database) -> dict[str, dict]:
    rows = db.fetchall("SELECT version, name, checksum FROM meta.schema_migration")
    return {r["version"]: r for r in rows}


def status(dsn: str, sql_dir: Optional[Path] = None) -> list[tuple[str, str, str]]:
    """Return (version, name, state) for every migration. state in applied/pending/DRIFT."""
    migrations = discover_migrations(sql_dir)
    out: list[tuple[str, str, str]] = []
    with Database(dsn, autocommit=True) as db:
        _ensure_bookkeeping(db)
        applied = applied_versions(db)
        for mig in migrations:
            if mig.version in applied:
                state = "applied"
                if applied[mig.version]["checksum"] != mig.checksum:
                    state = "DRIFT"
            else:
                state = "pending"
            out.append((mig.version, mig.name, state))
    return out


def migrate(
    dsn: str, sql_dir: Optional[Path] = None, target: Optional[str] = None
) -> list[str]:
    """
    Apply all pending up-migrations (optionally up to ``target`` version).
    Returns the list of versions applied. Raises on checksum drift.
    """
    migrations = discover_migrations(sql_dir)
    applied_now: list[str] = []
    with Database(dsn, autocommit=True) as db:
        _ensure_bookkeeping(db)
        applied = applied_versions(db)
        for mig in migrations:
            if mig.version in applied:
                if applied[mig.version]["checksum"] != mig.checksum:
                    raise RuntimeError(
                        f"Migration {mig.version} ({mig.name}) has drifted: the file "
                        f"differs from the applied checksum. Add a new migration instead "
                        f"of editing an applied one."
                    )
                continue
            logger.info("Applying migration %s_%s", mig.version, mig.name)
            db.execute_script(mig.up_path.read_text())
            db.execute(
                "INSERT INTO meta.schema_migration (version, name, checksum) "
                "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
                (mig.version, mig.name, mig.checksum),
            )
            applied_now.append(mig.version)
            if target is not None and mig.version == target:
                break
    if not applied_now:
        logger.info("No pending migrations; database is up to date.")
    return applied_now


def rollback(dsn: str, sql_dir: Optional[Path] = None, steps: int = 1) -> list[str]:
    """Roll back the most recently applied ``steps`` migrations using their .down.sql."""
    migrations = {m.version: m for m in discover_migrations(sql_dir)}
    rolled: list[str] = []
    with Database(dsn, autocommit=True) as db:
        _ensure_bookkeeping(db)
        rows = db.fetchall(
            "SELECT version FROM meta.schema_migration ORDER BY version DESC LIMIT %s",
            (steps,),
        )
        for row in rows:
            version = row["version"]
            mig = migrations.get(version)
            if mig is None or mig.down_path is None:
                raise RuntimeError(f"No down migration available for {version}")
            logger.info("Rolling back migration %s_%s", mig.version, mig.name)
            db.execute_script(mig.down_path.read_text())
            db.execute(
                "DELETE FROM meta.schema_migration WHERE version = %s", (version,)
            )
            rolled.append(version)
    return rolled
