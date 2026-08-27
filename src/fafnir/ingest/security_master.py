"""
Security-master loader.

Builds ``core.security`` + ``core.symbol_xref`` from FMP. The default
``us-equity-etf`` universe comes from ``company-screener``, the only bulk endpoint
carrying the exchange; ``stock-list`` / ``etf-list`` back the unfiltered universes.
The screener also supplies market cap and beta, so screening data costs nothing
beyond the universe load (0010). :func:`enrich_profiles` is optional and adds only
the long-form description and the identifiers, at one request per symbol.

Delisted/inactive securities are never deleted; a reconciliation step
(``fafnir ingest delisted``) flips ``is_actively_trading``/``delisted_date``.

This loader is an *upkeep* step, not just a build step. ``scripts/daily_update.sh``
runs it nightly, which is how a security that listed today (an IPO, a spin-off, a
new ETF) enters scope: it appears in the screener, the upsert mints it, and the
price step -- which has no watermark for it -- pulls its full available history on
the same run. :func:`load_securities` reports which securities were new so the
nightly log says so out loud.

Renames are NOT this loader's job, and must be reconciled before it runs
(``fafnir ingest symbol-changes``): to the screener a renamed ticker looks exactly
like a new listing, and minting it as one forks the company's identity. See
``fafnir/ingest/symbol_changes.py``.
"""

from __future__ import annotations

from math import isfinite
from typing import Iterable, NamedTuple, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, SourceError

logger = get_logger("ingest.security")

US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "BATS", "CBOE", "OTC"}

# Codes used to *query* the screener, largest venues first so a --limit run gets
# the recognizable names. US_EXCHANGES stays the wider acceptance set: it also
# has to admit the aliases FMP hands back (NYSEAMERICAN, which normalizes to
# AMEX), and OTC, which is a US venue we simply do not ingest -- this is a
# listed-equity research warehouse, and OTC roughly doubled the universe.
SCREENER_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "BATS", "CBOE")

# Securities committed per batch. The bulk list is already in memory, so this is
# purely a durability boundary, not a rate limit.
COMMIT_EVERY = 500

# New listings in one nightly load beyond which the run says so loudly. A handful a
# night is the steady state; hundreds means something structural -- most often the
# first nightly run on a deployment built with `--limit`, where the rest of the
# universe arrives at once. That matters because none of them has a price watermark,
# so the very next `ingest prices` asks each for its FULL history: a screener refresh
# costing a few MB can queue a multi-hour, multi-GB price backfill against the
# 50 GB/month budget. Better to warn than to have it discovered in the bandwidth bill.
LARGE_NEW_LISTING_BATCH = 100

# A NUMERIC(p, s) column cannot hold a value >= 10 ** (p - s); these precisions are
# 0010's, on core.security. FMP occasionally returns an absurd magnitude, and
# psycopg raises NumericValueOutOfRange -- which once aborted a 21k-symbol run at
# whichever symbol happened to carry it.
SECURITY_NUMERIC_LIMITS = {
    "market_cap_usd": 10 ** (24 - 2),
    "beta": 10 ** (12 - 6),
}


class SecurityLoadResult(NamedTuple):
    """Outcome of a security-master load.

    ``new_symbols`` is what makes the nightly run legible: the difference between
    "refreshed 21,412 securities" and "refreshed 21,412 securities, 3 of them new
    tonight" is the difference between a load you can ignore and one you can audit.
    """

    total: int
    new_symbols: list[str]


def _norm_exchange(entry: dict) -> Optional[str]:
    code = (
        (entry.get("exchangeShortName") or entry.get("exchange") or "").upper().strip()
    )
    if code in {"NYSEAMERICAN", "AMEX"}:
        return "AMEX"
    if code in {"NASDAQ", "NYSE", "BATS", "CBOE", "OTC"}:
        return code
    return code or None


def _is_us(entry: dict) -> bool:
    # Equality on the normalized code, not substring containment: otherwise foreign
    # venues like "NASDAQ DUBAI" / "CBOE EUROPE" / "XOTC" would leak in.
    return _norm_exchange(entry) in US_EXCHANGES


def _us_entries(fmp: FMPClient, include_etfs: bool) -> list[tuple[dict, str, bool]]:
    """Build the US universe from the company screener, one paged pass per venue.

    ``stock-list`` / ``etf-list`` cannot answer this question: their stable
    payloads are ``symbol`` + name only, so the exchange that *defines* the
    universe is absent and every row fails :func:`_is_us` -- which showed up as a
    silent "Loaded 0 securities" against a multi-megabyte download.
    """
    entries: list[tuple[dict, str, bool]] = []
    seen: set[str] = set()
    rows_seen = 0
    us_rows = 0
    for code in SCREENER_EXCHANGES:
        try:
            rows = fmp.company_screener(exchange=code)
        except SourceError as exc:
            # A venue the plan does not cover (FMP answers 402) or does not
            # recognize must not sink the load: the venues already fetched are
            # still a valid universe, and NASDAQ/NYSE/AMEX carry the bulk of it.
            logger.warning("screener exchange=%s unavailable, skipping (%s)", code, exc)
            continue
        rows_seen += len(rows)
        kept = 0
        for row in rows:
            symbol = (row.get("symbol") or "").strip()
            # Re-check client-side rather than trusting the server filter: an
            # exchange code FMP does not recognize comes back as an unfiltered
            # page, not an error. `seen` then keeps the overlap out.
            if not symbol or symbol in seen or not _is_us(row):
                continue
            seen.add(symbol)
            us_rows += 1
            is_etf = bool(row.get("isEtf"))
            if is_etf and not include_etfs:
                continue
            entries.append((row, "etf" if is_etf else "equity", is_etf))
            kept += 1
        logger.info("screener exchange=%s: %d rows, %d kept", code, len(rows), kept)

    # Fail loudly on an empty universe -- a silent "Loaded 0 securities" against
    # a multi-megabyte download is what made the stock-list regression invisible.
    # An empty `entries` is legitimate when --no-etfs filtered out the only rows,
    # so the guards test what came back from FMP, not what survived the filters.
    if not rows_seen:
        raise SourceError(
            "company-screener returned nothing for any venue in "
            f"{list(SCREENER_EXCHANGES)} -- check the plan covers the endpoint"
        )
    if not us_rows:
        raise SourceError(
            f"company-screener returned {rows_seen} rows, none carrying a US "
            "venue -- the payload shape or the exchange codes have changed"
        )
    return entries


def _bounded_security_numerics(
    db: Database, *, row: dict, symbol: str, run: RunLog, sec_id: Optional[int] = None
) -> dict:
    """Return the screener's numerics, nulling any the column cannot hold.

    Never silently: an out-of-range value becomes NULL *and* a DQ flag, the same
    contract the price loader applies to a bad bar. Dropping one field beats
    aborting the run -- the rest of that security is still good, and so are the
    thousands of symbols behind it.
    """
    values = {
        "market_cap_usd": _to_float(row.get("marketCap") or row.get("mktCap")),
        "beta": _to_float(row.get("beta")),
    }
    for field, value in list(values.items()):
        if value is None:
            continue
        # isfinite also catches the inf/nan that float("inf") happily produces
        # from a malformed payload.
        if not isfinite(value) or abs(value) >= SECURITY_NUMERIC_LIMITS[field]:
            values[field] = None
            run.rows_quarantined += 1
            logger.warning(
                "%s: %s=%r is out of range for its column; storing NULL",
                symbol,
                field,
                value,
            )
            repo.add_dq_flag(
                db,
                check_name=f"security_{field}_out_of_range",
                security_id=sec_id,
                table_name="core.security",
                record_key={"symbol": symbol},
                detail={"field": field, "value": str(value)},
                ingestion_run_id=run.run_id,
            )
    return values


def load_securities(
    db: Database,
    fmp: FMPClient,
    *,
    universe: str = "us-equity-etf",
    include_etfs: bool = True,
    limit: Optional[int] = None,
) -> SecurityLoadResult:
    """Load the security master from FMP bulk lists.

    Returns the number upserted and the tickers that were not in the master
    before this run -- new listings entering scope.
    """
    with RunLog(
        db,
        source="fmp",
        endpoint="stock-list",
        params={"universe": universe, "limit": limit},
    ) as run:
        if universe == "us-equity-etf":
            entries = _us_entries(fmp, include_etfs)
        else:
            stocks = fmp.stock_list()
            entries = [(s, "equity", False) for s in stocks]
            if include_etfs:
                etfs = fmp.etf_list()
                etf_symbols = {e.get("symbol") for e in etfs}
                # stock-list may already include ETFs; de-dup by symbol, prefer
                # the ETF flag.
                entries = [
                    (
                        s,
                        ("etf" if s.get("symbol") in etf_symbols else "equity"),
                        s.get("symbol") in etf_symbols,
                    )
                    for s in stocks
                ]
                known = {s.get("symbol") for s in stocks}
                for e in etfs:
                    if e.get("symbol") not in known:
                        entries.append((e, "etf", True))

        # Snapshot the upsert keys of everything currently listed, so a genuinely
        # new listing can be told from a refresh of one already held. This mirrors
        # the partial unique index exactly -- (source, symbol) over rows with
        # delisted_date IS NULL -- because that index is what decides whether the
        # upsert below inserts or updates. The venue is not part of it (0012), so a
        # company changing exchange is a refresh, not an arrival.
        known = repo.active_security_keys(db)
        new_symbols: list[str] = []

        count = 0
        for entry, asset_type, is_etf in entries:
            symbol = (entry.get("symbol") or "").strip()
            if not symbol:
                continue
            # The us-equity-etf filter lives in _us_entries now -- it needs the
            # screener's fields, which the bulk lists do not carry.
            exchange = _norm_exchange(entry)
            repo.ensure_exchange(db, exchange) if exchange else None
            # The screener carries market cap and beta, so screening data costs
            # nothing beyond this call -- `--enrich` is only needed for the
            # long-form description now (0010).
            nums = _bounded_security_numerics(db, row=entry, symbol=symbol, run=run)
            if symbol not in known:
                new_symbols.append(symbol)
                known.add(symbol)
            sec_id = repo.upsert_security(
                db,
                primary_symbol=symbol,
                company_name=entry.get("name") or entry.get("companyName"),
                asset_type=asset_type,
                exchange_code=exchange,
                country=entry.get("country"),
                is_actively_trading=bool(entry.get("isActivelyTrading", True)),
                is_etf=is_etf,
                is_fund=bool(entry.get("isFund", False)),
                market_cap_usd=nums["market_cap_usd"],
                beta=nums["beta"],
            )
            repo.upsert_symbol_xref(db, security_id=sec_id, symbol=symbol)
            count += 1
            run.rows_inserted = count
            # Batched, unlike the price and action loaders: there is no network
            # call per row here, so a commit per security would dominate the
            # runtime for no resumability gain worth having.
            if count % COMMIT_EVERY == 0:
                db.commit()
            if limit and count >= limit:
                break
        run.symbols_requested = count
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info(
            "Loaded %d securities (universe=%s); %d new to the master%s",
            count,
            universe,
            len(new_symbols),
            (
                (
                    ": "
                    + ", ".join(new_symbols[:20])
                    + ("..." if len(new_symbols) > 20 else "")
                )
                if new_symbols
                else ""
            ),
        )
        if len(new_symbols) >= LARGE_NEW_LISTING_BATCH:
            logger.warning(
                "%d securities are new to the master this run. None has a price "
                "watermark, so the next `fafnir ingest prices` will pull FULL "
                "history for every one of them -- expect a long run and a large "
                "download. If this is the first nightly run on a universe built "
                "with --limit, consider `scripts/initial_backfill.sh` instead.",
                len(new_symbols),
            )
        return SecurityLoadResult(count, new_symbols)


def enrich_profiles(db: Database, fmp: FMPClient, symbols: Iterable[str]) -> int:
    """Fetch per-symbol profiles for the long-form description.

    Optional since 0010: market cap and beta now come from the screener in
    :func:`load_securities`, so the only thing this adds is `description`
    (plus CIK/ISIN/CUSIP and the IPO date). One request per symbol -- over an
    hour across a 21k universe -- so weigh it against what you actually read.
    """
    symbols = list(symbols)
    with RunLog(
        db, source="fmp", endpoint="profile", params={"symbols": len(symbols)}
    ) as run:
        count = 0
        for symbol in symbols:
            prof = fmp.profile(symbol)
            if not prof:
                continue
            sec_id = repo.resolve_security_id(db, symbol)
            if sec_id is None:
                continue
            sector_id = repo.get_or_create_sector(db, prof.get("sector"))
            industry_id = repo.get_or_create_industry(db, prof.get("industry"))
            nums = _bounded_security_numerics(
                db, row=prof, symbol=symbol, run=run, sec_id=sec_id
            )
            repo.upsert_security(
                db,
                primary_symbol=symbol,
                company_name=prof.get("companyName"),
                asset_type="etf" if prof.get("isEtf") else "equity",
                exchange_code=_norm_exchange(prof),
                sector_id=sector_id,
                industry_id=industry_id,
                currency=prof.get("currency") or "USD",
                country=prof.get("country"),
                is_actively_trading=bool(prof.get("isActivelyTrading", True)),
                is_etf=bool(prof.get("isEtf", False)),
                is_fund=bool(prof.get("isFund", False)),
                market_cap_usd=nums["market_cap_usd"],
                beta=nums["beta"],
                ipo_date=prof.get("ipoDate") or None,
                cik=prof.get("cik"),
                isin=prof.get("isin"),
                cusip=prof.get("cusip"),
            )
            repo.upsert_company_profile(
                db, security_id=sec_id, description=prof.get("description")
            )
            count += 1
            run.rows_inserted = count
            # One HTTP call per symbol here, so commit per symbol: enrichment of
            # a 20k universe runs for over an hour and must not be all-or-nothing.
            db.commit()
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded
        return count


def _to_float(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None
