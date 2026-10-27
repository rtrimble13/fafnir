"""
`fafnir ingest fundamentals`: Sharadar SF1's statements into ``core.income_statement``,
``core.balance_sheet`` and ``core.cash_flow_statement`` (#90; plan
``doc/plans/fundamentals-sf1.md`` §4.5-4.7 and §6; ADR 0018, ADR 0019).

Two modes, both reading the ledger (no API, no landing table):

- ``--backfill``: the ledger's current SF1 full file, one ticker's rows at a time
  (:func:`sharadar_ledger.select_by_ticker`).
- **the nightly** (the default): the SF1 delta files recorded since the last nightly.
  Where two files hold a row, the one stamped later wins, and on the same stamp the
  later file's. The run's ``params.through`` names the last file applied, so a night
  that failed is read again the next, and the file prune reads it (#106, which also
  fetches the deltas). It applies them on top of a backfill: until one has succeeded
  and the statements hold rows, it waits, reads nothing and records no run
  (:func:`backfilled`), so the deltas stay pending for the night after it.

**Rows.** First-reported rows (ARQ, ARY) and restated rows (MRQ, MRY). SF1's trailing
rows (ART, MRT) are not read: TTM is the mart's, from quarters. A row's ticker is
SF1's ticker for a permaticker (``core.vendor_security``, ``fundamentals``), and the
permaticker resolves to the security whose Sharadar link covers the row's
``reportperiod``, else to its open link (§6.2). Never by bare ticker. A row of a key
fafnir does not link is outside the universe, and only counted.

**Versions** (ADR 0018). Per statement table and period, first-reported rows first:

1. A first-reported row is version 1, known from its filing (``filed``). Where
   version 1 is held and the row changed, the vendor corrected its record: the row is
   superseded on the load timeline and the corrected one inserted with the same
   version and knowledge interval (``fundamentals_vendor_correction``).
2. A restated row that differs from the version in force, compared in that version's
   share terms and without ``shares_outstanding``, is a new version known from the
   load day (``observed``). The previous version's ``known_to`` becomes that day
   (``fundamentals_restatement``). A restatement of a version known from the load
   day itself is known from the next day: an interval is never empty.
3. A restated row with nothing held for its period is version 1, known from the
   issuer's first filing for a period ending on or after it (``estimated``), or with
   none from the load day (``observed``).

**Share terms** (§4.7). A version is stored in the terms of its ``known_from``. SF1's
share counts are in today's terms, so they are divided by the product of Sharadar's
splits (ACTIONS, as exact ratios) with an ex-date after that day, and rounded to the
share. After a later split SF1 restates its history in the new terms, the recovered
counts come out as stored, and nothing is written. A count that moved by no more
than a recovery's rounding (1,000 shares, or a millionth of it) is unchanged, as the
price nightly leaves a bar recovered again after a split. ``shares_outstanding`` (the
cover count) comes from first-reported rows only: a restated version carries its
predecessor's, in its own terms, and an estimated one has none.

**The content hash** is SHA-256 over one table's typed values, quantized to their
columns, share counts to 1,000 shares, with ``None`` told from ``0``, and the filing
date. Equal hash, or equal values: nothing is written, not even ``loaded_at``.

**Validation** (§6.3). A row with a malformed period label, a value its column cannot
hold, a filing before its period ended, or a twin in the same file is quarantined
(``ops.data_quality_flag``, once per row), and the rest loads. A first-reported period
SF1 lists once per filing (a 10-K and its 10-K/A) is not a twin: its earliest filing
loads, and the later ones are counted (``later_filing``). SF1's metrics,
per-share values and ``*usd`` conversions go to the income row's ``extra``
(``vendor_metric``), and a column SF1 adds later to ``unmapped``: mart reads neither.

A vendor key is the unit of work: its periods and versions are written and committed
before the next. The watermark is ``(sharadar, SF1:<dimension>, security_id)``, the
latest ``reportperiod`` applied. Restatements and corrections are flagged once per
security and run (``warn`` from the nightly; ``info`` from a backfill, which observes
the whole of SF1's restated history in a day).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import fundamentals_map as fm
from fafnir.ingest import sf1_probe
from fafnir.ingest import sharadar_actions as sa
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.ingest import sharadar_map
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.sharadar_master import SECURITY_TABLES
from fafnir.ingest.sharadar_prices import report_progress
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf

logger = get_logger("ingest.fundamentals")

VENDOR = "sharadar"
#: The ledger's and the master's name for SF1.
VENDOR_TABLE = "fundamentals"
ENDPOINT = "fundamentals"
#: The nightly's run: its ``params.through`` is the last SF1 delta applied.
NIGHTLY_ENDPOINT = "fundamentals:nightly"

FIRST_REPORTED = {"ARQ": "quarter", "ARY": "annual"}
RESTATED = {"MRQ": "quarter", "MRY": "annual"}
DIMENSIONS = {**FIRST_REPORTED, **RESTATED}
PERIODS = {
    "annual": ("annual",),
    "quarter": ("quarter",),
    "both": ("annual", "quarter"),
}
LABEL = re.compile(r"(\d{4})-(FY|Q[1-4])")

FILED, OBSERVED, ESTIMATED = "filed", "observed", "estimated"

# Quarantine checks (§6.3) and the flags the loader writes.
MISSING_LABEL = "fundamentals_missing_period_label"
FILED_EARLY = "fundamentals_filing_before_period"
OUT_OF_RANGE = "fundamentals_value_out_of_range"
DUPLICATE = "fundamentals_duplicate_period"
UNLINKED = "fundamentals_unlinked"
RESTATEMENT = "fundamentals_restatement"
CORRECTION = "fundamentals_vendor_correction"
ESTIMATED_FLAG = "fundamentals_estimated_knowledge_date"

#: SF1's columns that place a row rather than describe the company.
ROW_KEYS = (
    "ticker",
    "dimension",
    "calendardate",
    "date",
    "reportperiod",
    "fiscalperiod",
    "lastupdated",
)
TYPED = frozenset(i.vendor_field for items in fm.STATEMENTS.values() for i in items)
#: What goes to ``extra`` under ``vendor_metric`` (ADR 0019): every column the vendor
#: documents that is neither a row key nor typed.
VENDOR_METRIC_FIELDS = frozenset(sf1_probe.KNOWN) - set(ROW_KEYS) - TYPED

MONEY_QUANTUM = Decimal("0.01")
SHARE_QUANTUM = Decimal("0.0001")
#: Below these a value fits ``NUMERIC(24,2)`` and ``NUMERIC(24,4)``.
MONEY_LIMIT = Decimal(10) ** 22
SHARE_LIMIT = Decimal(10) ** 20
#: The content hash's share-count precision (decision 3).
HASH_SHARES = Decimal(1000)
#: A share count is unchanged within this many shares, or this share of itself (a
#: count recovered across a deep reverse split carries the vendor's rounding times
#: the ratio).
SHARE_TOLERANCE = Decimal(1000)
SHARE_RELATIVE = Decimal("0.000001")
#: The cover count: first-reported rows only, and never compared for a restatement.
COVER_COUNT = "shares_outstanding"
#: Entries a restatement or correction flag lists, the largest moves first.
FLAG_ENTRIES = 10
FLAG_FIELDS = 5


class FundamentalsError(RuntimeError):
    """The loader cannot run: a file or the master is missing, or a backfill read
    nothing."""


class Quarantined(ValueError):
    """A row the loader sets aside, under one of §6.3's checks."""

    def __init__(self, check: str, reason: str):
        super().__init__(reason)
        self.check = check
        self.reason = reason


# ---------------------------------------------------------------------------
# A row, typed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sf1Row:
    """One SF1 row the loader reads: its period, and its typed values per statement
    in SF1's terms (today's)."""

    ticker: str
    dimension: str
    period_type: str
    fiscal_date: date
    calendar_date: date
    fiscal_year: int
    fiscal_period: str
    filing_date: Optional[date]  # first-reported rows: SF1's `date`
    lastupdated: Optional[date]
    values: dict  # statement -> column -> Decimal | None
    vendor_metric: dict
    unmapped: dict

    @property
    def first_reported(self) -> bool:
        return self.dimension in FIRST_REPORTED

    @property
    def period(self) -> tuple[str, date]:
        return self.period_type, self.fiscal_date


def parse(row: dict) -> Sf1Row:
    """An SF1 row of ARQ, ARY, MRQ or MRY, checked and typed. Raises
    :class:`Quarantined` with the check it fails."""
    dim = row.get("dimension")
    period_type = DIMENSIONS[dim]
    first = dim in FIRST_REPORTED
    fiscal_date, calendar_date = row.get("reportperiod"), row.get("calendardate")
    if fiscal_date is None or calendar_date is None:
        raise Quarantined(MISSING_LABEL, "no reportperiod or calendardate")
    label = LABEL.fullmatch(row.get("fiscalperiod") or "")
    if label is None:
        raise Quarantined(
            MISSING_LABEL,
            f"fiscalperiod {row.get('fiscalperiod')!r} is not YYYY-FY or YYYY-Qn",
        )
    if (label.group(2) == "FY") != (period_type == "annual"):
        raise Quarantined(
            MISSING_LABEL, f"fiscalperiod {label.group(0)} on a {dim} row"
        )
    filing = row.get("date") if first else None
    if first and filing is None:
        raise Quarantined(MISSING_LABEL, "a first-reported row with no filing date")
    if first and filing < fiscal_date:
        raise Quarantined(
            FILED_EARLY, f"filed {filing}, before the period ended on {fiscal_date}"
        )
    values: dict[str, dict] = {}
    for statement, items in fm.STATEMENTS.items():
        values[statement] = {}
        for item in items:
            v = row.get(item.vendor_field)
            if v is not None:
                limit = SHARE_LIMIT if item.item_class == "share_count" else MONEY_LIMIT
                if not isinstance(v, Decimal) or not v.is_finite() or abs(v) >= limit:
                    raise Quarantined(
                        OUT_OF_RANGE,
                        f"{item.vendor_field} = {v} does not fit {item.sql_type}",
                    )
            values[statement][item.column] = v
    return Sf1Row(
        ticker=row["ticker"],
        dimension=dim,
        period_type=period_type,
        fiscal_date=fiscal_date,
        calendar_date=calendar_date,
        fiscal_year=int(label.group(1)),
        fiscal_period=label.group(2),
        filing_date=filing,
        lastupdated=row.get("lastupdated"),
        values=values,
        vendor_metric={
            k: v for k, v in row.items() if k in VENDOR_METRIC_FIELDS and v is not None
        },
        unmapped={
            k: v for k, v in row.items() if k not in sf1_probe.KNOWN and v is not None
        },
    )


# ---------------------------------------------------------------------------
# Share terms, the hash, the comparison
# ---------------------------------------------------------------------------


def split_product(splits: Sequence[tuple[date, Fraction]], day: date) -> Fraction:
    """The exact product of the splits with an ex-date after ``day``: what SF1's share
    counts for ``day`` were multiplied by. A split's own ex-date already trades in the
    new terms, so a filing on it is too."""
    product = Fraction(1)
    for ex_date, ratio in splits:
        if ex_date > day:
            product *= ratio
    return product


def _round_half_up(x: Fraction) -> int:
    return (
        math.floor(x + Fraction(1, 2)) if x >= 0 else -math.floor(-x + Fraction(1, 2))
    )


def _shares(value: Decimal, divisor: Fraction) -> Decimal:
    """A share count divided by ``divisor``, to the nearest share; as given when
    nothing divides it."""
    if divisor == 1:
        return value.quantize(SHARE_QUANTUM, rounding=ROUND_HALF_UP)
    return Decimal(_round_half_up(Fraction(value) / divisor)).quantize(SHARE_QUANTUM)


def in_terms(statement: str, values: dict, divisor: Fraction) -> dict:
    """One statement's SF1 values in the share terms ``divisor`` takes them to,
    quantized to their columns."""
    out = {}
    for item in fm.STATEMENTS[statement]:
        v = values.get(item.column)
        if v is None:
            out[item.column] = None
        elif item.item_class == "share_count":
            out[item.column] = _shares(v, divisor)
        else:
            out[item.column] = v.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return out


def _text(v: Decimal) -> str:
    return format(v.copy_abs() if v == 0 else v, "f")


def content_hash(statement: str, values: dict, filing_date: Optional[date]) -> str:
    """SHA-256 over a version's typed values and filing date (§4.6)."""
    parts = []
    for item in fm.STATEMENTS[statement]:
        v = values.get(item.column)
        if v is not None and item.item_class == "share_count":
            v = (v / HASH_SHARES).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            v *= HASH_SHARES
        parts.append(f"{item.column}={'None' if v is None else _text(v)}")
    parts.append(f"filing_date={filing_date.isoformat() if filing_date else 'None'}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def moved(statement: str, held: dict, new: dict, *, skip: Iterable[str] = ()) -> dict:
    """``{column: (held, new)}`` for the columns whose values differ: money at the
    cent, share counts beyond a recovery's rounding, ``None`` told from ``0``."""
    skip = set(skip)
    out = {}
    for item in fm.STATEMENTS[statement]:
        c = item.column
        if c in skip:
            continue
        a, b = held.get(c), new.get(c)
        if a is None or b is None:
            if (a is None) != (b is None):
                out[c] = (a, b)
        elif item.item_class == "share_count":
            if abs(a - b) > max(SHARE_TOLERANCE, SHARE_RELATIVE * max(abs(a), abs(b))):
                out[c] = (a, b)
        elif a != b:
            out[c] = (a, b)
    return out


def _carried(value: Optional[Decimal], ratio: Fraction) -> Optional[Decimal]:
    """A stored share count taken into later terms: × the splits between."""
    if value is None:
        return None
    return _shares(value, 1 / ratio)


# ---------------------------------------------------------------------------
# Planning: what one security's rows do to the versions it holds
# ---------------------------------------------------------------------------


@dataclass
class Version:
    """The current row of one version of a period, held or planned."""

    statement: str
    period_type: str
    fiscal_date: date
    version_no: int
    known_from: date
    known_to: Optional[date]
    knowledge_source: str
    filing_date: Optional[date]
    values: dict
    content_hash: str
    vendor_key: str
    calendar_date: Optional[date] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    reported_currency: Optional[str] = None
    extra: dict = field(default_factory=dict)
    #: Planned by this run, not yet written: closing it sets its ``known_to``
    #: before the insert, rather than updating a row.
    planned: bool = False

    @property
    def period(self) -> tuple[str, date]:
        return self.period_type, self.fiscal_date

    @classmethod
    def from_row(cls, statement: str, row: dict) -> "Version":
        return cls(
            statement=statement,
            period_type=row["period_type"],
            fiscal_date=row["fiscal_date"],
            version_no=row["version_no"],
            known_from=row["known_from"],
            known_to=row["known_to"],
            knowledge_source=row["knowledge_source"],
            filing_date=row["filing_date"],
            values={i.column: row[i.column] for i in fm.STATEMENTS[statement]},
            content_hash=row["content_hash"],
            vendor_key=row["vendor_key"],
        )


@dataclass(frozen=True)
class Op:
    """One write: ``supersede`` a version's current row, ``close`` the version in
    force (set its ``known_to``), or ``insert`` a row."""

    kind: str
    version: Version
    known_to: Optional[date] = None


@dataclass
class Event:
    kind: str  # inserted, unchanged, corrected, restated, refused, other_key
    statement: str
    period: tuple[str, date]
    version: Optional[Version] = None
    moved: dict = field(default_factory=dict)


class Book:
    """The current rows of one security's statements, as planning moves them."""

    def __init__(self, held: Iterable[Version] = ()):
        self._rows: dict[tuple, dict[int, Version]] = defaultdict(dict)
        for v in held:
            self.put(v)

    def put(self, v: Version) -> None:
        self._rows[(v.statement, v.period)][v.version_no] = v

    def versions(self, statement: str, period: tuple) -> dict[int, Version]:
        return self._rows.get((statement, period), {})

    def in_force(self, statement: str, period: tuple) -> Optional[Version]:
        open_ = [
            v for v in self.versions(statement, period).values() if v.known_to is None
        ]
        return max(open_, key=lambda v: v.version_no) if open_ else None

    def filings(self) -> list[tuple[date, date]]:
        """``(fiscal_date, filing_date)`` of every filed version held."""
        return [
            (v.fiscal_date, v.filing_date)
            for versions in self._rows.values()
            for v in versions.values()
            if v.knowledge_source == FILED and v.filing_date is not None
        ]


def _extra(statement: str, row: Sf1Row) -> dict:
    if statement != "income":
        return {}
    out = {"vendor_metric": row.vendor_metric} if row.vendor_metric else {}
    if row.unmapped:
        out["unmapped"] = row.unmapped
    return out


def _version(
    statement: str,
    row: Sf1Row,
    *,
    version_no: int,
    known_from: date,
    source: str,
    values: dict,
    vendor_key: str,
    currency: Optional[str],
) -> Version:
    filing = row.filing_date if source == FILED else None
    return Version(
        statement=statement,
        period_type=row.period_type,
        fiscal_date=row.fiscal_date,
        version_no=version_no,
        known_from=known_from,
        known_to=None,
        knowledge_source=source,
        filing_date=filing,
        values=values,
        content_hash=content_hash(statement, values, filing),
        vendor_key=vendor_key,
        calendar_date=row.calendar_date,
        fiscal_year=row.fiscal_year,
        fiscal_period=row.fiscal_period,
        reported_currency=currency,
        extra=_extra(statement, row),
        planned=True,
    )


def plan(
    rows: Sequence[Sf1Row],
    book: Book,
    *,
    vendor_key: str,
    currency: Optional[str],
    splits: Sequence[tuple[date, Fraction]],
    as_of: date,
) -> tuple[list[Op], list[Event]]:
    """What one security's rows write, in order, and why: first-reported rows first
    (corrections), then restated rows (restatements), per statement (§4.6). ``book``
    is updated as it goes, so a restated row meets the version its first-reported
    twin just wrote."""
    ops: list[Op] = []
    events: list[Event] = []
    filings = sorted(
        {(r.fiscal_date, r.filing_date) for r in rows if r.first_reported}
        | set(book.filings())
    )

    def next_filing(fiscal_date: date) -> Optional[date]:
        later = [f for d, f in filings if d >= fiscal_date]
        return min(later) if later else None

    for row in sorted(rows, key=lambda r: (not r.first_reported, r.period)):
        for statement in fm.STATEMENTS:
            if row.first_reported:
                _first_reported(
                    statement, row, book, ops, events, vendor_key, currency, splits
                )
            else:
                _restated(
                    statement,
                    row,
                    book,
                    ops,
                    events,
                    vendor_key,
                    currency,
                    splits,
                    as_of=as_of,
                    estimated_from=next_filing(row.fiscal_date),
                )
    return ops, events


def _first_reported(statement, row, book, ops, events, vendor_key, currency, splits):
    values = in_terms(
        statement, row.values[statement], split_product(splits, row.filing_date)
    )
    new = _version(
        statement,
        row,
        version_no=1,
        known_from=row.filing_date,
        source=FILED,
        values=values,
        vendor_key=vendor_key,
        currency=currency,
    )
    held = book.versions(statement, row.period).get(1)
    if held is None:
        ops.append(Op("insert", new))
        book.put(new)
        events.append(Event("inserted", statement, row.period, new))
        return
    if held.vendor_key != vendor_key:
        events.append(Event("other_key", statement, row.period))
        return
    if held.content_hash == new.content_hash or (
        held.knowledge_source == FILED
        and held.filing_date == new.filing_date
        and not moved(statement, held.values, values)
    ):
        events.append(Event("unchanged", statement, row.period))
        return
    change = moved(statement, held.values, values)
    if held.filing_date != new.filing_date:
        change["filing_date"] = (held.filing_date, new.filing_date)
    new = replace(new, known_to=held.known_to)
    if new.known_to is not None and new.known_to <= new.known_from:
        # The corrected filing is no earlier than a restatement fafnir already dated:
        # the version cannot be re-dated without moving its successor. Left for a
        # person, in the correction flag.
        events.append(Event("refused", statement, row.period, held, change))
        return
    ops += [Op("supersede", held), Op("insert", new)]
    book.put(new)
    events.append(Event("corrected", statement, row.period, new, change))


def _restated(
    statement,
    row,
    book,
    ops,
    events,
    vendor_key,
    currency,
    splits,
    *,
    as_of: date,
    estimated_from: Optional[date],
):
    held = book.versions(statement, row.period)
    if not held:
        if estimated_from is not None:
            known_from, source = estimated_from, ESTIMATED
        else:
            known_from, source = as_of, OBSERVED
        values = in_terms(
            statement, row.values[statement], split_product(splits, known_from)
        )
        if COVER_COUNT in values:
            values[COVER_COUNT] = None  # first-reported rows only
        new = _version(
            statement,
            row,
            version_no=1,
            known_from=known_from,
            source=source,
            values=values,
            vendor_key=vendor_key,
            currency=currency,
        )
        ops.append(Op("insert", new))
        book.put(new)
        events.append(Event("inserted", statement, row.period, new))
        return
    force = book.in_force(statement, row.period)
    if force is None or force.vendor_key != vendor_key:
        events.append(Event("other_key", statement, row.period))
        return
    compared = in_terms(
        statement, row.values[statement], split_product(splits, force.known_from)
    )
    change = moved(statement, force.values, compared, skip=(COVER_COUNT,))
    if not change:
        events.append(Event("unchanged", statement, row.period))
        return
    known_from = max(as_of, force.known_from + timedelta(days=1))
    values = in_terms(
        statement, row.values[statement], split_product(splits, known_from)
    )
    if COVER_COUNT in values:
        values[COVER_COUNT] = _carried(
            force.values.get(COVER_COUNT),
            split_product(splits, force.known_from) / split_product(splits, known_from),
        )
    new = _version(
        statement,
        row,
        version_no=max(held) + 1,
        known_from=known_from,
        source=OBSERVED,
        values=values,
        vendor_key=vendor_key,
        currency=currency,
    )
    if force.planned:
        force.known_to = known_from  # its insert carries it
        ops.append(Op("insert", new))
    else:
        ops += [Op("close", force, known_to=known_from), Op("insert", new)]
        book.put(replace(force, known_to=known_from))
    book.put(new)
    events.append(Event("restated", statement, row.period, new, change))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _json(value) -> str:
    """JSON text with a Decimal written as the number it is, not a float."""
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(f"{json.dumps(str(k))}:{_json(v)}" for k, v in value.items())
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(v) for v in value) + "]"
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    return json.dumps(value)


def _write(db: Database, security_id: int, ops: Sequence[Op], run_id: int) -> int:
    """Apply one security's ops: the updates in order, then the inserts, one batch per
    table. An update touches only a row already written (a planned version is closed
    in its insert), so every update can go first. Returns rows inserted."""
    inserts: dict[str, list[dict]] = defaultdict(list)
    for op in ops:
        v = op.version
        table = fm.TABLES[v.statement]
        where = dict(
            security_id=security_id,
            period_type=v.period_type,
            fiscal_date=v.fiscal_date,
            version_no=v.version_no,
        )
        if op.kind == "supersede":
            repo.supersede_statement_row(db, table, **where)
        elif op.kind == "close":
            repo.close_statement_version(db, table, known_to=op.known_to, **where)
        else:
            inserts[table].append(
                {
                    **where,
                    "vendor_key": v.vendor_key,
                    "calendar_date": v.calendar_date,
                    "fiscal_year": v.fiscal_year,
                    "fiscal_period": v.fiscal_period,
                    "known_from": v.known_from,
                    "known_to": v.known_to,
                    "knowledge_source": v.knowledge_source,
                    "filing_date": v.filing_date,
                    "reported_currency": v.reported_currency,
                    **v.values,
                    "content_hash": v.content_hash,
                    "extra": _json(v.extra),
                    "source": VENDOR,
                    "ingestion_run_id": run_id,
                }
            )
    for table, rows in inserts.items():
        repo.insert_statement_rows(db, table, rows)
    return sum(len(rows) for rows in inserts.values())


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class Links:
    """Every Sharadar link, by key and by security (§6.2)."""

    def __init__(self, rows: Iterable[dict]):
        self.by_key: dict[str, list[tuple[int, date, Optional[date]]]] = defaultdict(
            list
        )
        self.by_security: dict[int, list[tuple[str, date, Optional[date]]]] = (
            defaultdict(list)
        )
        for r in rows:
            sid = int(r["security_id"])
            self.by_key[r["vendor_key"]].append((sid, r["valid_from"], r["valid_to"]))
            self.by_security[sid].append(
                (r["vendor_key"], r["valid_from"], r["valid_to"])
            )

    @staticmethod
    def _covers(start: date, end: Optional[date], day: date) -> bool:
        return start <= day and (end is None or day <= end)

    def resolve(self, key: str, day: date) -> tuple[Optional[int], str]:
        """The security a key's period on ``day`` belongs to, and how: ``covered`` (a
        link of the key's spans the day) or ``open`` (the key's open link). None with
        ``outside_universe`` (no link), ``unlinked`` (none covers it and none is
        open) or ``other_key`` (another key's link to that security covers the day:
        the period is that key's)."""
        links = self.by_key.get(key)
        if not links:
            return None, "outside_universe"
        for sid, start, end in links:
            if self._covers(start, end, day):
                return sid, "covered"
        open_ = [sid for sid, _, end in links if end is None]
        if not open_:
            return None, "unlinked"
        sid = open_[0]
        for other, start, end in self.by_security[sid]:
            if other != key and self._covers(start, end, day):
                return None, "other_key"
        return sid, "open"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class FundamentalsResult:
    dry_run: bool
    backfill: bool
    run_id: Optional[int] = None
    keys: int = 0  # vendor keys in scope
    securities: int = 0  # securities written to, or that would be
    read: int = 0  # SF1 rows read for the keys in scope
    written: int = 0  # rows inserted
    counts: Counter = field(default_factory=Counter)
    #: Versions inserted, by the decade of their period and knowledge source (§7).
    by_decade: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    unmapped: set[str] = field(default_factory=set)
    files: list[int] = field(default_factory=list)
    through: Optional[int] = None
    #: The nightly before any backfill: nothing read, no run (:func:`backfilled`).
    waiting: bool = False


@dataclass
class _Key:
    vendor_key: str
    ticker: str
    currency: Optional[str]
    splits: list = field(default_factory=list)


def _splits_by_key(
    db: Database, bulk_dir: str | Path, keys: set[str], as_of: date
) -> dict[str, list[tuple[date, Fraction]]]:
    """Sharadar's splits per key, from the ledger's current ACTIONS file, as exact
    ratios, one per ex-date, none after ``as_of`` (SF1 cannot have applied it)."""
    actions = repo.current_sharadar_full(db, "actions")
    if actions is None or not keys:
        if keys:
            logger.warning("no current ACTIONS file: share counts are taken as given")
        return {}
    key_of = repo.vendor_keys_by_ticker(db, VENDOR, SECURITY_TABLES)
    found = sa.key_actions(ledger.rows(db, actions["file_id"], bulk_dir), keys, key_of)
    out = {}
    for key, kinds in found.items():
        by_date = {}
        for day, value in kinds.get("split", ()):
            if value and value > 0 and day <= as_of and day not in by_date:
                by_date[day] = Fraction(*sharadar_map.split_ratio(value))
        out[key] = sorted(by_date.items())
    return out


def backfilled(db: Database) -> bool:
    """Whether the nightly has statements to apply its deltas to: a backfill has
    finished and the statements hold rows (a reset empties them and keeps the
    runs). Before that a delta would load a security's latest filings alone, and
    the backfill (doc/backfill.md) would find them there. A backfill that
    quarantined rows ('partial') has finished: the rest of the file loaded."""
    if repo.last_completed_run(db, VENDOR, ENDPOINT) is None:
        return False
    return repo.statements_loaded(db)


def pending_deltas(db: Database) -> tuple[list[dict], int]:
    """The SF1 delta files the nightly has not applied, oldest first, and the file it
    was applied through: the last finished nightly's, or the full file the last
    whole backfill read if that is newer (a re-run from a fresh file takes over from
    the deltas before it, which the file holds); with neither, the current full
    file. A run that quarantined rows ('partial') counts: a quarantined row is
    flagged, and reading its file again would only quarantine it again."""
    last = repo.last_completed_run(db, VENDOR, NIGHTLY_ENDPOINT)
    applied = (((last or {}).get("params") or {}).get("through") or {}).get(
        VENDOR_TABLE
    )
    whole = repo.last_completed_run(db, VENDOR, ENDPOINT, params={"whole": True})
    read = (whole["params"].get("files") or [None])[0] if whole else None
    known = [f for f in (applied, read) if f is not None]
    if known:
        through = max(known)
    else:
        full = repo.current_sharadar_full(db, VENDOR_TABLE)
        through = full["file_id"] if full else 0
    return repo.sharadar_deltas_after(db, VENDOR_TABLE, through), through


def _twins(rows: Iterable[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """The rows of one file split on their (ticker, dimension, reportperiod) into
    those to load, the later filings of a first-reported period, and twins.

    SF1 lists a first-reported period once per filing that reported it: a 10-K and its
    10-K/A, a quarter filed again. The earliest filing is the first report and loads;
    the later ones are set apart and counted, since what they change reaches the
    period through its restated row. Rows that cannot be told apart are twins
    (§6.3: neither is loaded): a restated period listed twice, or two different first
    reports on the earliest day."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("ticker"), r.get("dimension"), r.get("reportperiod"))].append(r)
    alone, later, twins = [], [], []
    for (_, dim, _), group in groups.items():
        first = group[0] if len(group) == 1 else None
        if first is None and dim in FIRST_REPORTED:
            first = _first_filing(group)
        if first is None:
            twins.extend(group)
            continue
        alone.append(first)
        later.extend(r for r in group if r is not first)
    return alone, later, twins


def _first_filing(group: Sequence[dict]) -> Optional[dict]:
    """The earliest filing of a first-reported period listed more than once, or None
    where that is not one row: a filing date missing, or rows filed on the earliest
    day that differ in more than ``lastupdated``."""
    if not all(isinstance(r.get("date"), date) for r in group):
        return None
    day = min(r["date"] for r in group)
    earliest = [r for r in group if r["date"] == day]

    def content(r: dict) -> dict:
        return {k: v for k, v in r.items() if k != "lastupdated"}

    if any(content(r) != content(earliest[0]) for r in earliest[1:]):
        return None
    return earliest[0]


def _merged_deltas(
    db: Database, bulk_dir: str | Path, files: Sequence[dict], tickers: set[str]
) -> tuple[dict[str, list[dict]], list[dict], list[tuple[dict, int]]]:
    """``{ticker: rows}`` across the delta files, oldest first: where two files hold a
    row, the one stamped later wins, and on the same stamp the later file's. Later
    filings within one file are returned apart, to count, and twins, with their file,
    to quarantine (:func:`_twins`)."""
    wanted = {t: sf.ALL_DATES for t in tickers}
    held: dict[tuple, dict] = {}
    later: list[dict] = []
    twins: list[tuple[dict, int]] = []
    for f in files if wanted else ():
        alone, again, dup = _twins(ledger.select(db, f["file_id"], bulk_dir, wanted))
        later += again
        twins += [(r, f["file_id"]) for r in dup]
        for r in alone:
            k = (r["ticker"], r["dimension"], r["reportperiod"])
            stamp = r.get("lastupdated") or date.min
            if k not in held or stamp >= (held[k].get("lastupdated") or date.min):
                held[k] = r
    out: dict[str, list[dict]] = defaultdict(list)
    for (ticker, _, _), r in held.items():
        out[ticker].append(r)
    return dict(out), later, twins


def load(
    db: Database,
    bulk_dir: str | Path,
    *,
    backfill: bool,
    security_ids: Optional[Sequence[int]] = None,
    period: str = "both",
    limit: Optional[int] = None,
    dry_run: bool = False,
    as_of: Optional[date] = None,
) -> FundamentalsResult:
    """Load SF1's statements: the full file with ``backfill``, else the pending
    deltas. One run unless ``dry_run``; a vendor key's writes are committed
    together."""
    if period not in PERIODS:
        raise ValueError(f"--period is annual, quarter or both, not {period!r}")
    as_of = as_of or date.today()
    result = FundamentalsResult(dry_run=dry_run, backfill=backfill)
    if not backfill and not backfilled(db):
        result.waiting = True
        return result
    links = Links(repo.vendor_links(db, VENDOR))
    listing = repo.vendor_table_rows(db, VENDOR, VENDOR_TABLE)
    if not listing:
        raise FundamentalsError(
            "core.vendor_security holds no fundamentals tickers: run "
            "`fafnir ingest sharadar-master` first"
        )
    if security_ids is not None:
        chosen = set(security_ids)
        keys = sorted(
            {k for sid in chosen for k, _, _ in links.by_security.get(sid, ())}
        )
    else:
        chosen = None
        keys = sorted(links.by_key)
    in_scope = [k for k in keys if k in listing]
    result.counts["key_without_fundamentals"] += len(keys) - len(in_scope)
    if limit is not None:
        in_scope = in_scope[:limit]
    targets = {
        listing[k]["ticker"]: _Key(k, listing[k]["ticker"], listing[k]["currency"])
        for k in in_scope
    }
    result.keys = len(targets)
    partial = security_ids is not None or limit is not None or period != "both"

    if backfill and not targets:
        raise FundamentalsError(
            "no security in scope has a Sharadar link and a fundamentals ticker"
        )
    if backfill:
        full = repo.current_sharadar_full(db, VENDOR_TABLE)
        if full is None:
            raise FundamentalsError(
                "the ledger holds no current SF1 file: "
                "`fafnir source sharadar-bulk --tables SF1` first"
            )
        result.files = [full["file_id"]]
        files, through = [], None
    else:
        files, through = pending_deltas(db)
        result.files = [f["file_id"] for f in files]
        result.through = through if partial else max(result.files, default=through)

    splits = _splits_by_key(
        db, bulk_dir, {t.vendor_key for t in targets.values()}, as_of
    )
    for t in targets.values():
        t.splits = splits.get(t.vendor_key, [])

    def groups() -> (
        Iterator[tuple[str, list[dict], list[dict], list[tuple[dict, int]], int]]
    ):
        """``(ticker, rows, later, twins, file_id)``: one key's rows at a time."""
        if backfill:
            wanted = {ticker: sf.ALL_DATES for ticker in targets}
            file_id = result.files[0]
            for ticker, rows in ledger.select_by_ticker(db, file_id, bulk_dir, wanted):
                alone, later, dup = _twins(rows)
                yield ticker, alone, later, [(r, file_id) for r in dup], file_id
            return
        merged, later, twins = _merged_deltas(db, bulk_dir, files, set(targets))
        later_of: dict[str, list] = defaultdict(list)
        for r in later:
            later_of[r["ticker"]].append(r)
        twins_of: dict[str, list] = defaultdict(list)
        for r, f in twins:
            twins_of[r["ticker"]].append((r, f))
        last = result.files[-1] if result.files else None
        for ticker in sorted(set(merged) | set(later_of) | set(twins_of)):
            yield (
                ticker,
                merged.get(ticker, []),
                later_of.get(ticker, []),
                twins_of.get(ticker, []),
                last,
            )

    def scan(run: Optional[RunLog]) -> None:
        written_to: set[int] = set()
        last = time.monotonic()
        for done, (ticker, rows, later, twins, file_id) in enumerate(groups(), start=1):
            t = targets[ticker]
            result.read += len(rows) + len(later) + len(twins)
            sids = _load_key(
                db,
                t,
                rows,
                later,
                twins,
                file_id=file_id,
                links=links,
                chosen=chosen,
                periods=PERIODS[period],
                as_of=as_of,
                run=run,
                result=result,
            )
            written_to |= sids
            if run is not None:
                run.rows_inserted = result.written
            last = report_progress(
                run,
                last,
                ticker=ticker,
                tickers_done=done,
                tickers=len(targets),
                read=result.read,
                written=result.written,
            )
        result.securities = len(written_to)
        if backfill and targets and result.read == 0:
            raise FundamentalsError(
                f"the backfill read no SF1 row for any of {len(targets)} keys in "
                f"file {result.files[0]}: is it the SF1 file?"
            )
        if run is None:
            return
        run.rows_inserted = result.written
        run.params.pop("progress", None)
        run.params.update(_summary(result))
        db.commit()

    if dry_run:
        scan(None)
        return result
    params = {"backfill": backfill, "period": period, "files": result.files}
    if backfill:
        # Every key and period: the nightly takes over from its file.
        params["whole"] = not partial
    run = RunLog(
        db,
        source=VENDOR,
        endpoint=ENDPOINT if backfill else NIGHTLY_ENDPOINT,
        params=params,
    )
    with run:
        result.run_id = run.run_id
        scan(run)
    return result


def _summary(result: FundamentalsResult) -> dict:
    out = {
        "keys": result.keys,
        "securities": result.securities,
        "read": result.read,
        "written": result.written,
        "counts": dict(sorted(result.counts.items())),
        "by_decade": {d: dict(c) for d, c in sorted(result.by_decade.items())},
    }
    if result.unmapped:
        out["unmapped"] = sorted(result.unmapped)
    if not result.backfill:
        out["through"] = {VENDOR_TABLE: result.through}
    return out


def _quarantine(
    db: Database,
    run: Optional[RunLog],
    result: FundamentalsResult,
    row: dict,
    check: str,
    reason: str,
    *,
    security_id: Optional[int],
    file_id: Optional[int],
) -> None:
    result.counts[f"quarantined:{check}"] += 1
    if run is None:
        return
    run.rows_quarantined += 1
    period = row.get("reportperiod")
    repo.add_dq_flag_once(
        db,
        check_name=check,
        severity="warn",
        security_id=security_id,
        table_name="landing.sharadar_file",
        record_key={
            "ticker": row.get("ticker"),
            "dimension": row.get("dimension"),
            "reportperiod": period.isoformat() if isinstance(period, date) else period,
        },
        detail={
            "reason": reason,
            "file_id": file_id,
            "fiscalperiod": row.get("fiscalperiod"),
        },
        ingestion_run_id=run.run_id,
    )


def _load_key(
    db: Database,
    key: _Key,
    rows: Sequence[dict],
    later: Sequence[dict],
    twins: Sequence[tuple[dict, int]],
    *,
    file_id: Optional[int],
    links: Links,
    chosen: Optional[set[int]],
    periods: Sequence[str],
    as_of: date,
    run: Optional[RunLog],
    result: FundamentalsResult,
) -> set[int]:
    """One vendor key's rows: checked, resolved, planned per security and, with
    ``run``, written and committed. The securities written to."""
    counts = result.counts
    by_security: dict[int, list[Sf1Row]] = defaultdict(list)
    unlinked: list[dict] = []

    def wanted(r: dict) -> bool:
        dim = r.get("dimension")
        if dim not in DIMENSIONS:
            counts["trailing_or_other_dimension"] += 1
            return False
        if DIMENSIONS[dim] not in periods:
            counts["other_period"] += 1
            return False
        return True

    counts["later_filing"] += sum(1 for r in later if wanted(r))
    for r, f in twins:
        if wanted(r):
            sid = None
            if isinstance(r.get("reportperiod"), date):
                sid = links.resolve(key.vendor_key, r["reportperiod"])[0]
            _quarantine(
                db,
                run,
                result,
                r,
                DUPLICATE,
                "two rows for one ticker, dimension and period in one file",
                security_id=sid,
                file_id=f,
            )
    for r in rows:
        if not wanted(r):
            continue
        day = r.get("reportperiod")
        sid, how = (
            links.resolve(key.vendor_key, day)
            if isinstance(day, date)
            else (None, "no_period")
        )
        try:
            parsed = parse(r)
        except Quarantined as q:
            _quarantine(
                db, run, result, r, q.check, q.reason, security_id=sid, file_id=file_id
            )
            continue
        if parsed.unmapped:
            result.unmapped |= set(parsed.unmapped)
        if (parsed.filing_date or parsed.fiscal_date) > as_of:
            counts["future"] += 1
            continue
        if sid is None:
            counts[how] += 1
            if how == "unlinked":
                unlinked.append(r)
            continue
        if chosen is not None and sid not in chosen:
            counts["outside_selection"] += 1
            continue
        by_security[sid].append(parsed)

    if unlinked and run is not None:
        repo.add_dq_flag_once(
            db,
            check_name=UNLINKED,
            severity="info",
            table_name="core.security_vendor_xref",
            record_key={"vendor": VENDOR, "vendor_key": key.vendor_key},
            detail={
                "ticker": key.ticker,
                "rows": len(unlinked),
                "first": min(r["reportperiod"] for r in unlinked).isoformat(),
                "last": max(r["reportperiod"] for r in unlinked).isoformat(),
            },
            ingestion_run_id=run.run_id,
        )

    written_to: set[int] = set()
    for sid, parsed in sorted(by_security.items()):
        book = Book(
            Version.from_row(statement, row)
            for statement, table in fm.TABLES.items()
            for row in repo.statement_rows(db, table, [sid])
        )
        ops, events = plan(
            parsed,
            book,
            vendor_key=key.vendor_key,
            currency=key.currency,
            splits=key.splits,
            as_of=as_of,
        )
        _count(result, events)
        if ops:
            written_to.add(sid)
        if run is None:
            continue
        result.written += _write(db, sid, ops, run.run_id)
        _flag(db, sid, key, events, run=run, backfill=result.backfill)
        for dim in sorted({p.dimension for p in parsed}):
            repo.set_watermark(
                db,
                VENDOR,
                f"SF1:{dim}",
                max(p.fiscal_date for p in parsed if p.dimension == dim),
                sid,
            )
    if run is not None:
        db.commit()
    return written_to


def _count(result: FundamentalsResult, events: Sequence[Event]) -> None:
    for e in events:
        result.counts[f"{e.statement}:{e.kind}"] += 1
        if e.kind in ("inserted", "corrected", "restated") and e.version is not None:
            decade = f"{e.period[1].year // 10 * 10}s"
            result.by_decade[decade][e.version.knowledge_source] += 1


def _magnitude(change: dict) -> Decimal:
    """The largest relative move among a change's values (1 where one appeared or
    vanished, or moved off zero)."""
    worst = Decimal(0)
    for a, b in change.values():
        if not isinstance(a, Decimal) or not isinstance(b, Decimal):
            worst = max(worst, Decimal(1))
        elif a == 0:
            worst = max(worst, Decimal(1) if b != 0 else Decimal(0))
        else:
            worst = max(worst, abs((b - a) / a))
    return worst


def _entry(e: Event) -> dict:
    fields = sorted(
        e.moved.items(), key=lambda kv: _magnitude({kv[0]: kv[1]}), reverse=True
    )
    entry = {
        "table": fm.TABLES[e.statement],
        "period_type": e.period[0],
        "fiscal_date": e.period[1].isoformat(),
        "moved": {
            c: [None if a is None else str(a), None if b is None else str(b)]
            for c, (a, b) in fields[:FLAG_FIELDS]
        },
    }
    if e.version is not None:
        entry.update(
            version_no=e.version.version_no, known_from=e.version.known_from.isoformat()
        )
    return entry


def _flag(
    db: Database,
    security_id: int,
    key: _Key,
    events: Sequence[Event],
    *,
    run: RunLog,
    backfill: bool,
) -> None:
    """One flag per security and run for its restatements, one for its corrections,
    and one (standing) for its estimated knowledge dates."""
    for check, kinds, severity in (
        (RESTATEMENT, ("restated",), "info" if backfill else "warn"),
        (CORRECTION, ("corrected", "refused"), "info"),
    ):
        hits = [e for e in events if e.kind in kinds]
        if not hits:
            continue
        hits.sort(key=lambda e: _magnitude(e.moved), reverse=True)
        repo.add_dq_flag_once(
            db,
            check_name=check,
            severity=severity,
            security_id=security_id,
            record_key={"ingestion_run_id": run.run_id},
            detail={
                "mode": "backfill" if backfill else "nightly",
                "vendor_key": key.vendor_key,
                "by_table": dict(Counter(fm.TABLES[e.statement] for e in hits)),
                "refused": sum(1 for e in hits if e.kind == "refused"),
                "largest": [_entry(e) for e in hits[:FLAG_ENTRIES]],
            },
            ingestion_run_id=run.run_id,
        )
    estimated = sorted(
        {
            e.period
            for e in events
            if e.kind == "inserted" and e.version.knowledge_source == ESTIMATED
        }
    )
    if estimated:
        repo.add_dq_flag_once(
            db,
            check_name=ESTIMATED_FLAG,
            severity="info",
            security_id=security_id,
            record_key={"vendor": VENDOR, "vendor_key": key.vendor_key},
            detail={
                "periods": len(estimated),
                "first": estimated[0][1].isoformat(),
                "last": estimated[-1][1].isoformat(),
            },
            ingestion_run_id=run.run_id,
        )
