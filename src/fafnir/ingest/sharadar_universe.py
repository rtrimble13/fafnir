"""
`fafnir ingest sharadar-universe`: Sharadar's renames, delistings and new listings,
applied through the xref (plan SA-0904, #93; ADR 0016 §1).

From cutover Sharadar's events drive the universe instead of FMP's three sweeps
(`ingest symbol-changes`, `securities`, `delisted`). Those resolve a feed row by
``(source, ticker)``, which would find none of the FMP-minted securities under
``source = 'sharadar'``. This resolves by the permaticker: a vendor key's open link
in ``core.security_vendor_xref`` is the security. It reads the vendor tables
`ingest sharadar-master` loads, so that runs first.

It takes the TICKERS/ACTIONS events dated in a window: from :data:`LOOKBACK_DAYS`
before the last real run (or before today, the first time), since ACTIONS records
some events days late. Every step is safe to re-run over the same window:

1. **Renames.** ``tickerchangefrom`` names the old ticker and ``tickerchangeto``
   the new one, both filed under the issuer's current ticker. A linked security
   still trading as the old ticker moves to the new one through
   :func:`repository.apply_rename`, the FMP sweep's own rules: an instrument-
   changing rename is refused, an empty duplicate holding the new ticker is
   folded. Each is recorded in ``core.symbol_change`` with ``source =
   'sharadar'``; one already terminal is not reconsidered. A security already on
   the new ticker (FMP's sweep got there first) is ``already``; one on a third
   ticker is a ``conflict`` (``ticker_mismatch``), retried on the next run.
2. **Delistings.** A ``delisted`` event marks the linked security delisted on that
   date (:func:`repository.mark_delisted`), unless TICKERS no longer lists the key
   as delisted, a later ``listed`` event relisted it, its own bars contradict the
   date (``delisted.delisting_contradicted``, flagged
   ``security_delisting_contradicted`` as the FMP sweep does), or an operator
   already decided that flag.
3. **New listings.** A live key first priced in the window, in a category and on a
   venue ADR 0016 keeps, with no link: minted (``source = 'sharadar'``, active, an
   open ticker period from its first price, a ``minted`` feeding link). Link before
   mint: a key whose ticker a fafnir security already trades under is ``held``,
   never minted, so one issuer cannot become two rows. `fafnir vendor link` decides
   it.

``dry_run`` runs the same steps and rolls them back: the report is what a real run
would do tonight. A real run is the cutover's (#125): the command refuses one
until ``[general] primary_price_vendor = "sharadar"``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import delisted as dl
from fafnir.ingest import sharadar_map
from fafnir.ingest import symbol_changes as sc
from fafnir.ingest import vendor_link as vl
from fafnir.ingest import vendor_overlap as vo
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger

logger = get_logger("ingest.sharadar_universe")

VENDOR = "sharadar"
ENDPOINT = "universe"
CREATED_BY = "fafnir ingest sharadar-universe"
#: How far before the last real run the window opens: ACTIONS records some events
#: days after they happen.
LOOKBACK_DAYS = 30
RENAME_EVENTS = ("tickerchangefrom", "tickerchangeto")
#: The vendor's reason for a delisting, reported beside it (and read by
#: mart.v_security_lifecycle).
REASON_EVENTS = (
    "acquisitionby",
    "mergerto",
    "bankruptcyliquidation",
    "regulatorydelisting",
    "voluntarydelisting",
)
EVENTS = RENAME_EVENTS + ("delisted", "listed") + REASON_EVENTS


class UniverseError(RuntimeError):
    """The step cannot run: the vendor tables are empty."""


@dataclass
class Change:
    step: str  # rename | delisting | listing
    outcome: str
    vendor_key: str
    symbol: str  # OLD->NEW for a rename
    day: date
    security_id: Optional[int] = None
    detail: str = ""


@dataclass
class UniverseResult:
    dry_run: bool
    since: date
    run_id: Optional[int] = None
    changes: list[Change] = field(default_factory=list)

    def counts(self) -> Counter:
        return Counter((c.step, c.outcome) for c in self.changes)

    def add(self, *args, **kwargs) -> Change:
        change = Change(*args, **kwargs)
        self.changes.append(change)
        return change


def _ticker(value: Optional[str]) -> Optional[str]:
    """A ticker as the vendor spells it, or None for its ``N/A``."""
    text = (value or "").strip().upper()
    return None if text in ("", "N/A") else text


def renames(events: list[dict]) -> list[tuple[date, str, str]]:
    """``(date, old, new)`` for one key's ticker changes, oldest first.

    Both events of a change are filed under the issuer's ticker at load time, so on
    a chain (A to B to C) neither event's own ticker is the change's: the old one is
    ``tickerchangefrom``'s other party, the new one ``tickerchangeto``'s. With only
    the first, the new ticker is the one it is filed under.
    """
    by_day: dict[date, dict] = defaultdict(dict)
    for e in events:
        day = by_day[e["event_date"]]
        if e["action"] == "tickerchangefrom":
            day["old"] = _ticker(e["contra_ticker"])
            day["filed"] = _ticker(e["ticker"])
        elif e["action"] == "tickerchangeto":
            day["new"] = _ticker(e["contra_ticker"])
    out = []
    for d in sorted(by_day):
        old = by_day[d].get("old")
        new = by_day[d].get("new") or by_day[d].get("filed")
        if old and new and old != new:
            out.append((d, old, new))
    return out


def default_since(db: Database, today: date) -> date:
    """:data:`LOOKBACK_DAYS` before the last real run, or before ``today``."""
    last = repo.last_successful_run(db, VENDOR, ENDPOINT)
    anchor = last["started_at"].date() if last else today
    return anchor - timedelta(days=LOOKBACK_DAYS)


def _apply_renames(db, result, by_key, links, symbols, run_id) -> None:
    for key in sorted(by_key):
        for day, old, new in renames(by_key[key]):
            sid = links.get(key)
            label = f"{old}->{new}"
            if sid is None:
                result.add("rename", "not_linked", key, label, day)
                continue
            prior = repo.symbol_change_status(
                db, old_symbol=old, new_symbol=new, change_date=day, source=VENDOR
            )
            if prior in repo.TERMINAL_CHANGE_STATUSES:
                result.add("rename", f"recorded_{prior}", key, label, day, sid)
                continue
            current = symbols.get(sid)
            if current == new:
                outcome, reason = repo.CHANGE_APPLIED, "already"
            elif current != old:
                outcome, reason = repo.CHANGE_CONFLICT, f"ticker_mismatch:{current}"
            else:
                done = repo.apply_rename(
                    db,
                    security_id=sid,
                    old_symbol=old,
                    new_symbol=new,
                    change_date=day,
                    source=VENDOR,
                )
                outcome, reason = done.status, done.reason or ""
                if done.status == repo.CHANGE_APPLIED:
                    symbols[sid] = new
            repo.record_symbol_change(
                db,
                old_symbol=old,
                new_symbol=new,
                change_date=day,
                status=outcome,
                security_id=sid,
                detail={"vendor_key": key, "reason": reason, "run_id": run_id},
                source=VENDOR,
            )
            if outcome == repo.CHANGE_CONFLICT:
                # The FMP sweep's notification, keyed the same way: once per
                # (old, new), however many runs retry it.
                repo.add_dq_flag_once(
                    db,
                    check_name="symbol_change_conflict",
                    severity="error",
                    security_id=sid,
                    table_name="core.security",
                    record_key={"old_symbol": old, "new_symbol": new},
                    detail={
                        "reason": reason,
                        "change_date": str(day),
                        "source": VENDOR,
                        "vendor_key": key,
                    },
                    ingestion_run_id=run_id,
                )
            else:
                sc.close_applied_conflict(
                    db, old=old, new=new, change_date=day, security_id=sid
                )
            shown = "already" if reason == "already" else outcome
            result.add("rename", shown, key, label, day, sid, reason)


def _apply_delistings(db, result, by_key, links, vendor, run_id) -> None:
    for key in sorted(by_key):
        events = by_key[key]
        dates = [e["event_date"] for e in events if e["action"] == "delisted"]
        if not dates:
            continue
        day = max(dates)
        sid = links.get(key)
        symbol = (vendor.get(key) or {}).get("ticker") or key
        if sid is None:
            result.add("delisting", "not_linked", key, symbol, day)
            continue
        row = db.fetchone(
            "SELECT primary_symbol, delisted_date FROM core.security "
            "WHERE security_id = %s",
            (sid,),
        )
        symbol = row["primary_symbol"]
        reasons = sorted(
            {
                e["action"]
                for e in events
                if e["action"] in REASON_EVENTS
                and abs((e["event_date"] - day).days) <= LOOKBACK_DAYS
            }
        )
        why = ",".join(reasons)
        if row["delisted_date"] is not None:
            result.add("delisting", "already", key, symbol, day, sid, why)
            continue
        if not (vendor.get(key) or {}).get("is_delisted"):
            result.add("delisting", "declined:vendor_lists_it", key, symbol, day, sid)
            continue
        if any(e["action"] == "listed" and e["event_date"] > day for e in events):
            result.add("delisting", "declined:relisted", key, symbol, day, sid)
            continue
        status = repo.condition_status(
            db,
            security_id=sid,
            check_name=dl.CONTRADICTED_CHECK,
            record_key=dl.contradiction_key(symbol, day),
        )
        if status is not None:
            outcome = "known_transfer" if status == "accepted" else "pending_decision"
            result.add("delisting", f"declined:{outcome}", key, symbol, day, sid)
            continue
        first_bar, last_bar = repo.security_bar_span(db, sid)
        contradiction = dl.delisting_contradicted(day, first_bar, last_bar)
        if contradiction is not None:
            repo.add_dq_flag_once(
                db,
                check_name=dl.CONTRADICTED_CHECK,
                severity="warn",
                security_id=sid,
                table_name="core.security",
                record_key=dl.contradiction_key(symbol, day),
                detail={
                    "source": VENDOR,
                    "vendor_key": key,
                    "reason": contradiction,
                    "first_bar": first_bar.isoformat() if first_bar else None,
                    "last_bar": last_bar.isoformat() if last_bar else None,
                },
                ingestion_run_id=run_id,
            )
            result.add("delisting", f"declined:{contradiction}", key, symbol, day, sid)
            continue
        repo.mark_delisted(db, security_id=sid, delisted_date=day)
        result.add("delisting", "marked", key, symbol, day, sid, why)


def _in_scope(c: dict) -> bool:
    """A live listing ADR 0016 keeps: a priced table, a stock or exchange-traded
    category (not a NAV fund), a screener venue."""
    if c["vendor_table"] not in vl.PRICED_TABLES:
        return False
    kind = sharadar_map.map_category(c.get("category"))
    if kind is None or kind.asset_type == "fund":
        return False
    return sharadar_map.map_exchange(c.get("exchange")) in vo.MINT_VENUES


def _mint_listing(db, c, ipo, sectors, industries, run_id) -> int:
    kind = sharadar_map.map_category(c["category"])
    venue = sharadar_map.map_exchange(c["exchange"])
    sector, industry = sharadar_map.map_classification(
        c.get("sector"), c.get("industry"), sectors=sectors, industries=industries
    )
    repo.ensure_exchange(db, venue)
    sid = repo.upsert_security(
        db,
        primary_symbol=c["ticker"],
        company_name=c["name"],
        asset_type=kind.asset_type,
        exchange_code=venue,
        sector_id=repo.get_or_create_sector(db, sector),
        industry_id=repo.get_or_create_industry(db, industry),
        is_actively_trading=True,
        is_etf=kind.is_etf,
        is_fund=kind.is_fund,
        ipo_date=ipo,
        cik=c["cik"],
        source=VENDOR,
    )
    repo.upsert_symbol_xref(
        db,
        security_id=sid,
        symbol=c["ticker"],
        valid_from=c["first_price_date"],
        source=VENDOR,
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor=VENDOR,
        vendor_key=c["vendor_key"],
        valid_from=c["first_price_date"],
        match_method="minted",
        confidence=None,
        evidence={
            "run_id": run_id,
            "vendor_table": c["vendor_table"],
            "category": c["category"],
            "listing": True,
        },
        created_by=CREATED_BY,
        is_feed=True,
    )
    return sid


def _apply_listings(db, result, candidates, by_key, since, run_id) -> None:
    fresh = [
        c
        for c in candidates
        if not c["taken"]
        and not c["is_delisted"]
        and c["first_price_date"] is not None
        and c["first_price_date"] >= since
    ]
    sectors, industries = repo.classification_names(db)
    for c in sorted(fresh, key=lambda c: c["vendor_key"]):
        key, ticker, day = c["vendor_key"], c["ticker"], c["first_price_date"]
        if not _in_scope(c):
            result.add("listing", "out_of_scope", key, ticker, day)
            continue
        holder = repo.active_security_for_symbol(db, ticker, VENDOR)
        if holder is not None:
            result.add("listing", "held", key, ticker, day, holder)
            continue
        listed = [
            e["event_date"] for e in by_key.get(key, ()) if e["action"] == "listed"
        ]
        sid = _mint_listing(
            db, c, min(listed) if listed else day, sectors, industries, run_id
        )
        result.add("listing", "minted", key, ticker, day, sid)


def _apply(db: Database, result: UniverseResult, run_id: Optional[int]) -> None:
    candidates = repo.vendor_link_candidates(db, VENDOR, vl.PRICED_TABLES)
    if not candidates:
        raise UniverseError(
            "core.vendor_security holds no Sharadar stocks or funds: "
            "`fafnir ingest sharadar-master` first"
        )
    vendor = {c["vendor_key"]: c for c in candidates}
    by_key: dict[str, list[dict]] = defaultdict(list)
    for e in repo.vendor_events_since(db, VENDOR, EVENTS, result.since):
        by_key[e["vendor_key"]].append(e)
    links = repo.open_vendor_links(db, VENDOR)
    linked = sorted({links[k] for k in by_key if k in links})
    symbols = {
        int(r["security_id"]): r["primary_symbol"]
        for r in db.fetchall(
            "SELECT security_id, primary_symbol FROM core.security "
            "WHERE security_id = ANY(%s)",
            (linked,),
        )
    }
    _apply_renames(db, result, by_key, links, symbols, run_id)
    _apply_delistings(db, result, by_key, links, vendor, run_id)
    _apply_listings(db, result, candidates, by_key, result.since, run_id)


def run(
    db: Database,
    *,
    since: Optional[date] = None,
    dry_run: bool = False,
    today: Optional[date] = None,
) -> UniverseResult:
    """Apply the window's renames, delistings and new listings (one run), or with
    ``dry_run`` apply them and roll back."""
    today = today or date.today()
    result = UniverseResult(dry_run=dry_run, since=since or default_since(db, today))
    if dry_run:
        with db.conn.transaction(force_rollback=True):
            _apply(db, result, None)
        return result
    run_log = RunLog(
        db, source=VENDOR, endpoint=ENDPOINT, params={"since": result.since}
    )
    with run_log:
        result.run_id = run_log.run_id
        _apply(db, result, run_log.run_id)
        counts = result.counts()
        run_log.rows_inserted = counts[("listing", "minted")]
        run_log.params.update(
            counts={f"{s}:{o}": n for (s, o), n in sorted(counts.items())}
        )
        db.commit()
    return result
