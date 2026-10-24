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
3. **New listings.** A live key first priced in the window, with no link, in a
   category and on a venue fafnir keeps (:data:`LISTING_VENUES`), and not an
   instrument ADR 0012 excludes: minted (``source = 'sharadar'``, active, an open
   ticker period from its first price, a ``minted`` feeding link). Link before
   mint: a key whose ticker a fafnir security already trades under is ``held``,
   never minted, so one issuer cannot become two rows. `fafnir vendor link` decides
   it.
4. **Fed but inactive.** Not windowed, and it changes no security. A security the
   vendor feeds, held inactive here while the vendor lists its key as live and it
   still trades (a bar with volume in the last 5 sessions), is flagged
   ``security_inactive_but_trading`` with what the vendor says (its ticker, the
   rename that moved it there, any other row on that ticker). FMP called many
   renames delistings, and the cutover fed such rows (88 on 2026-10-24): invisible
   to ``stale``, absent from the marts' active universe, and often the same company
   as another row. `fafnir security relist` decides each; a flag whose condition
   has gone is resolved here.

``dry_run`` runs the same steps and rolls them back: the report is what a real run
would do tonight. A real run is the cutover's (#125): the command refuses one
until ``[general] primary_price_vendor = "sharadar"``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import delisted as dl
from fafnir.ingest import sharadar_map
from fafnir.ingest import symbol_changes as sc
from fafnir.ingest import vendor_link as vl
from fafnir.ingest import vendor_overlap as vo
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import (
    INACTIVE_BUT_TRADING_CHECK,
    INACTIVE_BUT_TRADING_SESSIONS,
)
from fafnir.instruments import INSTRUMENT_KINDS, out_of_scope_kind
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
#: Where a new listing is minted: the dead-issuer mint's venues (ADR 0016 §3) and
#: the Texas Stock Exchange, which Sharadar lists and FMP's screener cannot be
#: asked for.
LISTING_VENUES = vo.MINT_VENUES | {"TXSE"}


class UniverseError(RuntimeError):
    """The step cannot run: the vendor tables are empty."""


@dataclass
class Change:
    step: str  # rename | delisting | listing | inactive
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


def fafnir_ticker(value: Optional[str]) -> Optional[str]:
    """The vendor's ticker as fafnir spells it: Sharadar writes a share class with
    a dot (``BRK.B``), FMP and this warehouse with a dash (``BRK-B``)."""
    text = _ticker(value)
    return text.replace(".", "-") if text else None


def latest_rename_to(
    events: list[dict], ticker: str
) -> Optional[tuple[date, str, str]]:
    """The newest of one key's renames that ends on ``ticker`` (fafnir's spelling),
    as ``(date, old, new)``, or None."""
    found = [
        (day, old, new)
        for day, old, new in renames(events)
        if fafnir_ticker(new) == ticker
    ]
    return found[-1] if found else None


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
    """A live listing fafnir keeps: a priced table, a stock or exchange-traded
    category (not a NAV fund), one of :data:`LISTING_VENUES`."""
    if c["vendor_table"] not in vl.PRICED_TABLES:
        return False
    kind = sharadar_map.map_category(c.get("category"))
    if kind is None or kind.asset_type == "fund":
        return False
    return sharadar_map.map_exchange(c.get("exchange")) in LISTING_VENUES


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


def _apply_listings(db, result, candidates, by_key, since, run_id, excluded) -> None:
    fresh = [
        c
        for c in candidates
        if not c["taken"]
        and not c["is_delisted"]
        and c["first_price_date"] is not None
        and c["first_price_date"] >= since
    ]
    sectors, industries = repo.classification_names(db)
    # The FMP security-master load's exemption: a declared symbol is kept.
    declared = (
        {r["symbol"] for r in repo.list_tracked_symbols(db)} if excluded else set()
    )
    for c in sorted(fresh, key=lambda c: c["vendor_key"]):
        key, ticker, day = c["vendor_key"], c["ticker"], c["first_price_date"]
        if not _in_scope(c):
            result.add("listing", "out_of_scope", key, ticker, day)
            continue
        # ADR 0012 by ticker and name, as the FMP load does. Sharadar's categories
        # carry no warrant or right today; this keeps one out if it ever files one
        # as a stock.
        kind = sharadar_map.map_category(c["category"])
        instrument = (
            None
            if ticker in declared
            else out_of_scope_kind(
                ticker,
                c["name"],
                is_etf=kind.is_etf,
                is_fund=kind.is_fund,
                excluded=excluded,
            )
        )
        if instrument:
            result.add("listing", f"out_of_scope:{instrument}", key, ticker, day)
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


#: What the step 4 flag's ``detail.next`` says decides it.
NEXT_RELIST = "relist"  # `security relist`
NEXT_HELD = "held"  # a row with history on its ticker: `merge --across-vendors`
NEXT_PENDING = "pending_decision"  # `security venue-transfer` or `security delist`
NEXT_VENUE = "venue_transfer"  # same ticker, another venue: `security venue-transfer`
NEXT_EARLIER = "earlier_issuer"  # an earlier issuer's bars too: `split-history`

#: How far back step 4 looks for a traded bar (it needs one within
#: :data:`INACTIVE_BUT_TRADING_SESSIONS` sessions; this only bounds the scan).
TRADED_LOOKBACK_DAYS = 30

#: Two codes for one venue: FMP files Cboe BZX listings as CBOE, Sharadar as BATS.
_ONE_VENUE = frozenset({"BATS", "CBOE"})


def same_venue(a: Optional[str], b: Optional[str]) -> bool:
    """True when two ``ref.exchange`` codes name one venue, or either is unknown."""
    if a is None or b is None or a == b:
        return True
    return {a, b} <= _ONE_VENUE


def _flag_inactive_but_fed(db, result, vendor, run_id) -> None:
    """Step 4: flag each security the vendor feeds and lists as live that is held
    inactive here while it still trades, and resolve this step's flags whose
    condition has gone. Changes no security: `security relist` decides each.

    "Still trades" is a bar with volume within :data:`INACTIVE_BUT_TRADING_SESSIONS`
    sessions of the vendor's newest price date: the FMP universe load's window, but
    a traded bar, because the vendor keeps printing zero-volume rows for a halted
    listing it has not called delisted.
    """
    rows = [
        r
        for r in repo.inactive_fed_securities(db, VENDOR)
        if r["vendor_key"] in vendor and not vendor[r["vendor_key"]]["is_delisted"]
    ]
    newest = max(
        (c["last_price_date"] for c in vendor.values() if c["last_price_date"]),
        default=None,
    )
    events: dict[str, list[dict]] = defaultdict(list)
    for e in repo.vendor_events_for_keys(
        db, VENDOR, RENAME_EVENTS, [r["vendor_key"] for r in rows]
    ):
        events[e["vendor_key"]].append(e)
    kept: list[str] = []
    for r in rows:
        sid, key, symbol = int(r["security_id"]), r["vendor_key"], r["primary_symbol"]
        if newest is None:
            break
        traded = repo.last_traded_bar(
            db, sid, newest - timedelta(days=TRADED_LOOKBACK_DAYS)
        )
        if (
            traded is None
            or repo.sessions_apart(db, traded, newest) > INACTIVE_BUT_TRADING_SESSIONS
        ):
            continue
        first_bar, _ = repo.security_bar_span(db, sid)
        earlier = repo.holds_earlier_issuer(
            first_bar=first_bar,
            delisted_date=r["delisted_date"],
            link_from=r["link_from"],
        )
        ticker = fafnir_ticker(vendor[key]["ticker"]) or symbol
        rename = holder = None
        if ticker != symbol:
            rename = latest_rename_to(events[key], ticker)
            holder = repo.active_security_for_symbol(db, ticker, VENDOR)
            if holder == sid:
                holder = None
        held = holder is not None and repo.security_has_history(db, holder)
        venue = sharadar_map.map_exchange(vendor[key].get("exchange"))
        if repo.condition_open(db, security_id=sid, check_name=dl.CONTRADICTED_CHECK):
            hint = NEXT_PENDING
        elif earlier:
            hint = NEXT_EARLIER
        elif held:
            hint = NEXT_HELD
        elif ticker == symbol and not same_venue(venue, r["exchange_code"]):
            hint = NEXT_VENUE
        else:
            hint = NEXT_RELIST
        delisted = r["delisted_date"]
        new = repo.add_dq_flag_once(
            db,
            check_name=INACTIVE_BUT_TRADING_CHECK,
            severity="warn",
            security_id=sid,
            table_name="core.security",
            record_key={"symbol": symbol},
            detail={
                "source": VENDOR,
                "vendor_key": key,
                "vendor_ticker": ticker,
                "vendor_exchange": venue,
                "last_traded": traded.isoformat(),
                "first_bar": first_bar.isoformat() if first_bar else None,
                "link_from": r["link_from"].isoformat(),
                "vendor_newest": newest.isoformat(),
                "delisted_date": delisted.isoformat() if delisted else None,
                "rename": (
                    {"date": rename[0].isoformat(), "old": rename[1], "new": rename[2]}
                    if rename
                    else None
                ),
                "held_by": holder,
                "held_with_history": held,
                "next": hint,
            },
            ingestion_run_id=run_id,
        )
        label = symbol if ticker == symbol else f"{symbol}->{ticker}"
        result.add(
            "inactive", "flagged" if new else "open", key, label, traded, sid, hint
        )
        kept.append(f"{sid}|{symbol}")
    for f in repo.resolve_sourced_flags_except(
        db,
        check_name=INACTIVE_BUT_TRADING_CHECK,
        source=VENDOR,
        keep=kept,
        note="Condition gone: trading here again, no longer fed, or the vendor no "
        "longer lists it live with bars arriving.",
        resolved_by=CREATED_BY,
    ):
        result.add(
            "inactive",
            "resolved",
            (f["detail"] or {}).get("vendor_key") or "",
            (f["record_key"] or {}).get("symbol") or "",
            f["detected_at"].date(),
            int(f["security_id"]),
        )


def _apply(
    db: Database,
    result: UniverseResult,
    run_id: Optional[int],
    excluded: Sequence[str],
) -> None:
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
    _apply_listings(db, result, candidates, by_key, result.since, run_id, excluded)
    _flag_inactive_but_fed(db, result, vendor, run_id)


def run(
    db: Database,
    *,
    since: Optional[date] = None,
    dry_run: bool = False,
    today: Optional[date] = None,
    excluded_kinds: Sequence[str] = INSTRUMENT_KINDS,
) -> UniverseResult:
    """Apply the window's renames, delistings and new listings (one run), or with
    ``dry_run`` apply them and roll back. ``excluded_kinds`` is ``[general]
    exclude_instruments`` (ADR 0012)."""
    excluded = tuple(excluded_kinds)
    today = today or date.today()
    result = UniverseResult(dry_run=dry_run, since=since or default_since(db, today))
    if dry_run:
        with db.conn.transaction(force_rollback=True):
            _apply(db, result, None, excluded)
        return result
    run_log = RunLog(
        db, source=VENDOR, endpoint=ENDPOINT, params={"since": result.since}
    )
    with run_log:
        result.run_id = run_log.run_id
        _apply(db, result, run_log.run_id, excluded)
        counts = result.counts()
        run_log.rows_inserted = counts[("listing", "minted")]
        run_log.params.update(
            counts={f"{s}:{o}": n for (s, o), n in sorted(counts.items())}
        )
        db.commit()
    return result
