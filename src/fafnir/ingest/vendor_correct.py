"""
`fafnir vendor correct`: replace the FMP bars that disagree with Sharadar's, audited
(plan SA-0601, #75; #66 and #69 folded in).

Sharadar is the more reliable vendor (plan decision of 2026-10-20). For every FMP
security linked to Sharadar, this reads the vendor's bars from the current bulk
files, recovers them raw exactly as `ingest sharadar-prices` does, and compares
them with the FMP bars on the dates both hold, from the link's ``valid_from``:

- **A bar differs** when its close is off by more than max($0.01, 1 bp), or its
  open, high or low by more than that or the recovery's own rounding (0.0005 x the
  split factor, #49).
- **A differing bar is replaced** by Sharadar's through the correction path (ADR 0015
  §3): the FMP row is kept in ``ops.vendor_correction`` first, and `vendor corrections
  revert` puts it back. A bar an operator edited is never corrected, and a recovered
  bar that fails validation is not offered.
- **Volume** is FMP's by default (``--volume keep``): whether the two vendors count
  volume the same way (consolidated against primary-exchange) is a convention the
  dry run measures, not an error. ``--volume sharadar`` takes the vendor's.
- **A security's open ``vendor_history_disagrees`` flag** is resolved once every
  sampled date it names agrees, corrected or not.

**``--split-bearing``** covers what that leaves out: the FMP-minted securities linked
to Sharadar, fed or not, that hold Sharadar's splits, which are the splits FMP never
listed. FMP's history before such a split is often already divided by it: CELG's
2003-2013 bars are exactly half Sharadar's as-traded closes, against its 2014 2:1
(2026-10-23). The adjusted series then divides them twice. A bar is replaced only
when it is **explained**: FMP's close times the product of the Sharadar splits after
it, :func:`sharadar_map.split_product_after`, meets Sharadar's recovered close within
0.5% (or a cent, or FMP's 6-dp rounding at that factor). A stretch between two of
those splits is corrected only when at least 90% of its compared bars are explained,
so a chance match never patches a stretch that differs for another reason (an ADR's
ratio, another instrument). Sharadar is not always the right side of those, and they
are counted and left, split into the bars that already agree with the vendor
(``left_agree``: FMP was as traded, the split on file is right) and the ones that do
not (``left_differ``).

An explained stretch is corrected whole, so it stays one series:

- **every compared bar** is replaced by Sharadar's, the few unexplained ones too
  (``bar_replaced_unexplained``). In a stretch FMP is known to have pre-adjusted, an
  FMP bar left as it is would sit ``Π`` times off its neighbours;
- **FMP's bars on dates the vendor does not hold** (before the link, in a gap, or a
  vendor bar that does not recover) are rescaled by ``Π``, the volume by ``1/Π``.
  This uses :func:`price_edits.plan_rescale_bars`, the operator rescale's own
  checks, as one edit per stretch, revertible with `fafnir override revoke`
  (``uncovered_rescaled``). If that edit would be refused (a bar the loader would
  quarantine at the new scale), the whole stretch is left (``stretch_refused``),
  because correcting part of it would leave a break.

A corrected bar takes the vendor's recovered volume, because FMP's volume is
pre-adjusted with its prices.

``--dry-run`` writes nothing and is the comparison report (#69's): bars compared and
bars that would be replaced, by asset type, exchange and decade, and the volume
ratio (FMP over Sharadar) on the bars whose prices agree. Counts only, safe to post.

One ``ops.ingestion_run`` (``sharadar`` / ``correct``), a commit per security: the
files are grouped by ticker on disk first, as ``ingest sharadar-prices`` reads them,
and the progress is written to the open run as it goes. The factors of the
corrected securities that hold actions are recomputed.
"""

from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median, quantiles
from typing import Mapping, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import adjustments, price_edits
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.ingest import sharadar_map
from fafnir.ingest import sharadar_prices as sp
from fafnir.ingest.daily_price import _is_nav_priced, _validate_bar
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.vendor_link import HISTORY
from fafnir.logging_config import get_logger
from fafnir.sources import sharadar_files as sf

logger = get_logger("ingest.vendor_correct")

VENDOR = "sharadar"
ENDPOINT = "correct"
CREATED_BY = "fafnir vendor correct"
#: The volume ratios kept for the percentiles: a seeded sample of this many. The
#: counts within 1% and 5% are exact.
VOLUME_SAMPLE = 100_000
ABS_TOLERANCE = Decimal("0.01")
REL_TOLERANCE = Decimal("0.0001")


#: --split-bearing: FMP's close times the product of the Sharadar splits after it
#: must meet Sharadar's recovered close within this share of it...
SPLIT_TOLERANCE = Decimal("0.005")
#: ...and a stretch between two of those splits is corrected only when this share of
#: its compared bars is explained.
SEGMENT_SHARE = Decimal("0.9")
#: A stored price's unit (NUMERIC(20, 6)): FMP's pre-adjusted close is rounded to it.
PRICE_QUANTUM = Decimal("0.000001")


def _tolerance(price: Decimal) -> Decimal:
    return max(ABS_TOLERANCE, REL_TOLERANCE * abs(price))


def explained(fmp: dict, sharadar: dict, product: Decimal) -> bool:
    """Whether an FMP bar is Sharadar's divided by ``product``, the product of the
    Sharadar splits after it: FMP's history pre-adjusted for splits FMP never
    listed."""
    want = sharadar["close"]
    allowed = max(_tolerance(want), SPLIT_TOLERANCE * want, PRICE_QUANTUM * product)
    return abs(fmp["close"] * product - want) <= allowed


def differs(fmp: dict, sharadar: dict, factor: Decimal) -> bool:
    """Whether a stored FMP bar and a recovered Sharadar bar disagree on price."""
    if abs(fmp["close"] - sharadar["close"]) > _tolerance(sharadar["close"]):
        return True
    rounding = sp.ROUNDING * factor
    for f in ("open", "high", "low"):
        allowed = max(_tolerance(sharadar[f]), rounding)
        if abs(fmp[f] - sharadar[f]) > allowed:
            return True
    return False


@dataclass
class Bucket:
    compared: int = 0
    differ: int = 0


@dataclass
class CorrectResult:
    dry_run: bool
    run_id: Optional[int] = None
    securities: int = 0
    read: int = 0
    corrected: int = 0
    counts: Counter = field(default_factory=Counter)
    by: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(Bucket)))
    volume_bars: int = 0
    volume_within: Counter = field(default_factory=Counter)  # "1pct", "5pct"
    volume_sample: list = field(default_factory=list)
    _rng: random.Random = field(default_factory=lambda: random.Random(0), repr=False)
    corrected_security_ids: set[int] = field(default_factory=set)
    flags_resolved: int = 0
    adjusted: int = 0
    #: --split-bearing: per symbol, ``explained`` (bars replaced), ``rescaled`` and
    #: ``left_differ``: what the dry run lists for review.
    split_bearing_by: dict = field(default_factory=lambda: defaultdict(Counter))

    def add_volume_ratio(self, ratio: float) -> None:
        """Count one FMP/Sharadar volume ratio; keep a reservoir sample of them."""
        self.volume_bars += 1
        if abs(ratio - 1) <= 0.01:
            self.volume_within["1pct"] += 1
        if abs(ratio - 1) <= 0.05:
            self.volume_within["5pct"] += 1
        if len(self.volume_sample) < VOLUME_SAMPLE:
            self.volume_sample.append(ratio)
        else:
            i = self._rng.randrange(self.volume_bars)
            if i < VOLUME_SAMPLE:
                self.volume_sample[i] = ratio

    def volume_summary(self) -> Optional[dict]:
        """FMP's volume over Sharadar's on the bars whose prices agree: the median
        and deciles (from the sample), and the share within 1% and 5% (exact)."""
        n = self.volume_bars
        if len(self.volume_sample) < 2:
            return None
        deciles = quantiles(self.volume_sample, n=10, method="inclusive")
        return {
            "bars": n,
            "median": round(median(self.volume_sample), 4),
            "p10": round(deciles[0], 4),
            "p90": round(deciles[-1], 4),
            "within_1pct": round(self.volume_within["1pct"] / n, 4),
            "within_5pct": round(self.volume_within["5pct"] / n, 4),
        }


def _compare_run(
    db: Database,
    t: sp._Target,
    rows: list[dict],
    *,
    as_of: date,
    volume: str,
    run: Optional[RunLog],
    result: CorrectResult,
    agreed: dict,
    sampled: Mapping[int, frozenset],
) -> None:
    """Compare one run of a security's rows with its FMP bars; correct the ones
    that differ. Records in ``agreed`` the dates that agree or were corrected,
    of those in ``sampled`` only."""
    samples = sampled.get(t.security_id, frozenset())
    rows = [r for r in rows if t.valid_from <= r["date"] <= as_of]
    stored = repo.stored_bars_on(db, t.security_id, [r["date"] for r in rows])
    profile = repo.security_price_profile(db, t.security_id) or {}
    nav_only = _is_nav_priced(profile.get("asset_type"), profile.get("is_fund"))
    asset = profile.get("asset_type") or "?"
    venue = profile.get("exchange_code") or "?"
    fixes = []
    for r in rows:
        old = stored.get(r["date"])
        if old is None or old["source"] != repo.FMP_SOURCE:
            continue
        factor, _ = sp.recovery_factor(r, t.splits)
        if factor is None:
            result.counts["no_vendor_price"] += 1
            continue
        bar, _ = sp.recovered_bar(r, t.splits, result.counts)
        new, reason = _validate_bar(bar, nav_only=nav_only)
        if reason:
            result.counts[f"vendor_invalid:{reason}"] += 1
            continue
        decade = f"{r['date'].year // 10 * 10}s"
        cells = (result.by["asset_type"][asset], result.by["exchange"][venue],
                 result.by["decade"][decade])  # fmt: skip
        for b in cells:
            b.compared += 1
        if not differs(old, new, factor):
            if r["date"] in samples:
                agreed[t.security_id].add(r["date"])
            if old["volume"] and new["volume"]:
                result.add_volume_ratio(old["volume"] / new["volume"])
            continue
        for b in cells:
            b.differ += 1
        if volume == "keep":
            new["volume"] = old["volume"]
        new["security_id"] = t.security_id
        fixes.append(new)
    result.counts["differ"] += len(fixes)
    if run is None or not fixes:
        return
    written = repo.upsert_daily_prices(
        db,
        fixes,
        ingestion_run_id=run.run_id,
        source=VENDOR,
        correct=True,
        reason="reconcile_divergence",
    )
    result.counts["skipped_suppressed"] += written.skipped_suppressed
    result.corrected += written.corrected
    if written.corrected:
        result.corrected_security_ids.add(t.security_id)
        corrected = {f["trade_date"] for f in fixes} & samples
        agreed[t.security_id] |= corrected
    db.commit()


def _compare_split_bearing(
    db: Database,
    t: sp._Target,
    rows: list[dict],
    splits: Sequence[tuple[date, Decimal]],
    *,
    as_of: date,
    run: Optional[RunLog],
    result: CorrectResult,
    agreed: dict,
    sampled: Mapping[int, frozenset],
) -> None:
    """``--split-bearing`` for one security: one stretch between the stored Sharadar
    ``splits`` at a time, correct whole the stretches where FMP's bars are Sharadar's
    divided by the product of those splits after them."""
    samples = sampled.get(t.security_id, frozenset())
    rows = [r for r in rows if t.valid_from <= r["date"] <= as_of]
    stored = repo.stored_bars_on(db, t.security_id, [r["date"] for r in rows])
    profile = repo.security_price_profile(db, t.security_id) or {}
    nav_only = _is_nav_priced(profile.get("asset_type"), profile.get("is_fund"))
    ex_dates = [d for d, _ in splits]
    tally = result.split_bearing_by[t.symbol]
    stretches: dict[date, list] = defaultdict(list)  # keyed by the next split
    for r in rows:
        old = stored.get(r["date"])
        if old is None or old["source"] != repo.FMP_SOURCE:
            continue
        product = sharadar_map.split_product_after(splits, r["date"])
        if product == 1:
            result.counts["split_bearing:no_split_after"] += 1
            continue
        factor, _ = sp.recovery_factor(r, t.splits)
        if factor is None:
            result.counts["no_vendor_price"] += 1
            continue
        bar, _ = sp.recovered_bar(r, t.splits, result.counts)
        new, reason = _validate_bar(bar, nav_only=nav_only)
        if reason:
            result.counts[f"vendor_invalid:{reason}"] += 1
            continue
        nxt = min(d for d in ex_dates if d > r["date"])
        stretches[nxt].append((old, new, factor, explained(old, new, product)))
    fixes, plans = [], []
    for nxt, items in stretches.items():
        ok = sum(1 for *_, e in items if e)
        result.counts["split_bearing:compared"] += len(items)
        if ok < SEGMENT_SHARE * len(items):
            agree = sum(1 for old, new, f, _ in items if not differs(old, new, f))
            result.counts["split_bearing:stretch_left"] += 1
            if agree:
                result.counts["split_bearing:left_agree"] += agree
            if agree < len(items):
                result.counts["split_bearing:left_differ"] += len(items) - agree
                tally["left_differ"] += len(items) - agree
            continue
        # Explained: the FMP bars the vendor does not hold join the edit too.
        product = sharadar_map.split_product_after(splits, items[0][0]["trade_date"])
        before = max((d for d in ex_dates if d < nxt), default=date.min)
        covered = {old["trade_date"] for old, *_ in items}
        uncovered = repo.source_dates(db, t.security_id, repo.FMP_SOURCE, before, nxt)
        uncovered -= covered
        plan = None
        if uncovered:
            bars = [
                b
                for b in repo.stored_bars(
                    db, t.security_id, min(uncovered), max(uncovered)
                )
                if b["trade_date"] in uncovered
            ]
            plan = price_edits.plan_rescale_bars(
                db, t.security_id, bars, product, 1 / product
            )
            if plan.refusals:
                result.counts["split_bearing:stretch_refused"] += 1
                result.counts["split_bearing:bars_refused"] += len(items)
                logger.warning(
                    "%s: stretch before %s left, its rescale refused: %s",
                    t.symbol,
                    nxt,
                    "; ".join(plan.refusals),
                )
                continue
            plans.append((nxt, product, plan))
            result.counts["split_bearing:uncovered_rescaled"] += len(plan.changes)
            tally["rescaled"] += len(plan.changes)
        result.counts["split_bearing:stretch_explained"] += 1
        if ok < len(items):
            result.counts["split_bearing:bar_replaced_unexplained"] += len(items) - ok
        for old, new, _, _ in items:
            new["security_id"] = t.security_id
            fixes.append(new)
        tally["explained"] += len(items)
    result.counts["differ"] += len(fixes)
    if run is None or not (fixes or plans):
        return
    for nxt, product, plan in plans:
        price_edits.apply_plan(
            db,
            plan,
            note=(
                f"vendor correct --split-bearing, run {run.run_id}: FMP's bars before "
                f"the Sharadar split of {nxt} are pre-adjusted by x{1 / product:g}; "
                "the vendor holds no bar on these dates, so they are rescaled by the "
                "factor their neighbours were corrected by."
            ),
            created_by=CREATED_BY,
        )
        result.corrected_security_ids.add(t.security_id)
    if fixes:
        written = repo.upsert_daily_prices(
            db,
            fixes,
            ingestion_run_id=run.run_id,
            source=VENDOR,
            correct=True,
            reason="reconcile_divergence",
        )
        result.counts["skipped_suppressed"] += written.skipped_suppressed
        result.corrected += written.corrected
        if written.corrected:
            result.corrected_security_ids.add(t.security_id)
            agreed[t.security_id] |= {f["trade_date"] for f in fixes} & samples
    db.commit()


def _history_flags(db: Database, targets: Sequence[sp._Target]) -> list[dict]:
    """The open ``vendor_history_disagrees`` flags of these securities."""
    ids = tuple(t.security_id for t in targets)
    if not ids:
        return []
    return repo.list_dq_flags(
        db, repo.DqFilter(checks=(HISTORY,), security_ids=ids), limit=1_000_000
    )


def _samples(flag: dict) -> set[date]:
    """The dates a ``vendor_history_disagrees`` flag sampled."""
    return {
        date.fromisoformat(s["date"])
        for s in (flag.get("detail") or {}).get("samples", ())
    }


def _sampled_dates(db: Database, targets: Sequence[sp._Target]) -> dict[int, frozenset]:
    """Every date an open ``vendor_history_disagrees`` flag sampled, by security:
    the only agreeing dates :func:`_resolve_history_flags` ever reads.

    Recording every agreeing date instead held one per compared bar, of every
    linked security, until the end of the run: the first full dry run on the host
    (2026-10-21) reached 1.6 GB and pushed a 3.8 GB machine into swap within 50
    minutes, and was stopped.
    """
    out: dict[int, set] = defaultdict(set)
    for f in _history_flags(db, targets):
        out[f["security_id"]] |= _samples(f)
    return {sid: frozenset(dates) for sid, dates in out.items()}


def _resolve_history_flags(
    db: Database, targets: Sequence[sp._Target], agreed: dict, run: RunLog
) -> int:
    """Resolve each open ``vendor_history_disagrees`` flag whose sampled dates now
    all agree."""
    done = []
    for f in _history_flags(db, targets):
        dates = _samples(f)
        if dates and dates <= agreed.get(f["security_id"], set()):
            done.append(f["dq_flag_id"])
    if done:
        repo.resolve_dq_flags(
            db,
            repo.DqFilter(flag_ids=tuple(done)),
            note=f"vendor correct run {run.run_id}: every sampled date now agrees",
            resolved_by=CREATED_BY,
        )
    return len(done)


def correct(
    db: Database,
    bulk_dir: str | Path,
    *,
    security_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
    volume: Optional[str] = None,
    adjust: bool = True,
    as_of: Optional[date] = None,
    split_bearing: bool = False,
) -> CorrectResult:
    """Compare and, unless ``dry_run``, correct (one run). ``volume`` defaults to
    ``keep``; ``split_bearing`` takes the vendor's, and refuses ``keep``."""
    if split_bearing and volume == "keep":
        raise ValueError(
            "--split-bearing takes the vendor's volume: FMP's is pre-adjusted with its "
            "prices"
        )
    volume = volume or ("sharadar" if split_bearing else "keep")
    if volume not in ("keep", "sharadar"):
        raise ValueError(f"volume is keep or sharadar, not {volume!r}")
    as_of = as_of or date.today()
    result = CorrectResult(dry_run=dry_run)
    if split_bearing:
        fmp = [
            t
            for t in repo.vendor_action_targets(db, VENDOR, security_ids)
            if t["source"] == repo.FMP_SOURCE
        ]
        splits = repo.stored_split_values(db, [t["security_id"] for t in fmp], VENDOR)
        targets = [t for t in fmp if t["security_id"] in splits]
    else:
        splits = {}
        targets = [
            t
            for t in repo.vendor_action_targets(db, VENDOR, security_ids)
            if not t["is_feed"] and t["source"] == repo.FMP_SOURCE
        ]
    result.securities = len(targets)
    by_file, files, keys = sp.prepare(db, bulk_dir, targets, result.counts)
    sampled = _sampled_dates(db, list(keys.values()))
    agreed: dict[int, set] = defaultdict(set)

    def scan(run: Optional[RunLog]) -> None:
        last = time.monotonic()
        for table, file_id in files.items():
            wanted = {ticker: sf.ALL_DATES for ticker in by_file[table]}
            groups = ledger.select_by_ticker(db, file_id, bulk_dir, wanted)
            for done, (ticker, rows) in enumerate(groups, start=1):
                result.read += len(rows)
                t = by_file[table][ticker]
                if split_bearing:
                    _compare_split_bearing(
                        db,
                        t,
                        rows,
                        splits[t.security_id],
                        as_of=as_of,
                        run=run,
                        result=result,
                        agreed=agreed,
                        sampled=sampled,
                    )
                else:
                    _compare_run(
                        db,
                        t,
                        rows,
                        as_of=as_of,
                        volume=volume,
                        run=run,
                        result=result,
                        agreed=agreed,
                        sampled=sampled,
                    )
                if run is not None:
                    run.rows_inserted = result.corrected
                last = sp.report_progress(
                    run,
                    last,
                    table=table,
                    ticker=ticker,
                    tickers_done=done,
                    tickers=len(wanted),
                    read=result.read,
                    corrected=result.corrected,
                )

    if dry_run:
        scan(None)
        return result
    run = RunLog(
        db,
        source=VENDOR,
        endpoint=ENDPOINT,
        params={"volume": volume, "files": files, "split_bearing": split_bearing},
    )
    with run:
        result.run_id = run.run_id
        scan(run)
        result.flags_resolved = _resolve_history_flags(
            db, list(keys.values()), agreed, run
        )
        run.rows_inserted = result.corrected
        run.params.pop("progress", None)  # the counts below are the final word
        run.params.update(
            securities=result.securities,
            read=result.read,
            corrected=result.corrected,
            counts=dict(sorted(result.counts.items())),
            volume=result.volume_summary(),
        )
        db.commit()

    if adjust and result.corrected_security_ids:
        with_actions = {
            a["security_id"]
            for a in repo.action_dates(db, sorted(result.corrected_security_ids))
        }
        ids = sorted(result.corrected_security_ids & with_actions)
        if ids:
            result.adjusted = adjustments.adjust_all(db, security_ids=ids)["securities"]
    return result
