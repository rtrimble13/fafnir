"""
The file ledger's side of reading a Sharadar file (ADR 0017).

:mod:`fafnir.sources.sharadar_files` turns a file into typed rows and knows nothing
of the database. This module is the step before it: which file a ledger row
(``landing.sharadar_file``) names, whether it is still where the ledger says and
the size the ledger recorded, and then its rows. Every loader reads through
:func:`rows`, so a file the ledger marks deleted, one missing after a restore onto
a new host, or one replaced under the same name is refused the same way everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Iterator, Mapping, Optional

from fafnir.sources.sharadar_files import (
    SharadarFileError,
    group_by_ticker,
    iter_rows,
    select_rows,
)


def ledger_path(bulk_dir: str | Path, entry: dict) -> Path:
    """Where a ledger row's file is: ``path`` is relative to ``bulk_dir``."""
    return Path(bulk_dir) / entry["path"]


def checked_path(db, file_id: int, bulk_dir: str | Path) -> tuple[dict, Path]:
    """The ledger row of a file and where it is, after the checks every reader
    makes: refuses a file the ledger marks deleted, one missing from ``bulk_dir``
    (after a restore onto a new host, the ledger comes back without the files), and
    one whose size differs from what was recorded, which is a different file under
    the same name."""
    from fafnir.db import repository as repo

    entry = repo.sharadar_file(db, file_id)
    if entry is None:
        raise SharadarFileError(f"no file {file_id} in landing.sharadar_file")
    if entry["status"] == "deleted":
        raise SharadarFileError(f"file {file_id} ({entry['path']}) is marked deleted")
    path = ledger_path(bulk_dir, entry)
    if not path.is_file():
        raise SharadarFileError(
            f"file {file_id} is not at {path}: re-download it "
            "(`fafnir source sharadar-bulk --force`), or check [sharadar] bulk_dir"
        )
    if path.stat().st_size != entry["bytes"]:
        raise SharadarFileError(
            f"{path} is {path.stat().st_size} bytes; the ledger recorded "
            f"{entry['bytes']} for file {file_id}"
        )
    return entry, path


def rows(db, file_id: int, bulk_dir: str | Path) -> Iterator[dict]:
    """The typed rows of a file the ledger holds: the one reader the loaders use.
    The file is checked first (:func:`checked_path`)."""
    entry, path = checked_path(db, file_id, bulk_dir)
    return iter_rows(path, entry["vendor_table"], member=entry["member"])


def select(
    db, file_id: int, bulk_dir: str | Path, wanted: Mapping[str, Collection[str]]
) -> Iterator[dict]:
    """:func:`~fafnir.sources.sharadar_files.select_rows` of a ledger file: the rows
    whose ``ticker`` is a key of ``wanted`` and whose ``date`` is in that key's
    collection, in file order. The file is checked first."""
    entry, path = checked_path(db, file_id, bulk_dir)
    return select_rows(path, entry["vendor_table"], wanted, member=entry["member"])


def select_by_ticker(
    db,
    file_id: int,
    bulk_dir: str | Path,
    wanted: Mapping[str, Collection[str]],
    *,
    scratch_dir: Optional[str | Path] = None,
) -> Iterator[tuple[str, list[dict]]]:
    """:func:`select`'s rows grouped: ``(ticker, rows)`` once per ticker. The file
    is checked first, and the buckets go under ``scratch_dir``, by default
    ``bulk_dir`` (see :func:`~fafnir.sources.sharadar_files.group_by_ticker`)."""
    entry, path = checked_path(db, file_id, bulk_dir)
    return group_by_ticker(
        path,
        entry["vendor_table"],
        wanted,
        member=entry["member"],
        scratch_dir=scratch_dir or bulk_dir,
    )
