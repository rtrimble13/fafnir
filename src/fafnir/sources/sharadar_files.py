"""
Read a downloaded Sharadar file as typed rows (plan SA-0203, #50).

Plan v2 keeps Sharadar's files on disk, recorded in ``landing.sharadar_file``, and
loads ``core`` straight from them: there are no typed landing tables, and nothing is
COPYed into the database. This module is the one place a file becomes rows, so
every loader (#52's downloader checks headers with it; #55, #58, #75 read with it)
types a value the same way.

* **Containers.** A bulk file is a zip holding one CSV (measured 2026-10-19:
  ``/bulk-sharadar/<table>.csv.zip`` holding ``<modern name>.csv``; a zip with
  exactly one ``.csv`` member needs none named). A slice saved from the API
  may be a plain or gzipped CSV. All are streamed: a 45M-row SEP is never in
  memory.
* **Types** follow Sharadar's own schemas (``GET /v1.0/schema/<table>``,
  2026-08-18): ``date`` -> :class:`datetime.date`, ``double precision`` ->
  :class:`decimal.Decimal` (from the text, so the 3 decimals stored are the 3
  decimals read; never a float), ``bigint`` -> ``int``, ``text`` -> ``str``. An
  empty field is ``None``: the probe found nulls and zeros distinct (#49, item
  10), and a zero stays a zero.
* **A value that does not parse is an error**, naming the file, line and column.
  A loader that skipped it would load a table with a silent hole.
* A column the schema does not know is kept as text; :func:`unknown_columns` lists
  them, for #52's header-drift check.
"""

from __future__ import annotations

import csv
import gzip
import io
import zipfile
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Collection, Iterator, Mapping, Optional

#: Legacy codes Sharadar still accepts in its paths, to the modern names the ledger
#: stores (``landing.sharadar_file.vendor_table``).
LEGACY_NAMES = {
    "SEP": "stocks",
    "SFP": "funds",
    "SF1": "fundamentals",
    "SF2": "insiders",
    "SF3": "holdings",
    "SF3A": "holdings_ticker",
    "SF3B": "holdings_investor",
}

_PRICES = {
    "ticker": "text",
    "date": "date",
    "open": "double",
    "high": "double",
    "low": "double",
    "close": "double",
    "volume": "double",  # split-adjusted, so not always whole (#49, item 2)
    "closeadj": "double",
    "closeunadj": "double",
    "lastupdated": "date",
}

#: Column types per table, from Sharadar's schemas. Fundamentals has 105 columns:
#: its text and date columns are listed, and every other column is numeric.
SCHEMAS: dict[str, dict[str, str]] = {
    "stocks": _PRICES,
    "funds": _PRICES,
    "actions": {
        "date": "date",
        "action": "text",
        "ticker": "text",
        "name": "text",
        "value": "double",
        "contraticker": "text",
        "contraname": "text",
    },
    "tickers": {
        "table": "text",
        "permaticker": "bigint",
        "ticker": "text",
        "name": "text",
        "exchange": "text",
        "isdelisted": "text",
        "category": "text",
        "cusips": "text",
        "siccode": "bigint",
        "sicsector": "text",
        "sicindustry": "text",
        "figi": "text",
        "famaindustry": "text",
        "sector": "text",
        "industry": "text",
        "scalemarketcap": "text",
        "scalerevenue": "text",
        "relatedtickers": "text",
        "currency": "text",
        "location": "text",
        "lastupdated": "date",
        "firstadded": "date",
        "firstpricedate": "date",
        "lastpricedate": "date",
        "firstquarter": "text",
        "lastquarter": "text",
        "secfilings": "text",
        "companysite": "text",
    },
    "daily": {
        "ticker": "text",
        "date": "date",
        "lastupdated": "date",
        **{
            c: "double"
            for c in ("ev", "evebit", "evebitda", "marketcap", "pb", "pe", "ps")
        },
    },
    "fundamentals": {
        "ticker": "text",
        "dimension": "text",
        "calendardate": "date",
        "date": "date",
        "reportperiod": "date",
        "fiscalperiod": "text",
        "lastupdated": "date",
    },
}
#: Tables whose unlisted columns are numeric rather than unknown.
NUMERIC_BY_DEFAULT = {"fundamentals"}


def canonical_table(name: str) -> str:
    """``SEP`` -> ``stocks``; a modern name in lower case (``TICKERS`` -> ``tickers``).
    The API takes any of these spellings (2026-10-19); the ledger and the vendor
    tables store the modern, lower-case name."""
    return LEGACY_NAMES.get(name.upper(), name.lower())


def _to_date(text: str) -> date:
    return date.fromisoformat(text)


def _to_decimal(text: str) -> Decimal:
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError(f"not a finite number: {text!r}")
    return value


def _to_int(text: str) -> int:
    # A bigint written as "123.0" by a float-minded exporter is still a whole
    # number; "123.5" is not one, and fails.
    value = Decimal(text)
    if value != value.to_integral_value():
        raise ValueError(f"not a whole number: {text!r}")
    return int(value)


_CONVERTERS: dict[str, Callable[[str], object]] = {
    "date": _to_date,
    "double": _to_decimal,
    "bigint": _to_int,
    "text": str,
}


def column_type(table: str, column: str) -> Optional[str]:
    """The column's type, or ``None`` when the schema does not know it."""
    schema = SCHEMAS.get(canonical_table(table), {})
    if column in schema:
        return schema[column]
    if canonical_table(table) in NUMERIC_BY_DEFAULT:
        return "double"
    return None


class SharadarFileError(ValueError):
    """A file that cannot be read as the table it claims to hold."""


@contextmanager
def open_text(
    path: str | Path, member: Optional[str] = None
) -> Iterator[io.TextIOBase]:
    """The file's CSV text, streamed: a zip's member, a gzip, or a plain file."""
    p = Path(path)
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            name = member or _only_csv(zf, p)
            with zf.open(name) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8", newline="")
        return
    if member is not None:
        raise SharadarFileError(
            f"{p.name}: a member ({member}) was named, but it is not a zip"
        )
    with p.open("rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    if gz:
        with gzip.open(p, "rt", encoding="utf-8", newline="") as fh:
            yield fh
    else:
        with p.open("r", encoding="utf-8", newline="") as fh:
            yield fh


def _only_csv(zf: zipfile.ZipFile, p: Path) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise SharadarFileError(
            f"{p.name}: expected one .csv member, found {names or zf.namelist()}; "
            "name the member"
        )
    return names[0]


def header(path: str | Path, member: Optional[str] = None) -> list[str]:
    """The CSV's column names, as written."""
    with open_text(path, member) as fh:
        row = next(csv.reader(fh), None)
    if not row:
        raise SharadarFileError(f"{Path(path).name}: empty file, no header")
    return [c.strip() for c in row]


def unknown_columns(table: str, columns: list[str]) -> list[str]:
    """The columns the table's schema does not know (kept as text when read)."""
    return [c for c in columns if column_type(table, c) is None]


def iter_rows(
    path: str | Path, table: str, *, member: Optional[str] = None
) -> Iterator[dict]:
    """Every data row of the file as a dict of typed values, in file order.

    Raises :class:`SharadarFileError` on a value that does not parse as its column's
    type, or a row with the wrong number of fields, naming the line and column.
    """
    name = Path(path).name
    with open_text(path, member) as fh:
        reader = csv.reader(fh)
        cols = [c.strip() for c in next(reader, [])]
        if not cols:
            raise SharadarFileError(f"{name}: empty file, no header")
        convert = [_CONVERTERS.get(column_type(table, c) or "text", str) for c in cols]
        for line, fields in enumerate(reader, start=2):
            if not fields:
                continue  # a trailing blank line
            if len(fields) != len(cols):
                raise SharadarFileError(
                    f"{name} line {line}: {len(fields)} fields, the header has {len(cols)}"
                )
            row = {}
            for col, conv, text in zip(cols, convert, fields):
                if text == "":
                    row[col] = None
                    continue
                try:
                    row[col] = conv(text)
                except (ValueError, InvalidOperation) as exc:
                    raise SharadarFileError(
                        f"{name} line {line}, column {col}: {text!r} is not a "
                        f"{column_type(table, col)} ({exc})"
                    ) from None
            yield row


def count_rows(path: str | Path, member: Optional[str] = None) -> int:
    """Data rows in the file (the header excluded), for the ledger's ``rows``."""
    with open_text(path, member) as fh:
        reader = csv.reader(fh)
        next(reader, None)
        return sum(1 for fields in reader if fields)


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
    """The typed rows of a ledger file whose ``ticker`` is a key of ``wanted`` and
    whose ``date`` (``YYYY-MM-DD``) is in that key's collection.

    Both are tested on the raw text, and only a row that passes is converted. A
    price table is 45M rows, and a caller that wants a few dates for each of
    thousands of tickers would otherwise parse most of them.
    """
    entry, path = checked_path(db, file_id, bulk_dir)
    table, name = entry["vendor_table"], path.name
    with open_text(path, entry["member"]) as fh:
        reader = csv.reader(fh)
        cols = [c.strip() for c in next(reader, [])]
        if "ticker" not in cols or "date" not in cols:
            raise SharadarFileError(f"{name}: needs ticker and date columns")
        t, d = cols.index("ticker"), cols.index("date")
        width = len(cols)
        convert = [_CONVERTERS.get(column_type(table, c) or "text", str) for c in cols]
        for line, fields in enumerate(reader, start=2):
            if len(fields) != width:
                if fields:
                    raise SharadarFileError(
                        f"{name} line {line}: {len(fields)} fields, the header has "
                        f"{width}"
                    )
                continue
            dates = wanted.get(fields[t])
            if dates is None or fields[d] not in dates:
                continue
            row = {}
            for col, conv, text in zip(cols, convert, fields):
                try:
                    row[col] = None if text == "" else conv(text)
                except (ValueError, InvalidOperation) as exc:
                    raise SharadarFileError(
                        f"{name} line {line}, column {col}: {text!r} is not a "
                        f"{column_type(table, col)} ({exc})"
                    ) from None
            yield row
