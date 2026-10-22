"""
`fafnir vendor feed`: which vendor loads a security's new rows (plan SA-1005, #98;
ADR 0015 §2).

A security is fed by one vendor at a time. FMP feeds the securities it minted
(:func:`repository.fmp_fed`) unless a feeding link in ``core.security_vendor_xref``
(``is_feed`` on an open link) gives them to another vendor. This command moves
that link:

- ``--vendor sharadar|tiingo``: sets ``is_feed`` on each in-scope security's open
  link to the vendor, and clears it on any other vendor's link first, since one
  feeding link per security is the rule (``ux_svx_one_feed``). A security FMP was
  feeding leaves FMP's universes at once. One with no open link to the vendor is
  reported, not linked: that is `vendor link`'s decision.
- ``--vendor fmp``: clears every feeding link of the in-scope securities FMP
  minted, so FMP feeds them again: the rollback. A security another vendor minted
  (a Sharadar dead issuer) keeps its feed, since FMP serves whoever trades under its
  ticker today.

Nothing else moves: no row in ``core.daily_price`` or ``core.corporate_action``
changes, whatever vendor wrote it. A switch is one ``ops.ingestion_run``
(``endpoint = feed``) naming who made it and the securities it changed; the dry run
applies and rolls back.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger

logger = get_logger("ingest.vendor_feed")

ENDPOINT = "feed"
VENDORS = (repo.FMP_SOURCE,) + repo.LINKED_VENDORS


class FeedError(ValueError):
    """The switch cannot run: an unknown vendor, or no scope."""


@dataclass
class FeedChange:
    security_id: int
    symbol: str
    outcome: str  # fed | already | no_link | restored | not_fmp_minted
    previous: Optional[str] = None  # the vendor that fed it before


@dataclass
class FeedResult:
    vendor: str
    dry_run: bool
    run_id: Optional[int] = None
    changes: list[FeedChange] = field(default_factory=list)

    def counts(self) -> Counter:
        return Counter(c.outcome for c in self.changes)

    def changed(self) -> list[int]:
        return [c.security_id for c in self.changes if c.outcome in ("fed", "restored")]


def _scope_sql(funds: bool, security_ids: Optional[Sequence[int]]) -> tuple[str, list]:
    where, params = [], []
    if funds:
        where.append("s.is_fund")
    if security_ids is not None:
        where.append("s.security_id = ANY(%s)")
        params.append(list(security_ids))
    return "".join(f" AND {w}" for w in where), params


def _feed_to(db, result, vendor, funds, security_ids) -> None:
    extra, params = _scope_sql(funds, security_ids)
    rows = db.fetchall(
        f"""
        SELECT l.security_id, l.vendor_key, l.is_feed, s.primary_symbol, s.source,
               (SELECT o.vendor FROM core.security_vendor_xref o
                 WHERE o.security_id = l.security_id AND o.is_feed
                   AND o.valid_to IS NULL AND o.vendor <> l.vendor) AS fed_by
          FROM core.security_vendor_xref l
          JOIN core.security s ON s.security_id = l.security_id
         WHERE l.vendor = %s AND l.valid_to IS NULL{extra}
         ORDER BY l.security_id
        """,
        [vendor] + params,
    )
    for r in rows:
        sid = int(r["security_id"])
        if r["is_feed"]:
            result.changes.append(FeedChange(sid, r["primary_symbol"], "already"))
            continue
        previous = r["fed_by"] or (
            repo.FMP_SOURCE if r["source"] == repo.FMP_SOURCE else None
        )
        # One feeding link per security: clear the other before setting this one.
        db.execute(
            "UPDATE core.security_vendor_xref SET is_feed = false "
            "WHERE security_id = %s AND is_feed AND valid_to IS NULL",
            (sid,),
        )
        db.execute(
            "UPDATE core.security_vendor_xref SET is_feed = true "
            "WHERE vendor = %s AND vendor_key = %s AND valid_to IS NULL",
            (vendor, r["vendor_key"]),
        )
        result.changes.append(FeedChange(sid, r["primary_symbol"], "fed", previous))
    if security_ids is not None:
        seen = {int(r["security_id"]) for r in rows}
        for sid, symbol in _symbols(db, [i for i in security_ids if i not in seen]):
            result.changes.append(FeedChange(sid, symbol, "no_link"))


def _feed_to_fmp(db, result, funds, security_ids) -> None:
    extra, params = _scope_sql(funds, security_ids)
    rows = db.fetchall(
        f"""
        SELECT s.security_id, s.primary_symbol, s.source, l.vendor
          FROM core.security_vendor_xref l
          JOIN core.security s ON s.security_id = l.security_id
         WHERE l.is_feed AND l.valid_to IS NULL{extra}
         ORDER BY s.security_id
        """,
        params,
    )
    for r in rows:
        sid = int(r["security_id"])
        if r["source"] != repo.FMP_SOURCE:
            result.changes.append(
                FeedChange(sid, r["primary_symbol"], "not_fmp_minted", r["vendor"])
            )
            continue
        db.execute(
            "UPDATE core.security_vendor_xref SET is_feed = false "
            "WHERE security_id = %s AND is_feed AND valid_to IS NULL",
            (sid,),
        )
        result.changes.append(
            FeedChange(sid, r["primary_symbol"], "restored", r["vendor"])
        )
    if security_ids is not None:
        seen = {int(r["security_id"]) for r in rows}
        for sid, symbol in _symbols(db, [i for i in security_ids if i not in seen]):
            fed = repo.is_fed_by(db, sid, repo.FMP_SOURCE)
            outcome = "already" if fed else "not_fmp_minted"
            result.changes.append(FeedChange(sid, symbol, outcome))


def _symbols(db, ids: Sequence[int]) -> list[tuple[int, str]]:
    if not ids:
        return []
    return [
        (int(r["security_id"]), r["primary_symbol"])
        for r in db.fetchall(
            "SELECT security_id, primary_symbol FROM core.security "
            "WHERE security_id = ANY(%s) ORDER BY security_id",
            (list(ids),),
        )
    ]


def _apply(db, result, funds, security_ids) -> None:
    """Neither filter is ``--all-linked``."""
    if result.vendor == repo.FMP_SOURCE:
        _feed_to_fmp(db, result, funds, security_ids)
    else:
        _feed_to(db, result, result.vendor, funds, security_ids)


def feed(
    db: Database,
    vendor: str,
    *,
    all_linked: bool = False,
    funds: bool = False,
    security_ids: Optional[Sequence[int]] = None,
    by: str,
    dry_run: bool = False,
) -> FeedResult:
    """Make ``vendor`` feed the securities in scope (one run), or with ``dry_run``
    apply it and roll back. The scope is one of ``all_linked``, ``funds`` and
    ``security_ids``."""
    if vendor not in VENDORS:
        raise FeedError(f"unknown vendor {vendor!r}: expected one of {VENDORS}")
    if sum([all_linked, funds, security_ids is not None]) != 1:
        raise FeedError("give exactly one of --all-linked, --funds, --security-id")
    result = FeedResult(vendor=vendor, dry_run=dry_run)
    if dry_run:
        with db.conn.transaction(force_rollback=True):
            _apply(db, result, funds, security_ids)
        return result
    scope = "all_linked" if all_linked else ("funds" if funds else "security_ids")
    run = RunLog(
        db,
        source=vendor,
        endpoint=ENDPOINT,
        params={"scope": scope, "by": by},
    )
    with run:
        result.run_id = run.run_id
        _apply(db, result, funds, security_ids)
        run.rows_inserted = len(result.changed())
        run.params.update(
            counts=dict(sorted(result.counts().items())),
            changed=result.changed(),
        )
        db.commit()
    logger.info(
        "vendor feed %s (%s, by %s): %s",
        vendor,
        scope,
        by,
        dict(result.counts()),
    )
    return result
