"""
Declared-universe loader (ADR 0006).

``ingest securities`` builds a *discovered* universe: it re-reads FMP's company
screener nightly and keeps whatever carries a US venue. An open-end mutual fund has
no venue -- it is struck at NAV once a day, not traded on an exchange -- so it never
appears there and no amount of nightly upkeep will mint it.

This loader is the other half: it reads ``ref.tracked_symbol``, the operator's
declaration of what must be in the security master regardless of any screener, and
turns each declaration into a ``security_id``. From that point nothing downstream
knows the difference -- prices, actions, factors, marts and ``duk`` all key off
``core.security``.

It runs AFTER ``ingest securities`` in the nightly job. Both write through
``repo.upsert_security`` on the same conflict key, so a symbol that is in both
universes converges rather than forking; running second means the declared
attributes (``asset_type``, ``exchange_code``) are the ones that stand, which is
the point of declaring them.

Handful-of-symbols scale by design: one profile request each, nightly.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import _to_float
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.tracked")

ENDPOINT = "profile"

# The venue open-end funds are recorded under (migration 0018). Not a real trading
# venue: it exists so a fund has a non-NULL exchange_code that is demonstrably not
# one of SCREENER_EXCHANGES, which is what keeps the delisting sweep away from it.
FUND_EXCHANGE = "MUTF"


class TrackedLoadResult(NamedTuple):
    """Outcome of a declared-universe load.

    ``minted`` is the list worth reading in the nightly log: a declaration that has
    just become a security_id has no price watermark, so the very next
    ``ingest prices`` pulls its full available history.
    """

    total: int
    minted: list[str]
    renamed: list[tuple[str, str]]
    missing: list[str]


def _declared_exchange(declaration: dict) -> Optional[str]:
    """The venue to record, defaulting a fund to MUTF.

    Taken from the declaration rather than the vendor profile: FMP spells a fund's
    "exchange" inconsistently (and sometimes not at all), and the operator saying
    "this is a fund" is a better answer than a feed field that has no venue to name.
    """
    code = (declaration.get("exchange_code") or "").strip().upper()
    if code:
        return code
    return FUND_EXCHANGE if declaration.get("asset_type") == "fund" else None


def load_tracked(db: Database, fmp: FMPClient) -> TrackedLoadResult:
    """Mint or refresh every symbol in the declared universe.

    Returns what happened, so the nightly log can say it: how many declarations were
    loaded, which are new securities, which followed a rename, and which FMP could
    not identify at all.
    """
    declarations = repo.list_tracked_symbols(db, tracked_only=True)
    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"universe": "declared", "symbols": len(declarations)},
    ) as run:
        minted: list[str] = []
        renamed: list[tuple[str, str]] = []
        missing: list[str] = []
        count = 0

        for declaration in declarations:
            symbol = declaration["symbol"]

            # Identity first, network second. A declaration whose security has been
            # renamed since it was written still names the old ticker -- upserting
            # on that would mint a SECOND security_id and strand the fund's history,
            # watermark and actions on the first. Follow the rename and move the
            # declaration onto the ticker the security actually trades under.
            existing = repo.listed_security_for_declaration(db, symbol=symbol)
            if existing is not None and existing["primary_symbol"] != symbol:
                current = existing["primary_symbol"]
                logger.info(
                    "tracked %s has been renamed to %s; following the rename",
                    symbol,
                    current,
                )
                if repo.retarget_tracked_symbol(
                    db, old_symbol=symbol, new_symbol=current
                ):
                    renamed.append((symbol, current))
                    symbol = current
                else:
                    # The new ticker is already declared in its own right, so that
                    # row will load this security. Nothing to do under the old one.
                    logger.info(
                        "%s was already declared; untracked the stale %s declaration",
                        current,
                        symbol,
                    )
                    db.commit()
                    continue

            profile = fmp.profile(symbol)
            repo.land_payload(
                db,
                endpoint=ENDPOINT,
                params={"symbol": symbol, "universe": "declared"},
                symbol=symbol,
                http_status=200,
                payload=profile or [],
                payload_hash=payload_hash(profile or []),
                nbytes=0,
                ingestion_run_id=run.run_id,
            )
            if not profile:
                # A declared symbol FMP cannot identify is an operator error (a
                # typo, or a share class the plan does not cover), not bad data:
                # there is no record to quarantine. Flag it so it surfaces in the
                # review queue instead of failing silently every night, and carry
                # on -- the other declarations are still good.
                missing.append(symbol)
                logger.warning(
                    "%s is declared in ref.tracked_symbol but FMP returns no "
                    "profile for it -- check the ticker",
                    symbol,
                )
                repo.add_dq_flag_once(
                    db,
                    check_name="tracked_symbol_unknown_to_source",
                    severity="warn",
                    table_name="ref.tracked_symbol",
                    record_key={"symbol": symbol},
                    detail={"source": declaration["source"]},
                    ingestion_run_id=run.run_id,
                )
                db.commit()
                continue

            asset_type = declaration["asset_type"]
            exchange = _declared_exchange(declaration)
            repo.ensure_exchange(db, exchange) if exchange else None
            sec_id = repo.upsert_security(
                db,
                primary_symbol=symbol,
                company_name=profile.get("companyName") or profile.get("name"),
                # The declaration wins over the profile. FMP reports many funds as
                # plain equities; the operator declared what this is, and
                # asset_type is what the price loader reads to decide whether a
                # NAV-shaped bar is a valid bar or a defect.
                asset_type=asset_type,
                exchange_code=exchange,
                sector_id=repo.get_or_create_sector(db, profile.get("sector")),
                industry_id=repo.get_or_create_industry(db, profile.get("industry")),
                currency=profile.get("currency") or "USD",
                country=profile.get("country"),
                is_actively_trading=bool(profile.get("isActivelyTrading", True)),
                is_etf=bool(profile.get("isEtf", False)),
                is_fund=asset_type == "fund" or bool(profile.get("isFund", False)),
                beta=_to_float(profile.get("beta")),
                ipo_date=profile.get("ipoDate") or None,
                cik=profile.get("cik"),
                isin=profile.get("isin"),
                cusip=profile.get("cusip"),
            )
            if existing is None:
                minted.append(symbol)
            repo.upsert_symbol_xref(db, security_id=sec_id, symbol=symbol)
            repo.upsert_company_profile(
                db, security_id=sec_id, description=profile.get("description")
            )
            count += 1
            run.rows_inserted = count
            # One HTTP call per declaration, so commit per declaration -- the same
            # unit boundary the price and action loaders use.
            db.commit()

        run.symbols_requested = len(declarations)
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info(
            "Loaded %d declared securities; %d newly minted%s%s",
            count,
            len(minted),
            (": " + ", ".join(minted)) if minted else "",
            (
                f"; {len(renamed)} followed a rename"
                + "".join(f" ({old}->{new})" for old, new in renamed)
                if renamed
                else ""
            ),
        )
        if missing:
            logger.warning(
                "%d declared symbol(s) unknown to FMP: %s",
                len(missing),
                ", ".join(missing),
            )
        return TrackedLoadResult(count, minted, renamed, missing)
