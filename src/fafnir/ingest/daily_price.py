"""
Daily OHLCV loader.

For each symbol: resolve security_id, compute the incremental window from the
watermark (minus an overlap to catch late corrections), fetch raw bars, land the
raw payload, validate each bar at the boundary, quarantine bad bars (never drop),
upsert good bars idempotently, and advance the watermark.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
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

# What core.daily_price can actually store (sql/migrations/0005_daily_price.up.sql):
# money is NUMERIC(20, 6), volume is BIGINT. Postgres rounds a value to the column's
# scale on insert and only *then* evaluates ck_daily_price_positive, so a positive
# but sub-resolution price -- a delisted sub-penny shell quoted at 1e-7 -- is stored
# as 0.000000 and fails the constraint, aborting the whole batch. Asking `float(v) > 0`
# is therefore the wrong question: a bar has to survive the column, not Python. Every
# numeric field below is coerced to what the column would hold before it is judged.
_MONEY_SCALE = Decimal("0.000001")  # NUMERIC(20, 6) -> 6 decimal places
_MONEY_MAX = Decimal(10) ** 14  # NUMERIC(20, 6) -> 14 integer digits
_VOLUME_MAX = 2**63 - 1  # BIGINT

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


def _decimal(value) -> Optional[Decimal]:
    """Parse one feed value as an exact Decimal, or None if it is not a finite number.

    Goes through ``str`` so a float's binary noise is not carried into the warehouse:
    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827, ``Decimal("0.1")`` is 0.1.
    NaN and the infinities parse but are not numbers a price column can hold, so they
    are rejected here rather than blowing up further down.
    """
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return parsed if parsed.is_finite() else None


def _as_money(value) -> Optional[Decimal]:
    """Quantize a price to NUMERIC(20, 6), or None if the column cannot hold it.

    One None covers every way a field fails to carry a usable price -- junk, zero or
    negative, sub-resolution, or wider than the column -- because the caller that
    needs the precise quarantine reason gets it from :func:`_reject_reason`.
    """
    parsed = _decimal(value)
    if parsed is None:
        return None
    try:
        rounded = parsed.quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None  # more digits than the column could hold anyway
    if rounded <= 0 or rounded >= _MONEY_MAX:
        return None
    return rounded


def _reject_reason(value) -> str:
    """Say why a present-but-unusable price was rejected, for the DQ review queue.

    The distinction is what an operator acts on: a zero means the feed sent nothing
    tradeable, while a sub-resolution price means the feed sent a real quote this
    warehouse cannot represent -- different data, different remedy.
    """
    parsed = _decimal(value)
    if parsed is None:
        return "missing_or_nonnumeric_ohlc"
    if parsed <= 0:
        return "non_positive_price"
    if parsed >= _MONEY_MAX:
        return "price_out_of_range"
    return "subresolution_price"


def _as_volume(value) -> Optional[int]:
    """Parse a volume as an int, truncating toward zero; None if it is not a number."""
    parsed = _decimal(value)
    return None if parsed is None else int(parsed)


def _as_vwap(value) -> Optional[Decimal]:
    """Quantize vwap to NUMERIC(20, 6), or None when absent, junk or too wide.

    vwap is nullable and carries no CHECK, so only precision can break the insert.
    An unusable one is dropped rather than failing an otherwise good bar -- unlike
    OHLC, a missing vwap costs nothing.
    """
    parsed = _decimal(value)
    if parsed is None:
        return None
    try:
        rounded = parsed.quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None
    return None if abs(rounded) >= _MONEY_MAX else rounded


def _ohlc(bar: dict, field: str) -> tuple[Optional[Decimal], Optional[str]]:
    """Read one OHLC field, accepting either FMP spelling (``open``/``adjOpen``).

    Returns ``(price, None)`` for the first spelling carrying a *storable* price --
    one core.daily_price will hold as a positive value -- so a payload with
    ``"open": 0`` (or ``"open": 1e-7``) next to a valid ``"adjOpen": 39.0`` is read
    from the field that has the price, rather than being quarantined on the strength
    of the one that does not. Preferring the unprefixed name is only a tie-break
    between two storable values, not a reason to discard a good one.

    When nothing is storable it returns ``(None, reason)`` for the first value that
    was *present*, so the caller reports the precise reason instead of collapsing
    every failure into one. Neither spelling present is its own reason.
    """
    present = None
    for key in _OHLC_ALIASES[field]:
        value = bar.get(key)
        if value in (None, ""):
            continue
        if present is None:
            present = value
        price = _as_money(value)
        if price is not None:
            return price, None
    if present is None:
        return None, "missing_or_nonnumeric_ohlc"
    return None, _reject_reason(present)


def _validate_bar(bar: dict) -> tuple[Optional[dict], Optional[str]]:
    """Type and sanity-check a single bar. Returns (clean_row, reason_if_bad).

    Prices come back as Decimal, not float: core.daily_price is exact NUMERIC, and
    handing psycopg a float would reintroduce a coercion after validation -- exactly
    where the sub-resolution bug lived. What is checked here is what gets stored.
    """
    trade_date = _parse_date(bar.get("date"))
    if trade_date is None:
        return None, "unparseable_date"
    prices: dict[str, Decimal] = {}
    for field in ("open", "high", "low", "close"):
        price, reason = _ohlc(bar, field)
        if reason:
            return None, reason
        prices[field] = price
    o, h, lo, c = prices["open"], prices["high"], prices["low"], prices["close"]

    raw_volume = 0
    for key in _VOLUME_ALIASES:
        value = bar.get(key)
        if value not in (None, ""):
            raw_volume = value
            break
    vol = _as_volume(raw_volume)
    if vol is None:
        return None, "nonnumeric_volume"
    if h < lo or h < o or h < c or lo > o or lo > c:
        return None, "cross_field_violation"
    if vol < 0:
        return None, "negative_volume"
    if vol > _VOLUME_MAX:
        return None, "volume_out_of_range"
    return {
        "trade_date": trade_date,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": vol,
        "vwap": _as_vwap(bar.get("vwap")),
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
