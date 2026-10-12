"""`fafnir security venue-transfer` and `security delist`: the operator's decision
on a security the delisting sweep will not judge alone (ADR 0014).

On 2026-10-05 ET, SUN, SUNC, USAC, DDS and DDT moved from the NYSE to the Texas
Stock Exchange under the same tickers. FMP's delisted feed reported each as an NYSE
delisting, the sweep stamped all six, and the price step stopped asking for their
bars, while FMP's price feed went on serving them. The sweep now declines a recent
delisting the vendor still has bars after, and flags it
(``security_delisting_contradicted``). It cannot decide the rest itself:

* a move to another listed venue keeps printing bars -- ``venue-transfer``;
* so does a move to OTC, which takes a security out of this warehouse's scope:
  GRP-UN left the NYSE for OTCQX on 2025-12-31 and FMP served its OTC bars until
  2026-01-22 -- ``delist``.

``venue-transfer`` records the move whether or not the sweep already stamped the
delisting, and reverses the stamp when it did. It sets the venue as an operator
override the nightly load keeps (0027), and *accepts* the contradiction flag for
each feed record it settles, which is how the sweep then declines that record
without asking (``known_transfer``). ``delist`` stamps the delisting the sweep
declined. Neither deletes anything, and each can be undone by the other, so both
are commands an agent may run after showing the dry run and getting an explicit
yes, like ``security merge``.

The work is split the usual way: a plan reads the warehouse and says what would
change and what refuses it; :func:`vendor_bars_after` asks the vendor; the
repository makes the change.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.delisted import CONTRADICTED_CHECK, bars_after, contradiction_key
from fafnir.ingest.security_master import (
    INACTIVE_BUT_TRADING_CHECK,
    LISTED_US_EXCHANGES,
    _norm_exchange,
)
from fafnir.sources.fmp import FMPClient, SourceError

# The codes a venue may be given as, for the refusal message: aliases normalize away.
_VENUE_CHOICES = sorted(LISTED_US_EXCHANGES - {"NYSEAMERICAN"})


def _security(db: Database, security_id: int) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT primary_symbol, company_name, exchange_code, venue_override,
               venue_override_left, is_actively_trading, delisted_date, source
          FROM core.security WHERE security_id = %s
        """,
        (security_id,),
    )


def _open_flags(db: Database, security_id: int, checks: tuple[str, ...]) -> list[int]:
    return [
        int(r["dq_flag_id"])
        for r in db.fetchall(
            """
            SELECT dq_flag_id FROM ops.data_quality_flag
             WHERE security_id = %s AND check_name = ANY(%s) AND resolved_at IS NULL
             ORDER BY dq_flag_id
            """,
            (security_id, list(checks)),
        )
    ]


class TransferPlan(NamedTuple):
    """What a venue transfer would change, and why it would refuse."""

    security_id: int
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    delisted_date: Optional[date] = None
    active_before: Optional[bool] = None
    exchange_before: Optional[str] = None
    venue: Optional[str] = None
    left: Optional[str] = None
    moved_on: Optional[date] = None
    periods: Sequence = ()
    watermark: Optional[date] = None
    arm_keys: Sequence = ()
    open_contradictions: Sequence = ()
    open_inactive: Sequence = ()
    refusals: Sequence = ()


def plan_venue_transfer(
    db: Database, *, security_id: int, venue_code: str, moved_on: date
) -> TransferPlan:
    """Read what recording ``security_id``'s move to ``venue_code`` would change.

    Refuses a venue that is not a listed US venue, an operator-minted security and,
    for one the sweep stamped delisted, a ticker another security now holds or
    whose period the delisting did not close (re-opening nothing would leave the
    ticker unresolvable: half a repair).

    ``arm_keys`` are the feed records the move settles: the stamped delisting, and
    one dated ``moved_on``, which is how FMP dated ET's (2026-10-05, its first day
    on TXSE). Each is accepted, so the sweep declines it without asking. A feed
    record dated otherwise is flagged once, and settled by running this again or
    by ``dq accept``.
    """
    row = _security(db, security_id)
    if row is None:
        return TransferPlan(security_id, refusals=[f"No such security: {security_id}."])
    symbol = row["primary_symbol"]
    delisted = row["delisted_date"]
    venue = _norm_exchange({"exchange": venue_code})
    refusals: list[str] = []
    if venue not in LISTED_US_EXCHANGES:
        refusals.append(
            f"--venue {venue_code} is not a listed US venue "
            f"({', '.join(_VENUE_CHOICES)}). A move to OTC takes a security out of "
            "this warehouse's scope: record it with `security delist` (ADR 0014)."
        )
    if row["source"] == "operator":
        refusals.append(
            f"{symbol} ({security_id}) was minted by `security split-history`: it "
            "holds a history split off another security, and its ticker belongs to "
            "another issuer."
        )
    periods: list[dict] = []
    if delisted is not None:
        holders = set(repo.open_ticker_holders(db, symbol))
        listed = repo.active_security_for_symbol(db, symbol)
        if listed is not None:
            holders.add(listed)
        holders.discard(security_id)
        if holders:
            refusals.append(
                f"{symbol} is held by security "
                f"{', '.join(map(str, sorted(holders)))}: reversing the delisting "
                "would put two securities on one ticker. If that row is a re-mint "
                "of this one, fold it in with `security merge` first."
            )
        periods = repo.periods_closed_by_delisting(
            db, security_id=security_id, delisted_date=delisted
        )
        if not any(p["symbol"] == symbol for p in periods):
            refusals.append(
                f"No {symbol} ticker period of {security_id} ends at its delisting "
                f"date {delisted}, so core.symbol_xref is not as `ingest delisted` "
                "left it, and reversing the delisting would not make the ticker "
                "resolve. Inspect it before going on."
            )
    arm_dates = sorted({moved_on, *([delisted] if delisted else [])})
    return TransferPlan(
        security_id=security_id,
        symbol=symbol,
        company_name=row["company_name"],
        delisted_date=delisted,
        active_before=row["is_actively_trading"],
        exchange_before=row["exchange_code"],
        venue=venue,
        # Re-recording a move keeps the venue it was first recorded as leaving.
        left=row["venue_override_left"] or row["exchange_code"],
        moved_on=moved_on,
        periods=periods,
        watermark=repo.get_watermark(db, "fmp", PRICE_ENDPOINT, security_id),
        arm_keys=[contradiction_key(symbol, d) for d in arm_dates],
        open_contradictions=_open_flags(db, security_id, (CONTRADICTED_CHECK,)),
        open_inactive=_open_flags(db, security_id, (INACTIVE_BUT_TRADING_CHECK,)),
        refusals=refusals,
    )


class VendorBars(NamedTuple):
    """The vendor's bars strictly after a date, oldest first, or why it could not
    be asked."""

    bars: list
    error: Optional[str]


def vendor_bars_after(
    fmp: FMPClient, symbol: str, *, after: date, today: date
) -> VendorBars:
    """One request: the price feed from ``after`` to ``today``."""
    try:
        raw = fmp.eod_raw(
            symbol, from_date=after.isoformat(), to_date=today.isoformat()
        )
    except SourceError as exc:
        return VendorBars([], str(exc))
    return VendorBars(bars_after(raw or [], after), None)


def evidence_refusals(evidence: VendorBars, *, symbol: str, moved_on: date) -> list:
    """The vendor must serve a bar after the move date: proof the ticker trades on.

    After, not on: FMP's delisting date is often a security's last trading day, so
    a bar on the date is what a real delisting looks like too.
    """
    if evidence.error:
        return [
            f"Could not ask the vendor for {symbol}'s bars after {moved_on}: "
            f"{evidence.error}."
        ]
    if not evidence.bars:
        return [
            f"The vendor has no bar for {symbol} after {moved_on}, so nothing shows it "
            "trading on. Record the move once it serves one; until then the nightly "
            "price universe keeps polling a listed security that traded in the last "
            "30 days (ADR 0014)."
        ]
    return []


class DelistPlan(NamedTuple):
    """What confirming a delisting would change, and why it would refuse."""

    security_id: int
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    exchange: Optional[str] = None
    delisted_on: Optional[date] = None
    last_bar: Optional[date] = None
    periods: Sequence = ()
    open_flags: Sequence = ()
    refusals: Sequence = ()


def plan_delist(db: Database, *, security_id: int, delisted_on: date) -> DelistPlan:
    """Read what stamping ``security_id`` delisted on ``delisted_on`` would change."""
    row = _security(db, security_id)
    if row is None:
        return DelistPlan(security_id, refusals=[f"No such security: {security_id}."])
    symbol = row["primary_symbol"]
    refusals: list[str] = []
    if row["delisted_date"] is not None:
        refusals.append(
            f"{symbol} ({security_id}) is already delisted, on {row['delisted_date']}."
        )
    _, last_bar = repo.security_bar_span(db, security_id)
    return DelistPlan(
        security_id=security_id,
        symbol=symbol,
        company_name=row["company_name"],
        exchange=row["exchange_code"],
        delisted_on=delisted_on,
        last_bar=last_bar,
        periods=db.fetchall(
            """
            SELECT symbol, valid_from FROM core.symbol_xref
             WHERE security_id = %s AND valid_to IS NULL ORDER BY symbol
            """,
            (security_id,),
        ),
        open_flags=_open_flags(
            db, security_id, (CONTRADICTED_CHECK, INACTIVE_BUT_TRADING_CHECK)
        ),
        refusals=refusals,
    )
