"""
`fafnir source reconcile` (``scripts/reconcile.sh``, #117): a sample of the
warehouse's raw closes against a vendor's, date by date.

The weekly timer runs it over a few symbols and 30 days. It began as a drift check
on one vendor: has FMP changed a bar it already served? Since the cutover
(2026-10-22) most of the warehouse's bars are Sharadar's, so against FMP it is also
an independent-vendor check. Which of the two a date is depends on whose bar the
warehouse holds there, so every date is judged on its own:

* ``match``: both sides hold it and the closes agree. Against the vendor that
  supplied the stored bar, agreement is exact. Against another vendor it is
  :mod:`fafnir.ingest.vendor_correct`'s tolerance, a cent or a basis point, so a
  vendor's rounding is not a difference.
* ``corrected``: ``vendor correct`` replaced the compared vendor's bar on that date
  (an open row in ``ops.vendor_correction``). Expected: the vendor still serves the
  bar the warehouse replaced.
* ``operator``: a person deleted or transformed the bar (an open
  ``ops.operator_override``), or wrote it. Expected.
* ``drift``: the stored bar is the compared vendor's own and the vendor now serves
  another, or none; or the vendor feeds the security and serves a bar the warehouse
  lacks. The warehouse no longer matches its own feed.
* ``differs``: the stored bar is another vendor's and the compared vendor
  disagrees, or the compared vendor serves a bar the warehouse lacks for a security
  it does not feed. The vendors disagree.
* ``extra``: the warehouse holds another vendor's bar on a date the compared vendor
  does not serve, a gap the other vendor filled. Listed, and no part of a verdict.

A symbol's verdict is ``DRIFT`` if any date drifted, else ``DIFFERS`` if any
differs, else ``OK``. When FMP serves nothing for a symbol (an outage, or a tier
without the unadjusted endpoint, #128), the symbol is compared with Sharadar over
its last :data:`FALLBACK_SESSIONS` stored sessions instead, when a Sharadar key is
set: a check that the nightly loaded what the vendor serves.

Reads only, like the script before it: it reports, and repairs nothing.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.daily_price import _as_money
from fafnir.ingest.sharadar_bulk import API_TABLE
from fafnir.ingest.vendor_correct import ABS_TOLERANCE, REL_TOLERANCE
from fafnir.sources.base import SourceError

FMP = repo.FMP_SOURCE
SHARADAR = "sharadar"
VENDORS = (FMP, SHARADAR)
VENDOR_NAME = {FMP: "FMP", SHARADAR: "Sharadar"}

DEFAULT_DAYS = 30
#: The stored sessions compared with Sharadar when FMP serves nothing.
FALLBACK_SESSIONS = 5

#: The kinds that decide a verdict, and the verdicts, worst first.
DRIFT, DIFFERS, OK, NOT_COMPARED = "DRIFT", "DIFFERS", "OK", "NOT COMPARED"
EXPECTED = ("corrected", "operator")


def _agrees(stored: Decimal, served: Decimal, *, own: bool) -> bool:
    if own:
        return stored == served
    return abs(stored - served) <= max(ABS_TOLERANCE, REL_TOLERANCE * abs(served))


def classify(
    stored: dict[dt.date, tuple[Decimal, str]],
    served: dict[dt.date, Decimal],
    *,
    vendor: str,
    fed_by_vendor: bool,
    corrected: set[dt.date],
    operator: set[dt.date],
) -> dict[dt.date, str]:
    """The kind of every date either side holds (see the module docstring).

    ``stored`` maps a date to the warehouse's close and its ``source``; ``served``
    to the compared vendor's close. ``fed_by_vendor`` says whether the compared
    vendor feeds the security's new bars, which decides whose fault a bar the
    warehouse lacks is.
    """
    kinds: dict[dt.date, str] = {}
    for day in sorted(set(stored) | set(served)):
        bar, close = stored.get(day), served.get(day)
        if day in operator or (bar is not None and bar[1] == "operator"):
            kinds[day] = "operator"
        elif day in corrected:
            kinds[day] = "corrected"
        elif bar is not None and close is not None:
            own = bar[1] == vendor
            if _agrees(bar[0], close, own=own):
                kinds[day] = "match"
            else:
                kinds[day] = "drift" if own else "differs"
        elif bar is not None:
            kinds[day] = "drift" if bar[1] == vendor else "extra"
        else:
            kinds[day] = "drift" if fed_by_vendor else "differs"
    return kinds


@dataclass
class Line:
    """One date that is not a match, as the report lists it."""

    day: dt.date
    kind: str
    stored: Optional[Decimal]
    stored_source: Optional[str]
    served: Optional[Decimal]


@dataclass
class SymbolResult:
    symbol: str
    vendor: str
    security_id: Optional[int] = None
    feed: Optional[str] = None
    window: Optional[tuple[dt.date, dt.date]] = None
    counts: Counter = field(default_factory=Counter)
    lines: list[Line] = field(default_factory=list)
    #: Why the symbol was not compared, or why the first vendor could not be.
    error: Optional[str] = None
    #: Set when this is the fallback: the vendor that served nothing.
    fallback_from: Optional[str] = None
    fallback_reason: Optional[str] = None

    @property
    def verdict(self) -> str:
        if not self.counts:
            return NOT_COMPARED
        if self.counts["drift"]:
            return DRIFT
        if self.counts["differs"]:
            return DIFFERS
        return OK


# -- the warehouse's side -----------------------------------------------------


def _feed(db: Database, security_id: int) -> Optional[str]:
    for vendor in (SHARADAR, FMP):
        if repo.is_fed_by(db, security_id, vendor):
            return vendor
    return None


def _stored(
    db: Database, security_id: int, start: dt.date, end: dt.date
) -> dict[dt.date, tuple[Decimal, str]]:
    rows = db.fetchall(
        "SELECT trade_date, close, source FROM core.daily_price "
        "WHERE security_id = %s AND trade_date BETWEEN %s AND %s",
        (security_id, start, end),
    )
    return {r["trade_date"]: (r["close"], r["source"]) for r in rows}


def _corrected(
    db: Database, security_id: int, vendor: str, start: dt.date, end: dt.date
) -> set[dt.date]:
    rows = db.fetchall(
        "SELECT DISTINCT key_date FROM ops.vendor_correction "
        "WHERE target = 'daily_price' AND security_id = %s AND old_source = %s "
        "AND reverted_at IS NULL AND key_date BETWEEN %s AND %s",
        (security_id, vendor, start, end),
    )
    return {r["key_date"] for r in rows}


def _overridden(
    db: Database, security_id: int, start: dt.date, end: dt.date
) -> set[dt.date]:
    rows = db.fetchall(
        "SELECT DISTINCT key_date FROM ops.operator_override "
        "WHERE target = 'daily_price' AND security_id = %s AND revoked_at IS NULL "
        "AND key_date BETWEEN %s AND %s",
        (security_id, start, end),
    )
    return {r["key_date"] for r in rows}


def _last_sessions(db: Database, security_id: int, n: int) -> Optional[dt.date]:
    """The first of the security's last ``n`` stored dates."""
    return db.fetchval(
        "SELECT min(trade_date) FROM (SELECT trade_date FROM core.daily_price "
        "WHERE security_id = %s ORDER BY trade_date DESC LIMIT %s) last_n",
        (security_id, n),
    )


def _sharadar_listing(db: Database, security_id: int) -> Optional[tuple[str, str]]:
    """The open Sharadar link's ticker and price table (``SEP`` or ``SFP``)."""
    row = db.fetchone(
        "SELECT vs.ticker, vs.vendor_table FROM core.security_vendor_xref x "
        "JOIN core.vendor_security vs "
        "ON vs.vendor = x.vendor AND vs.vendor_key = x.vendor_key "
        "WHERE x.security_id = %s AND x.vendor = 'sharadar' AND x.valid_to IS NULL "
        "AND vs.vendor_table IN ('stocks', 'funds') "
        "ORDER BY x.valid_from DESC LIMIT 1",
        (security_id,),
    )
    if row is None:
        return None
    return row["ticker"], API_TABLE[row["vendor_table"]]


# -- the vendor's side --------------------------------------------------------


def _served_fmp(fmp, symbol: str, start: dt.date, end: dt.date) -> dict:
    bars = fmp.eod_raw(symbol, start.isoformat(), end.isoformat())
    out: dict[dt.date, Decimal] = {}
    for bar in bars:
        # The unadjusted endpoint names its raw close adjClose; FMPClient maps it
        # to close, and either spelling is the same number.
        close = _as_money(bar["close"] if "close" in bar else bar.get("adjClose"))
        day = str(bar.get("date") or "")[:10]
        if close is not None and day:
            out[dt.date.fromisoformat(day)] = close
    return out


def _served_sharadar(
    client, listing: Optional[tuple[str, str]], symbol: str, start, end
) -> dict:
    """Sharadar's raw closes (``closeunadj``). With no link, the symbol is looked
    up in SEP, then SFP, as ``duk -S sharadar`` does."""
    tables = (
        [(listing[1], listing[0])] if listing else [(t, symbol) for t in ("SEP", "SFP")]
    )
    for table, ticker in tables:
        rows = client.query(
            table, {"ticker": ticker, "from": start.isoformat(), "to": end.isoformat()}
        )
        out: dict[dt.date, Decimal] = {}
        for row in rows:
            close = _as_money(row.get("closeunadj"))
            day = str(row.get("date") or "")[:10]
            if close is not None and day:
                out[dt.date.fromisoformat(day)] = close
        if out:
            return out
    return {}


# -- one symbol ---------------------------------------------------------------


def _compare(
    db: Database,
    result: SymbolResult,
    served: dict[dt.date, Decimal],
    start: dt.date,
    end: dt.date,
) -> None:
    sid, vendor = result.security_id, result.vendor
    stored = _stored(db, sid, start, end)
    kinds = classify(
        stored,
        served,
        vendor=vendor,
        fed_by_vendor=result.feed == vendor,
        corrected=_corrected(db, sid, vendor, start, end),
        operator=_overridden(db, sid, start, end),
    )
    result.window = (start, end)
    result.counts = Counter(kinds.values())
    for day, kind in kinds.items():
        if kind == "match" or kind in EXPECTED:
            continue
        bar = stored.get(day)
        result.lines.append(
            Line(
                day=day,
                kind=kind,
                stored=bar[0] if bar else None,
                stored_source=bar[1] if bar else None,
                served=served.get(day),
            )
        )


def reconcile_symbol(
    db: Database,
    symbol: str,
    *,
    vendor: str = FMP,
    from_date: dt.date,
    to_date: dt.date,
    fmp=None,
    sharadar=None,
    sessions: int = FALLBACK_SESSIONS,
) -> SymbolResult:
    """One symbol against ``vendor``, falling back from FMP to Sharadar.

    ``fmp`` and ``sharadar`` are the vendors' clients, or None where no key is set.
    Against Sharadar (asked for, or as the fallback) the window is the security's
    last ``sessions`` stored dates to ``to_date``.
    """
    symbol = symbol.upper()
    sid = repo.resolve_security_id(db, symbol)
    if sid is None:
        return SymbolResult(symbol, vendor, error="not in the warehouse")
    feed = _feed(db, sid)

    reason = None
    if vendor == FMP:
        result = SymbolResult(symbol, FMP, security_id=sid, feed=feed)
        if fmp is None:
            reason = "no FMP key"
        else:
            try:
                served = _served_fmp(fmp, symbol, from_date, to_date)
            except SourceError as exc:
                reason = f"FMP failed: {exc}"
            else:
                if served:
                    _compare(db, result, served, from_date, to_date)
                    return result
                reason = "FMP served no bar in the window"
        if sharadar is None:
            result.error = f"{reason}; no Sharadar key for the fallback"
            return result

    result = SymbolResult(
        symbol,
        SHARADAR,
        security_id=sid,
        feed=feed,
        fallback_from=FMP if reason else None,
        fallback_reason=reason,
    )
    if sharadar is None:
        result.error = "no Sharadar key"
        return result
    start = _last_sessions(db, sid, sessions)
    if start is None:
        result.error = "no stored bar to compare"
        return result
    try:
        served = _served_sharadar(
            sharadar, _sharadar_listing(db, sid), symbol, start, to_date
        )
    except SourceError as exc:
        result.error = f"Sharadar failed: {exc}"
        return result
    if not served:
        result.error = "Sharadar served no bar in the window"
        return result
    _compare(db, result, served, start, to_date)
    return result


def reconcile(
    db: Database,
    symbols: list[str],
    *,
    vendor: str = FMP,
    from_date: dt.date,
    to_date: dt.date,
    fmp=None,
    sharadar=None,
    sessions: int = FALLBACK_SESSIONS,
) -> list[SymbolResult]:
    return [
        reconcile_symbol(
            db,
            s,
            vendor=vendor,
            from_date=from_date,
            to_date=to_date,
            fmp=fmp,
            sharadar=sharadar,
            sessions=sessions,
        )
        for s in symbols
    ]


# -- the report ---------------------------------------------------------------


def _price(value: Optional[Decimal]) -> str:
    return "-" if value is None else format(value.normalize(), "f")


def _counts(counts: Counter) -> str:
    order = ("match", "corrected", "operator", "drift", "differs", "extra")
    parts = []
    for kind in order:
        if counts[kind]:
            label = f"{kind} (expected)" if kind in EXPECTED else kind
            parts.append(f"{counts[kind]} {label}")
    return ", ".join(parts)


def format_report(results: list[SymbolResult]) -> list[str]:
    """The text the weekly log keeps: a block per symbol, then a total."""
    out: list[str] = []
    for r in results:
        name = VENDOR_NAME[r.vendor]
        head = f"---- {r.symbol}"
        if r.security_id is not None:
            head += f" (security {r.security_id}): warehouse ({r.feed or 'unfed'})"
            head += f" vs {name}"
            if r.window:
                head += f", {r.window[0]} to {r.window[1]}"
        out.append(head + " ----")
        if r.fallback_from:
            out.append(
                f"    {VENDOR_NAME[r.fallback_from]} not compared ({r.fallback_reason});"
                f" compared with {name} instead."
            )
        if r.verdict == NOT_COMPARED:
            out.append(f"    {NOT_COMPARED}: {r.error}")
            continue
        out.append(f"    {sum(r.counts.values())} dates: {_counts(r.counts)}")
        for line in r.lines:
            stored = (
                f"{_price(line.stored)} ({line.stored_source})"
                if line.stored is not None
                else "-"
            )
            out.append(
                f"    {line.day}  warehouse {stored}  {r.vendor} "
                f"{_price(line.served)}  {line.kind}"
            )
        n_drift, n_differs = r.counts["drift"], r.counts["differs"]
        if r.verdict == DRIFT:
            out.append(
                f"    {DRIFT}: the warehouse no longer matches {name}, its own feed, "
                f"on {n_drift} date(s) (review above)"
            )
        if n_differs:
            out.append(
                f"    {DIFFERS}: the warehouse ({r.feed or 'unfed'}) and {name} "
                f"disagree on {n_differs} date(s) (review above)"
            )
        if r.verdict == OK:
            out.append(f"    {OK}: the warehouse matches {name}")
    verdicts = Counter(r.verdict for r in results)
    out.append(
        f"==> {len(results)} symbol(s): "
        + ", ".join(f"{verdicts[v]} {v}" for v in (OK, DIFFERS, DRIFT, NOT_COMPARED))
    )
    return out
