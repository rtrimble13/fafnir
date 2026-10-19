"""
Manual link review: `fafnir vendor link accept|reject` (plan SA-0402, #63).

The linking engine (#62) refuses to guess. A person resolves what it cannot, and
each decision is recorded with who made it and why:

* **accept** opens a ``match_method = 'manual'`` link from a security to a vendor
  key. It refuses a key another security's open link holds: a key is one security
  at a time, so that link has to be rejected or closed first. It refuses the
  security's own open link if that one is a feed, since moving a feed is #98's;
  any other open link is closed the day before the new one starts. It resolves the
  security's open ``vendor_xref_*`` flags, and withdraws a rejection of the same
  pair.
* **reject** records the pair in ``core.security_vendor_rejection``, so the engine
  never proposes it again. If the security's open link is to that key it is closed
  today, unless it is a feed.

Both are planned first (:func:`plan_accept`, :func:`plan_reject`): the plan says
what would change and refuses what must not, writing nothing. ``--dry-run`` stops
there, and the apply functions do exactly what the plan says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import vendor_link as vl


class ReviewRefused(ValueError):
    """A decision the warehouse must not take as asked; the message says why."""


@dataclass
class AcceptPlan:
    security: dict
    vendor: str
    vendor_key: str
    valid_from: date
    close: Optional[dict] = None  # the security's open link this replaces
    noop: bool = False  # already linked to this key
    unrejects: bool = False
    flags: list = field(default_factory=list)  # open vendor_xref_* flags to resolve


@dataclass
class RejectPlan:
    security: dict
    vendor: str
    vendor_key: str
    close: Optional[dict] = None  # the open link to the rejected key
    restates: bool = False  # already rejected: the note and owner are replaced


def review_security(db: Database, security_id: int) -> dict:
    rows = repo.link_scope(db, vl.VENDOR, security_ids=[security_id], review=True)
    if not rows:
        raise ReviewRefused(f"no security {security_id}")
    return rows[0]


def _open_link(db: Database, security_id: int, vendor: str) -> Optional[dict]:
    return next(
        (
            r
            for r in repo.vendor_links_of(db, security_id, vendor)
            if r["valid_to"] is None
        ),
        None,
    )


def _vendor_rows(db: Database, vendor: str, vendor_key: str) -> list[dict]:
    rows = repo.vendor_security_rows(db, vendor, vendor_key)
    if not rows:
        raise ReviewRefused(
            f"no {vendor} key {vendor_key} in core.vendor_security "
            "(`fafnir ingest sharadar-master` loads it)"
        )
    return rows


def open_flags(db: Database, security_id: int) -> list[dict]:
    return repo.list_dq_flags(
        db,
        repo.DqFilter(checks=("vendor_xref_*",), security_ids=(security_id,)),
        limit=100,
    )


def _default_from(security: dict, rows: list[dict]) -> date:
    """Where the engine would start the link: the overlap of the two price
    histories. Without one, the vendor's first price, else today."""
    priced = [r for r in rows if r["vendor_table"] in vl.PRICED_TABLES] or rows
    for r in priced:
        span = vl.overlap(security, r)
        if span:
            return span[0]
    firsts = [r["first_price_date"] for r in priced if r["first_price_date"]]
    return min(firsts) if firsts else date.today()


def plan_accept(
    db: Database,
    *,
    security_id: int,
    vendor_key: str,
    valid_from: Optional[date] = None,
    vendor: str = vl.VENDOR,
) -> AcceptPlan:
    security = review_security(db, security_id)
    rows = _vendor_rows(db, vendor, vendor_key)
    holder = repo.open_vendor_link_by_key(db, vendor, vendor_key)
    if holder is not None and holder["security_id"] != security_id:
        raise ReviewRefused(
            f"{vendor} key {vendor_key} is security {holder['security_id']}'s "
            f"(open since {holder['valid_from']}, {holder['match_method']}): reject "
            "or close that link first; a key is one security at a time"
        )
    current = _open_link(db, security_id, vendor)
    start = valid_from or _default_from(security, rows)
    plan = AcceptPlan(security, vendor, vendor_key, start)
    plan.unrejects = any(
        r["vendor_key"] == vendor_key
        for r in repo.vendor_rejections(db, vendor, security_id)
    )
    plan.flags = open_flags(db, security_id)
    if current is not None and current["vendor_key"] == vendor_key:
        plan.noop = True
        return plan
    earlier = repo.vendor_link_at(db, vendor, vendor_key, start)
    if earlier is not None:
        raise ReviewRefused(
            f"a closed link to {vendor_key} already starts {start} (to "
            f"{earlier['valid_to']}): pass --from after {earlier['valid_to']}"
        )
    if current is not None:
        if current["is_feed"]:
            raise ReviewRefused(
                f"security {security_id}'s open link to {current['vendor_key']} is "
                "its feed: move the feed first (#98)"
            )
        if start - timedelta(days=1) < current["valid_from"]:
            raise ReviewRefused(
                f"the open link to {current['vendor_key']} starts "
                f"{current['valid_from']}: pass --from after it"
            )
        plan.close = current
    return plan


def apply_accept(db: Database, plan: AcceptPlan, *, by: str, note: str) -> list[int]:
    """Write the plan; return the ids of the flags it resolved."""
    sid, key = plan.security["security_id"], plan.vendor_key
    if plan.unrejects:
        repo.delete_vendor_rejection(db, sid, plan.vendor, key)
    if not plan.noop:
        if plan.close is not None:
            repo.close_vendor_link(
                db,
                plan.vendor,
                plan.close["vendor_key"],
                plan.close["valid_from"],
                plan.valid_from - timedelta(days=1),
            )
        repo.insert_vendor_link(
            db,
            security_id=sid,
            vendor=plan.vendor,
            vendor_key=key,
            valid_from=plan.valid_from,
            match_method="manual",
            confidence=None,
            evidence={
                "decision": "accepted",
                "by": by,
                "note": note,
                "replaced": _summary(plan.close),
                "flags_resolved": [f["dq_flag_id"] for f in plan.flags],
            },
            created_by=by,
            note=note,
        )
    if not plan.flags:
        return []
    return repo.resolve_dq_flags(
        db,
        repo.DqFilter(flag_ids=tuple(f["dq_flag_id"] for f in plan.flags)),
        note=f"manual link to {plan.vendor} {key}: {note}",
        resolved_by=by,
    )


def plan_reject(
    db: Database, *, security_id: int, vendor_key: str, vendor: str = vl.VENDOR
) -> RejectPlan:
    security = review_security(db, security_id)
    _vendor_rows(db, vendor, vendor_key)
    plan = RejectPlan(security, vendor, vendor_key)
    plan.restates = any(
        r["vendor_key"] == vendor_key
        for r in repo.vendor_rejections(db, vendor, security_id)
    )
    current = _open_link(db, security_id, vendor)
    if current is not None and current["vendor_key"] == vendor_key:
        if current["is_feed"]:
            raise ReviewRefused(
                f"the link to {vendor_key} is security {security_id}'s feed: move "
                "the feed first (#98)"
            )
        plan.close = current
    return plan


def apply_reject(db: Database, plan: RejectPlan, *, by: str, note: str) -> None:
    sid = plan.security["security_id"]
    if plan.close is not None:
        repo.close_vendor_link(
            db,
            plan.vendor,
            plan.vendor_key,
            plan.close["valid_from"],
            max(date.today(), plan.close["valid_from"]),
        )
    repo.record_vendor_rejection(
        db,
        security_id=sid,
        vendor=plan.vendor,
        vendor_key=plan.vendor_key,
        rejected_by=by,
        note=note,
        evidence={"closed_link": _summary(plan.close)},
    )


def _summary(link: Optional[dict]) -> Optional[dict]:
    if link is None:
        return None
    return {
        k: link[k]
        for k in (
            "vendor_key",
            "valid_from",
            "match_method",
            "confidence",
            "created_by",
        )
    }
