"""
`fafnir source sharadar-bulk`: download Sharadar's whole-table files and record
them (plan SA-0206, #52).

For each table asked for:

1. **Skip it** if the ledger already holds a current full file of the table,
   unless ``force``. A run that died part-way resumes from the first table it had
   not finished.
2. **Download it** (:meth:`SharadarClient.bulk_export`): ``years=5|10|full``, a 302
   to a pre-signed zip, streamed to ``bulk_dir/<sha256><suffix>``.
3. **Check its header** against the table's schema
   (:mod:`fafnir.sources.sharadar_files`): a missing expected column fails the
   table, naming the columns; an unknown one is a warning, kept in the run's
   ``params.unmapped_columns``.
4. **Record it** in ``landing.sharadar_file`` with its row count, then mark the
   table's older full files superseded. The same content downloaded twice is one
   file, not two.

Each table is its own ``ops.ingestion_run`` (``source = sharadar``, ``endpoint =
bulk:<table>``), with the bytes downloaded and, in ``params``, the rows, file and
header findings. A table that fails does not undo the ones before it. Nothing is
loaded into ``core`` here: the loaders (#55, #58, #75) read the files through
:func:`fafnir.sources.sharadar_files.rows`.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf
from fafnir.sources.base import SourceError
from fafnir.sources.sharadar import BULK_YEARS, SharadarClient

logger = get_logger("ingest.sharadar_bulk")

#: What the initial population downloads (plan v2, P1). Legacy codes are
#: accepted and recorded under the modern name.
DEFAULT_TABLES = ("tickers", "actions", "SEP", "SFP", "SF1")


@dataclass
class TableResult:
    table: str
    outcome: str  # skipped | recorded | unchanged | failed
    file_id: Optional[int] = None
    path: Optional[str] = None
    bytes: int = 0
    rows: Optional[int] = None
    superseded: int = 0
    unmapped_columns: list = field(default_factory=list)
    error: Optional[str] = None


class HeaderDrift(SourceError):
    """A file lacks columns its table's schema expects."""


def expected_columns(vendor_table: str) -> list[str]:
    """The columns a file of the table must carry: its schema's known columns."""
    return list(sf.SCHEMAS.get(vendor_table, {}))


def zip_member(path: Path) -> Optional[str]:
    """The CSV inside a zip (refusing a zip of several); None for a non-zip."""
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise SourceError(f"{path.name}: expected one CSV in the zip, found {names}")
    return names[0]


def download_table(
    db: Database,
    client: SharadarClient,
    table: str,
    bulk_dir: str | Path,
    *,
    years: str = "full",
    force: bool = False,
) -> TableResult:
    """Download, check and record one table. Never raises for the table's own
    failure: the result says what happened, and the run row records it."""
    vendor_table = sf.canonical_table(table)
    if vendor_table not in sf.SCHEMAS:
        return TableResult(
            vendor_table,
            "failed",
            error=f"no schema for {table!r}: expected one of {sorted(sf.SCHEMAS)}",
        )
    current = repo.current_sharadar_full(db, vendor_table)
    if current is not None and not force:
        return TableResult(
            vendor_table, "skipped", file_id=current["file_id"], path=current["path"]
        )

    params = {"table": vendor_table, "years": years, "force": force}
    run = RunLog(db, source="sharadar", endpoint=f"bulk:{vendor_table}", params=params)
    result = TableResult(vendor_table, "failed")
    try:
        with run:
            before = client.bytes_downloaded
            export = client.bulk_export(table, bulk_dir, years=years)
            run.bytes_downloaded = client.bytes_downloaded - before
            member = zip_member(export.path)
            columns = sf.header(export.path, member)
            missing = [c for c in expected_columns(vendor_table) if c not in columns]
            if missing:
                raise HeaderDrift(
                    f"{vendor_table}: {export.path.name} lacks expected column(s) "
                    f"{missing}; it has {columns}. The file is left in place for "
                    "inspection and not recorded."
                )
            unmapped = sf.unknown_columns(vendor_table, columns)
            if unmapped:
                logger.warning(
                    "%s: column(s) the schema does not know, read as text: %s",
                    vendor_table,
                    unmapped,
                )
            nrows = sf.count_rows(export.path, member)
            entry, created = repo.record_sharadar_file(
                db,
                vendor_table=vendor_table,
                kind="full",
                filters={"years": years},
                path=export.path.name,
                member=member,
                sha256=export.sha256,
                nbytes=export.bytes,
                rows=nrows,
                columns=columns,
                ingestion_run_id=run.run_id,
            )
            superseded = repo.supersede_sharadar_fulls(
                db, vendor_table, entry["file_id"]
            )
            db.commit()
            run.params.update(
                file_id=entry["file_id"],
                sha256=export.sha256,
                member=member,
                rows=nrows,
                file_bytes=export.bytes,
                new_file=created,
                superseded=superseded,
                unmapped_columns=unmapped,
            )
            result = TableResult(
                vendor_table,
                "recorded" if created else "unchanged",
                file_id=entry["file_id"],
                path=entry["path"],
                bytes=export.bytes,
                rows=nrows,
                superseded=superseded,
                unmapped_columns=unmapped,
            )
    except (SourceError, sf.SharadarFileError, OSError) as exc:
        result.error = str(exc)
        logger.error("Sharadar bulk %s failed: %s", vendor_table, exc)
    return result


def download_tables(
    db: Database,
    client: SharadarClient,
    tables: Sequence[str],
    bulk_dir: str | Path,
    *,
    years: str = "full",
    force: bool = False,
) -> list[TableResult]:
    """:func:`download_table` for each table, in order; one table's failure does
    not stop the rest."""
    if str(years) not in BULK_YEARS:
        raise ValueError(f"years must be one of {BULK_YEARS}, not {years!r}")
    return [
        download_table(db, client, t, bulk_dir, years=str(years), force=force)
        for t in tables
    ]
