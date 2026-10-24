"""
`fafnir ingest sharadar-actions`: Sharadar's splits and dividends into
``core.corporate_action`` (plan SA-0602, #73).

`fafnir adjust` needs the splits and dividends of the issuers Sharadar minted (#71),
and FMP's history of a live security can lack some. The ACTIONS file holds both,
filed under each security's current ticker; the other lifecycle events are already
in ``core.vendor_security_event`` (#58). Two modes, either or both:

- ``--backfill``: every split and dividend of the securities Sharadar **feeds**: the
  minted dead issuers, and from the cutover the securities `vendor feed` gave it.
- ``--gap-fill``: **fill-only** for the FMP securities linked to Sharadar, up to the
  security's last bar.

A security another vendor minted takes only the actions dated **after** its link's
``valid_from``, fed or not: the link starts where the vendor's own prices do, and an
action on or before that date adjusts only the other vendor's bars, which the
vendor's prices never confirmed (2026-10-23: COPR's 1:20 of 2025-12-15, when
Sharadar prices it from 2026-07-02). Those are counted ``outside_span``.
:func:`prune_outside_span` removes the ones written before this rule held.

In both, an action is never added within :data:`NEAR` of an action of the same type
another source holds: a date the two vendors disagree on would otherwise store one
dividend twice, and a security Sharadar took over from FMP (#98) holds FMP's whole
history. Those are counted ``near_existing``; correcting them is #75's. The nightly
(`ingest sharadar`) runs both modes over the ACTIONS file it re-downloads.

Every row is written under ADR 0015's precedence as ``source = 'sharadar'``, never
as a correction: a key another party holds is left alone, an operator's above all,
and a key an operator deleted is set aside. The conversions are #56's:
:func:`sharadar_map.split_ratio` (new shares per old) and
:func:`sharadar_map.as_declared_dividend` (ACTIONS' split-adjusted value times the
split product after the ex-date). Two dividends on one ex-date (a regular and a
special) are summed; identical rows count once. **A future ex-date is never stored**
(ADR 0007, :func:`corporate_actions._within_horizon`). Spin-offs are not adjusted,
as before: they stay events.

One ``ops.ingestion_run`` (``sharadar`` / ``actions``), a commit per 1,000
securities, and a re-run writes nothing new. The factors of the securities it
changed are recomputed at the end, for those with bars; a minted security gets
its factors once its prices are loaded (#75).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import adjustments
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.ingest import sharadar_map
from fafnir.ingest.corporate_actions import _within_horizon
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.sharadar_master import SECURITY_TABLES
from fafnir.logging_config import get_logger

logger = get_logger("ingest.sharadar_actions")

VENDOR = "sharadar"
ENDPOINT = "actions"
ACTIONS = ("split", "dividend")
#: An action of the same type another source holds this close is the same event.
NEAR = timedelta(days=7)
BATCH = 1000


class ActionsError(RuntimeError):
    """The loader cannot run: no ACTIONS file, or no master to resolve it."""


@dataclass
class Planned:
    security_id: int
    action_type: str
    ex_date: date
    split_numerator: Optional[int] = None
    split_denominator: Optional[int] = None
    dividend_amount: Optional[object] = None


@dataclass
class ActionsResult:
    dry_run: bool
    run_id: Optional[int] = None
    securities: int = 0  # in scope
    planned: list[Planned] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    written: int = 0  # new or changed
    changed_security_ids: set[int] = field(default_factory=set)
    adjusted: int = 0
    awaiting_prices: int = 0  # changed, but no bars yet to adjust against


def key_actions(rows: Iterable[dict], keys: set[str], key_of: dict[str, str]) -> dict:
    """``vendor_key -> {"split": [(date, value)], "dividend": [(date, value)]}`` for
    the ACTIONS rows of ``keys``, resolved through the master's tickers."""
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["action"] not in ACTIONS:
            continue
        key = key_of.get(r["ticker"])
        if key in keys:
            out[key][r["action"]].append((r["date"], r.get("value")))
    return out


def planned_for(
    security_id: int, actions: dict[str, list], skipped: Counter
) -> list[Planned]:
    """One security's rows, converted. A non-positive or missing value is
    ``invalid`` and dropped."""
    out: list[Planned] = []
    splits: dict[date, object] = {}  # one per ex-date: the dividends' split product
    for d, v in sorted(actions.get("split", ()), key=lambda x: x[0]):
        if v is None or v <= 0:
            skipped["invalid"] += 1
            continue
        if d in splits:
            skipped["duplicate"] += 1
            continue
        splits[d] = v
        num, den = sharadar_map.split_ratio(v)
        out.append(Planned(security_id, "split", d, num, den))
    by_date: dict[date, set] = defaultdict(set)
    for d, v in actions.get("dividend", ()):
        if v is None or v <= 0:
            skipped["invalid"] += 1
            continue
        if v in by_date[d]:
            skipped["duplicate"] += 1
        by_date[d].add(v)
    for d in sorted(by_date):
        values = by_date[d]
        if len(values) > 1:
            skipped["summed"] += len(values) - 1
        amount = sharadar_map.as_declared_dividend(
            sum(values), sharadar_map.split_product_after(splits.items(), d)
        )
        out.append(Planned(security_id, "dividend", d, dividend_amount=amount))
    return out


def _in_scope(
    p: Planned, target: dict, foreign: dict, as_of: date, suppressed: set
) -> Optional[str]:
    """Why ``p`` is not written, or None."""
    if not _within_horizon(p.ex_date, as_of):
        return "future"
    if (p.security_id, p.action_type, p.ex_date) in suppressed:
        return "suppressed"
    if target["source"] != VENDOR and p.ex_date <= target["valid_from"]:
        return "outside_span"
    if not target["is_feed"]:
        last = target["last_trade_date"]
        if last is None or p.ex_date > last:
            return "outside_span"
    # Fed or not: a security Sharadar took over from FMP keeps FMP's history (#98).
    for d in foreign.get((p.security_id, p.action_type), ()):
        if abs(d - p.ex_date) <= NEAR:
            return "near_existing"
    return None


def load(
    db: Database,
    bulk_dir: str | Path,
    *,
    backfill: bool,
    gap_fill: bool,
    security_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
    adjust: bool = True,
    as_of: Optional[date] = None,
) -> ActionsResult:
    """Plan and, unless ``dry_run``, write and adjust (one run)."""
    as_of = as_of or date.today()
    result = ActionsResult(dry_run=dry_run)
    entry = repo.current_sharadar_full(db, "actions")
    if entry is None:
        raise ActionsError(
            "the ledger holds no current actions file: "
            "`fafnir source sharadar-bulk --tables actions` first"
        )
    key_of = repo.vendor_keys_by_ticker(db, VENDOR, SECURITY_TABLES)
    if not key_of:
        raise ActionsError(
            "core.vendor_security holds no Sharadar rows: "
            "`fafnir ingest sharadar-master` first"
        )
    targets = [
        t
        for t in repo.vendor_action_targets(db, VENDOR, security_ids)
        if (backfill and t["is_feed"])
        or (gap_fill and not t["is_feed"] and t["source"] == "fmp")
    ]
    result.securities = len(targets)
    by_key = {t["vendor_key"]: t for t in targets}
    actions = key_actions(
        ledger.rows(db, entry["file_id"], bulk_dir), set(by_key), key_of
    )

    foreign: dict[tuple, list[date]] = defaultdict(list)
    for a in repo.action_dates(db, [t["security_id"] for t in targets]):
        if a["source"] != VENDOR:
            foreign[(a["security_id"], a["action_type"])].append(a["ex_date"])
    suppressed = repo.suppressed_action_keys(db)
    for key, target in by_key.items():
        for p in planned_for(
            target["security_id"], actions.get(key, {}), result.skipped
        ):
            why = _in_scope(p, target, foreign, as_of, suppressed)
            if why:
                result.skipped[why] += 1
            else:
                result.planned.append(p)
    if dry_run:
        return result

    run = RunLog(
        db,
        source=VENDOR,
        endpoint=ENDPOINT,
        params={
            "backfill": backfill,
            "gap_fill": gap_fill,
            "file_id": entry["file_id"],
        },
    )
    with run:
        result.run_id = run.run_id
        done: set[int] = set()
        for p in result.planned:
            outcome: dict = {}
            changed = repo.upsert_corporate_action(
                db,
                security_id=p.security_id,
                action_type=p.action_type,
                ex_date=p.ex_date,
                split_numerator=p.split_numerator,
                split_denominator=p.split_denominator,
                dividend_amount=p.dividend_amount,
                currency="USD",
                ingestion_run_id=run.run_id,
                source=VENDOR,
                counts=outcome,
            )
            result.skipped["foreign"] += outcome.get("skipped_foreign", 0)
            if changed:
                result.written += 1
                result.changed_security_ids.add(p.security_id)
            if p.security_id not in done:
                done.add(p.security_id)
                if len(done) % BATCH == 0:
                    db.commit()
        run.rows_inserted = result.written
        run.params.update(
            securities=result.securities,
            planned=len(result.planned),
            written=result.written,
            skipped=dict(sorted(result.skipped.items())),
        )
        db.commit()

    if adjust and result.changed_security_ids:
        with_bars = {
            t["security_id"] for t in targets if t["last_trade_date"] is not None
        }
        ids = sorted(result.changed_security_ids & with_bars)
        result.awaiting_prices = len(result.changed_security_ids) - len(ids)
        if ids:
            result.adjusted = adjustments.adjust_all(db, security_ids=ids)["securities"]
    return result


PRUNE_ENDPOINT = "actions:prune"
#: The flags `adjust` keys on a dividend's ex-date. A removed dividend's are closed
#: with it: nothing will write them again.
DIVIDEND_FLAGS = ("dividend_no_prior_close", "dividend_exceeds_price")


@dataclass
class PruneResult:
    dry_run: bool
    run_id: Optional[int] = None
    rows: list[dict] = field(default_factory=list)
    #: ``<split|dividend>:<reaches_bars|before_bars>``: whether a stored bar
    #: precedes the ex-date, so the action changed an adjusted price.
    counts: Counter = field(default_factory=Counter)
    override_ids: list[int] = field(default_factory=list)
    closed_flags: list[int] = field(default_factory=list)
    adjusted: int = 0


def prune_outside_span(
    db: Database,
    *,
    created_by: str,
    security_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
    adjust: bool = True,
) -> PruneResult:
    """Remove the Sharadar actions a security minted by another vendor holds on or
    before its link's start: the ones written before :func:`_in_scope` refused them
    (the gap-fill of 2026-10-21 counted the start as inside; the backfill of
    2026-10-23 checked no span for the securities the cutover fed).

    Each row goes through the operator-override path with reason ``cleanup``. The
    row as it stood is kept in ``ops.operator_override`` and its key is set aside if
    the vendor files it again. No label is written: nothing says the vendor's
    action was wrong, only that no price of theirs stands behind it. The
    ``dividend_no_prior_close`` and ``dividend_exceeds_price`` flags of a removed
    dividend are closed with it, and the factors of the securities that hold bars
    are recomputed. One ``ops.ingestion_run`` (``sharadar`` / ``actions:prune``).
    """
    result = PruneResult(dry_run=dry_run)
    result.rows = repo.vendor_actions_outside_span(db, VENDOR, security_ids)
    for r in result.rows:
        first = r["first_trade_date"]
        reach = (
            "reaches_bars"
            if first is not None and first < r["ex_date"]
            else ("before_bars")
        )
        result.counts[f"{r['action_type']}:{reach}"] += 1
    if dry_run or not result.rows:
        return result

    run = RunLog(db, source=VENDOR, endpoint=PRUNE_ENDPOINT, params={})
    with run:
        result.run_id = run.run_id
        for r in result.rows:
            result.override_ids.append(
                repo.delete_operator_action(
                    db,
                    corporate_action_id=int(r["corporate_action_id"]),
                    note=(
                        f"Outside the Sharadar link's span: dated {r['ex_date']}, on "
                        f"or before the link's start ({r['valid_from']}), so it "
                        "adjusts only another vendor's bars, which no Sharadar price "
                        "confirms. Removed by `ingest sharadar-actions "
                        f"--prune-outside-span` (run {run.run_id})."
                    ),
                    created_by=created_by,
                    reason="cleanup",
                    detail_extra={"pruned_by_run": run.run_id},
                )
            )
        keys = [
            (int(r["security_id"]), r["ex_date"])
            for r in result.rows
            if r["action_type"] == "dividend"
        ]
        flag_ids = repo.open_flags_on_ex_dates(db, DIVIDEND_FLAGS, keys)
        if flag_ids:
            result.closed_flags = repo.resolve_dq_flags(
                db,
                repo.DqFilter(flag_ids=flag_ids),
                note=(
                    "Its dividend was outside the Sharadar link's span and was "
                    f"removed by `ingest sharadar-actions --prune-outside-span` "
                    f"(run {run.run_id}; the override names the row)."
                ),
                resolved_by=created_by,
                judgement_basis="by_rule",
            )
        run.params.update(
            removed=len(result.rows),
            counts=dict(sorted(result.counts.items())),
            securities=len({r["security_id"] for r in result.rows}),
            overrides=[min(result.override_ids), max(result.override_ids)],
            closed_flags=len(result.closed_flags),
        )
        db.commit()

    if adjust:
        ids = sorted(
            {int(r["security_id"]) for r in result.rows if r["first_trade_date"]}
        )
        if ids:
            result.adjusted = adjustments.adjust_all(db, security_ids=ids)["securities"]
    return result
