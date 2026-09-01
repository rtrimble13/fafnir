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

TWO MODES. The nightly sweep only *marks* securities the master already holds: a
feed row for a name fafnir never tracked has no bars to protect, and minting it
every night would grow the master with empty securities. ``backfill=True`` inverts
that judgement deliberately -- for an operator who wants the delisted universe
itself, a security with no bars is still the record that the company existed, and
without it every point-in-time universe query silently excludes it. See
:func:`load_delisted`.

WHAT THIS STILL CANNOT DO, in the order the holes bite:

1. *Feed depth.* FMP's delisted list is newest-first and shallow. Backfill can only
   mint what the feed returns; it does not reach names that died before FMP's
   retention window, whatever ``max_pages`` says.
2. *Price history.* FMP serves no EOD bars for long-dead tickers (LEH, WCOM, ENRNQ
   all return zero). A minted security with no bars gives an unbiased *universe*
   and still-biased *returns*.
3. *Reused tickers.* Backfill mints only tickers the master has never seen
   (:func:`repository.resolve_security_id` returns None). When the ticker is held
   by a live security -- Circuit City's CC, now Chemours -- the dead issuer is
   skipped, because minting it safely means inserting a second row past 0009's
   active-only unique index and a second xref period past 0015's one-open-period
   index, and getting that wrong corrupts a live company's identity. The count is
   reported (``DelistedSweepResult.unmintable``) rather than silently dropped.

THE REUSE GUARD. A deep sweep re-reads delistings from years back, and a ticker
retired then may belong to someone else now. Marking on ticker alone would retire
*Chemours* on Circuit City's 2009 date -- one-way, silent, and it drops the live
company out of the price universe for good. So before stamping anything, the
security holding the ticker is checked for a bar after the reported delisting
(:func:`repository.security_traded_after`): a company that stopped trading on D
has none, so one that does is not the company the row is about. Those rows raise
``delisted_ticker_reuse`` instead. The nightly tail does not reach far enough back
to contain such a row, which is why this only surfaces with ``--full``.

A vendor with real delisted history (Sharadar, Norgate, CRSP) is the answer to all
three. ``fafnir source audit-delisted`` measures how big each hole is against your
own warehouse before you spend anything. See doc/ingestion.md.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, NamedTuple, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import SCREENER_EXCHANGES, _norm_exchange
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.delisted")

ENDPOINT = "delisted-companies"

# What a backfilled security is recorded as. The delisted feed carries no
# isEtf/isFund flag, and 'equity' is the conservative choice: it is what the price
# loader validates strictly (a bar missing open/high/low quarantines rather than
# being expanded as a NAV strike -- see daily_price.NAV_ASSET_TYPES).
BACKFILL_ASSET_TYPE = "equity"

# Raised when the delisted feed reports a ticker whose CURRENT holder traded after
# the reported delisting: the row is about the ticker's previous owner, and acting
# on it would retire a live company. Worth a flag rather than a silent skip --
# it means the warehouse is missing the dead issuer that row belongs to.
REUSE_CHECK = "delisted_ticker_reuse"


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _venue_scope(exchanges: Iterable[str]) -> set[str]:
    return {_norm_exchange({"exchangeShortName": code}) for code in exchanges} - {None}


class DelistedSweepResult(NamedTuple):
    """What one sweep did.

    ``seen`` counts feed rows on our venues, and the rest partition them:
    ``marked + already + undated + unmatched + reused == seen``, with ``minted`` a
    subset of ``unmatched`` (the part backfill was able to act on).
    """

    marked: int  # existing securities newly stamped delisted
    seen: int  # feed rows on our venues
    minted: int  # securities created by backfill
    unmatched: int  # nothing currently listed under this ticker
    undated: int  # no usable delistedDate, so nothing can be stamped
    already: int  # already carried a delisted_date
    reused: int = 0  # the ticker's CURRENT holder traded after this delisting

    @property
    def unmintable(self) -> int:
        """Unmatched rows backfill could not safely mint: the ticker is already
        known to the master (a stamped delisting, or a live security's retired
        alias). Hole 3 in the module docstring. Equals ``unmatched`` when the
        sweep ran without ``backfill``."""
        return self.unmatched - self.minted


def _mint_delisted(
    db: Database,
    row: dict,
    *,
    symbol: str,
    exchange: str,
    delisted_date: date,
) -> bool:
    """Create a security for a delisted name the master has never held.

    Minted listed-then-immediately-retired rather than inserted with
    ``delisted_date`` pre-set, because ``upsert_security``'s conflict arbiter is
    0009's partial index over ``delisted_date IS NULL``: a row inserted with the
    date already on it matches no arbiter, so a second run would insert a duplicate
    instead of colliding. Minting into the active index and letting
    :func:`repository.mark_delisted` stamp it keeps the sequence idempotent -- on
    the next sweep ``resolve_security_id`` finds the row and the caller never gets
    here. Both writes are committed together by the caller, so a crash between them
    cannot leave an active security with no bars for `dq run` to flag forever.

    The xref period needs no explicit ``valid_from``: the caller has already
    established this ticker is unknown to the master, so it has no periods at all
    and ``upsert_symbol_xref`` opens one at its 1900 fallback, which
    ``mark_delisted`` then closes at the delisting.
    """
    repo.ensure_exchange(db, exchange)
    sec_id = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=row.get("companyName") or row.get("name"),
        asset_type=BACKFILL_ASSET_TYPE,
        exchange_code=exchange,
        is_actively_trading=False,
        ipo_date=_parse_date(row.get("ipoDate")),
    )
    repo.upsert_symbol_xref(db, security_id=sec_id, symbol=symbol)
    return repo.mark_delisted(db, security_id=sec_id, delisted_date=delisted_date)


def load_delisted(
    db: Database,
    fmp: FMPClient,
    *,
    max_pages: int = 5,
    exchanges: Iterable[str] = SCREENER_EXCHANGES,
    backfill: bool = False,
) -> DelistedSweepResult:
    """Mark newly delisted securities, and optionally mint the ones we never held.

    ``backfill=False`` (the nightly default) only stamps securities the master
    already holds. ``backfill=True`` also mints a security for every on-venue feed
    row whose ticker the master has never seen, retired on arrival. Pair it with a
    deep ``max_pages`` -- the recent tail is by definition the part you already
    have -- and follow it with ``fafnir ingest prices --include-inactive``, since
    a minted security is inactive from birth and the ordinary price run skips it.
    """
    wanted = _venue_scope(exchanges)

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"max_pages": max_pages, "backfill": backfill},
    ) as run:
        rows = fmp.delisted_companies(max_pages=max_pages)
        # Land the feed before interpreting it. Every other loader does, and without
        # it there is no record of what the vendor offered on any given night --
        # which is exactly the question a survivorship-bias audit asks in hindsight,
        # when the feed has already rolled forward and cannot be re-fetched.
        repo.land_payload(
            db,
            endpoint=ENDPOINT,
            params={"max_pages": max_pages, "backfill": backfill},
            symbol=None,
            http_status=200,
            payload=rows,
            payload_hash=payload_hash(rows),
            nbytes=fmp.bytes_downloaded,
            ingestion_run_id=run.run_id,
        )
        db.commit()

        marked = minted = unmatched = seen = undated = already = reused = 0
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
                # Nothing is listed under this ticker. Either the master has never
                # heard of it (a name that died before fafnir first ran -- the
                # backfill case), or it knows the ticker from somewhere else: a
                # delisted row already stamped, or a ticker a live security used to
                # trade under. Only the first is safe to mint, and
                # resolve_security_id is exactly that test because it is the
                # resolution the read path uses.
                unmatched += 1
                if not backfill:
                    continue
                if repo.resolve_security_id(db, symbol) is not None:
                    # See hole 3 in the module docstring: minting past a known
                    # ticker means writing past two unique indexes that exist to
                    # protect a live company's identity. Counted, never guessed at.
                    continue
                if _mint_delisted(
                    db, row, symbol=symbol, exchange=exchange, delisted_date=when
                ):
                    minted += 1
                    db.commit()
                    logger.info("minted delisted %s (%s) on %s", symbol, exchange, when)
                else:  # pragma: no cover -- mark_delisted just created this row
                    db.rollback()
                continue
            if repo.security_traded_after(db, sec_id, when):
                # The ticker was REUSED, and this row is about its previous holder.
                # A security that stopped trading on D has no bar after D, so one
                # that does cannot be the company this row is reporting -- and
                # stamping it would retire a live company on a stranger's date,
                # one-way and silently, dropping it out of the price universe for
                # good. Only reachable on a deep sweep: the nightly tail does not
                # reach back far enough to carry the dead issuer's row.
                reused += 1
                repo.add_dq_flag_once(
                    db,
                    check_name=REUSE_CHECK,
                    severity="warn",
                    security_id=sec_id,
                    table_name="core.security",
                    record_key={"symbol": symbol, "delisted_date": when.isoformat()},
                    detail={
                        "reason": "feed row predates this security's own bars",
                        "company_name": row.get("companyName") or row.get("name"),
                        "exchange": exchange,
                    },
                    ingestion_run_id=run.run_id,
                )
                db.commit()
                logger.warning(
                    "%s delisted %s in the feed, but the security holding that "
                    "ticker traded after it -- ticker reuse, not delisting",
                    symbol,
                    when,
                )
                continue
            if repo.mark_delisted(db, security_id=sec_id, delisted_date=when):
                marked += 1
                db.commit()
                logger.info("delisted %s (%s) on %s", symbol, exchange, when)
            else:
                already += 1

        run.symbols_requested = seen
        run.rows_inserted = marked + minted
        run.bytes_downloaded = fmp.bytes_downloaded
        if undated:
            logger.warning("%d delisted rows had no usable delistedDate", undated)
        logger.info(
            "Delisting sweep: %d rows, %d on our venues, %d newly marked, "
            "%d minted, %d unmatched (%d of those already-known tickers), "
            "%d refused as ticker reuse",
            len(rows),
            seen,
            marked,
            minted,
            unmatched,
            unmatched - minted,
            reused,
        )
        return DelistedSweepResult(
            marked=marked,
            seen=seen,
            minted=minted,
            unmatched=unmatched,
            undated=undated,
            already=already,
            reused=reused,
        )


# ---------------------------------------------------------------------------
# Coverage audit (read-only)
# ---------------------------------------------------------------------------

# Row classifications, in the order the audit tests them.
OUT_OF_SCOPE = "out_of_scope"  # venue we do not ingest
UNDATED = "undated"  # on our venues, but no usable delistedDate
HELD = "held"  # a security is listed under this ticker now
REUSED = "reused"  # ticker known to the master, but nothing listed under it
MINTABLE = "mintable"  # unknown ticker -- what --backfill would create


def audit_delisted(
    db: Database,
    fmp: FMPClient,
    *,
    max_pages: int = 500,
    exchanges: Iterable[str] = SCREENER_EXCHANGES,
) -> dict[str, Any]:
    """Measure the delisted feed against this warehouse. Writes nothing.

    Answers the three questions that decide whether a backfill is worth running:
    how deep the feed goes (``oldest``), how much of it this warehouse is throwing
    away on venue normalization (``unmapped_venues``), and how many names a
    ``--backfill`` sweep would actually add (``mintable``).

    No ``RunLog`` and no landing on purpose: an audit that mutates the warehouse it
    is auditing cannot be run freely, and being free to run is the point.
    """
    wanted = _venue_scope(exchanges)
    rows = fmp.delisted_companies(max_pages=max_pages)
    coverage = repo.symbol_coverage_index(db)

    venues: Counter[str] = Counter()
    unmapped: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    detail: list[dict[str, Any]] = []
    dates: list[date] = []

    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        norm = _norm_exchange(row)
        raw = (row.get("exchange") or row.get("exchangeShortName") or "").strip()
        venues[norm or "(none)"] += 1
        when = _parse_date(row.get("delistedDate"))

        if not symbol or norm not in wanted:
            status = OUT_OF_SCOPE
            # Keyed on the NORMALIZED name, not the raw one: that is the uppercased
            # string `_EXCHANGE_ALIASES` is keyed on, so a US venue showing up here
            # can be pasted straight in. The raw spelling stays on the CSV row.
            if norm not in wanted:
                unmapped[norm or "(none)"] += 1
        elif when is None:
            status = UNDATED
        elif symbol in coverage.listed:
            status = HELD
        elif symbol in coverage.known:
            status = REUSED
        else:
            status = MINTABLE

        status_counts[status] += 1
        if status != OUT_OF_SCOPE and when is not None:
            dates.append(when)
            by_year[str(when.year)] += 1
        ipo = _parse_date(row.get("ipoDate"))
        detail.append(
            {
                "symbol": symbol,
                "company": row.get("companyName") or row.get("name"),
                "raw_exchange": raw,
                "norm_exchange": norm,
                "delisted_date": when.isoformat() if when else "",
                "ipo_date": ipo.isoformat() if ipo else "",
                "status": status,
            }
        )

    in_scope = sum(status_counts[s] for s in (UNDATED, HELD, REUSED, MINTABLE))
    return {
        "feed_rows": len(rows),
        "bytes_downloaded": fmp.bytes_downloaded,
        "max_pages": max_pages,
        "in_scope": in_scope,
        "held": status_counts[HELD],
        "reused": status_counts[REUSED],
        "mintable": status_counts[MINTABLE],
        "undated": status_counts[UNDATED],
        "out_of_scope": status_counts[OUT_OF_SCOPE],
        "oldest": min(dates).isoformat() if dates else None,
        "newest": max(dates).isoformat() if dates else None,
        "venues": dict(venues.most_common()),
        "unmapped_venues": dict(unmapped.most_common()),
        "by_year": dict(sorted(by_year.items())),
        "master_listed": len(coverage.listed),
        "master_known": len(coverage.known),
        "rows": detail,
    }


def format_audit_report(report: dict[str, Any]) -> str:
    """Render :func:`audit_delisted` for a terminal."""
    out: list[str] = []
    a = out.append
    a(
        f"Delisted feed: {report['feed_rows']} rows "
        f"(max_pages={report['max_pages']}, {report['bytes_downloaded']} bytes)"
    )
    if report["feed_rows"] and report["feed_rows"] % 100 == 0:
        a(
            "  NOTE: the row count is an exact multiple of the page size, which is "
            "what truncation looks like. Re-run with a higher --max-pages."
        )
    a(
        f"On our venues: {report['in_scope']}   "
        f"held={report['held']}  mintable={report['mintable']}  "
        f"reused={report['reused']}  undated={report['undated']}"
    )
    a(f"Out of scope (other venues): {report['out_of_scope']}")
    if report["oldest"]:
        a(
            f"Feed depth: {report['oldest']} .. {report['newest']}  "
            "<- nothing before the left-hand date is recoverable from this vendor"
        )
    a(
        f"Master holds {report['master_listed']} listed tickers, "
        f"{report['master_known']} known ever."
    )

    a("")
    a("Venues the feed named (normalized), most common first:")
    for name, n in list(report["venues"].items())[:20]:
        a(f"  {name:<34} {n}")

    if report["unmapped_venues"]:
        a("")
        a("Dropped as out-of-universe -- check for a US venue spelled long-form:")
        for name, n in list(report["unmapped_venues"].items())[:20]:
            a(f"  {name:<34} {n}")
        a(
            "  A US venue in this list is a missing entry in "
            "security_master._EXCHANGE_ALIASES, not a vendor limit."
        )

    if report["by_year"]:
        a("")
        a("In-scope delistings by year:")
        for year, n in report["by_year"].items():
            a(f"  {year}  {'#' * min(n // 5 + 1, 60)} {n}")

    a("")
    if report["mintable"]:
        a(
            f"`fafnir ingest delisted --full --backfill` would mint "
            f"{report['mintable']} securities."
        )
        a(
            "Then `fafnir ingest prices --include-inactive` for their bars -- but "
            "sample first: FMP serves no EOD history for long-dead tickers, and a "
            "minted security with no bars fixes the universe, not the returns."
        )
    else:
        a("Nothing to backfill: every in-scope feed row is already in the master.")
    return "\n".join(out)
