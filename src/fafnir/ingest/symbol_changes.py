"""
Ticker-rename reconciliation.

This is the loader that keeps a renamed company *one* company. Without it a
rename reaches the warehouse disguised as a new listing: the screener reports
META, no active row matches ``(fmp, 'META', 'NASDAQ')``, and the security-master
upsert mints a second ``security_id`` -- leaving the company's bars, corporate
actions and price watermark stranded on the FB row, which no delisting sweep will
ever close (a rename is not a delisting) and which is therefore re-polled every
night for bars that will never come.

Applying the rename to the security that already exists is what
``core.symbol_xref`` was designed for (doc/adr/0002): the old ticker's period is
closed the day before the change, a new period opens for the new ticker against
the same ``security_id``, and ``primary_symbol`` moves across. Every join, every
watermark and every backtest sees one continuous entity.

Run it nightly *before* the security-master load, so the rename is applied while
the new ticker is still unclaimed. Run the other way round and the screener mints
the duplicate first, and this step has to clean it up -- which it can do only when
that duplicate is still empty (see :func:`fafnir.db.repository.fold_empty_security`).

WHAT THIS CANNOT DO. It reconciles renames from the point it starts running. A
rename that happened while fafnir was not watching -- and whose duplicate has
since accumulated its own price history -- is reported as a ``conflict`` and left
alone: merging two price histories is not a decision a loader should make
silently. ``fafnir status`` surfaces the queue.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.symbol_change")

ENDPOINT = "symbol-change"


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _clean(value) -> str:
    return (value or "").strip().upper()


def _ordered_changes(rows: list[dict]) -> list[tuple[date, str, str, Optional[str]]]:
    """Normalize the feed and put it in the order the renames actually happened.

    Chronological order is load-bearing, not tidiness. The feed arrives newest
    first, and a ticker can change twice (A->B, then B->C). Applied newest first,
    B->C finds no security called B and is dropped, A->B then leaves the security
    sitting on a ticker that no longer trades -- and the second rename is lost
    until it drops off the tail of the feed entirely. Oldest first, the chain
    applies as a chain.
    """
    out: list[tuple[date, str, str, Optional[str]]] = []
    for row in rows:
        when = _parse_date(row.get("date") or row.get("changeDate"))
        old = _clean(row.get("oldSymbol"))
        new = _clean(row.get("newSymbol"))
        # No date means no period boundary to write, and the xref is a bitemporal
        # record -- an undated rename would have to guess when the old ticker
        # stopped being valid.
        if when is None or not old or not new or old == new:
            continue
        out.append((when, old, new, row.get("companyName") or row.get("name")))
    out.sort(key=lambda item: (item[0], item[1], item[2]))
    return out


def load_symbol_changes(
    db: Database, fmp: FMPClient, *, max_pages: int = 5
) -> dict[str, int]:
    """Apply ticker renames to the securities that already carry the history.

    Returns a count per outcome: ``applied``, ``folded`` (an empty duplicate the
    screener had already minted was absorbed), ``conflict``, ``ignored``,
    ``unknown`` (a rename for a ticker fafnir does not track -- the feed is
    global, so this is the common case) and ``skipped`` (already applied by an
    earlier sweep).
    """
    counts = {
        "rows": 0,
        "applied": 0,
        "folded": 0,
        "conflict": 0,
        "ignored": 0,
        "unknown": 0,
        "skipped": 0,
    }

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"max_pages": max_pages},
    ) as run:
        rows = fmp.symbol_changes(max_pages=max_pages)
        # Land the raw feed before transforming it: one small payload per sweep,
        # and it is the only evidence of what the source claimed on the night a
        # rename was applied (or refused).
        repo.land_payload(
            db,
            endpoint=ENDPOINT,
            params={"max_pages": max_pages},
            symbol=None,
            http_status=200,
            payload=rows,
            payload_hash=payload_hash(rows),
            nbytes=0,
            ingestion_run_id=run.run_id,
        )

        changes = _ordered_changes(rows)
        counts["rows"] = len(changes)
        for when, old, new, company_name in changes:
            recorded = repo.symbol_change_status(
                db, old_symbol=old, new_symbol=new, change_date=when
            )
            if recorded in repo.TERMINAL_CHANGE_STATUSES:
                counts["skipped"] += 1
                continue

            outcome = repo.apply_symbol_change(
                db,
                old_symbol=old,
                new_symbol=new,
                change_date=when,
                company_name=company_name,
            )
            counts[outcome.status] += 1

            if outcome.status == repo.CHANGE_UNKNOWN:
                # The feed is every venue on earth; a ticker we have never tracked
                # is the normal case and must not fill the audit table with other
                # people's renames.
                logger.debug("symbol change %s -> %s: not in the master", old, new)
                continue

            detail = {"old_symbol": old, "new_symbol": new}
            if outcome.folded_security_id is not None:
                counts["folded"] += 1
                detail["folded_security_id"] = outcome.folded_security_id
                logger.info(
                    "folded empty duplicate security %s into %s while renaming %s -> %s",
                    outcome.folded_security_id,
                    outcome.security_id,
                    old,
                    new,
                )
            repo.record_symbol_change(
                db,
                old_symbol=old,
                new_symbol=new,
                change_date=when,
                status=outcome.status,
                security_id=outcome.security_id,
                company_name=company_name,
                detail=detail,
            )

            if (
                outcome.status == repo.CHANGE_CONFLICT
                and recorded != repo.CHANGE_CONFLICT
            ):
                # Two live securities claiming one ticker, both with history. A
                # human decides; the flag is how they find out.
                #
                # Only on the night the conflict is first seen. Conflicts are
                # retried on every sweep by design, and add_dq_flag is an
                # unguarded insert -- flagging each retry would add an unresolved
                # flag per night for one unresolved problem, inflating the open-DQ
                # count until it says nothing. core.symbol_change is the durable
                # queue; the flag is the notification.
                repo.add_dq_flag(
                    db,
                    check_name="symbol_change_conflict",
                    severity="error",
                    security_id=outcome.security_id,
                    table_name="core.security",
                    record_key={"old_symbol": old, "new_symbol": new},
                    detail={
                        "reason": "new ticker already belongs to another listed "
                        "security that carries price history",
                        "change_date": str(when),
                    },
                    ingestion_run_id=run.run_id,
                )
                logger.warning(
                    "symbol change %s -> %s on %s conflicts with an existing "
                    "security; left unapplied for review",
                    old,
                    new,
                    when,
                )
            elif outcome.status == repo.CHANGE_APPLIED:
                logger.info(
                    "renamed %s -> %s (security %s) effective %s",
                    old,
                    new,
                    outcome.security_id,
                    when,
                )

            # One rename -- its xref periods, primary_symbol, audit row and any
            # fold -- is the unit of work, committed here so an interruption
            # costs that rename and not the sweep.
            db.commit()

        run.symbols_requested = counts["rows"]
        run.rows_inserted = counts["applied"]
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info(
            "Symbol-change sweep: %d dated rows, %d applied (%d folded), "
            "%d conflicts, %d ignored, %d untracked, %d already applied",
            counts["rows"],
            counts["applied"],
            counts["folded"],
            counts["conflict"],
            counts["ignored"],
            counts["unknown"],
            counts["skipped"],
        )
        return counts
