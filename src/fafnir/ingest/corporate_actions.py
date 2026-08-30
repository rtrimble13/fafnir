"""
Corporate-actions loader: splits and cash dividends from FMP.

Handles the field-name variation across FMP responses (numerator/denominator vs
splitFrom/splitTo; dividend vs adjDividend). Validates and upserts idempotently.

THREE WAYS TO FETCH THE SAME EVENTS (see doc/adr/0007-incremental-corporate-actions.md).

``symbol``
    Two unbounded requests per security -- its entire split and dividend history,
    every run. This is what the loader did originally, and it is still the right
    shape for an explicit ``--symbols`` pull and for the initial backfill. As a
    *nightly* job it is 42,800 requests and ~2.5 hours to capture a few hundred
    changed rows, and it grows with every delisting.

``calendar``
    One market-wide ``splits-calendar`` + ``dividends-calendar`` sweep over the window
    since the last one, plus a per-symbol full pull for any security that has never
    had one. Note what does *not* fix this: a per-symbol watermark. The per-symbol
    endpoints take no date window, so a watermark would narrow 42,800 tiny payloads
    while still costing 42,800 requests. The cost is the request count, so the fix is
    to stop asking per symbol -- the natural grain of "what happened since I last
    looked?" is the date, not the security.

``auto``
    ``calendar``, plus the securities the calendar cannot be trusted to cover. Today
    that is the declared fund universe: a fund has no listing venue (ADR 0006), so an
    exchange-oriented calendar feed is not assumed to carry its distributions. A few
    dozen requests a night is cheap next to a coverage gap in the series the operator
    actually reads.

NEVER STORE A FUTURE EX-DATE. Both the calendar feed and the per-symbol ``dividends``
endpoint return dividends that have been *declared* but have not gone ex. Storing one
would give the security an ``adjustment_factor`` row whose ``effective_date`` is in the
future -- and ``mart.v_daily_price_adjusted`` applies, to a price at date t, the factor
at the smallest ``effective_date`` greater than t. Today's close would therefore be
back-adjusted for a dividend that has not happened: the whole series would move on the
announcement and move again on the ex-date, destroying the point-in-time stability
ADR 0001 rests on, silently and with every constraint satisfied. :func:`_within_horizon`
is that guard, and it sits in the shared transform so both paths get it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, NamedTuple, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.daily_price import NAV_ASSET_TYPES
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.actions")

# The endpoint name every corporate-actions run logs under, and the key of the
# per-security "this security has had its full history pulled" watermark.
ENDPOINT = "corporate-actions"

# The market-wide sweep's own watermark: how far forward the calendar has been read,
# for the whole universe at once. ops.load_watermark has carried this case since
# migration 0004 -- `security_id BIGINT NOT NULL DEFAULT 0`, commented
# "0 = whole-endpoint (non per-symbol)" -- so there is no migration to write.
SWEEP_ENDPOINT = "corporate-actions-calendar"
WHOLE_ENDPOINT = 0

MODES = ("symbol", "calendar", "auto")


class Applied(NamedTuple):
    """One stored action: its natural key, and whether this write changed anything."""

    key: tuple[str, date]
    changed: bool


@dataclass
class ActionsResult:
    """What one `fafnir ingest actions` invocation did."""

    run_id: Optional[int] = None
    mode: str = "symbol"
    upserted: int = 0  # valid actions written or confirmed
    changed: int = 0  # of those, rows that were new or actually different
    changed_security_ids: set[int] = field(default_factory=set)
    symbols_pulled: int = 0  # securities fetched on the per-symbol path
    first_loaded: int = 0  # of those, securities getting their first-ever pull
    calendar_rows: int = 0  # rows seen on the market-wide feeds
    unresolved_rows: int = 0  # of those, symbols this warehouse does not hold
    future_skipped: int = 0  # declared but not yet ex -- correctly not stored
    reconciled: int = 0  # securities checked by the rotating reconciliation
    drift: int = 0  # of those, securities where the feed disagreed

    def absorb(self, other: "ActionsResult") -> None:
        """Fold a sub-result's counters in. Identity fields (run_id, mode) are kept."""
        self.upserted += other.upserted
        self.changed += other.changed
        self.changed_security_ids |= other.changed_security_ids
        self.symbols_pulled += other.symbols_pulled
        self.calendar_rows += other.calendar_rows
        self.unresolved_rows += other.unresolved_rows
        self.future_skipped += other.future_skipped


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _split_ratio(rec: dict) -> tuple[Optional[float], Optional[float]]:
    num = rec.get("numerator", rec.get("splitTo"))
    den = rec.get("denominator", rec.get("splitFrom"))
    try:
        num = float(num) if num not in (None, "") else None
        den = float(den) if den not in (None, "") else None
    except (TypeError, ValueError):
        return None, None
    if not num or not den or num <= 0 or den <= 0:
        return None, None
    return num, den


def _dividend_amount(rec: dict) -> Optional[float]:
    """The cash amount to divide into the raw prior close.

    ``dividend`` is the as-declared amount; ``adjDividend`` is restated into today's
    share terms. core.daily_price holds unadjusted prices, so the as-declared amount
    is the one that divides into the raw prior close -- adjDividend is only a fallback
    for rows where FMP omits ``dividend`` entirely.
    """
    amount = rec.get("dividend")
    if amount in (None, ""):
        amount = rec.get("adjDividend")
    try:
        return float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _within_horizon(ex_date: date, as_of: date) -> bool:
    """False for an event that has been declared but has not gone ex. See the module docstring."""
    return ex_date <= as_of


def _apply_split(
    db: Database,
    *,
    security_id: int,
    symbol: str,
    rec: dict,
    run: RunLog,
    as_of: date,
    result: ActionsResult,
) -> Optional[Applied]:
    """Validate and upsert one split row.

    Returns what was stored, or None when the row was not -- invalid (quarantined) or
    declared but not yet ex.
    """
    ex_date = _parse_date(rec.get("date"))
    num, den = _split_ratio(rec)
    if ex_date is None or num is None:
        run.rows_quarantined += 1
        repo.add_dq_flag_once(
            db,
            check_name="split_invalid",
            security_id=security_id,
            table_name="core.corporate_action",
            record_key={"symbol": symbol, "date": str(rec.get("date"))},
            ingestion_run_id=run.run_id,
        )
        return None
    if not _within_horizon(ex_date, as_of):
        result.future_skipped += 1
        return None
    changed = repo.upsert_corporate_action(
        db,
        security_id=security_id,
        action_type="split",
        ex_date=ex_date,
        split_numerator=num,
        split_denominator=den,
        ingestion_run_id=run.run_id,
    )
    result.upserted += 1
    if changed:
        result.changed += 1
        result.changed_security_ids.add(security_id)
    return Applied(("split", ex_date), changed)


def _apply_dividend(
    db: Database,
    *,
    security_id: int,
    symbol: str,
    rec: dict,
    run: RunLog,
    as_of: date,
    result: ActionsResult,
) -> Optional[Applied]:
    """Validate and upsert one dividend row.

    Returns what was stored, or None when the row was not -- invalid (quarantined) or
    declared but not yet ex.
    """
    ex_date = _parse_date(rec.get("date"))
    amount = _dividend_amount(rec)
    if ex_date is None or amount is None or amount < 0:
        run.rows_quarantined += 1
        repo.add_dq_flag_once(
            db,
            check_name="dividend_invalid",
            security_id=security_id,
            table_name="core.corporate_action",
            record_key={"symbol": symbol, "date": str(rec.get("date"))},
            ingestion_run_id=run.run_id,
        )
        return None
    if not _within_horizon(ex_date, as_of):
        result.future_skipped += 1
        return None
    changed = repo.upsert_corporate_action(
        db,
        security_id=security_id,
        action_type="dividend",
        ex_date=ex_date,
        dividend_amount=amount,
        record_date=_parse_date(rec.get("recordDate")),
        payment_date=_parse_date(rec.get("paymentDate")),
        declaration_date=_parse_date(rec.get("declarationDate")),
        ingestion_run_id=run.run_id,
    )
    result.upserted += 1
    if changed:
        result.changed += 1
        result.changed_security_ids.add(security_id)
    return Applied(("dividend", ex_date), changed)


# ---------------------------------------------------------------------------
# The per-symbol path
# ---------------------------------------------------------------------------


class _BorrowedRun:
    """Use a caller's RunLog without closing it, so ``with`` reads the same either way.

    A mixed-mode invocation puts first-loads, the sweep and the reconciliation under
    one ``ops.ingestion_run`` row: three lineage rows for one command would make
    "which run changed this security's actions?" a question with three answers.
    """

    def __init__(self, run: RunLog):
        self._run = run

    def __enter__(self) -> RunLog:
        return self._run

    def __exit__(self, *exc) -> bool:
        return False


def load_symbol_actions(
    db: Database,
    fmp: FMPClient,
    symbol: str,
    security_id: int,
    *,
    run: RunLog,
    as_of: date,
    result: ActionsResult,
    seen: Optional[dict] = None,
) -> None:
    """One security's complete split and dividend history. Two requests.

    ``seen``, when given, maps the ``(action_type, ex_date)`` of every record the feed
    actually carried to whether storing it changed anything. Only :func:`reconcile`
    asks for it, because it is the only caller that needs to know what the feed
    *omitted* -- an upsert-only loader cannot otherwise tell a withdrawn action from
    one it simply never saw.
    """
    splits = fmp.splits(symbol)
    repo.land_payload(
        db,
        endpoint="splits",
        params={"symbol": symbol},
        symbol=symbol,
        http_status=200,
        payload=splits,
        payload_hash=payload_hash(splits),
        nbytes=0,
        ingestion_run_id=run.run_id,
    )
    for rec in splits:
        applied = _apply_split(
            db,
            security_id=security_id,
            symbol=symbol,
            rec=rec,
            run=run,
            as_of=as_of,
            result=result,
        )
        if seen is not None and applied is not None:
            seen[applied.key] = applied.changed

    divs = fmp.dividends(symbol)
    repo.land_payload(
        db,
        endpoint="dividends",
        params={"symbol": symbol},
        symbol=symbol,
        http_status=200,
        payload=divs,
        payload_hash=payload_hash(divs),
        nbytes=0,
        ingestion_run_id=run.run_id,
    )
    for rec in divs:
        applied = _apply_dividend(
            db,
            security_id=security_id,
            symbol=symbol,
            rec=rec,
            run=run,
            as_of=as_of,
            result=result,
        )
        if seen is not None and applied is not None:
            seen[applied.key] = applied.changed
    result.symbols_pulled += 1


def _load_securities(
    db: Database,
    fmp: FMPClient,
    securities: list[dict],
    *,
    run: RunLog,
    as_of: date,
    result: ActionsResult,
) -> None:
    """Full per-symbol pull for securities whose id is already known.

    Stamps the per-security watermark on each success. That watermark records "this
    security's full history has been pulled", not "its last event was on this date",
    so it is stamped with ``as_of`` rather than with the last ex-date -- a security
    that has never paid a dividend must still stop being re-pulled every night.
    """
    for sec in securities:
        load_symbol_actions(
            db,
            fmp,
            sec["symbol"],
            sec["security_id"],
            run=run,
            as_of=as_of,
            result=result,
        )
        repo.set_watermark(db, "fmp", ENDPOINT, as_of, sec["security_id"])
        # Same unit boundary as the price loader: one security's splits, dividends
        # and watermark land together or not at all, and an interruption keeps
        # every security already processed.
        db.commit()


def load_actions(
    db: Database,
    fmp: FMPClient,
    symbols: Iterable[str],
    *,
    run: Optional[RunLog] = None,
    as_of: Optional[date] = None,
    result: Optional[ActionsResult] = None,
) -> ActionsResult:
    """Load splits + dividends for the named symbols, one security at a time.

    The by-name entry point: it resolves each ticker and hands the rest to
    :func:`_load_securities`, so the watermark and commit boundary have one
    definition rather than two that can drift apart.

    Opens its own ``ops.ingestion_run`` unless ``run`` is passed, so it stays usable
    on its own (`fafnir ingest actions --symbols AAPL`) while the mixed-mode driver
    can put every path of one invocation under a single lineage row.
    """
    symbols = list(symbols)
    as_of = as_of or date.today()
    owns_run = run is None
    result = result if result is not None else ActionsResult()
    ctx = (
        RunLog(
            db,
            source="fmp",
            endpoint=ENDPOINT,
            params={"symbols": len(symbols), "mode": "symbol"},
        )
        if owns_run
        else _BorrowedRun(run)
    )
    with ctx as run:
        result.run_id = run.run_id
        securities = []
        for symbol in symbols:
            sec_id = repo.resolve_security_id(db, symbol)
            if sec_id is None:
                logger.warning("Unknown symbol %s; skipping actions", symbol)
                continue
            securities.append({"security_id": sec_id, "symbol": symbol})
        _load_securities(db, fmp, securities, run=run, as_of=as_of, result=result)
        if owns_run:
            run.symbols_requested = len(symbols)
            run.rows_inserted = result.upserted
            run.bytes_downloaded = fmp.bytes_downloaded
        logger.info(
            "Loaded %d corporate actions (%d changed) across %d symbols",
            result.upserted,
            result.changed,
            len(symbols),
        )
    return result


# ---------------------------------------------------------------------------
# The market-wide calendar sweep
# ---------------------------------------------------------------------------


def sweep_calendar(
    db: Database,
    fmp: FMPClient,
    *,
    run: RunLog,
    as_of: date,
    overlap_days: int,
    result: ActionsResult,
) -> None:
    """Read every symbol's events since the sweep watermark. Two requests, typically.

    The window runs from ``watermark - overlap_days`` to ``as_of`` -- **never later**,
    because a dividend reaches the feed when it is declared and storing a future
    ex-date corrupts the whole adjusted series (module docstring). The overlap is what
    lets an amended dividend land: the amount, record date and payment date can all
    move after the event first appears.

    The whole window is one transaction. Unlike the per-symbol path -- which commits
    per security because it is 21,000 of them and must be resumable -- a sweep is a
    few hundred rows, and making the window atomic is what keeps the watermark
    honest: it advances only if every row in it was transformed.
    """
    watermark = repo.get_watermark(db, "fmp", SWEEP_ENDPOINT, WHOLE_ENDPOINT)
    # No watermark means no sweep has ever run. It does NOT mean history is missing:
    # every security with no per-security watermark is getting a full per-symbol pull
    # in this same invocation (see :func:`run_actions`), exactly as a newly minted
    # security's prices are backfilled by the run that mints it. So start at the
    # overlap window rather than at the beginning of time.
    start = (watermark or as_of) - timedelta(days=overlap_days)
    if start > as_of:
        return

    # Most rows on a market-wide feed are for symbols this warehouse does not hold.
    # Resolution is cached per symbol rather than bulk-loaded into a map so that the
    # sweep and every other loader answer "which security is this ticker?" through
    # the same function -- xref first, then primary_symbol, then a ticker the company
    # used to trade under. A second implementation of that precedence is a second
    # thing to keep correct.
    resolved: dict[str, Optional[int]] = {}

    def _resolve(symbol: str) -> Optional[int]:
        if symbol not in resolved:
            resolved[symbol] = repo.resolve_security_id(db, symbol)
        return resolved[symbol]

    for endpoint, fetch, apply in (
        ("splits-calendar", fmp.splits_calendar, _apply_split),
        ("dividends-calendar", fmp.dividends_calendar, _apply_dividend),
    ):
        rows = fetch(start, as_of)
        # One landing row per request window, not one per symbol: landing growth
        # becomes proportional to how many events happened, not to universe size.
        repo.land_payload(
            db,
            endpoint=endpoint,
            params={"from": start.isoformat(), "to": as_of.isoformat()},
            symbol=None,
            http_status=200,
            payload=rows,
            payload_hash=payload_hash(rows),
            nbytes=0,
            ingestion_run_id=run.run_id,
        )
        result.calendar_rows += len(rows)
        for rec in rows:
            symbol = str(rec.get("symbol") or "").strip().upper()
            sec_id = _resolve(symbol) if symbol else None
            if sec_id is None:
                # Not ours. The overwhelming majority of a market-wide feed is other
                # people's securities; counting them is the report, a warning per row
                # would be 15,000 lines of noise and a DQ flag would be a queue of
                # anomalies that are not anomalies.
                result.unresolved_rows += 1
                continue
            apply(
                db,
                security_id=sec_id,
                symbol=symbol,
                rec=rec,
                run=run,
                as_of=as_of,
                result=result,
            )

    # Only now, with both feeds transformed, is the window actually covered.
    repo.set_watermark(db, "fmp", SWEEP_ENDPOINT, as_of, WHOLE_ENDPOINT)
    db.commit()
    logger.info(
        "Calendar sweep %s..%s: %d rows, %d for our universe, %d changed",
        start,
        as_of,
        result.calendar_rows,
        result.calendar_rows - result.unresolved_rows,
        result.changed,
    )


# ---------------------------------------------------------------------------
# The rotating reconciliation
# ---------------------------------------------------------------------------


def reconcile(
    db: Database,
    fmp: FMPClient,
    securities: list[dict],
    *,
    run: RunLog,
    as_of: date,
    settle_days: int,
    result: ActionsResult,
) -> None:
    """Re-pull a slice of the universe the old way and check the sweep against it.

    This is what makes the calendar path safe to trust. A missing event is otherwise
    silent -- no error, no flag, just an adjusted series that is quietly wrong -- so
    coverage is verified on a schedule instead of when somebody eventually notices.
    At the default 1/30 per night every security is checked monthly, for ~1.3% of what
    a full nightly refresh costs.

    Run it AFTER the sweep, never before: anything the sweep was going to pick up
    tonight has been picked up by then, so what the reconciliation still finds
    different is genuine drift rather than a race with the same night's events.

    Differences are repaired *and* flagged. Repairing is not "silently upserting over
    the evidence" -- the flag is the evidence, and it names the security. Leaving
    known-wrong data in place to make a point would be the worse trade.

    ONLY SETTLED EVENTS ARE JUDGED. The two feeds do not update in lockstep: an event
    that went ex last night can be on the calendar and not yet on the per-symbol
    endpoint, or the reverse, and an amount can still be amended. Comparing those
    would flag every security that just went ex, every night -- the queue-as-log
    failure that ``add_dq_flag_once`` exists to prevent. So drift is only reported for
    ex-dates older than ``settle_days`` (the same window the sweep re-reads); anything
    more recent is still repaired, just not called a discrepancy.
    """
    cutoff = as_of - timedelta(days=settle_days)
    for sec in securities:
        symbol, sec_id = sec["symbol"], sec["security_id"]
        stored_before = {
            (r["action_type"], r["ex_date"])
            for r in repo.corporate_actions_for(db, sec_id)
        }
        on_feed: dict = {}
        marker = ActionsResult()
        load_symbol_actions(
            db,
            fmp,
            symbol,
            sec_id,
            run=run,
            as_of=as_of,
            result=marker,
            seen=on_feed,
        )
        result.absorb(marker)
        result.reconciled += 1

        settled_feed = {k for k in on_feed if k[1] <= cutoff}
        settled_stored = {k for k in stored_before if k[1] <= cutoff}
        # On the feed and not in the warehouse: an event the sweep missed. This is the
        # coverage gap the rotation exists to find.
        missing = sorted(f"{t} {d}" for t, d in settled_feed - settled_stored)
        # In the warehouse and no longer on the feed: a withdrawn or corrected-away
        # action. The loader upserts and never deletes, so this is reported rather
        # than acted on -- but comparing against the feed, rather than the warehouse
        # before against the warehouse after, is the only way to see one at all.
        withdrawn = sorted(f"{t} {d}" for t, d in settled_stored - settled_feed)
        # On both sides, but the values moved: an amendment the sweep's overlap
        # window should have caught and did not.
        amended = sorted(
            f"{t} {d}"
            for (t, d), changed in on_feed.items()
            if changed and (t, d) in settled_stored
        )

        if missing or withdrawn or amended:
            result.drift += 1
            result.changed_security_ids.add(sec_id)
            repo.add_dq_flag_once(
                db,
                check_name="corporate_action_drift",
                severity="warn",
                security_id=sec_id,
                table_name="core.corporate_action",
                record_key={"symbol": symbol},
                detail={
                    "missing_from_calendar": missing[:20],
                    "withdrawn_by_source": withdrawn[:20],
                    "amended": amended[:20],
                    "settled_through": cutoff.isoformat(),
                },
                ingestion_run_id=run.run_id,
            )
        db.commit()
    if result.reconciled:
        logger.info(
            "Reconciled %d securities against the per-symbol feed; %d drifted",
            result.reconciled,
            result.drift,
        )


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def run_actions(
    db: Database,
    fmp: FMPClient,
    *,
    mode: str = "symbol",
    symbols: Optional[Iterable[str]] = None,
    as_of: Optional[date] = None,
    overlap_days: int = 7,
    reconcile_buckets: int = 30,
    include_inactive: bool = False,
) -> ActionsResult:
    """One `fafnir ingest actions` invocation, under a single lineage row.

    ``symbols``, when given, always takes the per-symbol path for exactly those
    symbols regardless of ``mode``: that is what an operator asking for
    ``--symbols VFIAX`` means, and what doc/backfill.md documents.

    Order in the mixed modes is deliberate: first-loads, then the sweep, then the
    reconciliation. First-loads go first because a security with no history has
    nothing for the sweep's window to add to; the reconciliation goes last for the
    reason given in :func:`reconcile`.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    as_of = as_of or date.today()
    result = ActionsResult(mode=mode)

    explicit = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
    if explicit:
        return load_actions(db, fmp, explicit, as_of=as_of, result=result)

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"mode": mode, "include_inactive": include_inactive},
        window_to=as_of,
    ) as run:
        result.run_id = run.run_id

        if mode == "symbol":
            _load_securities(
                db,
                fmp,
                repo.universe_securities(db, include_inactive=include_inactive),
                run=run,
                as_of=as_of,
                result=result,
            )
        else:
            # Securities that have never had a full pull: an IPO minted last night, a
            # fund just declared. The sweep only looks forward from its watermark, so
            # without this they would carry no history at all.
            pending = repo.securities_without_actions_watermark(
                db, ENDPOINT, include_inactive=include_inactive
            )
            if mode == "auto":
                # Funds are pulled per symbol every run, not just once: the calendar
                # is not assumed to reach them at all (ADR 0006 -- no listing venue),
                # so a watermark for one proves nothing about its next distribution.
                known = {p["security_id"] for p in pending}
                pending += [
                    f
                    for f in repo.securities_by_asset_type(
                        db, sorted(NAV_ASSET_TYPES), include_inactive=include_inactive
                    )
                    if f["security_id"] not in known
                ]
            result.first_loaded = len(pending)
            _load_securities(db, fmp, pending, run=run, as_of=as_of, result=result)

            sweep_calendar(
                db,
                fmp,
                run=run,
                as_of=as_of,
                overlap_days=overlap_days,
                result=result,
            )

            if reconcile_buckets > 0:
                # Cycle the slice on the day number so consecutive nights take
                # different securities and a full cycle covers everything.
                bucket = as_of.toordinal() % reconcile_buckets
                reconcile(
                    db,
                    fmp,
                    repo.actions_reconciliation_slice(
                        db,
                        buckets=reconcile_buckets,
                        bucket=bucket,
                        include_inactive=include_inactive,
                    ),
                    run=run,
                    as_of=as_of,
                    # The sweep re-reads this window every night anyway, so an event
                    # inside it is not yet evidence the two feeds disagree.
                    settle_days=overlap_days,
                    result=result,
                )

        run.symbols_requested = result.symbols_pulled
        run.rows_inserted = result.upserted
        run.bytes_downloaded = fmp.bytes_downloaded
    return result
