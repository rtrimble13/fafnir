"""
`fafnir vendor mint-delisted`: mint the issuers that died before fafnir first ran,
from Sharadar (plan SA-0502, #71).

FMP's screener cannot return a security that no longer trades, so the warehouse
holds only the dead issuers it watched die. Sharadar's master covers every one it
priced. Each that ADR 0016 §3 admits becomes a ``core.security`` row minted by
Sharadar:

- **Which.** :func:`vendor_overlap.mintable` (priced in SEP/SFP, delisted on or
  after 1998-01-01, a category and a venue fafnir keeps, and not a warrant, right or
  unit ADR 0012 keeps out by ``[general] exclude_instruments``), with no open link
  to its permaticker, and no open ``vendor_history_overlap`` flag for it (#68): a
  flagged issuer's history is already on an FMP row. ``--permaticker K --allow-overlap``
  mints one flagged issuer on purpose, for the split-history playbook.
- **The row.** ``source = 'sharadar'``, the vendor's ticker as ``primary_symbol``,
  its name, ``asset_type``/``is_etf``/``is_fund`` and venue through
  :mod:`sharadar_map`, the sector and industry where fafnir's vocabulary has them
  (ADR 0012 allows Sharadar's own rows), ``is_actively_trading = false``,
  ``delisted_date`` from the ``delisted`` event (else the last price), ``ipo_date``
  from the ``listed`` event.
- **Tickers.** One **closed** ``core.symbol_xref`` period per ticker it traded
  under, cut at its ``tickerchangefrom``/``tickerchangeto`` events, ending on its
  last price. Closed periods never meet the open-period index, so a dead issuer
  cannot take a live security's ticker.
- **The link.** ``match_method = 'minted'``, ``is_feed``: Sharadar feeds it (#75)
  and FMP never does (ADR 0015 §2); `vendor link` never evaluates it.

The rows arrive empty: their prices come with #75. A re-run mints nothing new (each
minted permaticker is linked). It refuses to run until `vendor overlap` has run
since the master was last loaded, so no issuer is minted onto history an FMP row
already holds.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_map
from fafnir.ingest import vendor_link as vl
from fafnir.ingest import vendor_overlap as vo
from fafnir.ingest.runlog import RunLog
from fafnir.instruments import INSTRUMENT_KINDS
from fafnir.logging_config import get_logger

logger = get_logger("ingest.vendor_mint")

CREATED_BY = "fafnir vendor mint-delisted"
ENDPOINT = "mint-delisted"
BATCH = 500
MASTER_ENDPOINTS = ("master:tickers", "master:actions")
EVENTS = ("listed", "delisted", "tickerchangefrom", "tickerchangeto")


class MintError(RuntimeError):
    """The mint cannot run: no fresh overlap scan, or the permaticker asked for
    cannot be minted."""


@dataclass
class Mint:
    """One dead issuer to mint."""

    vendor_key: str
    vendor_table: str
    ticker: str
    name: Optional[str]
    category: str
    asset_class: sharadar_map.AssetClass
    exchange_code: str
    sector: Optional[str]
    industry: Optional[str]
    cik: Optional[str]
    first_price_date: date
    last_price_date: date
    delisted_date: date
    ipo_date: Optional[date]
    periods: list[tuple[str, date, date]] = field(default_factory=list)


@dataclass
class MintResult:
    dry_run: bool
    run_id: Optional[int] = None
    mints: list[Mint] = field(default_factory=list)
    held_back: int = 0  # mintable, but flagged vendor_history_overlap
    out_of_scope: int = 0  # dead issuers ADR 0012 keeps out (warrants, rights, units)
    minted: int = 0
    unclassified: int = 0  # minted with no sector or industry fafnir knows
    periods_skipped: int = 0  # a ticker period another row starts on that date

    def by(self, what: str) -> Counter:
        if what == "category":
            return Counter(m.category for m in self.mints)
        if what == "exchange":
            return Counter(m.exchange_code for m in self.mints)
        return Counter(m.delisted_date.year for m in self.mints)


def ticker_periods(
    ticker: str, first: date, last: date, events: Sequence[dict]
) -> list[tuple[str, date, date]]:
    """``(symbol, valid_from, valid_to)``, every one closed: a period per ticker the
    issuer traded under, cut the day before each ticker change and ending on its
    last price under the vendor's ticker. A change outside the price span, or that
    names no new ticker, is ignored."""
    changes: dict[date, tuple[str, str]] = {}
    for e in events:
        if e["action"] == "tickerchangefrom":
            old, new = e["contra_ticker"], e["ticker"]
        elif e["action"] == "tickerchangeto":
            old, new = e["ticker"], e["contra_ticker"]
        else:
            continue
        d = e["event_date"]
        if old and new and old != new and first < d <= last:
            changes.setdefault(d, (old, new))
    out, start = [], first
    for d in sorted(changes):
        old, _ = changes[d]
        out.append((old, start, d - timedelta(days=1)))
        start = d
    out.append((ticker, start, last))
    return out


def _guard(db: Database) -> None:
    """`vendor overlap` must have run since the master was last loaded."""
    scan = repo.last_successful_run(db, vl.VENDOR, vo.ENDPOINT)
    loads = [repo.last_successful_run(db, vl.VENDOR, e) for e in MASTER_ENDPOINTS]
    newest = max((r["ingestion_run_id"] for r in loads if r), default=None)
    if scan is None or (newest is not None and scan["ingestion_run_id"] < newest):
        raise MintError(
            "`fafnir vendor overlap` has not run since the Sharadar master was last "
            "loaded: run it first, so no dead issuer is minted onto history an FMP "
            "security already holds"
        )


def plan(
    db: Database,
    *,
    permaticker: Optional[str] = None,
    allow_overlap: bool = False,
    limit: int = 0,
    sample: int = 0,
    seed: int = 0,
    excluded_kinds: Sequence[str] = INSTRUMENT_KINDS,
) -> MintResult:
    """What would be minted; writes nothing. ``sample`` picks that many at random
    (``seed`` makes the pick repeatable): #76 measures DQ on a sample, minted,
    loaded and checked on one day. ``excluded_kinds`` is ``[general]
    exclude_instruments`` (ADR 0012)."""
    _guard(db)
    excluded = tuple(excluded_kinds)
    declared = vo.declared_symbols(db, excluded)
    candidates = repo.vendor_link_candidates(db, vl.VENDOR, vl.PRICED_TABLES)
    flags = repo.list_dq_flags(db, repo.DqFilter(checks=(vo.CHECK,)), limit=1_000_000)
    flagged = {
        (f.get("record_key") or {}).get("vendor_key")
        for f in flags
        if (f.get("record_key") or {}).get("kind") == "dead_issuer"
    }
    if permaticker is not None:
        c = next((c for c in candidates if c["vendor_key"] == permaticker), None)
        if c is None:
            raise MintError(f"no Sharadar stock or fund has permaticker {permaticker}")
        if c["taken"]:
            raise MintError(f"permaticker {permaticker} is already linked")
        kind = vo.out_of_scope(c, excluded=excluded, declared=declared)
        if kind and vo.mintable(c, excluded=()):
            raise MintError(
                f"permaticker {permaticker} ({c['ticker']}) is a {kind}, which ADR "
                "0012 keeps out ([general] exclude_instruments)"
            )
        if not vo.mintable(c, excluded=excluded, declared=declared):
            raise MintError(
                f"permaticker {permaticker} is outside ADR 0016's rules (alive, before "
                "1998, or a category or venue fafnir does not keep)"
            )
        if permaticker in flagged and not allow_overlap:
            raise MintError(
                f"permaticker {permaticker} has an open vendor_history_overlap flag: "
                "pass --allow-overlap to mint it on purpose (the split-history playbook)"
            )
        chosen, held_back, kept_out = [c], 0, 0
    else:
        open_ = [
            c for c in candidates if not c["taken"] and vo.mintable(c, excluded=())
        ]
        eligible = [
            c
            for c in open_
            if vo.out_of_scope(c, excluded=excluded, declared=declared) is None
        ]
        kept_out = len(open_) - len(eligible)
        chosen = [c for c in eligible if c["vendor_key"] not in flagged]
        held_back = len(eligible) - len(chosen)
    chosen.sort(key=lambda c: c["vendor_key"])
    if sample and sample < len(chosen):
        chosen = sorted(
            random.Random(seed).sample(chosen, sample), key=lambda c: c["vendor_key"]
        )
    if limit:
        chosen = chosen[:limit]

    events: dict[str, list[dict]] = {}
    for e in repo.vendor_events(
        db, vl.VENDOR, [c["vendor_key"] for c in chosen], EVENTS
    ):
        events.setdefault(e["vendor_key"], []).append(e)
    sectors, industries = repo.classification_names(db)
    result = MintResult(dry_run=True, held_back=held_back, out_of_scope=kept_out)
    for c in chosen:
        mine = events.get(c["vendor_key"], [])
        delisted = [e["event_date"] for e in mine if e["action"] == "delisted"]
        listed = [e["event_date"] for e in mine if e["action"] == "listed"]
        sector, industry = sharadar_map.map_classification(
            c.get("sector"), c.get("industry"), sectors=sectors, industries=industries
        )
        result.mints.append(
            Mint(
                vendor_key=c["vendor_key"],
                vendor_table=c["vendor_table"],
                ticker=c["ticker"],
                name=c["name"],
                category=c["category"],
                asset_class=sharadar_map.map_category(c["category"]),
                exchange_code=sharadar_map.map_exchange(c["exchange"]),
                sector=sector,
                industry=industry,
                cik=c["cik"],
                first_price_date=c["first_price_date"],
                last_price_date=c["last_price_date"],
                delisted_date=max(delisted) if delisted else c["last_price_date"],
                ipo_date=min(listed) if listed else None,
                periods=ticker_periods(
                    c["ticker"], c["first_price_date"], c["last_price_date"], mine
                ),
            )
        )
    return result


def _mint_one(db: Database, m: Mint, run_id: int) -> tuple[bool, int]:
    """Write one dead issuer. Returns (unclassified, periods skipped)."""
    repo.ensure_exchange(db, m.exchange_code)
    sid = repo.upsert_security(
        db,
        primary_symbol=m.ticker,
        company_name=m.name,
        asset_type=m.asset_class.asset_type,
        exchange_code=m.exchange_code,
        sector_id=repo.get_or_create_sector(db, m.sector),
        industry_id=repo.get_or_create_industry(db, m.industry),
        is_actively_trading=False,
        is_etf=m.asset_class.is_etf,
        is_fund=m.asset_class.is_fund,
        ipo_date=m.ipo_date,
        delisted_date=m.delisted_date,
        cik=m.cik,
        source=vl.VENDOR,
    )
    skipped = 0
    for symbol, lo, hi in m.periods:
        if not repo.insert_symbol_period(
            db,
            security_id=sid,
            symbol=symbol,
            valid_from=lo,
            valid_to=hi,
            source=vl.VENDOR,
        ):
            skipped += 1
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor=vl.VENDOR,
        vendor_key=m.vendor_key,
        valid_from=m.first_price_date,
        match_method="minted",
        confidence=None,
        evidence={
            "run_id": run_id,
            "vendor_table": m.vendor_table,
            "category": m.category,
        },
        created_by=CREATED_BY,
        is_feed=True,
    )
    return (m.sector is None or m.industry is None), skipped


def mint(
    db: Database,
    *,
    permaticker: Optional[str] = None,
    allow_overlap: bool = False,
    limit: int = 0,
    sample: int = 0,
    seed: int = 0,
    dry_run: bool = False,
    excluded_kinds: Sequence[str] = INSTRUMENT_KINDS,
) -> MintResult:
    """Plan and, unless ``dry_run``, mint (one run, a commit per 500)."""
    result = plan(
        db,
        permaticker=permaticker,
        allow_overlap=allow_overlap,
        limit=limit,
        sample=sample,
        seed=seed,
        excluded_kinds=excluded_kinds,
    )
    result.dry_run = dry_run
    if dry_run:
        return result
    run = RunLog(
        db,
        source=vl.VENDOR,
        endpoint=ENDPOINT,
        params={
            "permaticker": permaticker,
            "allow_overlap": allow_overlap,
            "limit": limit,
            "sample": sample,
            "seed": seed,
        },
    )
    with run:
        result.run_id = run.run_id
        for i, m in enumerate(result.mints, 1):
            unclassified, skipped = _mint_one(db, m, run.run_id)
            result.minted += 1
            result.unclassified += unclassified
            result.periods_skipped += skipped
            if i % BATCH == 0:
                db.commit()
        run.rows_inserted = result.minted
        run.params.update(
            minted=result.minted,
            held_back=result.held_back,
            out_of_scope=result.out_of_scope,
            unclassified=result.unclassified,
            periods_skipped=result.periods_skipped,
        )
        db.commit()
    return result
