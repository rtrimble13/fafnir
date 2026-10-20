"""
`fafnir vendor overlap`: find the history a reused ticker glued onto an FMP security,
before a dead issuer is minted from Sharadar (plan SA-0503, #68).

FMP files every issuer of a ticker on the row that holds it today: BID holds
Sotheby's 2003-2019 as well as the current issuer. Minting Sotheby's from Sharadar
(#71) would then put the same history on two rows. So, before anything is minted:

1. **Dead issuers.** For each Sharadar security #71 would mint (ADR 0016 §3,
   :func:`mintable`), look for an FMP security, linked or not, that has held one of
   its tickers (its own, a ``related_tickers`` entry, the other side of a ticker
   change) and holds bars inside the dead issuer's price span. Each is flagged
   ``vendor_history_overlap`` (``kind = dead_issuer``) on the FMP security, with the
   overlapping range, its bar count and five sampled closes against Sharadar's
   ``closeunadj``. #71 does not mint a permaticker with an open flag.
   - The samples tell the two cases apart. Agreeing samples mean the FMP row holds
     the dead issuer's bars. If that row is the dead issuer itself (unlinked,
     delisted), it wants a link (`vendor link accept`), not a mint. Otherwise its
     bars for that era move to the minted row (`security split-history`).
   - Disagreeing samples mean the FMP row's bars are another issuer's. The dead
     issuer can be minted on purpose (#71's `--allow-overlap`).
2. **Earlier history.** The inverse: a linked security whose FMP bars start more
   than 30 days before its permaticker's first price, where Sharadar's history does
   not simply start at its own 1998 floor (``kind = earlier_history``). An earlier
   issuer was glued on.

A full run is the whole picture, so it also resolves the open flags whose overlap
is gone (the bars moved, the issuer was minted or linked). Writes nothing with
``dry_run``. The samples are vendor data: they stay in the flags, never posted.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_map
from fafnir.ingest import vendor_link as vl
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger

logger = get_logger("ingest.vendor_overlap")

CHECK = "vendor_history_overlap"
CREATED_BY = "fafnir vendor overlap"
ENDPOINT = "overlap"
#: ADR 0016 §2: no vendor records a delisting before this.
FLOOR = date(1998, 1, 1)
#: ADR 0016 §3: the venues a dead issuer is minted from (Sharadar's NYSEMKT and
#: NYSEARCA map to AMEX).
MINT_VENUES = frozenset({"NYSE", "NASDAQ", "AMEX", "BATS"})
#: FMP history this much older than the permaticker's is an earlier issuer's.
EARLIER_MARGIN = timedelta(days=30)
#: SEP and SFP start in December 1997: a permaticker whose prices start within
#: this of the floor was listed before it, so an older FMP history is expected.
VENDOR_START = FLOOR + timedelta(days=31)


def mintable(c: dict) -> bool:
    """ADR 0016 §3, less the two rules about fafnir's own rows (no open link, not
    flagged here): what #71 would mint."""
    if c["vendor_table"] not in vl.PRICED_TABLES or not c.get("is_delisted"):
        return False
    if c.get("last_price_date") is None or c["last_price_date"] < FLOOR:
        return False
    kind = sharadar_map.map_category(c.get("category"))
    if kind is None or kind.asset_type == "fund":
        return False
    return sharadar_map.map_exchange(c.get("exchange")) in MINT_VENUES


@dataclass
class Overlap:
    kind: str  # dead_issuer | earlier_history
    security_id: int
    symbol: str
    vendor_key: str
    vendor_ticker: str
    start: date
    end: date
    bars: int = 0
    agreed: Optional[int] = None
    of: Optional[int] = None
    samples: list = field(default_factory=list)
    linked_key: Optional[str] = None  # the FMP security's own link

    @property
    def record_key(self) -> dict:
        return {"vendor": vl.VENDOR, "vendor_key": self.vendor_key, "kind": self.kind}

    def detail(self) -> dict:
        out = {
            "kind": self.kind,
            "vendor_key": self.vendor_key,
            "vendor_ticker": self.vendor_ticker,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "bars": self.bars,
            "linked_key": self.linked_key,
        }
        if self.kind == "dead_issuer":
            out.update(
                agreed=self.agreed,
                of=self.of,
                samples=vl._sample_json(self.samples),
            )
        return out


@dataclass
class OverlapResult:
    dry_run: bool
    run_id: Optional[int] = None
    price_source: Optional[str] = None
    dead_issuers: int = 0  # mintable permatickers evaluated
    overlaps: list[Overlap] = field(default_factory=list)
    resolved: int = 0

    def counts(self) -> Counter:
        """``(kind, agreement)``: ``agree`` (four samples or more), ``disagree``,
        or ``-`` for earlier history, which has no samples."""
        out: Counter = Counter()
        for o in self.overlaps:
            if o.kind != "dead_issuer":
                out[(o.kind, "-")] += 1
            else:
                out[
                    (
                        o.kind,
                        "agree" if (o.agreed or 0) >= vl.MIN_AGREED else "disagree",
                    )
                ] += 1
        return out

    def flagged_keys(self) -> int:
        """Permatickers #71 will not mint unasked."""
        return len({o.vendor_key for o in self.overlaps if o.kind == "dead_issuer"})


def find(db: Database, source_for) -> tuple[list[Overlap], int, Optional[str]]:
    """Every overlap, the number of dead issuers evaluated, and the price source
    used. Writes nothing."""
    candidates = repo.vendor_link_candidates(db, vl.VENDOR, vl.PRICED_TABLES)
    by_key = {c["vendor_key"]: c for c in candidates}
    dead = [c for c in candidates if mintable(c) and not c["taken"]]
    securities = repo.overlap_scope(db, vl.VENDOR)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for s in securities:
        for t in vl._keys(s["tickers"]):
            by_ticker[t].append(s)

    out: list[Overlap] = []
    for c in dead:
        seen = set()
        for alias in sorted(vl._keys(c["aliases"])):
            for s in by_ticker.get(alias, ()):
                span = vl.overlap(s, c)
                if span is None or s["security_id"] in seen:
                    continue
                seen.add(s["security_id"])
                out.append(
                    Overlap(
                        "dead_issuer",
                        s["security_id"],
                        s["primary_symbol"],
                        c["vendor_key"],
                        c["ticker"],
                        *span,
                        linked_key=s["linked_key"],
                    )
                )
    for s in securities:
        c = by_key.get(s["linked_key"]) if s["linked_key"] else None
        if c is None or c["first_price_date"] is None or s["first_trade_date"] is None:
            continue
        if c["first_price_date"] <= VENDOR_START:
            continue
        if s["first_trade_date"] < c["first_price_date"] - EARLIER_MARGIN:
            out.append(
                Overlap(
                    "earlier_history",
                    s["security_id"],
                    s["primary_symbol"],
                    c["vendor_key"],
                    c["ticker"],
                    s["first_trade_date"],
                    c["first_price_date"] - timedelta(days=1),
                    linked_key=s["linked_key"],
                )
            )

    counts = repo.count_bars_between(
        db, [(i, o.security_id, o.start, o.end) for i, o in enumerate(out)]
    )
    for i, o in enumerate(out):
        o.bars = counts.get(i, 0)
    out = [o for o in out if o.bars]
    source_name = _sample(
        db, [o for o in out if o.kind == "dead_issuer"], by_key, source_for
    )
    return out, len(dead), source_name


def _sample(db: Database, overlaps: Sequence[Overlap], by_key: dict, source_for):
    """Five sampled closes per dead-issuer overlap against Sharadar's, as `vendor
    link` samples them."""
    probes, owner = [], {}
    for n, o in enumerate(overlaps):
        for lo, hi in vl.sample_windows(o.start, o.end):
            owner[len(probes)] = n
            probes.append((len(probes), o.security_id, lo, hi))
    bars = repo.first_bars_on_or_after(db, probes)
    ours: dict[int, list] = defaultdict(list)
    wanted: vl.Wanted = defaultdict(set)
    for probe, (d, close) in sorted(bars.items()):
        n = owner[probe]
        ours[n].append((d, close))
        c = by_key[overlaps[n].vendor_key]
        wanted[(c["vendor_table"], c["ticker"])].add(d)
    if not wanted:
        for o in overlaps:
            o.agreed, o.of = 0, 0
        return None
    source = source_for(len(wanted))
    theirs = source.closes(dict(wanted))
    for n, o in enumerate(overlaps):
        c = by_key[o.vendor_key]
        rows = [
            {
                "date": d,
                "fmp": close,
                "sharadar": theirs.get((c["vendor_table"], c["ticker"], d)),
            }
            for d, close in ours.get(n, [])
        ]
        o.samples = rows
        o.of = len(rows)
        o.agreed = sum(
            1
            for r in rows
            if r["sharadar"] is not None and vl.agrees(r["fmp"], r["sharadar"])
        )
    return source.name


def _write(db: Database, overlaps: Sequence[Overlap], run: RunLog) -> int:
    """Flag every overlap once, refresh an open flag's evidence, and resolve the
    open flags whose overlap is gone. Returns how many were resolved."""
    open_flags = repo.list_dq_flags(db, repo.DqFilter(checks=(CHECK,)), limit=1_000_000)
    by_key = {
        (f["security_id"], tuple(sorted((f.get("record_key") or {}).items()))): f
        for f in open_flags
    }
    current = set()
    for o in overlaps:
        key = (o.security_id, tuple(sorted(o.record_key.items())))
        current.add(key)
        f = by_key.get(key)
        if f is not None:
            repo.refresh_open_flag_detail(db, f["dq_flag_id"], o.detail(), run.run_id)
        else:
            repo.add_dq_flag_once(
                db,
                check_name=CHECK,
                severity="warn",
                security_id=o.security_id,
                table_name="core.daily_price",
                record_key=o.record_key,
                detail=o.detail(),
                ingestion_run_id=run.run_id,
            )
    gone = [f["dq_flag_id"] for k, f in by_key.items() if k not in current]
    if gone:
        repo.resolve_dq_flags(
            db,
            repo.DqFilter(flag_ids=tuple(gone)),
            note=f"vendor overlap run {run.run_id}: the overlap is gone",
            resolved_by=CREATED_BY,
        )
    return len(gone)


def scan(db: Database, source_for, *, dry_run: bool = False) -> OverlapResult:
    """Find every overlap and, unless ``dry_run``, flag it (one run)."""
    result = OverlapResult(dry_run=dry_run)
    result.overlaps, result.dead_issuers, result.price_source = find(db, source_for)
    if dry_run:
        return result
    run = RunLog(db, source=vl.VENDOR, endpoint=ENDPOINT, params={})
    with run:
        result.run_id = run.run_id
        result.resolved = _write(db, result.overlaps, run)
        run.rows_inserted = len(result.overlaps)
        run.params.update(
            dead_issuers=result.dead_issuers,
            price_source=result.price_source,
            overlaps={f"{k}:{a}": n for (k, a), n in result.counts().items()},
            resolved=result.resolved,
        )
        db.commit()
    return result
