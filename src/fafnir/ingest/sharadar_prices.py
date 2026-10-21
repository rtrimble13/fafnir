"""
`fafnir ingest sharadar-prices`: Sharadar's daily bars into ``core.daily_price``, as
raw OHLCV recovered from SEP/SFP (plan SA-0601, #75).

Two modes, either or both, reading the ledger's current ``stocks`` and ``funds``
bulk files (no API, no landing copy):

- ``--backfill``: the whole history of the securities Sharadar **feeds**, the dead
  issuers #71 minted. Nobody else holds their bars.
- ``--gap-fill``: **fill-only** for the FMP securities linked to Sharadar: the
  dates FMP never served, from the link's ``valid_from`` on (before it, the FMP row
  may hold another issuer's era, #68).

**Raw is recovered** (ADR 0004's amendment, #74). SEP's ``open/high/low/close/volume``
are split-adjusted and ``closeunadj`` is the raw close, so the rest is
:func:`sharadar_map.recover_raw`. The factor is the product of the ACTIONS splits
after the bar's date, exact, where it agrees with ``closeunadj / close`` within the
two closes' 3-dp rounding (#49). Where it does not, a split is missing from ACTIONS
or one is wrong: the bar is recovered with the vendor's own ratio, still loaded, and
the security is flagged ``sharadar_split_ratio_mismatch`` once, with the count and
the first and last such dates. Recovered with ACTIONS, a high can land below the
exact close, or a low above it, by the recovery's own rounding; within that bound it
is set to the close (:func:`clamp_to_close`), and beyond it the validator decides.

Each bar then goes through the FMP loader's own gates, unchanged: dates the venue
was closed and dates an operator edited are set aside, and a bar that fails
validation is quarantined under the same ``price_*`` checks. Rows are written under
ADR 0015's precedence as ``source = 'sharadar'``, never as a correction (#75's
``vendor correct`` does that). A future date is never read.

The files are read once each and grouped by ticker on disk first
(:func:`sharadar_files.select_by_ticker`): SEP is largely date-major, and batching
the rows in file order made a batch, with its lookups and commit, of nearly every
row (run 403 managed about a quarter of SEP in 13 hours). Each security's rows are
then one batch, written and committed before the next, so memory holds one bucket
of tickers, not 45M rows, and a run killed part-way keeps every security it
finished. Every ``PROGRESS_SECONDS`` the counts so far and the ticker reached are
written to the open run (``params.progress``).

A date the security already holds, whoever supplied it, is not read again, so a
re-run writes nothing new; the vendor's later revisions of its own history come
with the nightly (#98). The watermark is ``(sharadar, SEP|SFP, security_id)``, for
the securities Sharadar feeds. The factors of the securities that gained bars and
hold actions are recomputed at the end.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import adjustments
from fafnir.ingest import sharadar_actions as sa
from fafnir.ingest import sharadar_map
from fafnir.ingest.daily_price import (
    _drop_non_session,
    _drop_suppressed,
    _is_nav_priced,
    _session_calendar,
    _validate_bar,
)
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.sharadar_master import SECURITY_TABLES
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf

logger = get_logger("ingest.sharadar_prices")

VENDOR = "sharadar"
ENDPOINT = "prices"
#: The watermark's endpoint per file: the vendor's own table codes.
WATERMARK_ENDPOINT = {"stocks": "SEP", "funds": "SFP"}
TABLES = ("stocks", "funds")
RATIO_CHECK = "sharadar_split_ratio_mismatch"
#: Half a unit in the third decimal place: SEP's precision (#49).
ROUNDING = Decimal("0.0005")
#: Half a unit of NUMERIC(20, 6), the quantum a recovered price is rounded to.
QUANTUM = Decimal("0.0000005")
#: How often a scan writes its progress to the open run, in seconds.
PROGRESS_SECONDS = 60


class PricesError(RuntimeError):
    """The loader cannot run: a bulk file or the master is missing."""


@dataclass
class PricesResult:
    dry_run: bool
    run_id: Optional[int] = None
    securities: int = 0  # in scope
    read: int = 0  # rows of theirs read from the files
    written: int = 0
    counts: Counter = field(default_factory=Counter)
    mismatched_securities: int = 0
    changed_security_ids: set[int] = field(default_factory=set)
    adjusted: int = 0


def ratio_agrees(close: Decimal, closeunadj: Decimal, expected: Decimal) -> bool:
    """Whether ``closeunadj / close`` is ``expected`` within the two prices' 3-dp
    rounding: a relative error of up to 0.0005/close + 0.0005/closeunadj."""
    tolerance = ROUNDING / close + ROUNDING / closeunadj
    return abs(closeunadj / close / expected - 1) <= tolerance


def recovery_factor(row: dict, splits) -> tuple[Optional[Decimal], Optional[bool]]:
    """The factor a bar is recovered with, and whether the vendor's ratio agreed
    with ACTIONS: the ACTIONS split product where it does, else ``closeunadj /
    close``. ``(None, None)`` when the row has no price to recover from."""
    close, unadj = row.get("close"), row.get("closeunadj")
    if close is None or unadj is None or close <= 0 or unadj <= 0:
        return None, None
    expected = sharadar_map.split_product_after(splits, row["date"])
    if ratio_agrees(close, unadj, expected):
        return expected, True
    return unadj / close, False


def clamp_to_close(raw: dict, slack: Decimal) -> tuple[dict, bool]:
    """``raw`` with a high below its close, or a low above it, by at most ``slack``
    set to the close, and whether either was.

    Recovered with the ACTIONS factor ``f``, the close is ``closeunadj``, within
    0.0005 of the price traded, while the high and low are SEP's 3-dp adjusted
    prices times ``f``, each within 0.0005 x ``f`` of it. On a bar that closed at its
    high, the recovered high therefore lands below the close about half the time,
    and the validator quarantined a bar the vendor had right (PFFBQ, 1998, ``f`` =
    2.1: 20 7/8 recovered as 20.874 against a close of 20.875; 55 of its 210 bars).
    A shortfall within ``slack``, 0.0005 x (``f`` + 1) and the quantum, is the
    recovery's: the traded high was at least the close. A larger one is the
    vendor's, and is left for the validator to quarantine.
    """
    close, out, clamped = raw["close"], dict(raw), False
    if out.get("high") is not None and close - slack <= out["high"] < close:
        out["high"], clamped = close, True
    if out.get("low") is not None and close < out["low"] <= close + slack:
        out["low"], clamped = close, True
    return out, clamped


def recovered_bar(
    row: dict, splits, counts: Optional[Counter] = None
) -> tuple[dict, Optional[bool]]:
    """The raw bar, shaped as the FMP loader's validator reads one, and whether the
    vendor's ratio agreed with ACTIONS (None when there was no close to test).

    Recovered with ACTIONS, a high or low on the wrong side of the close by the
    recovery's rounding is set to the close (:func:`clamp_to_close`), counted
    ``rounding_clamped`` in ``counts`` when given. Recovered with the vendor's ratio,
    the close and the rest are scaled alike, so no such rounding arises.
    """
    day = row["date"]
    factor, agrees = recovery_factor(row, splits)
    if factor is None:
        # No price to recover from: the validator quarantines it as such.
        return {"date": day.isoformat(), "close": None}, None
    raw = sharadar_map.recover_raw(row, factor=factor if agrees else None)
    if agrees:
        raw, clamped = clamp_to_close(raw, ROUNDING * (factor + 1) + QUANTUM)
        if clamped and counts is not None:
            counts["rounding_clamped"] += 1
    return {
        "date": day.isoformat(),
        "open": raw["open"],
        "high": raw["high"],
        "low": raw["low"],
        "close": raw["close"],
        "volume": raw["volume"],
    }, agrees


def report_progress(run: Optional[RunLog], last: float, **progress) -> float:
    """Write ``progress`` to the open run (``params.progress``, with the run's counts)
    once ``PROGRESS_SECONDS`` have passed since ``last``; the time it last wrote."""
    now = time.monotonic()
    if run is None or now - last < PROGRESS_SECONDS:
        return last
    run.params["progress"] = progress
    run.checkpoint()
    return now


@dataclass
class _Target:
    security_id: int
    symbol: str
    vendor_key: str
    is_feed: bool
    valid_from: date
    splits: list = field(default_factory=list)
    mismatched: list = field(default_factory=list)  # dates


def _load_run(
    db: Database,
    t: _Target,
    table: str,
    rows: list[dict],
    *,
    as_of: date,
    run: Optional[RunLog],
    result: PricesResult,
) -> None:
    """Recover, gate and write one run of a security's rows."""
    held = repo.price_dates(db, t.security_id)
    wanted = [
        r
        for r in rows
        if r["date"] <= as_of
        and r["date"] not in held
        and (t.is_feed or r["date"] >= t.valid_from)
    ]
    result.counts["held_or_outside"] += len(rows) - len(wanted)
    if not wanted:
        return
    bars = []
    for r in sorted(wanted, key=lambda r: r["date"]):
        bar, agrees = recovered_bar(r, t.splits, result.counts)
        if agrees is False:
            t.mismatched.append(r["date"])
        bars.append(bar)

    profile = repo.security_price_profile(db, t.security_id) or {}
    nav_only = _is_nav_priced(profile.get("asset_type"), profile.get("is_fund"))
    bars, off = _drop_non_session(
        bars, _session_calendar(db, profile.get("exchange_code"), bars)
    )
    result.counts["non_session"] += off
    bars, removed = _drop_suppressed(
        bars, repo.suppressed_price_dates(db, t.security_id)
    )
    result.counts["suppressed"] += removed

    clean = []
    for bar in bars:
        row, reason = _validate_bar(bar, nav_only=nav_only)
        if reason:
            result.counts[f"quarantined:{reason}"] += 1
            if run is not None:
                run.rows_quarantined += 1
                repo.add_dq_flag(
                    db,
                    check_name=f"price_{reason}",
                    severity="warn",
                    security_id=t.security_id,
                    table_name="core.daily_price",
                    record_key={"symbol": t.symbol, "date": bar["date"]},
                    detail={"reason": reason, "source": VENDOR},
                    ingestion_run_id=run.run_id,
                )
            continue
        row["security_id"] = t.security_id
        clean.append(row)
    result.counts["to_write"] += len(clean)
    if run is None or not clean:
        return
    written = repo.upsert_daily_prices(
        db, clean, ingestion_run_id=run.run_id, source=VENDOR
    )
    result.counts["skipped_foreign"] += written.skipped_foreign
    result.written += written
    if written:
        result.changed_security_ids.add(t.security_id)
    if t.is_feed:
        repo.set_watermark(
            db,
            VENDOR,
            WATERMARK_ENDPOINT[table],
            max(r["trade_date"] for r in clean),
            t.security_id,
        )
    db.commit()


def prepare(
    db: Database, bulk_dir: str | Path, targets: Sequence[dict], counts: Counter
) -> tuple[dict, dict, dict]:
    """For linked securities (:func:`repo.vendor_action_targets` rows): which file
    and ticker hold each one's prices (``{table: {ticker: _Target}}``), the current
    file of each table needed (``{table: file_id}``), and every target by key, its
    ACTIONS splits read. A key with no priced row is counted ``no_priced_row``."""
    where = repo.vendor_tickers(db, VENDOR, [t["vendor_key"] for t in targets])
    by_file: dict[str, dict[str, _Target]] = {table: {} for table in TABLES}
    for t in targets:
        if t["vendor_key"] not in where:
            counts["no_priced_row"] += 1
            continue
        table, ticker = where[t["vendor_key"]]
        by_file[table][ticker] = _Target(
            t["security_id"],
            t["primary_symbol"],
            t["vendor_key"],
            t["is_feed"],
            t["valid_from"],
        )
    files = {}
    for table in TABLES:
        if by_file[table]:
            entry = repo.current_sharadar_full(db, table)
            if entry is None:
                raise PricesError(
                    f"the ledger holds no current {table} file: "
                    f"`fafnir source sharadar-bulk --tables {table}` first"
                )
            files[table] = entry["file_id"]

    # The splits that turn the vendor's adjusted bars back into raw ones.
    actions = repo.current_sharadar_full(db, "actions")
    keys = {t.vendor_key: t for f in by_file.values() for t in f.values()}
    if actions is not None and keys:
        key_of = repo.vendor_keys_by_ticker(db, VENDOR, SECURITY_TABLES)
        found = sa.key_actions(
            sf.rows(db, actions["file_id"], bulk_dir), set(keys), key_of
        )
        for key, t in keys.items():
            splits = [
                (d, v) for d, v in found.get(key, {}).get("split", ()) if v and v > 0
            ]
            t.splits = list(dict(splits).items())  # one per ex-date
    return by_file, files, keys


def load(
    db: Database,
    bulk_dir: str | Path,
    *,
    backfill: bool,
    gap_fill: bool,
    security_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
    adjust: bool = True,
    as_of: Optional[date] = None,
) -> PricesResult:
    """Read the files once each and, unless ``dry_run``, write (one run)."""
    as_of = as_of or date.today()
    result = PricesResult(dry_run=dry_run)
    targets = [
        t
        for t in repo.vendor_action_targets(db, VENDOR, security_ids)
        if (backfill and t["is_feed"])
        or (gap_fill and not t["is_feed"] and t["source"] == "fmp")
    ]
    result.securities = len(targets)
    by_file, files, keys = prepare(db, bulk_dir, targets, result.counts)

    def scan(run: Optional[RunLog]) -> None:
        last = time.monotonic()
        for table, file_id in files.items():
            wanted = {ticker: sf.ALL_DATES for ticker in by_file[table]}
            groups = sf.select_by_ticker(db, file_id, bulk_dir, wanted)
            for done, (ticker, rows) in enumerate(groups, start=1):
                result.read += len(rows)
                _load_run(
                    db,
                    by_file[table][ticker],
                    table,
                    rows,
                    as_of=as_of,
                    run=run,
                    result=result,
                )
                if run is not None:
                    run.rows_inserted = result.written
                last = report_progress(
                    run,
                    last,
                    table=table,
                    ticker=ticker,
                    tickers_done=done,
                    tickers=len(wanted),
                    read=result.read,
                    written=result.written,
                )
        mismatched = [t for t in keys.values() if t.mismatched]
        result.mismatched_securities = len(mismatched)
        result.counts["ratio_mismatch_bars"] = sum(
            len(t.mismatched) for t in mismatched
        )
        if run is None:
            return
        for t in mismatched:
            repo.add_dq_flag_once(
                db,
                check_name=RATIO_CHECK,
                severity="warn",
                security_id=t.security_id,
                table_name="core.daily_price",
                record_key={"vendor": VENDOR, "vendor_key": t.vendor_key},
                detail={
                    "bars": len(t.mismatched),
                    "first": min(t.mismatched).isoformat(),
                    "last": max(t.mismatched).isoformat(),
                },
                ingestion_run_id=run.run_id,
            )
        run.rows_inserted = result.written
        run.params.pop("progress", None)  # the counts below are the final word
        run.params.update(
            securities=result.securities,
            read=result.read,
            written=result.written,
            counts=dict(sorted(result.counts.items())),
        )
        db.commit()

    if dry_run:
        scan(None)
        return result
    run = RunLog(
        db,
        source=VENDOR,
        endpoint=ENDPOINT,
        params={"backfill": backfill, "gap_fill": gap_fill, "files": files},
    )
    with run:
        result.run_id = run.run_id
        scan(run)

    if adjust and result.changed_security_ids:
        with_actions = {
            a["security_id"]
            for a in repo.action_dates(db, sorted(result.changed_security_ids))
        }
        ids = sorted(result.changed_security_ids & with_actions)
        if ids:
            result.adjusted = adjustments.adjust_all(db, security_ids=ids)["securities"]
    return result
