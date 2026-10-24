"""`fafnir security relist`: a security the vendor that feeds it lists as trading,
held inactive here (#125).

FMP's delisted feed reported many renames as delistings (NLOK when it became GEN,
LVHB when it became BUFF), and its screener then called the old row inactive. At the
Sharadar cutover those rows were linked by permaticker and fed, so they take the
company's bars every night while fafnir still holds them inactive, on a ticker the
company no longer trades under. ``stale`` skips them (it judges active rows) and the
marts leave them out of the active universe. ``ingest sharadar-universe`` flags each
``security_inactive_but_trading`` (step 4); this decides one.

Relisting takes the feeding vendor's word, so it refuses unless that vendor's master
lists the key as live and the security's own bars are still arriving. It then:

* reverses a stamped delisting, or lists the row as trading again;
* moves it to the vendor's current ticker, when that differs, through the rename
  sweep's own rules (:func:`fafnir.db.repository.apply_rename`): an empty row on the
  new ticker is folded in, an instrument-changing rename is refused, and a row with
  history on the new ticker refuses the relist -- two rows for one company, or a
  ticker reused, is not this command's to settle. The rename is recorded in
  ``core.symbol_change`` under the vendor, dated by the vendor's own event;
* resolves the security's ``security_inactive_but_trading`` flags and the rename's
  ``symbol_change_conflict``.

Nothing is deleted and no bar changes. `security delist` stamps it delisted again.
An open ``security_delisting_contradicted`` flag refuses it: that decision is
`security venue-transfer`'s or `security delist`'s (ADR 0014).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import NamedTuple, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_map
from fafnir.ingest import sharadar_universe as su
from fafnir.ingest import symbol_changes as sc
from fafnir.ingest.delisted import CONTRADICTED_CHECK
from fafnir.ingest.security_master import (
    INACTIVE_BUT_TRADING_CHECK,
    INACTIVE_BUT_TRADING_SESSIONS,
)
from fafnir.ingest.vendor_link import PRICED_TABLES


class RelistRefused(Exception):
    """The security changed between the plan and the write."""


class RelistPlan(NamedTuple):
    """What relisting a security would change, and why it would refuse."""

    security_id: int
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    exchange: Optional[str] = None
    active_before: Optional[bool] = None
    delisted_date: Optional[date] = None
    vendor: Optional[str] = None
    vendor_key: Optional[str] = None
    vendor_ticker: Optional[str] = None
    vendor_exchange: Optional[str] = None
    last_traded: Optional[date] = None
    vendor_newest: Optional[date] = None
    # (change_date, old, new) when the security moves to the vendor's ticker.
    rename: Optional[tuple] = None
    # An empty row listed under the new ticker, folded into this one by the rename.
    folds: Optional[int] = None
    # Ticker periods the stamped delisting closed: re-opened without a rename.
    periods: Sequence = ()
    open_flags: Sequence = ()
    refusals: Sequence = ()

    @property
    def venue_differs(self) -> bool:
        """The vendor lists it on another venue than the one held here."""
        return not su.same_venue(self.vendor_exchange, self.exchange)


def _security(db: Database, security_id: int) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT primary_symbol, company_name, exchange_code, is_actively_trading,
               delisted_date
          FROM core.security WHERE security_id = %s
        """,
        (security_id,),
    )


def plan_relist(
    db: Database, *, security_id: int, rename_date: Optional[date] = None
) -> RelistPlan:
    """Read what relisting ``security_id`` would change. ``rename_date`` dates a
    move to the vendor's ticker its events do not name."""
    row = _security(db, security_id)
    if row is None:
        return RelistPlan(security_id, refusals=[f"No such security: {security_id}."])
    symbol = row["primary_symbol"]
    base = dict(
        security_id=security_id,
        symbol=symbol,
        company_name=row["company_name"],
        exchange=row["exchange_code"],
        active_before=row["is_actively_trading"],
        delisted_date=row["delisted_date"],
    )
    link = repo.feeding_link(db, security_id)
    if link is None:
        return RelistPlan(
            **base,
            refusals=[
                f"{symbol} ({security_id}) has no feeding vendor link. An FMP-fed "
                "security's status is FMP's universe load's; a move to another venue "
                "is `security venue-transfer`'s (ADR 0014)."
            ],
        )
    vendor, key = link["vendor"], link["vendor_key"]
    master = repo.vendor_master_row(db, vendor, key, PRICED_TABLES)
    if master is None:
        return RelistPlan(
            **base,
            vendor=vendor,
            vendor_key=key,
            refusals=[
                f"The {vendor} master has no {key}, so nothing says it trades "
                f"(`fafnir ingest {vendor}-master`)."
            ],
        )
    ticker = su.fafnir_ticker(master["ticker"]) or symbol
    newest = repo.vendor_newest_price_date(db, vendor, PRICED_TABLES)
    traded = (
        repo.last_traded_bar(
            db, security_id, newest - timedelta(days=su.TRADED_LOOKBACK_DAYS)
        )
        if newest
        else None
    )
    first_bar, _ = repo.security_bar_span(db, security_id)
    venue = (
        sharadar_map.map_exchange(master["exchange"]) if vendor == su.VENDOR else None
    )
    base.update(
        vendor=vendor,
        vendor_key=key,
        vendor_ticker=ticker,
        vendor_exchange=venue,
        last_traded=traded,
        vendor_newest=newest,
    )
    refusals: list[str] = []
    if master["is_delisted"]:
        refusals.append(
            f"{vendor} lists {key} ({master['ticker']}) as delisted: it does not "
            "trade, so there is nothing to relist."
        )
    if (
        traded is None
        or repo.sessions_apart(db, traded, newest) > INACTIVE_BUT_TRADING_SESSIONS
    ):
        refusals.append(
            f"{symbol} has no traded bar (volume > 0) within "
            f"{INACTIVE_BUT_TRADING_SESSIONS} sessions of {vendor}'s newest price date "
            f"({newest}); the last is {traded or 'not in the last month'}. Nothing "
            "here shows it trading."
        )
    if repo.holds_earlier_issuer(
        first_bar=first_bar,
        delisted_date=row["delisted_date"],
        link_from=link["valid_from"],
    ):
        refusals.append(
            f"{symbol} was delisted on {row['delisted_date']} and holds bars from "
            f"{first_bar}, both before its {vendor} link starts "
            f"({link['valid_from']}): an earlier issuer of the ticker. Separate them "
            "with `security split-history` first (operations.md, 'Separating two "
            "issuers held on one security')."
        )
    if row["is_actively_trading"] and symbol == ticker:
        refusals.append(f"{symbol} ({security_id}) is trading already, on {ticker}.")
    if repo.condition_open(db, security_id=security_id, check_name=CONTRADICTED_CHECK):
        refusals.append(
            f"An open {CONTRADICTED_CHECK} flag waits for `security venue-transfer` "
            "or `security delist` (ADR 0014): decide that first."
        )
    elif symbol == ticker and not su.same_venue(venue, row["exchange_code"]):
        refusals.append(
            f"{vendor} lists {symbol} on {venue}, not {row['exchange_code']}: a move "
            "between venues is `security venue-transfer`'s, which also lists it "
            "trading again (ADR 0014)."
        )

    rename = None
    folds = None
    if symbol != ticker:
        day = rename_date
        if day is None:
            found = su.latest_rename_to(
                repo.vendor_events_for_keys(db, vendor, su.RENAME_EVENTS, [key]),
                ticker,
            )
            day = found[0] if found else None
        if day is None:
            refusals.append(
                f"{vendor}'s events name no rename of {key} to {ticker}, so nothing "
                "dates the move from " + symbol + ". Pass --rename-date."
            )
        else:
            rename = (day, symbol, ticker)
        why = repo.rename_instrument_refusal(
            db, security_id=security_id, old_symbol=symbol, new_symbol=ticker
        )
        if why:
            refusals.append(f"{symbol} -> {ticker} would change the instrument: {why}")
        holders = set(repo.open_ticker_holders(db, ticker))
        listed = repo.active_security_for_symbol(db, ticker, vendor)
        if listed is not None:
            holders.add(listed)
        holders.discard(security_id)
        for holder in sorted(holders):
            if holder == listed and not repo.security_has_history(db, holder):
                folds = holder
                continue
            refusals.append(
                f"{ticker} is held by security {holder}"
                + (" with history" if repo.security_has_history(db, holder) else "")
                + ". If it is this company held twice, the two rows must become one "
                "first; if the ticker was reused, split or delist that row first "
                "(operations.md, 'A fed security held inactive')."
            )

    periods: list[dict] = []
    if row["delisted_date"] is not None:
        periods = repo.periods_closed_by_delisting(
            db, security_id=security_id, delisted_date=row["delisted_date"]
        )
        if rename is None and symbol == ticker:
            others = set(repo.open_ticker_holders(db, symbol)) - {security_id}
            if others:
                refusals.append(
                    f"{symbol} is held open by security "
                    f"{', '.join(map(str, sorted(others)))}: re-opening its period "
                    "here would put two securities on one ticker."
                )
            elif not any(p["symbol"] == symbol for p in periods):
                refusals.append(
                    f"No {symbol} ticker period of {security_id} ends at its "
                    f"delisting date {row['delisted_date']}, so relisting would leave "
                    "the ticker unresolvable. Inspect core.symbol_xref first."
                )

    open_flags = [
        int(r["dq_flag_id"])
        for r in db.fetchall(
            """
            SELECT dq_flag_id FROM ops.data_quality_flag
             WHERE security_id = %s AND check_name = %s AND resolved_at IS NULL
             ORDER BY dq_flag_id
            """,
            (security_id, INACTIVE_BUT_TRADING_CHECK),
        )
    ]
    return RelistPlan(
        **base,
        rename=rename,
        folds=folds,
        periods=periods,
        open_flags=open_flags,
        refusals=refusals,
    )


class RelistDone(NamedTuple):
    """What a relist changed."""

    periods_ended: int
    folded: Optional[int]
    flags_resolved: list
    conflicts_closed: int


def apply_relist(db: Database, plan: RelistPlan, *, note: str, by: str) -> RelistDone:
    """Make the change ``plan`` describes, in the caller's transaction. Raises
    :class:`RelistRefused` when the security is no longer as planned; the caller
    rolls back."""
    sid = plan.security_id
    if plan.refusals:
        raise RelistRefused("the plan refuses: " + " ".join(plan.refusals))
    if plan.delisted_date is not None:
        try:
            repo.reverse_delisting(
                db,
                security_id=sid,
                delisted_date=plan.delisted_date,
                # Under a rename the old ticker's periods stay closed, ended below.
                periods=() if plan.rename else plan.periods,
            )
        except repo.VenueTransferRefused as exc:
            raise RelistRefused(str(exc)) from exc
    elif not plan.active_before and not repo.mark_trading(db, sid):
        raise RelistRefused(f"security {sid} is no longer inactive and undelisted")

    ended = 0
    folded = None
    conflicts = 0
    if plan.rename:
        day, old, new = plan.rename
        if plan.delisted_date is not None:
            ended = repo.end_periods_before_rename(
                db,
                security_id=sid,
                symbol=old,
                delisted_date=plan.delisted_date,
                change_date=day,
            )
        done = repo.apply_rename(
            db,
            security_id=sid,
            old_symbol=old,
            new_symbol=new,
            change_date=day,
            source=plan.vendor,
        )
        if done.status != repo.CHANGE_APPLIED:
            raise RelistRefused(
                f"{old} -> {new} did not apply ({done.status}"
                + (f": {done.reason}" if done.reason else "")
                + ")"
            )
        folded = done.folded_security_id
        repo.record_symbol_change(
            db,
            old_symbol=old,
            new_symbol=new,
            change_date=day,
            status=repo.CHANGE_APPLIED,
            security_id=sid,
            detail={
                "vendor_key": plan.vendor_key,
                "reason": "relist",
                "by": by,
                "note": note,
            },
            source=plan.vendor,
        )
        conflicts = sc.close_applied_conflict(
            db, old=old, new=new, change_date=day, security_id=sid
        )
    resolved = repo.resolve_dq_flags(
        db,
        repo.DqFilter(
            state="open", checks=(INACTIVE_BUT_TRADING_CHECK,), security_ids=(sid,)
        ),
        note=f"Relisted: {note}",
        resolved_by=by,
    )
    return RelistDone(ended, folded, resolved, conflicts)
