"""
Security-master loader.

Builds ``core.security`` + ``core.symbol_xref`` from FMP's ``stock-list`` and
``etf-list``. The universe filter keeps US-listed equities and ETFs. Profiles
(sector/industry/description) are enriched separately via :func:`enrich_profiles`
because the per-symbol profile endpoint is far more expensive than the bulk lists.

Delisted/inactive securities are never deleted; a reconciliation step (fast-follow,
using FMP's delisted endpoint) flips ``is_actively_trading``/``delisted_date``.
"""

from __future__ import annotations

from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient

logger = get_logger("ingest.security")

US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "BATS", "CBOE", "OTC"}


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


def load_securities(
    db: Database,
    fmp: FMPClient,
    *,
    universe: str = "us-equity-etf",
    include_etfs: bool = True,
    limit: Optional[int] = None,
) -> int:
    """Load the security master from FMP bulk lists. Returns securities upserted."""
    with RunLog(
        db,
        source="fmp",
        endpoint="stock-list",
        params={"universe": universe, "limit": limit},
    ) as run:
        stocks = fmp.stock_list()
        entries = [(s, "equity", False) for s in stocks]
        if include_etfs:
            etfs = fmp.etf_list()
            etf_symbols = {e.get("symbol") for e in etfs}
            # stock-list may already include ETFs; de-dup by symbol, prefer ETF flag.
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

        count = 0
        for entry, asset_type, is_etf in entries:
            symbol = (entry.get("symbol") or "").strip()
            if not symbol:
                continue
            if universe == "us-equity-etf" and not _is_us(entry):
                continue
            exchange = _norm_exchange(entry)
            repo.ensure_exchange(db, exchange) if exchange else None
            sec_id = repo.upsert_security(
                db,
                primary_symbol=symbol,
                company_name=entry.get("name") or entry.get("companyName"),
                asset_type=asset_type,
                exchange_code=exchange,
                is_etf=is_etf,
                is_fund=False,
            )
            repo.upsert_symbol_xref(db, security_id=sec_id, symbol=symbol)
            count += 1
            run.rows_inserted = count
            if limit and count >= limit:
                break
        run.symbols_requested = count
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info("Loaded %d securities (universe=%s)", count, universe)
        return count


def enrich_profiles(db: Database, fmp: FMPClient, symbols: Iterable[str]) -> int:
    """Fetch per-symbol profiles and populate sector/industry/description."""
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
                ipo_date=prof.get("ipoDate") or None,
                cik=prof.get("cik"),
                isin=prof.get("isin"),
                cusip=prof.get("cusip"),
            )
            repo.upsert_company_profile(
                db,
                security_id=sec_id,
                description=prof.get("description"),
                ceo=prof.get("ceo"),
                full_time_employees=_to_int(prof.get("fullTimeEmployees")),
                website=prof.get("website"),
                beta=_to_float(prof.get("beta")),
                market_cap_usd=_to_float(prof.get("mktCap") or prof.get("marketCap")),
                last_dividend=_to_float(
                    prof.get("lastDiv") or prof.get("lastDividend")
                ),
                price_range=prof.get("range"),
                image_url=prof.get("image"),
            )
            count += 1
            run.rows_inserted = count
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded
        return count


def _to_int(value) -> Optional[int]:
    try:
        return int(str(value).replace(",", "")) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _to_float(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None
