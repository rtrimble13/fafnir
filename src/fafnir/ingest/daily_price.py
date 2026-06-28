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
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.price")

ENDPOINT = "historical-price-eod/full"


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _validate_bar(bar: dict) -> tuple[Optional[dict], Optional[str]]:
    """Type and sanity-check a single bar. Returns (clean_row, reason_if_bad)."""
    trade_date = _parse_date(bar.get("date"))
    if trade_date is None:
        return None, "unparseable_date"
    try:
        o = float(bar["open"])
        h = float(bar["high"])
        lo = float(bar["low"])
        c = float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return None, "missing_or_nonnumeric_ohlc"
    vol = bar.get("volume", 0) or 0
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
) -> int:
    """Load prices for one symbol within the (incremental) window. Returns rows upserted."""
    sec_id = repo.resolve_security_id(db, symbol)
    if sec_id is None:
        logger.warning("Unknown symbol %s; skipping (load securities first)", symbol)
        return 0

    if start_date is None:
        wm = repo.get_watermark(db, "fmp", ENDPOINT, sec_id)
        if wm is not None:
            start_date = wm - timedelta(days=overlap_days)

    bars = fmp.eod_full(
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
    clean_dates = [r["trade_date"] for r in clean]
    if quarantined_dates:
        cutoff = min(quarantined_dates)
        safe_dates = [d for d in clean_dates if d < cutoff]
    else:
        safe_dates = clean_dates
    if safe_dates:
        repo.set_watermark(db, "fmp", ENDPOINT, max(safe_dates), sec_id)

    run.rows_inserted += written
    return written


def load_prices(
    db: Database,
    fmp: FMPClient,
    symbols: Iterable[str],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    overlap_days: int = 5,
) -> int:
    symbols = list(symbols)
    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"symbols": len(symbols)},
        window_from=start_date,
        window_to=end_date,
    ) as run:
        total = 0
        for symbol in symbols:
            total += load_symbol_prices(
                db,
                fmp,
                symbol,
                run=run,
                start_date=start_date,
                end_date=end_date,
                overlap_days=overlap_days,
            )
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info("Loaded %d price rows across %d symbols", total, len(symbols))
        return total
