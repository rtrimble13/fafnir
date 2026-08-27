"""
Delisting reconciliation.

This is the loader that keeps the warehouse free of survivorship bias. A security
that stops trading is never deleted (see
``sql/migrations/0003_security_master.up.sql``); this step flips
``is_actively_trading`` off, stamps ``delisted_date``, and closes the ticker's
open period in ``core.symbol_xref`` so a later issuer reusing that ticker gets a
fresh ``security_id`` instead of inheriting a dead company's price history.

Run it nightly *before* the price step: prices load the active universe, so a name
that delisted today should be marked before it is asked for fresh bars.

WHAT THIS CANNOT DO. FMP serves no EOD history for long-dead tickers (LEH, WCOM,
ENRNQ and friends all return zero bars), and its delisted list is shallow. So this
makes the warehouse unbiased *from the point it starts running forward*, by
retaining what fafnir has already ingested. It cannot reconstruct issuers that
died before fafnir first saw them -- that needs a vendor with real delisted
history (Sharadar, Norgate, CRSP). See doc/ingestion.md.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import SCREENER_EXCHANGES, _norm_exchange
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient

logger = get_logger("ingest.delisted")

ENDPOINT = "delisted-companies"


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_delisted(
    db: Database,
    fmp: FMPClient,
    *,
    max_pages: int = 5,
    exchanges: Iterable[str] = SCREENER_EXCHANGES,
) -> tuple[int, int]:
    """Mark newly delisted securities. Returns (marked, seen_for_our_venues)."""
    wanted = {_norm_exchange({"exchangeShortName": code}) for code in exchanges} - {
        None
    }

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"max_pages": max_pages},
    ) as run:
        rows = fmp.delisted_companies(max_pages=max_pages)
        marked = 0
        seen = 0
        undated = 0
        for row in rows:
            symbol = (row.get("symbol") or "").strip()
            exchange = _norm_exchange(row)
            if not symbol or exchange not in wanted:
                continue
            seen += 1
            when = _parse_date(row.get("delistedDate"))
            if when is None:
                # No date means nothing to stamp; flagging it as delisted with a
                # NULL delisted_date would leave it in the active unique index.
                undated += 1
                continue
            # active_security_for_symbol, NOT resolve_security_id: the read path
            # deliberately falls back to a ticker a security used to trade under,
            # so that `duk ph FB` still reaches Meta. Resolving that way here would
            # be destructive -- the delisted feed reports retired tickers, so a
            # row for FB (retired by the rename, not by a delisting) would resolve
            # to the live META security and mark_delisted would flip it off the
            # active universe permanently, one-way. A delisting may only ever be
            # stamped on the security *currently* trading under that ticker.
            sec_id = repo.active_security_for_symbol(db, symbol)
            if sec_id is None:
                # A name fafnir never tracked, or one whose ticker has since moved
                # to another security. There are no bars to protect, and inserting
                # it would only add a security with no history, so skip it -- but
                # count it, because a large number here means the security master
                # is running behind the delisted feed.
                continue
            if repo.mark_delisted(db, security_id=sec_id, delisted_date=when):
                marked += 1
                db.commit()
                logger.info("delisted %s (%s) on %s", symbol, exchange, when)

        run.symbols_requested = seen
        run.rows_inserted = marked
        run.bytes_downloaded = fmp.bytes_downloaded
        if undated:
            logger.warning("%d delisted rows had no usable delistedDate", undated)
        logger.info(
            "Delisting sweep: %d rows, %d on our venues, %d newly marked",
            len(rows),
            seen,
            marked,
        )
        return marked, seen
