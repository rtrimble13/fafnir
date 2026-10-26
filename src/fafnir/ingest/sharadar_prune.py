"""
`fafnir source sharadar-prune`: delete the Sharadar files nothing reads any more,
keeping a short history for triage.

Every Sharadar night downloads TICKERS and ACTIONS whole and adds a SEP, an SFP and
an SF1 delta, and nothing deleted a file, so ``[sharadar] bulk_dir`` grew without
bound.
This decides from the ledger (``landing.sharadar_file``) what each file is still
for:

- **The current full file of a table** is kept: every loader reads it.
- **A delta its nightly has not applied** is kept: one after the run's
  ``params.through``, or after the current full file before the first nightly. The
  price nightly applies SEP's and SFP's (:func:`sharadar_prices.pending_deltas`),
  and `fafnir ingest fundamentals` SF1's (:func:`fundamentals.pending_deltas`,
  #106).
- **A delta fetched within** :data:`DELTA_WINDOW_DAYS` is kept. A listing the
  universe step mints tonight reads every delta on disk for its first days
  (`sharadar-prices --nightly`), and the step mints only listings first priced
  within its own window (:data:`sharadar_universe.LOOKBACK_DAYS`, 30 days).
- **A superseded full file** is kept for ``keep_days`` (``[sharadar] keep_days``,
  default 7), and the newest one of each table whatever its age. That is a week of
  TICKERS and ACTIONS versions, and the previous SEP, SFP and SF1, for triage.
- **Anything else on record is deleted.** Its row is marked ``deleted`` and
  committed, then the file is unlinked. The row stays: it is the lineage of every
  load that read the file. A file that would not unlink is found again next time.

Left behind by a run that was killed:
- **Temporary files:** a ``.*.part`` (a download) or ``.sharadar-sort-*`` (a file
  grouped by ticker) older than :data:`SCRATCH_HOURS` is deleted. Both are written to
  for as long as their run lives, and no run takes that long.
- **Unrecorded files:** a file named like the ledger's (``<sha256>.<ext>``) that
  the ledger does not hold is deleted after :data:`UNRECORDED_DAYS`. It is one whose
  header failed the check, left for inspection.
- **Any other file** is someone else's: it is reported, never touched.

It refuses to run when a current full file is missing from ``bulk_dir``: the
directory is then not the one the ledger describes, and everything in it would look
deletable. One ``ops.ingestion_run`` (``sharadar`` / ``prune``).
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import fundamentals as fu
from fafnir.ingest import sharadar_prices as sp
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.sharadar_universe import LOOKBACK_DAYS
from fafnir.logging_config import get_logger

logger = get_logger("ingest.sharadar_prune")

VENDOR = "sharadar"
ENDPOINT = "prune"
#: How long an applied delta stays: the universe step's window, and a margin for
#: nights that failed (it counts back from its last successful run).
DELTA_WINDOW_DAYS = LOOKBACK_DAYS + 15
#: A temporary file or directory this old belongs to a run that was killed.
SCRATCH_HOURS = 24
#: How long a file the ledger does not hold is left for inspection.
UNRECORDED_DAYS = 14
#: The names the downloader gives files: ``<sha256><suffix>``.
LEDGER_NAME = re.compile(r"^[0-9a-f]{64}(\.[A-Za-z0-9]+)+$")
SORT_PREFIX = ".sharadar-sort-"


class PruneError(RuntimeError):
    """The prune cannot run safely: ``bulk_dir`` is not the ledger's."""


@dataclass
class Removal:
    name: str  # relative to bulk_dir
    reason: str
    bytes: int
    file_id: Optional[int] = None


@dataclass
class PruneResult:
    dry_run: bool
    run_id: Optional[int] = None
    removals: list[Removal] = field(default_factory=list)
    kept: Counter = field(default_factory=Counter)
    foreign: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def counts(self) -> Counter:
        return Counter(r.reason for r in self.removals)

    def bytes_freed(self) -> int:
        return sum(r.bytes for r in self.removals if r.name not in self.failed)


def _size(path: Path) -> int:
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size if path.is_file() else 0


def _age(path: Path, now: datetime) -> timedelta:
    return now - datetime.fromtimestamp(path.lstat().st_mtime, timezone.utc)


def _plan_ledger(db, root, result, *, keep_days, now) -> set[str]:
    """Decide each file on record; the names the ledger holds."""
    rows = repo.sharadar_files_on_disk(db)
    current = {}
    for table in sorted({r["vendor_table"] for r in rows}):
        entry = repo.current_sharadar_full(db, table)
        if entry is None:
            continue
        if not (root / entry["path"]).is_file():
            raise PruneError(
                f"the current {table} file ({entry['path']}, file {entry['file_id']}) "
                f"is not in {root}: is [sharadar] bulk_dir the directory the ledger "
                "describes? Nothing was deleted."
            )
        current[table] = entry["file_id"]
    _, through = sp.pending_deltas(db)
    _, through[fu.VENDOR_TABLE] = fu.pending_deltas(db)
    previous: dict[str, int] = {}  # the newest superseded full of each table
    for r in rows:
        if r["kind"] == "full" and r["file_id"] != current.get(r["vendor_table"]):
            previous[r["vendor_table"]] = r["file_id"]  # rows are oldest first
    keep_after = now - timedelta(days=keep_days)
    window_after = now - timedelta(days=max(keep_days, DELTA_WINDOW_DAYS))

    for r in rows:
        table, file_id = r["vendor_table"], r["file_id"]
        if r["kind"] == "full":
            if file_id == current.get(table):
                result.kept["current"] += 1
                continue
            if file_id == previous.get(table):
                result.kept["previous"] += 1
                continue
            if r["fetched_at"] >= keep_after:
                result.kept["recent"] += 1
                continue
            reason = "superseded"
        else:
            if file_id > through.get(table, 0):
                result.kept["pending"] += 1
                continue
            if r["fetched_at"] >= window_after:
                result.kept["window"] += 1
                continue
            reason = "delta_applied"
        result.removals.append(
            Removal(r["path"], reason, _size(root / r["path"]), file_id)
        )
    return {r["path"] for r in rows}


def _plan_leftovers(db, root, result, known, *, now) -> None:
    """Decide what the ledger does not account for."""
    deleted = repo.sharadar_deleted_paths(db)
    scratch = timedelta(hours=SCRATCH_HOURS)
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in known:
            continue
        if entry.is_symlink():
            result.foreign.append(name)
            continue
        age = _age(entry, now)
        if entry.is_dir():
            if not name.startswith(SORT_PREFIX):
                result.foreign.append(name)
            elif age > scratch:
                result.removals.append(Removal(name, "sort_scratch", _size(entry)))
            else:
                result.kept["scratch_in_use"] += 1
        elif name.startswith(".") and name.endswith(".part"):
            if age > scratch:
                result.removals.append(Removal(name, "partial_download", _size(entry)))
            else:
                result.kept["download_in_use"] += 1
        elif LEDGER_NAME.match(name):
            if name in deleted:
                result.removals.append(Removal(name, "marked_deleted", _size(entry)))
            elif age > timedelta(days=UNRECORDED_DAYS):
                result.removals.append(Removal(name, "unrecorded", _size(entry)))
            else:
                result.kept["unrecorded_recent"] += 1
        else:
            result.foreign.append(name)


def plan(
    db: Database,
    bulk_dir: str | Path,
    *,
    keep_days: int = 7,
    now: Optional[datetime] = None,
) -> PruneResult:
    """What a prune would delete and keep, deleting nothing."""
    now = now or datetime.now(timezone.utc)
    root = Path(bulk_dir)
    result = PruneResult(dry_run=True)
    if not root.is_dir():
        return result
    known = _plan_ledger(db, root, result, keep_days=keep_days, now=now)
    _plan_leftovers(db, root, result, known, now=now)
    return result


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def prune(
    db: Database,
    bulk_dir: str | Path,
    *,
    keep_days: int = 7,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> PruneResult:
    """Delete what :func:`plan` finds (one run), or with ``dry_run`` only report it."""
    result = plan(db, bulk_dir, keep_days=keep_days, now=now)
    result.dry_run = dry_run
    if dry_run:
        return result
    root = Path(bulk_dir)
    run = RunLog(db, source=VENDOR, endpoint=ENDPOINT, params={"keep_days": keep_days})
    with run:
        result.run_id = run.run_id
        for rm in result.removals:
            if rm.file_id is not None:
                # Recorded first: a row marked deleted whose file survived is found
                # again (`marked_deleted`); the other order would leave a current-
                # looking row with no file behind it.
                repo.mark_sharadar_file_deleted(db, rm.file_id)
                db.commit()
            try:
                _remove(root / rm.name)
            except OSError as exc:
                result.failed.append(rm.name)
                logger.warning("Sharadar prune: could not remove %s: %s", rm.name, exc)
        run.params.update(
            deleted=dict(sorted(result.counts().items())),
            bytes_freed=result.bytes_freed(),
            kept=dict(sorted(result.kept.items())),
            foreign=len(result.foreign),
            failed=result.failed,
        )
        db.commit()
    logger.info(
        "Sharadar prune: %d removed (%d bytes), %s kept",
        len(result.removals) - len(result.failed),
        result.bytes_freed(),
        dict(result.kept),
    )
    return result
