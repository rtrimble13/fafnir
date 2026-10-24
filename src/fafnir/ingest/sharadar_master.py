"""
`fafnir ingest sharadar-master`: load Sharadar's security master and lifecycle
events into the vendor tables (plan SA-0304, #58).

The vendor's own view of every security, independent of fafnir's identity, so
linking (#62) and minting (#71) read one table. Nothing here touches
``core.security``: the link between the two is #62's.

* **TICKERS -> core.vendor_security**, one row per ``(table, permaticker)``. The
  file's ``table`` column uses legacy codes (``SEP``, ``SF1``; the API returns
  modern names), so it is mapped to the modern name the ledger and the vendor
  tables use. Rows for ``SF3B`` (``holdings_investor``) are skipped: they list
  institutional investors, not securities. The CIK comes from the ``secfilings``
  URL, as the SEC writes it (ten digits); ``cusips`` and ``relatedtickers`` are
  space-separated lists.
* **ACTIONS -> core.vendor_security_event**, every action except ``split`` and
  ``dividend``, which go to ``core.corporate_action`` (#73). The file has about
  seventy action names (2026-10-19), many more than Sharadar's documentation
  lists, so the loader keeps them all rather than a list that would silently drop
  the rest. Each row resolves to a permaticker through the master already loaded:
  ACTIONS carries only the ticker, and history is filed under the current one. A
  row whose ticker the master does not hold (an acquirer outside the dataset) is
  skipped and counted, since the event table is keyed on the vendor key. The
  contra party resolves the same way, into ``contra_vendor_key``.

  The grain is ``(vendor_key, date, action, contraticker)``. The file repeats it
  for some acquisitions, differing only in ``contraname``; the loader keeps the
  name that sorts first, so the choice does not depend on the file's order, and
  counts the rest.

Each file is one ``ops.ingestion_run`` (``endpoint = master:<table>``). Rows are
upserted, and a row equal to the stored one is not written: re-running a file
changes nothing. Nothing is deleted: a row the vendor stops sending stays.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf

logger = get_logger("ingest.sharadar_master")

VENDOR = "sharadar"

#: TICKERS tables that describe securities, in the order an ACTIONS ticker
#: resolves through them. ``holdings_investor`` lists investors.
SECURITY_TABLES = ("stocks", "funds", "fundamentals", "insiders")

#: ACTIONS rows that are not lifecycle events: they go to core.corporate_action.
NOT_EVENTS = frozenset({"split", "dividend"})

#: The files this loader reads.
LOADED_TABLES = ("tickers", "actions")

#: TICKERS fields with no column of their own, kept in ``attributes``.
ATTRIBUTE_FIELDS = (
    "scalemarketcap",
    "scalerevenue",
    "sicsector",
    "sicindustry",
    "firstadded",
    "secfilings",
    "companysite",
)

_CIK = re.compile(r"[?&]CIK=(\d+)", re.IGNORECASE)


class MasterNotLoaded(RuntimeError):
    """Events cannot resolve: the vendor's master holds no rows yet."""


@dataclass
class LoadResult:
    table: str
    file_id: int
    run_id: Optional[int] = None
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: dict = field(default_factory=dict)  # reason -> count
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def parse_cik(secfilings: Optional[str]) -> Optional[str]:
    """The CIK in an EDGAR URL (``...browse-edgar?action=getcompany&CIK=0000320193``),
    as written there, or None."""
    m = _CIK.search(secfilings or "")
    return m.group(1) if m else None


def _list(text: Optional[str]) -> Optional[list[str]]:
    items = (text or "").split()
    return items or None


def _date(text: Optional[str]) -> Optional[date]:
    """TICKERS' quarters are ``YYYY-MM-DD`` text in Sharadar's schema."""
    return date.fromisoformat(text) if text else None


_DELISTED = {"Y": True, "N": False}


def master_row(r: dict) -> Optional[dict]:
    """A TICKERS row as a ``core.vendor_security`` row, or None for a table that
    does not describe securities."""
    vendor_table = sf.canonical_table(r["table"])
    if vendor_table not in SECURITY_TABLES:
        return None
    attributes = {
        k: (r[k].isoformat() if isinstance(r[k], date) else r[k])
        for k in ATTRIBUTE_FIELDS
        if r.get(k) is not None
    }
    return {
        "vendor": VENDOR,
        "vendor_table": vendor_table,
        "vendor_key": str(r["permaticker"]),
        "ticker": r["ticker"],
        "name": r.get("name"),
        "exchange": r.get("exchange"),
        "category": r.get("category"),
        "is_delisted": _DELISTED.get(r.get("isdelisted") or ""),
        "cusips": _list(r.get("cusips")),
        "figi": r.get("figi"),
        "cik": parse_cik(r.get("secfilings")),
        "sic_code": None if r.get("siccode") is None else str(r["siccode"]),
        "sector": r.get("sector"),
        "industry": r.get("industry"),
        "fama_industry": r.get("famaindustry"),
        "currency": r.get("currency"),
        "location": r.get("location"),
        "first_price_date": r.get("firstpricedate"),
        "last_price_date": r.get("lastpricedate"),
        "first_quarter": _date(r.get("firstquarter")),
        "last_quarter": _date(r.get("lastquarter")),
        "related_tickers": _list(r.get("relatedtickers")),
        "attributes": attributes,
        "vendor_last_updated": r.get("lastupdated"),
    }


def event_rows(
    rows: Iterable[dict], key_of: dict[str, str], skipped: Counter
) -> Iterator[dict]:
    """ACTIONS rows as ``core.vendor_security_event`` rows, unique on the grain.

    ``key_of`` maps a ticker to its vendor key. What is left out is counted in
    ``skipped``: ``corporate_action`` (splits, dividends), ``unresolved:<action>``
    and ``duplicate``.
    """
    kept: dict[tuple, dict] = {}
    for r in rows:
        action = r["action"]
        if action in NOT_EVENTS:
            skipped["corporate_action"] += 1
            continue
        key = key_of.get(r["ticker"])
        if key is None:
            skipped[f"unresolved:{action}"] += 1
            continue
        contra = r.get("contraticker")
        event = {
            "vendor": VENDOR,
            "vendor_key": key,
            "event_date": r["date"],
            "action": action,
            "ticker": r["ticker"],
            "value": r.get("value"),
            "contra_ticker": contra,
            "contra_name": r.get("contraname"),
            "contra_vendor_key": key_of.get(contra) if contra else None,
        }
        grain = (key, r["date"], action, contra or "")
        held = kept.get(grain)
        if held is None:
            kept[grain] = event
            continue
        skipped["duplicate"] += 1
        if _name_order(event) < _name_order(held):
            kept[grain] = event
    yield from kept.values()


def _name_order(event: dict) -> tuple:
    name = event["contra_name"]
    return (name is None, name or "")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_tickers(db: Database, file_id: int, bulk_dir, run: RunLog, result):
    skipped: Counter = Counter()
    # Opened here, not inside the generator: ledger.rows reads the ledger, and the
    # generator runs while the upsert's COPY holds the connection.
    source = ledger.rows(db, file_id, bulk_dir)

    def mapped() -> Iterator[dict]:
        for r in source:
            result.rows += 1
            row = master_row(r)
            if row is None:
                skipped[f"not_a_security:{sf.canonical_table(r['table'])}"] += 1
                continue
            yield row

    counts = repo.upsert_vendor_securities(db, mapped(), run.run_id)
    return counts, skipped


def _load_actions(db: Database, file_id: int, bulk_dir, run: RunLog, result):
    key_of = repo.vendor_keys_by_ticker(db, VENDOR, SECURITY_TABLES)
    if not key_of:
        raise MasterNotLoaded(
            "core.vendor_security holds no Sharadar rows: load TICKERS first "
            "(`fafnir ingest sharadar-master` loads it before ACTIONS)"
        )
    skipped: Counter = Counter()

    def counted() -> Iterator[dict]:
        for r in ledger.rows(db, file_id, bulk_dir):
            result.rows += 1
            yield r

    events = list(event_rows(counted(), key_of, skipped))
    counts = repo.upsert_vendor_events(db, events, run.run_id)
    return counts, skipped


_LOADERS = {"tickers": _load_tickers, "actions": _load_actions}


def load_file(db: Database, file_id: int, bulk_dir: str | Path) -> LoadResult:
    """Load one ledger file (TICKERS or ACTIONS) in one transaction. Never raises
    for the file's own failure: the result and its run row say what happened."""
    entry = repo.sharadar_file(db, file_id)
    if entry is None:
        return LoadResult("?", file_id, error=f"no file {file_id} in the ledger")
    table = entry["vendor_table"]
    if table not in _LOADERS:
        return LoadResult(
            table,
            file_id,
            error=f"file {file_id} holds {table}; this loads {', '.join(LOADED_TABLES)}",
        )
    result = LoadResult(table, file_id)
    run = RunLog(
        db,
        source=VENDOR,
        endpoint=f"master:{table}",
        params={"file_id": file_id, "kind": entry["kind"]},
    )
    try:
        with run:
            result.run_id = run.run_id
            counts, skipped = _LOADERS[table](db, file_id, bulk_dir, run, result)
            result.inserted, result.updated, result.unchanged = counts
            result.skipped = dict(sorted(skipped.items()))
            run.rows_inserted = counts.inserted + counts.updated
            run.params.update(
                rows=result.rows,
                inserted=counts.inserted,
                updated=counts.updated,
                unchanged=counts.unchanged,
                skipped=result.skipped,
            )
            db.commit()
    except (sf.SharadarFileError, MasterNotLoaded, ValueError, OSError) as exc:
        result.error = str(exc)
        logger.error("Sharadar master load of file %s failed: %s", file_id, exc)
    return result


def default_files(db: Database) -> list[int]:
    """The current full TICKERS and ACTIONS files, TICKERS first: events resolve
    through the master. Raises when one is missing."""
    ids = []
    for table in LOADED_TABLES:
        entry = repo.current_sharadar_full(db, table)
        if entry is None:
            raise FileNotFoundError(
                f"the ledger holds no current {table} file: "
                f"`fafnir source sharadar-bulk --tables {table}` first"
            )
        ids.append(entry["file_id"])
    return ids


def load(
    db: Database, bulk_dir: str | Path, *, file_id: Optional[int] = None
) -> list[LoadResult]:
    """Load ``file_id``, or the current TICKERS then ACTIONS. ACTIONS is not
    loaded after a failed TICKERS: it would resolve through a stale master."""
    if file_id is not None:
        return [load_file(db, file_id, bulk_dir)]
    results = []
    for fid in default_files(db):
        results.append(load_file(db, fid, bulk_dir))
        if results[-1].error:
            break
    return results
