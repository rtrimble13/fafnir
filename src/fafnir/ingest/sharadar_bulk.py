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
:func:`fafnir.ingest.sharadar_ledger.rows`.

`fafnir source sharadar-delta` (#98) is the nightly half: a price table's rows the
vendor stamped ``lastupdated`` on or after a date, from the table API
(:func:`fetch_delta`). The date defaults to when the table's newest file was
fetched, less ``DELTA_OVERLAP_DAYS``, so a revision stamped the day the last file
was fetched is read again rather than missed. The rows are read in date order,
never by offset (:meth:`SharadarClient.query_by_date`), and a (ticker, date) that
comes back twice fails the table: it is how rows lost to paging show. The rows are
written as a plain CSV in the bulk file's column order, named by its content, and
recorded with ``kind = 'delta'``: one run per table (``endpoint = delta:<table>``).
A delta with no rows is not recorded.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
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
#: What the nightly fetches as deltas (#98): the price tables, by ``lastupdated``.
DELTA_TABLES = ("SEP", "SFP")
#: The API's code for each price table the ledger names in the modern way.
API_TABLE = {"stocks": "SEP", "funds": "SFP"}
#: How far a delta reaches back before its table's newest file was fetched.
DELTA_OVERLAP_DAYS = 3
#: Where a delta's date window starts. A revision restamps a security's whole
#: history, so a row stamped yesterday can be dated in any year the tables hold.
DELTA_FIRST_DATE = date(1900, 1, 1)


@dataclass
class TableResult:
    table: str
    outcome: str  # skipped | recorded | unchanged | empty | failed
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


def delta_since(db: Database, vendor_table: str) -> Optional[date]:
    """Where a delta of the table starts by default: the day its newest file, full
    or delta, was fetched, less ``DELTA_OVERLAP_DAYS``. None when the ledger holds
    no file of it."""
    newest = repo.newest_sharadar_file(db, vendor_table)
    if newest is None:
        return None
    return newest["fetched_at"].date() - timedelta(days=DELTA_OVERLAP_DAYS)


def _text(value) -> str:
    """A JSON value as the bulk CSV writes it: empty for null, the shortest repr
    for a number (a 3-dp price comes back as its 3 decimals)."""
    return "" if value is None else str(value)


def write_delta(
    rows: Sequence[dict], columns: Sequence[str], dest_dir: str | Path, table: str
) -> tuple[Path, str, int]:
    """Write ``rows`` as CSV to ``dest_dir/<sha256>.csv``; ``(path, sha256, bytes)``.
    Written under a temporary name and renamed, as a bulk download is."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_text(r.get(c)) for c in columns])
    data = buf.getvalue().encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{table}-", suffix=".part", dir=dest)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        final = dest / f"{sha}.csv"
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final, sha, len(data)


def fetch_delta(
    db: Database,
    client: SharadarClient,
    table: str,
    bulk_dir: str | Path,
    *,
    since: Optional[date] = None,
) -> TableResult:
    """Fetch, check and record one price table's rows stamped on or after ``since``
    (default :func:`delta_since`). Never raises for the table's own failure, as
    :func:`download_table`."""
    vendor_table = sf.canonical_table(table)
    if vendor_table not in API_TABLE:
        return TableResult(
            vendor_table,
            "failed",
            error=f"no delta for {table!r}: expected one of {list(DELTA_TABLES)}",
        )
    since = since or delta_since(db, vendor_table)
    if since is None:
        return TableResult(
            vendor_table,
            "failed",
            error=(
                f"the ledger holds no {vendor_table} file to start a delta from: "
                f"`fafnir source sharadar-bulk --tables {API_TABLE[vendor_table]}` "
                "first, or give --since"
            ),
        )
    filters = {"lastupdated.gte": since.isoformat()}
    run = RunLog(
        db,
        source="sharadar",
        endpoint=f"delta:{vendor_table}",
        params={"table": vendor_table, **filters},
    )
    result = TableResult(vendor_table, "failed")
    try:
        with run:
            before = client.bytes_downloaded
            rows = client.query_by_date(
                API_TABLE[vendor_table],
                filters,
                start=DELTA_FIRST_DATE,
                end=date.today() + timedelta(days=1),
            )
            run.bytes_downloaded = client.bytes_downloaded - before
            stale = [
                r
                for r in rows
                if not r.get("lastupdated")
                or r["lastupdated"] < filters["lastupdated.gte"]
            ]
            if stale:
                raise SourceError(
                    f"{vendor_table}: {len(stale)} of {len(rows)} rows were stamped "
                    f"before {since} (first {stale[0].get('ticker')} "
                    f"{stale[0].get('date')}): the lastupdated filter was not applied"
                )
            keys = {(r.get("ticker"), str(r.get("date"))[:10]) for r in rows}
            if len(keys) < len(rows):
                raise SourceError(
                    f"{vendor_table}: {len(rows) - len(keys)} of {len(rows)} rows "
                    "repeat a (ticker, date): the reads overlapped, so as many rows "
                    "may be missing"
                )
            run.params["rows"] = len(rows)
            if not rows:
                result = TableResult(vendor_table, "empty", rows=0)
                db.commit()
            else:
                result = _record_delta(db, run, vendor_table, rows, filters, bulk_dir)
    except (SourceError, OSError) as exc:
        result.error = str(exc)
        logger.error("Sharadar delta %s failed: %s", vendor_table, exc)
    return result


def _record_delta(db, run, vendor_table, rows, filters, bulk_dir) -> TableResult:
    expected = expected_columns(vendor_table)
    seen = list(dict.fromkeys(c for r in rows for c in r))
    missing = [c for c in expected if c not in seen]
    if missing:
        raise HeaderDrift(
            f"{vendor_table}: the delta's rows lack expected column(s) {missing}; "
            f"they have {seen}. Nothing was recorded."
        )
    unmapped = [c for c in seen if c not in expected]
    if unmapped:
        logger.warning(
            "%s delta: column(s) the schema does not know, kept as text: %s",
            vendor_table,
            unmapped,
        )
    columns = expected + unmapped
    path, sha, nbytes = write_delta(rows, columns, bulk_dir, vendor_table)
    entry, created = repo.record_sharadar_file(
        db,
        vendor_table=vendor_table,
        kind="delta",
        filters=filters,
        path=path.name,
        member=None,
        sha256=sha,
        nbytes=nbytes,
        rows=len(rows),
        columns=columns,
        ingestion_run_id=run.run_id,
    )
    db.commit()
    run.params.update(
        file_id=entry["file_id"],
        sha256=sha,
        file_bytes=nbytes,
        new_file=created,
        dates=[min(r["date"] for r in rows), max(r["date"] for r in rows)],
        unmapped_columns=unmapped,
    )
    return TableResult(
        vendor_table,
        "recorded" if created else "unchanged",
        file_id=entry["file_id"],
        path=entry["path"],
        bytes=nbytes,
        rows=len(rows),
        unmapped_columns=unmapped,
    )


def fetch_deltas(
    db: Database,
    client: SharadarClient,
    tables: Sequence[str],
    bulk_dir: str | Path,
    *,
    since: Optional[date] = None,
) -> list[TableResult]:
    """:func:`fetch_delta` for each table, in order."""
    return [fetch_delta(db, client, t, bulk_dir, since=since) for t in tables]
