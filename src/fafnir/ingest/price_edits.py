"""
Re-date or re-scale a block of bars the vendor has wrong: `fafnir prices shift|rescale`.

Both are for histories whose bars are real prices stored under the wrong key or at the
wrong scale, where deleting them throws the history away and a re-fetch returns the
same thing (migration 0026 has the production cases):

* **shift** moves every bar in a range by N calendar days. FVI and WLL carry their
  whole history one day early -- Sunday bars that are Monday's session, and no Friday
  bars -- in FMP's payload as well as in the warehouse.
* **rescale** multiplies prices (and, separately, volume) in a range by a stated
  factor. EQC before 1997-10-17 is stored at 1/20 of the traded price; AKR, HUN and
  JKL carry pre-split eras multiplied by a split the vendor applied backwards.

This module plans an edit and checks it; :func:`repository.replace_operator_bars`
writes it. The plan is built from the stored bars alone, and every row it would write
is put through the price loader's own validation (:func:`daily_price._validate_bar`)
and its scale-collapse test, so an edit cannot store a bar the loader would have
quarantined or flagged. A plan with refusals writes nothing: the refusals name every
offending date so an operator can see what to narrow.

A shift does not move corporate actions unless asked (``with_actions``): the vendor's
actions come from a different feed and are often dated correctly even when its bars
are not. The plan lists the actions in the range, with weekdays, so the dry run shows
which case this is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import daily_price as dp

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# How many offending dates a refusal lists before summarising the rest.
_LIST_LIMIT = 10


@dataclass
class EditPlan:
    """What an edit would do, and why it may not."""

    security_id: int
    symbol: str
    kind: str
    from_date: date
    to_date: date
    params: dict
    changes: list[tuple[dict, dict]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.changes) and not self.refusals


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def parse_factor(text: str) -> Decimal:
    """A positive factor from ``20``, ``0.05`` or ``1/20``. Raises ValueError.

    A fraction is accepted because the factors this command exists for are the
    inverses of split ratios, and ``1/25000`` is what an operator reads off a chart;
    ``0.00004`` is what they mistype.
    """
    raw = (text or "").strip()
    try:
        if "/" in raw:
            num, den = (Decimal(p.strip()) for p in raw.split("/"))
            value = num / den
        else:
            value = Decimal(raw)
    except (ValueError, InvalidOperation, ArithmeticError):
        raise ValueError(f"{text!r} is not a number or a fraction like 1/20.")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{text!r} is not a positive factor.")
    return value


def _fmt(value) -> str:
    if value is None:
        return "-"
    return format(Decimal(str(value)).normalize(), "f")


def _listed(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_LIST_LIMIT])
    more = len(items) - _LIST_LIMIT
    return shown + (f", and {more} more" if more > 0 else "")


def _day(d: date) -> str:
    return f"{d} ({WEEKDAYS[d.weekday()]})"


def rescale_row(
    row: dict, price_factor: Decimal, volume_factor: Decimal
) -> tuple[Optional[dict], Optional[str]]:
    """The bar ``row`` re-scaled, or ``(None, reason)`` if the loader would refuse it.

    Prices (and vwap) are multiplied by ``price_factor``, volume by ``volume_factor``
    rounded half-up to a whole share. The result goes through the loader's
    :func:`daily_price._validate_bar` -- so a price that rounds below the column's
    resolution or past its range is refused with the quarantine reason the loader
    would have written -- and through its scale-collapse test, which a bar with a real
    range fails when shrinking it rounds every field to one value.
    """
    scaled = {
        f: Decimal(str(row[f])) * price_factor for f in ("open", "high", "low", "close")
    }
    volume = (Decimal(int(row["volume"] or 0)) * volume_factor).to_integral_value(
        rounding=ROUND_HALF_UP
    )
    bar = {
        "date": row["trade_date"].isoformat(),
        **{f: scaled[f] for f in scaled},
        "volume": int(volume),
        "vwap": (
            Decimal(str(row["vwap"])) * price_factor
            if row.get("vwap") is not None
            else None
        ),
    }
    clean, reason = dp._validate_bar(bar)
    if reason:
        return None, reason
    if dp._scale_collapse_detail(bar, clean) is not None:
        return None, "scale_collapse"
    if bar["vwap"] is not None and clean.get("vwap") is None:
        # _as_vwap drops a vwap it cannot store rather than refusing the bar, which is
        # right for a vendor row (the bar is still worth keeping) and wrong for an
        # edit: the operator asked for a scaled copy of this bar, and silently writing
        # it without its vwap is not that. Refuse, as for any other field.
        return None, "vwap_out_of_range"
    return clean, None


def shift_row(row: dict, days: int) -> dict:
    """The bar ``row``, unchanged, dated ``days`` calendar days later (or earlier)."""
    out = {
        k: row[k]
        for k in ("open", "high", "low", "close", "volume", "vwap")
        if k in row
    }
    out["trade_date"] = row["trade_date"] + timedelta(days=days)
    return out


def weekday_counts(dates: Iterable[date]) -> list[int]:
    """Bars per weekday, Monday first -- the shape a one-day misdating leaves."""
    counts = [0] * 7
    for d in dates:
        counts[d.weekday()] += 1
    return counts


def is_calendar_mlk_gap(d: date) -> bool:
    """Whether ``d`` is Martin Luther King Jr. Day 1990-1997.

    ref.trading_calendar marks those days closed, but NYSE first closed for the
    holiday in 1998 -- real bars exist on them (BXMT, ALNT, AIRT, PRG, ...). A refusal
    naming one of these dates is the calendar's error, not the edit's.
    """
    return (
        d.month == 1
        and d.weekday() == 0
        and 15 <= d.day <= 21
        and 1990 <= d.year <= 1997
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _symbol(db: Database, security_id: int) -> str:
    return db.fetchval(
        "SELECT primary_symbol FROM core.security WHERE security_id = %s",
        (security_id,),
    ) or str(security_id)


def _refuse_operator_bars(db: Database, plan: EditPlan, bars: list[dict]) -> None:
    written = repo.active_price_adds(
        db, plan.security_id, [b["trade_date"] for b in bars]
    )
    if written:
        edits = sorted({w["edit"] for w in written.values() if w["edit"] is not None})
        plan.refusals.append(
            f"{len(written)} bar(s) in the range were already written by an operator "
            f"(edit {', '.join(map(str, edits))}): "
            f"{_listed([str(d) for d in sorted(written)])}. Revoke that edit first "
            "(`fafnir override revoke ID`) and plan one edit from the vendor's bars."
        )


def plan_rescale(
    db: Database,
    security_id: int,
    from_date: date,
    to_date: date,
    price_factor: Decimal,
    volume_factor: Decimal = Decimal(1),
) -> EditPlan:
    plan = EditPlan(
        security_id=security_id,
        symbol=_symbol(db, security_id),
        kind="rescale",
        from_date=from_date,
        to_date=to_date,
        params={
            "price_factor": format(price_factor, "f"),
            "volume_factor": format(volume_factor, "f"),
        },
    )
    if from_date > to_date:
        plan.refusals.append(f"--from {from_date} is after --to {to_date}.")
        return plan
    if price_factor <= 0 or volume_factor <= 0:
        plan.refusals.append("Factors must be positive.")
        return plan
    if price_factor == 1 and volume_factor == 1:
        plan.refusals.append("Both factors are 1: the edit would change nothing.")
        return plan
    bars = repo.stored_bars(db, security_id, from_date, to_date)
    if not bars:
        plan.refusals.append(f"No stored bars between {from_date} and {to_date}.")
        return plan
    _refuse_operator_bars(db, plan, bars)

    failures: dict[str, list[str]] = {}
    for bar in bars:
        new, reason = rescale_row(bar, price_factor, volume_factor)
        if reason:
            failures.setdefault(reason, []).append(str(bar["trade_date"]))
            continue
        plan.changes.append((bar, new))
    for reason, dates in sorted(failures.items()):
        plan.refusals.append(
            f"{len(dates)} bar(s) would not survive the price loader's validation "
            f"({reason}): {_listed(dates)}."
        )
    return plan


def plan_shift(
    db: Database,
    security_id: int,
    from_date: date,
    to_date: date,
    days: int,
    *,
    allow_non_session: bool = False,
    with_actions: bool = False,
    today: Optional[date] = None,
) -> EditPlan:
    today = today or date.today()
    plan = EditPlan(
        security_id=security_id,
        symbol=_symbol(db, security_id),
        kind="shift",
        from_date=from_date,
        to_date=to_date,
        params={"days": days},
    )
    if from_date > to_date:
        plan.refusals.append(f"--from {from_date} is after --to {to_date}.")
        return plan
    if days == 0:
        plan.refusals.append("--days 0 would change nothing.")
        return plan
    bars = repo.stored_bars(db, security_id, from_date, to_date)
    plan.actions = repo.list_corporate_actions(
        db, security_id, from_date=from_date, to_date=to_date
    )
    if not bars:
        plan.refusals.append(f"No stored bars between {from_date} and {to_date}.")
        return plan
    _refuse_operator_bars(db, plan, bars)

    plan.changes = [(bar, shift_row(bar, days)) for bar in bars]
    sources = {b["trade_date"] for b in bars}
    targets = [new["trade_date"] for _, new in plan.changes]

    future = [t for t in targets if t > today]
    if future:
        plan.refusals.append(
            f"{len(future)} bar(s) would be dated in the future: "
            f"{_listed([str(t) for t in future])}."
        )

    outside = [t for t in targets if t not in sources]
    if outside:
        collisions = db.fetchall(
            """
            SELECT trade_date FROM core.daily_price
             WHERE security_id = %s AND trade_date = ANY(%s) ORDER BY trade_date
            """,
            (security_id, outside),
        )
        if collisions:
            plan.refusals.append(
                f"{len(collisions)} bar(s) would land on a date that already has a "
                "bar outside the range: "
                f"{_listed([str(c['trade_date']) for c in collisions])}. Widen the "
                "range to move those bars too, or delete them first."
            )
        claimed = repo.active_price_overrides_on(db, security_id, outside)
        if claimed:
            plan.refusals.append(
                f"{len(claimed)} target date(s) carry an active bar override: "
                + _listed(
                    [
                        f"{d} (override {', '.join(str(o['override_id']) for o in os)})"
                        for d, os in sorted(claimed.items())
                    ]
                )
                + ". Revoke it first if a bar belongs there after all."
            )

    exchange = (repo.security_price_profile(db, security_id) or {}).get("exchange_code")

    # A target outside the calendar's span is not a session question, and
    # --allow-non-session does not cover it: "no row" means closed inside the span and
    # unknown outside it, so the session check below cannot see these at all. A typed
    # --days is the way they arise (--days -40000 dates a 2024 bar to 1914), and the
    # bar lands in core.daily_price_default, which then blocks ensure_year_partition
    # for that year until someone finds it. This command exists for a misdating of a
    # day or two.
    span = (
        repo.open_sessions(db, exchange, min(targets), max(targets))
        if exchange
        else None
    )
    if span is not None:
        _, cal_first, cal_last = span
        off_calendar = sorted(t for t in targets if t < cal_first or t > cal_last)
        if off_calendar:
            plan.refusals.append(
                f"{len(off_calendar)} bar(s) would be dated outside the venue "
                f"calendar's span ({cal_first}..{cal_last}): "
                f"{_listed([str(t) for t in off_calendar])}. Check --days "
                f"{days}."
            )

    closed = sorted(dp.non_session_dates(db, exchange, targets))
    if closed:
        mlk = [d for d in closed if is_calendar_mlk_gap(d)]
        text = (
            f"{len(closed)} bar(s) would be dated on a day the venue's calendar has no "
            f"session: {_listed([_day(d) for d in closed])}."
        )
        if mlk:
            text += (
                f" {len(mlk)} of them are Martin Luther King Jr. Day 1990-1997, which "
                "ref.trading_calendar marks closed although NYSE first closed for it "
                "in 1998 -- those are the calendar's error, not the shift's."
            )
        if allow_non_session:
            plan.notes.append(text + " Allowed by --allow-non-session.")
        else:
            plan.refusals.append(
                text + " Pass --allow-non-session if the calendar is what is wrong."
            )

    if with_actions and plan.actions:
        _plan_action_moves(db, plan, days, today)
    return plan


def _plan_action_moves(db: Database, plan: EditPlan, days: int, today: date) -> None:
    moving = {(a["action_type"], a["ex_date"]) for a in plan.actions}
    for a in plan.actions:
        if a["source"] == repo.OPERATOR_SOURCE:
            plan.refusals.append(
                f"Corporate action {a['corporate_action_id']} ({a['action_type']} "
                f"{a['ex_date']}) was written by an operator; revoke it before moving "
                "it with the bars."
            )
            continue
        target = a["ex_date"] + timedelta(days=days)
        if target > today:
            plan.refusals.append(
                f"Corporate action {a['corporate_action_id']} would move to {target}, "
                "in the future."
            )
            continue
        if (a["action_type"], target) in moving:
            continue
        clash = repo.corporate_action_at(
            db,
            security_id=plan.security_id,
            action_type=a["action_type"],
            ex_date=target,
        )
        if clash is not None:
            plan.refusals.append(
                f"Corporate action {a['corporate_action_id']} ({a['action_type']} "
                f"{a['ex_date']}) would land on {target}, which already has "
                f"{a['action_type']} {clash['corporate_action_id']}."
            )


def apply_plan(
    db: Database,
    plan: EditPlan,
    *,
    note: str,
    created_by: str,
    with_actions: bool = False,
) -> tuple[int, list[int], list[tuple[int, int, int]]]:
    """Write a checked plan. Returns ``(edit_id, override_ids, moved_actions)``.

    ``moved_actions`` is ``(new_action_id, delete_override, add_override)`` per
    corporate action re-dated with a shift. Raises ``repo.OverrideRefused`` for a
    plan that is not ``ok``; nothing is written in that case.
    """
    if not plan.ok:
        raise repo.OverrideRefused(
            "; ".join(plan.refusals) or "The edit changes no bars."
        )
    edit_id, override_ids = repo.replace_operator_bars(
        db,
        security_id=plan.security_id,
        kind=plan.kind,
        params=plan.params,
        changes=plan.changes,
        note=note,
        created_by=created_by,
    )
    moved: list[tuple[int, int, int]] = []
    if plan.kind == "shift" and with_actions and plan.actions:
        days = int(plan.params["days"])
        # Move the far end first, so an action never lands on one not yet moved.
        ordered = sorted(plan.actions, key=lambda a: a["ex_date"], reverse=days > 0)
        for a in ordered:
            pair = repo.redate_operator_action(
                db,
                corporate_action_id=int(a["corporate_action_id"]),
                new_ex_date=a["ex_date"] + timedelta(days=days),
                note=f"{note} [moved with bar edit {edit_id}]",
                created_by=created_by,
            )
            # Both halves join the edit. Without this the note is the only thing
            # tying them to it, and `override revoke` -- which the command itself
            # tells the operator to use -- restored the bars while leaving the action
            # a day away from them and the true ex-date still suppressed, then
            # recomputed the adjustment factors into that state.
            repo.mark_action_override_edit(
                db,
                override_ids=pair[1:],
                edit_id=edit_id,
                kind=plan.kind,
                days=days,
            )
            moved.append(pair)
    return edit_id, override_ids, moved


def boundary_lines(db: Database, plan: EditPlan) -> list[str]:
    """The closes either side of both ends of a rescale, before and after it.

    A correct rescale makes both joins look like ordinary sessions; a wrong factor
    shows up here as a jump that moved rather than vanished.
    """
    if not plan.changes:
        return []
    first_old, first_new = plan.changes[0]
    last_old, last_new = plan.changes[-1]
    sid = plan.security_id
    before = db.fetchone(
        """
        SELECT trade_date, close FROM core.daily_price
         WHERE security_id = %s AND trade_date < %s
         ORDER BY trade_date DESC LIMIT 1
        """,
        (sid, first_old["trade_date"]),
    )
    after = db.fetchone(
        """
        SELECT trade_date, close FROM core.daily_price
         WHERE security_id = %s AND trade_date > %s
         ORDER BY trade_date LIMIT 1
        """,
        (sid, last_old["trade_date"]),
    )
    lines = []
    if before and before["close"]:
        prev = Decimal(str(before["close"]))
        lines.append(
            f"Into the range: {before['trade_date']} close {_fmt(prev)} -> "
            f"{first_old['trade_date']} close {_fmt(first_old['close'])} "
            f"(x{float(Decimal(str(first_old['close'])) / prev):.4g}); after the edit "
            f"{_fmt(first_new['close'])} (x{float(first_new['close'] / prev):.4g})."
        )
    else:
        lines.append(f"Into the range: no bar before {first_old['trade_date']}.")
    if after and after["close"]:
        nxt = Decimal(str(after["close"]))
        lines.append(
            f"Out of the range: {last_old['trade_date']} close "
            f"{_fmt(last_old['close'])} -> {after['trade_date']} close {_fmt(nxt)} "
            f"(x{float(nxt / Decimal(str(last_old['close']))):.4g}); after the edit "
            f"{_fmt(last_new['close'])} -> {_fmt(nxt)} "
            f"(x{float(nxt / last_new['close']):.4g})."
        )
    else:
        lines.append(f"Out of the range: no bar after {last_old['trade_date']}.")
    return lines
