"""
Daily OHLCV loader.

For each symbol: resolve security_id, compute the incremental window from the
watermark (minus an overlap to catch late corrections), fetch raw bars, land the
raw payload, validate each bar at the boundary, quarantine bad bars (never drop),
upsert good bars idempotently, and advance the watermark.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, SourceError, payload_hash

logger = get_logger("ingest.price")

ENDPOINT = "historical-price-eod/non-split-adjusted"

# The endpoint this loader used before the unadjusted-feed fix. Its bars were
# split-adjusted, so anything ingested under it is wrong for core.daily_price and
# its watermarks must not be reused. Kept only so the changeover can be detected.
LEGACY_SPLIT_ADJUSTED_ENDPOINT = "historical-price-eod/full"

# A bar quarantined this many times stops holding the watermark (it stays flagged
# for review, but ingestion is allowed to advance past it).
MAX_QUARANTINE_HOLDS = 5

# FMP's unadjusted (and dividend-adjusted) endpoints prefix the OHLC field names
# with "adj"; the `full` endpoint does not. The prefix is a naming convention on
# those payloads, not a claim that the values carry an extra adjustment.
_OHLC_ALIASES = {
    "open": ("open", "adjOpen"),
    "high": ("high", "adjHigh"),
    "low": ("low", "adjLow"),
    "close": ("close", "adjClose"),
}

# Volume gets back-adjusted the opposite way to price -- a 4:1 split multiplies
# pre-split share counts by 4 -- so a volume that arrived already split-adjusted
# would be inflated by the split ratio SQUARED, not collapsed toward zero. Where a
# payload offers `unadjustedVolume` it is by definition the raw count, so prefer it;
# core.daily_price is defined as raw. See doc/adr/0004-unadjusted-price-feed.md.
_VOLUME_ALIASES = ("unadjustedVolume", "volume")


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _ohlc(bar: dict, field: str):
    """Read one OHLC field, accepting either FMP spelling (``open``/``adjOpen``).

    Returns the first spelling carrying a *usable* price -- numeric and positive --
    so a payload with `"open": 0` next to a valid `"adjOpen": 39.0` is read from the
    field that has the price, rather than being quarantined on the strength of the
    one that does not. Preferring the unprefixed name is only a tie-break between
    two usable values, not a reason to discard a good one.

    When nothing is usable it falls back to the first value that was *present*, so
    the caller still reports the precise reason (``non_positive_price`` for a zero,
    ``missing_or_nonnumeric_ohlc`` for junk) instead of collapsing both into one.
    Raises KeyError when neither spelling carries a value at all.
    """
    present = None
    for key in _OHLC_ALIASES[field]:
        value = bar.get(key)
        if value in (None, ""):
            continue
        if present is None:
            present = value
        try:
            if float(value) > 0:
                return value
        except (TypeError, ValueError):
            continue
    if present is not None:
        return present
    raise KeyError(field)


def _validate_bar(bar: dict) -> tuple[Optional[dict], Optional[str]]:
    """Type and sanity-check a single bar. Returns (clean_row, reason_if_bad)."""
    trade_date = _parse_date(bar.get("date"))
    if trade_date is None:
        return None, "unparseable_date"
    try:
        o = float(_ohlc(bar, "open"))
        h = float(_ohlc(bar, "high"))
        lo = float(_ohlc(bar, "low"))
        c = float(_ohlc(bar, "close"))
    except (KeyError, TypeError, ValueError):
        return None, "missing_or_nonnumeric_ohlc"
    vol = 0
    for key in _VOLUME_ALIASES:
        value = bar.get(key)
        if value not in (None, ""):
            vol = value
            break
    try:
        vol = int(float(vol))
    except (TypeError, ValueError):
        return None, "nonnumeric_volume"
    if min(o, h, lo, c) <= 0:
        return None, "non_positive_price"
    if h < lo or h < o or h < c or lo > o or lo > c:
        return None, "cross_field_violation"
    if vol < 0:
        return None, "negative_volume"
    vwap = bar.get("vwap")
    try:
        vwap = float(vwap) if vwap not in (None, "") else None
    except (TypeError, ValueError):
        vwap = None
    return {
        "trade_date": trade_date,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": vol,
        "vwap": vwap,
    }, None


def load_symbol_prices(
    db: Database,
    fmp: FMPClient,
    symbol: str,
    *,
    run: RunLog,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    overlap_days: int = 5,
    stats: Optional[dict] = None,
) -> int:
    """Load one symbol's prices within the (incremental) window.

    Returns rows upserted. ``stats``, when given, accumulates outcomes that only mean
    something in aggregate -- see :func:`load_prices`, which uses them to tell a
    genuinely empty load apart from a successful one.
    """

    def _tally(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    sec_id = repo.resolve_security_id(db, symbol)
    if sec_id is None:
        logger.warning("Unknown symbol %s; skipping (load securities first)", symbol)
        _tally("unknown")
        return 0

    if start_date is None:
        wm = repo.get_watermark(db, "fmp", ENDPOINT, sec_id)
        if wm is not None:
            start_date = wm - timedelta(days=overlap_days)

    bars = fmp.eod_raw(
        symbol,
        from_date=start_date.isoformat() if start_date else None,
        to_date=end_date.isoformat() if end_date else None,
    )

    repo.land_payload(
        db,
        endpoint=ENDPOINT,
        params={
            "symbol": symbol,
            "from": start_date.isoformat() if start_date else None,
            "to": end_date.isoformat() if end_date else None,
        },
        symbol=symbol,
        http_status=200,
        payload=bars,
        payload_hash=payload_hash(bars),
        nbytes=0,
        ingestion_run_id=run.run_id,
    )

    if stats is not None:
        stats["bars"] = stats.get("bars", 0) + len(bars)
    if not bars:
        # Routine on an incremental run (no new session yet); a red flag on a
        # backfill. Only the caller can tell which, so record and move on.
        _tally("empty")
        logger.debug("No bars returned for %s (from=%s)", symbol, start_date)

    clean: list[dict] = []
    quarantined_dates: list[date] = []
    for bar in bars:
        row, reason = _validate_bar(bar)
        if reason:
            run.rows_quarantined += 1
            repo.add_dq_flag(
                db,
                check_name=f"price_{reason}",
                severity="warn",
                security_id=sec_id,
                table_name="core.daily_price",
                record_key={"symbol": symbol, "date": str(bar.get("date"))},
                detail={"reason": reason},
                ingestion_run_id=run.run_id,
            )
            # Remember the (parseable) date so the watermark won't skip past it.
            qd = _parse_date(bar.get("date"))
            if qd is not None:
                quarantined_dates.append(qd)
            continue
        row["security_id"] = sec_id
        clean.append(row)

    written = repo.upsert_daily_prices(db, clean, ingestion_run_id=run.run_id)

    # Advance the watermark only up to the latest *contiguous* clean date: never
    # past the earliest quarantined bar, so the overlap re-fetches that date next
    # run and a later upstream correction can still land (no permanent gap).
    #
    # Bounded: a date that has already been quarantined MAX_QUARANTINE_HOLDS times
    # stops holding the line (it stays flagged for review, but the watermark is
    # allowed past it) so a permanently-bad bar can't stall ingestion forever and
    # grow the re-pull window without bound.
    holding = [
        qd
        for qd in quarantined_dates
        if repo.count_price_quarantines(db, sec_id, qd.isoformat())
        < MAX_QUARANTINE_HOLDS
    ]
    clean_dates = [r["trade_date"] for r in clean]
    if holding:
        cutoff = min(holding)
        safe_dates = [d for d in clean_dates if d < cutoff]
    else:
        safe_dates = clean_dates
    if safe_dates:
        repo.set_watermark(db, "fmp", ENDPOINT, max(safe_dates), sec_id)

    run.rows_inserted += written
    return written


def _guard_split_adjusted_changeover(db: Database, start_date: Optional[date]) -> None:
    """Refuse to run incrementally on a warehouse still holding split-adjusted prices.

    Switching to the unadjusted endpoint also changes the watermark key, so every
    symbol looks brand new. On an incremental run that means ``from_date=None``, and
    a bare request is capped at 5000 bars -- the warehouse would quietly refill with
    ~20 years of history and keep the old split-adjusted rows for everything older.
    A backfill with an explicit ``--from`` is the only safe path across the boundary,
    so make the operator take it.
    """
    if start_date is not None:
        return  # explicit window: this IS the re-backfill.
    if repo.count_watermarks(db, "fmp", ENDPOINT):
        return  # already loading from the unadjusted feed.
    legacy = repo.count_watermarks(db, "fmp", LEGACY_SPLIT_ADJUSTED_ENDPOINT)
    if not legacy:
        return  # a fresh warehouse: nothing to migrate.
    raise SourceError(
        f"{legacy} symbol(s) were last loaded from "
        f"{LEGACY_SPLIT_ADJUSTED_ENDPOINT}, whose bars are split-adjusted. "
        f"core.daily_price must hold raw prices, so those rows have to be replaced, "
        "not appended to. Re-backfill explicitly (see doc/backfill.md, "
        "'Switching to the unadjusted price feed'):\n"
        "  fafnir ingest prices --include-inactive --from <backfill-start>\n"
        "then `fafnir adjust` and `fafnir db refresh-marts`. An incremental run "
        "cannot cross this boundary safely and has been refused."
    )


def load_prices(
    db: Database,
    fmp: FMPClient,
    symbols: Iterable[str],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    overlap_days: int = 5,
) -> int:
    """Load prices for many symbols.

    Raises rather than returning a quiet zero when the load cannot have worked.
    A run that reports success having written nothing is the most expensive kind
    of failure here: ``initial_backfill.sh`` would march on to the adjustment and
    mart steps, and the warehouse would look finished while holding no prices.
    """
    symbols = list(symbols)
    if not symbols:
        raise ValueError(
            "No symbols to load. Populate the security master first "
            "(fafnir ingest securities), or pass --symbols."
        )
    _guard_split_adjusted_changeover(db, start_date)

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"symbols": len(symbols)},
        window_from=start_date,
        window_to=end_date,
    ) as run:
        total = 0
        stats: dict[str, int] = {}
        for symbol in symbols:
            total += load_symbol_prices(
                db,
                fmp,
                symbol,
                run=run,
                start_date=start_date,
                end_date=end_date,
                overlap_days=overlap_days,
                stats=stats,
            )
            # One symbol -- its landing payload, bars, DQ flags and watermark --
            # is the unit of work. Committing here is what makes the backfill
            # resumable in practice: an interruption costs this symbol, and the
            # watermarks already written let a re-run skip what is done.
            db.commit()
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded

        unknown = stats.get("unknown", 0)
        empty = stats.get("empty", 0)
        bars = stats.get("bars", 0)
        if unknown == len(symbols):
            raise ValueError(
                f"None of the {len(symbols)} requested symbols exist in the "
                "security master -- run `fafnir ingest securities` first"
            )
        if unknown:
            logger.warning(
                "%d of %d symbols are not in the security master", unknown, len(symbols)
            )
        # An explicit window means we asked for history that must exist. With no
        # window this is the incremental path, where "nothing new" is the normal
        # answer outside trading hours and must not fail the nightly run.
        if start_date is not None and bars == 0:
            raise SourceError(
                f"FMP returned no bars for any of {len(symbols)} symbols since "
                f"{start_date} -- treating a backfill that loaded nothing as a failure"
            )
        if empty:
            logger.info(
                "%d of %d symbols returned no bars in the requested window",
                empty,
                len(symbols),
            )
        logger.info("Loaded %d price rows across %d symbols", total, len(symbols))
        return total
