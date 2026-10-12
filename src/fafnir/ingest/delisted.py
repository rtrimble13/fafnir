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

A SECURITY STILL TRADING IS NOT DELISTED (ADR 0014). The feed reports a security
that left one venue for another under the same ticker exactly as it reports one
that stopped trading: ET, SUN, SUNC, USAC, DDS and DDT moved from the NYSE to the
Texas Stock Exchange on 2026-10-05, and this sweep stamped all six. The vendor's
profile cannot tell the two apart -- it went on calling ET inactive on the NYSE --
but its price feed can: a security that delisted has no bar after its delisting
date. So before stamping a recent delisting the sweep asks for the security's bars,
and declines one the vendor has bars after, flagged for an operator to decide:
``security venue-transfer`` if it moved, ``security delist`` if it left this
warehouse's scope (a move to OTC also keeps printing bars).

WHAT THIS CANNOT DO. FMP serves no EOD history for long-dead tickers (LEH, WCOM,
ENRNQ and friends all return zero bars), and its delisted list is shallow. So this
makes the warehouse unbiased *from the point it starts running forward*, by
retaining what fafnir has already ingested. It cannot reconstruct issuers that
died before fafnir first saw them -- that needs a vendor with real delisted
history (Sharadar, Norgate, CRSP). See doc/ingestion.md.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import LISTED_US_EXCHANGES, _norm_exchange
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, SourceError

logger = get_logger("ingest.delisted")

ENDPOINT = "delisted-companies"

# How long a security may go on printing bars after the delisting date the feed
# reports and still be the security that delisted. The nightly sweep reads the tail
# of the feed, which lags a delisting by days to weeks, and the price step keeps
# loading the name until it is marked -- so a genuine delisting can arrive with a
# few weeks of bars after it. Years of them cannot.
TRADED_AFTER_GRACE_DAYS = 30

# A delisting is checked against the vendor's price feed before it is stamped only
# when it is recent: within RECENT_DELISTING_DAYS of today, with the security's last
# stored bar within RECENT_BAR_SESSIONS sessions of the date. Its bars ran up to the
# delisting, so it may still be trading somewhere; one whose bars stopped well before
# stopped trading. That costs the nightly sweep about one request per security it
# would mark (1 to 22 a night in 2026-09), and keeps a `--full` sweep (4,041
# candidates in September) from asking about every old delisting.
RECENT_DELISTING_DAYS = 60
RECENT_BAR_SESSIONS = 10

# The feed is asked from about ten sessions before the date, not from the day
# after: a window that also covers the sessions before tells "the vendor serves
# nothing for this ticker" apart from "nothing since the date".
EVIDENCE_LOOKBACK_DAYS = 14

# The flag for a delisting the vendor's own bars contradict. Accepted (by
# `security venue-transfer`, or `dq accept`), it is also how the sweep remembers
# that the feed's record is a known transfer, and declines it without asking.
CONTRADICTED_CHECK = "security_delisting_contradicted"


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def delisting_contradicted(
    delisted: date, first_bar: Optional[date], last_bar: Optional[date]
) -> Optional[str]:
    """Why a delisting cannot describe the security now holding the ticker, or None.

    The feed reports delistings by ticker, and a ticker outlives its issuer: FMP's
    list still carries the 2018 delisting of an earlier CMDT, and applied by ticker
    it lands on whoever trades as CMDT today. Production, 2026-09: PIMCO's CMDT fund
    (bars 2023-05-11 onwards) stamped delisted 2018-10-10; WBIF delisted 2019-10-25
    with bars to 2026-08; RDFI delisted 2024-02-29 with bars to 2026-08; PHOS
    delisted 2026-03-18 with its first bar on 2026-07-20. Each live fund left the
    active universe, the next security-master load minted it a second row, and the
    ticker forked -- five open ``security_duplicate_identity`` flags that no command
    could fold.

    Two contradictions, both read off the security's own bars:

    * ``precedes_first_bar`` -- the delisting happened before this security ever
      traded. It is some earlier holder's.
    * ``traded_after`` -- the security kept printing bars for more than
      :data:`TRADED_AFTER_GRACE_DAYS` after it. A security that delisted has no
      such history. The one legitimate way to get it is a name that left the
      exchange and kept trading under the same ticker elsewhere; declining to mark
      that keeps a trading security loading, which is the cheaper error, and the
      warning names it for a human.

    A security with no bars contradicts nothing and is marked as before.
    """
    if first_bar is not None and delisted < first_bar:
        return "precedes_first_bar"
    if last_bar is not None and (last_bar - delisted).days > TRADED_AFTER_GRACE_DAYS:
        return "traded_after"
    return None


def is_recent_delisting(
    db: Database, delisted: date, last_bar: Optional[date], today: date
) -> bool:
    """Whether to ask the vendor for bars before stamping this delisting.

    Within :data:`RECENT_DELISTING_DAYS` of today, and with the security's last
    stored bar within :data:`RECENT_BAR_SESSIONS` sessions of the delisting. A
    security with no bars has nothing to protect and is not asked about.
    """
    if last_bar is None or (today - delisted).days > RECENT_DELISTING_DAYS:
        return False
    return repo.sessions_apart(db, delisted, last_bar) <= RECENT_BAR_SESSIONS


def contradiction_key(symbol: str, delisted: date) -> dict:
    """The record_key of a :data:`CONTRADICTED_CHECK` flag: one feed record."""
    return {"symbol": symbol, "delisted_date": delisted.isoformat()}


def bars_after(bars: Iterable[dict], delisted: date) -> list[dict]:
    """The vendor's bars strictly after the delisting date, oldest first.

    Strictly: FMP's delistedDate is often the last trading day, so a bar on the
    date is what a genuine delisting looks like too (AMZE, GBTG, BTAI, GWH, AETH and
    ZSC among 2026-09's). A bar after it is not.
    """
    after = [b for b in bars if (_parse_date(b.get("date")) or delisted) > delisted]
    return sorted(after, key=lambda b: str(b.get("date")))


def flag_still_trading(
    db: Database,
    *,
    security_id: int,
    symbol: str,
    feed_venue: str,
    delisted: date,
    after: list[dict],
    ingestion_run_id: Optional[int] = None,
) -> bool:
    """Flag a delisting the vendor's bars contradict, once per feed record.

    The volumes are there for the operator, not for the rule: they do not separate
    a transfer from a delisting. ET traded 7.2M shares its first day on TXSE against
    a 7.6M median, but USAC traded 18% of its median, and GRP-UN, which left the
    NYSE for OTC (a delisting here), traded 25%.
    """
    volumes = [float(b["volume"]) for b in after if b.get("volume") is not None]
    return repo.add_dq_flag_once(
        db,
        check_name=CONTRADICTED_CHECK,
        severity="warn",
        security_id=security_id,
        table_name="core.security",
        record_key=contradiction_key(symbol, delisted),
        detail={
            "feed_venue": feed_venue,
            "bars_after": len(after),
            "first_after": str(after[0].get("date"))[:10],
            "last_after": str(after[-1].get("date"))[:10],
            "median_volume_before_20": repo.median_volume_before(
                db, security_id, delisted
            ),
            "median_volume_after": median(volumes) if volumes else None,
        },
        ingestion_run_id=ingestion_run_id,
    )


def load_delisted(
    db: Database,
    fmp: FMPClient,
    *,
    max_pages: int = 5,
    exchanges: Iterable[str] = LISTED_US_EXCHANGES,
    today: Optional[date] = None,
) -> tuple[int, int]:
    """Mark newly delisted securities. Returns (marked, seen_for_our_venues).

    A delisting is declined, and the security left trading, when:

    * the security's own bars contradict it (:func:`delisting_contradicted`);
    * ``known_transfer`` -- an accepted :data:`CONTRADICTED_CHECK` flag names this
      feed record: an operator recorded the move (``security venue-transfer``).
      Silently, and without asking the vendor;
    * ``pending_decision`` -- an open one does: the vendor's bars contradicted it on
      an earlier night and no operator has decided. Without asking again, and past
      the window too, so an undecided record is never stamped by default;
    * ``still_trading`` -- the delisting is recent (:func:`is_recent_delisting`) and
      the vendor's price feed has bars after it. Flagged
      (:func:`flag_still_trading`) for an operator to decide: wrongly keeping a
      trading security active is the cheaper error;
    * ``fetch_failed`` -- the same question could not be asked tonight. The feed
      keeps the record for weeks, so the next sweep asks again.

    The feed rows read are those on every listed US venue, TXSE included though the
    screener cannot be asked for it yet. ``params`` record ``declined`` per reason
    and ``prices_requested``. ``rows_quarantined``, which marks the run ``partial``
    for ``scripts/monitor.sh``, counts every decline but ``known_transfer``, which
    repeats nightly for as long as the feed keeps the old record and needs nothing.
    """
    today = today or date.today()
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
        declined: dict[str, int] = {}
        fetched = 0
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
            # "Currently trading under that ticker" is necessary, not sufficient: the
            # security holding the ticker today may not be the one that delisted.
            # mark_delisted is one-way, so this is the last point it can be caught.
            first_bar, last_bar = repo.security_bar_span(db, sec_id)
            reason = delisting_contradicted(when, first_bar, last_bar)
            if reason is not None:
                declined[reason] = declined.get(reason, 0) + 1
                logger.warning(
                    "%s (%s): not applying delisting dated %s -- %s (bars %s..%s); "
                    "the feed's record describes an earlier holder of the ticker",
                    symbol,
                    exchange,
                    when,
                    reason,
                    first_bar,
                    last_bar,
                )
                continue
            # A feed record already flagged: an operator judged it a venue transfer
            # (accepted), or has yet to decide (open). Either way the vendor need not
            # be asked again, and an undecided one must not be stamped once it ages
            # out of the window below -- that would freeze a trading security, the
            # failure this guards.
            status = repo.condition_status(
                db,
                security_id=sec_id,
                check_name=CONTRADICTED_CHECK,
                record_key=contradiction_key(symbol, when),
            )
            if status is not None:
                reason = (
                    "known_transfer" if status == "accepted" else "pending_decision"
                )
                declined[reason] = declined.get(reason, 0) + 1
                logger.debug(
                    "%s (%s): delisting dated %s -- %s", symbol, exchange, when, reason
                )
                continue
            if is_recent_delisting(db, when, last_bar, today):
                fetched += 1
                try:
                    bars = fmp.eod_raw(
                        symbol,
                        from_date=(
                            when - timedelta(days=EVIDENCE_LOOKBACK_DAYS)
                        ).isoformat(),
                        to_date=today.isoformat(),
                    )
                except SourceError as exc:
                    declined["fetch_failed"] = declined.get("fetch_failed", 0) + 1
                    logger.warning(
                        "%s (%s): not applying delisting dated %s tonight -- its bars "
                        "could not be read (%s); the next sweep asks again",
                        symbol,
                        exchange,
                        when,
                        exc,
                    )
                    continue
                after = bars_after(bars or [], when)
                if after:
                    declined["still_trading"] = declined.get("still_trading", 0) + 1
                    flag_still_trading(
                        db,
                        security_id=sec_id,
                        symbol=symbol,
                        feed_venue=exchange,
                        delisted=when,
                        after=after,
                        ingestion_run_id=run.run_id,
                    )
                    db.commit()
                    logger.warning(
                        "%s (%s): not applying delisting dated %s -- still_trading: "
                        "the vendor has %d bars after it (%s..%s). Flagged %s: "
                        "`security venue-transfer` if it moved venue, `security "
                        "delist` if it left scope",
                        symbol,
                        exchange,
                        when,
                        len(after),
                        str(after[0].get("date"))[:10],
                        str(after[-1].get("date"))[:10],
                        CONTRADICTED_CHECK,
                    )
                    continue
            if repo.mark_delisted(db, security_id=sec_id, delisted_date=when):
                marked += 1
                db.commit()
                logger.info("delisted %s (%s) on %s", symbol, exchange, when)

        run.symbols_requested = seen
        run.rows_inserted = marked
        run.rows_quarantined = sum(
            n for reason, n in declined.items() if reason != "known_transfer"
        )
        run.bytes_downloaded = fmp.bytes_downloaded
        run.params.update(declined=declined, prices_requested=fetched)
        if undated:
            logger.warning("%d delisted rows had no usable delistedDate", undated)
        logger.info(
            "Delisting sweep: %d rows, %d on our venues, %d newly marked, declined "
            "%s (%d price requests)",
            len(rows),
            seen,
            marked,
            declined or "none",
            fetched,
        )
        return marked, seen
