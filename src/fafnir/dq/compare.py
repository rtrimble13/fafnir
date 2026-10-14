"""`fafnir dq compare`: a shadow night of dorq against the SQL checks and the labels.

The shadow period (dorq plan §7.4) exists to answer two questions before dorq's
flags reach the queue:

  * **Overlap.** Of the `outlier`, `gap` and `stale` flags the SQL checks wrote
    over the window, how many does dorq also report -- and how many of dorq's
    reports are conditions the SQL checks never saw? A successor that misses what
    its predecessor caught, or that reports ten times as much, is not ready.

    Recall is measured on the flags whose condition still **held** when dorq ran:
    open then, and for `stale`, on a security that had not traded since and was
    not delisted. A `stale` flag stays open until `dq recheck` closes it, so a
    security that has traded since still carries one, and no successor should
    report it. The state is read as of the run, not of the day `compare` is run,
    so a night's figures do not drift as the queue is worked.
  * **Precision.** Against the labelled decisions (`fafnir dq export-labels`), the
    share of dorq's reports that were real data errors, at warn and at error. The
    cutover bar is 0.85 at warn and 0.95 at error (dorq plan §8).

An SQL flag counts as "also reported" when a report of a successor code is about
its security within ``slack`` calendar days of its date. A report is about the
dates it spans, and the move a misdated split explains (DQ701's ``move_date``); a
cross-sectional row is about each security it names (``members``, from dorq 0.9:
a comma-separated string of series ids, as dorq writes it; a list is read too).

Matching against labels follows dorq's own evaluation tool (dorq-eval), and none
of the above: a report matches a fault label on the same security whose dates
overlap it within ``slack`` calendar days, and is *right* when its code is one the
label expects. It matches a market fact on an overlapping date, with no slack,
when its code starts with the fact's ``codes`` -- and is then *false*.

Reads only.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from fafnir.db.connection import Database
from fafnir.dq import dorq as dorq_engine

#: The SQL checks dorq is to replace, and the dorq codes that succeed each (by
#: prefix). A flag of the SQL check counts as "also reported" when a dorq report of
#: one of these codes sits on the same security within the slack.
SUCCESSORS: dict[str, tuple[str, ...]] = {
    # DQ601: several series moving by one split ratio, as a cohort row.
    "outlier": ("DQ2", "DQ601", "DQ7"),
    "gap": ("DQ301", "DQ303"),
    "stale": ("DQ304", "DQ501"),
}

DEFAULT_SLACK_DAYS = 3

#: Why an SQL flag's condition no longer held when dorq ran; anything else held.
NOT_HOLDING = ("closed", "traded_since", "inactive")


def _date(text) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _ids(values) -> tuple[int, ...]:
    # dorq writes a comma-separated string: every value in its detail is a scalar.
    if isinstance(values, str):
        values = values.split(",")
    out = []
    for v in values if isinstance(values, list) else ():
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class Report:
    """One dorq report, reduced to what matching needs.

    ``also`` holds the other dates it is about: a misdated split (DQ701) is keyed
    at the split's ex-date, and the move it explains is ``detail.detail.move_date``.
    ``members`` are the securities a cross-sectional row (no ``security_id``)
    names, from ``detail.detail.members``: a comma-separated string of series ids,
    as dorq writes it (``"9161,10640"``), or a list. Without them it is about no
    one.
    """

    security_id: Optional[int]
    code: str
    check_name: str
    severity: str
    first: dt.date
    last: dt.date
    also: tuple[dt.date, ...] = ()
    members: tuple[int, ...] = ()

    @classmethod
    def of(cls, record: dict) -> Optional["Report"]:
        detail = record.get("detail") or {}
        first = dorq_engine.report_date(record)
        if first is None:
            return None
        last = _date(detail.get("end_date")) or first
        inner = detail.get("detail")
        inner = inner if isinstance(inner, dict) else {}
        move = _date(inner.get("move_date"))
        sid = record.get("security_id")
        return cls(
            security_id=int(sid) if sid is not None else None,
            code=dorq_engine.code_of(record),
            check_name=record.get("check_name", ""),
            severity=record.get("severity", "warn"),
            first=first,
            last=last,
            also=(move,) if move is not None else (),
            members=_ids(inner.get("members")) if sid is None else (),
        )

    @property
    def securities(self) -> tuple[int, ...]:
        """The securities the report is about."""
        return (self.security_id,) if self.security_id is not None else self.members

    def covers(self, day: dt.date, slack: dt.timedelta) -> bool:
        """Whether the report is about ``day``, within ``slack``."""
        if self.first - slack <= day <= self.last + slack:
            return True
        return any(abs(d - day) <= slack for d in self.also)


@dataclass(frozen=True)
class Label:
    series: str
    first: dt.date
    last: dt.date
    label_class: str
    expect: tuple[str, ...]
    codes: str
    kind: str

    @property
    def is_fault(self) -> bool:
        return self.label_class != "market_fact"


def read_labels(path: Path) -> list[Label]:
    """The labels file `fafnir dq export-labels` writes (dorq's doc/labels.md)."""
    out = []
    with Path(path).open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                first = _date(obj.get("first") or obj.get("date"))
                last = _date(obj.get("last")) or first
                cls = obj["class"]
                series = str(obj["series"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path} line {n}: not a label ({exc})") from exc
            if first is None:
                raise ValueError(f"{path} line {n}: a label needs first (or date)")
            expect = tuple(c for c in str(obj.get("expect") or "").split("|") if c)
            codes = str(obj.get("codes") or ("DQ2" if cls == "market_fact" else ""))
            out.append(
                Label(
                    series, first, last, cls, expect, codes, str(obj.get("kind") or "")
                )
            )
    return out


def _overlaps(r: Report, lab: Label, slack: int) -> bool:
    if lab.series == "*":
        # A cross-sectional fault wants the cross-sectional row; a market-wide fact
        # covers every security.
        if lab.is_fault and r.security_id is not None:
            return False
    elif r.security_id is None or lab.series != str(r.security_id):
        return False
    return r.first <= lab.last + dt.timedelta(
        days=slack
    ) and lab.first <= r.last + dt.timedelta(days=slack)


def judge(r: Report, labels: Sequence[Label], slack: int) -> str:
    """``right``, ``other`` (a different fault), ``false`` or ``unlabelled``."""
    right = other = false = False
    for lab in labels:
        if lab.is_fault and _overlaps(r, lab, slack):
            if not lab.expect or r.code in lab.expect:
                right = True
            else:
                other = True
        elif not lab.is_fault and _overlaps(r, lab, 0) and r.code.startswith(lab.codes):
            false = True
    if right:
        return "right"
    if other:
        return "other"
    return "false" if false else "unlabelled"


@dataclass
class Precision:
    right: int = 0
    other: int = 0
    false: int = 0
    unlabelled: int = 0

    def add(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    @property
    def judged(self) -> int:
        return self.right + self.other + self.false

    @property
    def precision(self) -> Optional[float]:
        return self.right / self.judged if self.judged else None

    @property
    def fault_precision(self) -> Optional[float]:
        return (self.right + self.other) / self.judged if self.judged else None


@dataclass
class Comparison:
    shadow_file: str
    window_from: Optional[dt.date]
    window_to: Optional[dt.date]
    dorq_reports: int
    # per dorq code: reports, of which an SQL flag of the predecessor also covers
    by_code: dict[str, dict[str, int]] = field(default_factory=dict)
    # per SQL check: flags keyed in the window (within the slack), of which dorq
    # also reports; of those, the ones whose condition held when dorq ran, and
    # matched; and the ones dorq did not report, by why (see NOT_HOLDING)
    by_sql_check: dict[str, dict] = field(default_factory=dict)
    # when the flags' state is read: the run's start (None: now)
    state_at: Optional[dt.datetime] = None
    # securities whose whole history the run judged (params.backfill)
    backfill_securities: int = 0
    # dorq reports that match an SQL flag accepted as a market fact
    on_accepted: int = 0
    # precision against labels, overall at warn and at error, and per code
    precision_warn: Precision = field(default_factory=Precision)
    precision_error: Precision = field(default_factory=Precision)
    precision_by_code: dict[str, Precision] = field(default_factory=dict)
    labelled: bool = False

    def as_dict(self) -> dict:
        def p(x: Precision) -> dict:
            return {
                "right": x.right,
                "other_fault": x.other,
                "false": x.false,
                "unlabelled": x.unlabelled,
                "precision": x.precision,
                "fault_precision": x.fault_precision,
            }

        return {
            "shadow_file": self.shadow_file,
            "window": [
                self.window_from.isoformat() if self.window_from else None,
                self.window_to.isoformat() if self.window_to else None,
            ],
            "dorq_reports": self.dorq_reports,
            "by_code": self.by_code,
            "by_sql_check": self.by_sql_check,
            "state_at": self.state_at.isoformat() if self.state_at else None,
            "backfill_securities": self.backfill_securities,
            "on_accepted": self.on_accepted,
            "precision": (
                {
                    "warn": p(self.precision_warn),
                    "error": p(self.precision_error),
                    "by_code": {c: p(v) for c, v in self.precision_by_code.items()},
                }
                if self.labelled
                else None
            ),
        }


def _sql_flags(
    db: Database,
    lo: dt.date,
    hi: dt.date,
    backfilled: Sequence[int],
    as_of: Optional[dt.date],
    state_at: Optional[dt.datetime],
) -> list[dict]:
    """The flags of the SQL checks dorq is to replace that its night covers: keyed
    from ``lo`` to ``hi``, and every one on a security whose whole history the run
    judged. Each comes with its ``state`` when dorq ran (``state_at``, as of the
    night's ``as_of``): ``holding``, or one of :data:`NOT_HOLDING`. A flag written
    after the run is not one the night could have matched, and is left out.

    ``inactive`` reads the delisting date, so it holds for the night whenever the
    delisting was recorded; a security out of scope with no date counts too.
    """
    return db.fetchall(
        """
        WITH f AS (
            SELECT f.*,
                   COALESCE(f.record_key->>'trade_date',
                            f.record_key->>'last_date')::date AS d
              FROM ops.data_quality_flag f
             WHERE f.check_name = ANY(%(checks)s)
               AND f.detected_at <= COALESCE(%(t)s::timestamptz, 'infinity')
        )
        SELECT f.check_name, f.security_id, f.d,
               f.accepted_at IS NOT NULL AS accepted,
               CASE
                 WHEN f.resolved_at <= COALESCE(%(t)s::timestamptz, 'infinity')
                   OR f.accepted_at <= COALESCE(%(t)s::timestamptz, 'infinity')
                   THEN 'closed'
                 WHEN f.check_name = 'stale' AND EXISTS (
                        SELECT 1 FROM core.daily_price p
                         WHERE p.security_id = f.security_id
                           AND p.trade_date > f.d
                           AND p.trade_date <= COALESCE(%(as_of)s::date, 'infinity'))
                   THEN 'traded_since'
                 WHEN f.check_name = 'stale'
                  AND (s.delisted_date <= COALESCE(%(as_of)s::date, 'infinity')
                       OR (s.delisted_date IS NULL AND NOT s.is_actively_trading))
                   THEN 'inactive'
                 ELSE 'holding'
               END AS state
          FROM f
          LEFT JOIN core.security s ON s.security_id = f.security_id
         WHERE f.d BETWEEN %(lo)s AND %(hi)s
            OR f.security_id = ANY(%(backfilled)s)
        """,
        {
            "checks": list(SUCCESSORS),
            "t": state_at,
            "as_of": as_of,
            "lo": lo,
            "hi": hi,
            "backfilled": list(backfilled),
        },
    )


def compare(
    db: Database,
    shadow_file: Path,
    *,
    labels: Optional[Sequence[Label]] = None,
    slack_days: int = DEFAULT_SLACK_DAYS,
) -> Comparison:
    records = dorq_engine.read_shadow(shadow_file)
    reports = [r for r in (Report.of(x) for x in records) if r is not None]
    as_of = _date(Path(shadow_file).stem)
    # The run that wrote the file: each run for a date replaces it, so the newest.
    night = (
        dorq_engine.night_run(db, dorq_engine.WATERMARK_SHADOW, as_of)
        if as_of
        else None
    )
    window_from = state_at = None
    window_to = as_of
    backfilled: list[int] = []
    if night is not None:
        window_from = _date(night["since"]) or night["window_from"]
        state_at = night["started_at"]
        backfilled = list(
            ((night["params"] or {}).get("backfill") or {}).get("security_ids") or []
        )
    if reports:
        window_from = window_from or min(r.first for r in reports)
        window_to = window_to or max(r.last for r in reports)
    slack = dt.timedelta(days=slack_days)

    sql_flags = []
    if window_from and window_to:
        sql_flags = _sql_flags(
            db, window_from - slack, window_to + slack, backfilled, as_of, state_at
        )
    sql_by_security: dict[int, list[dict]] = {}
    for f in sql_flags:
        sql_by_security.setdefault(f["security_id"], []).append(f)

    def matches(r: Report, f: dict) -> bool:
        return r.code.startswith(SUCCESSORS[f["check_name"]]) and r.covers(
            f["d"], slack
        )

    def flags_of(r: Report):
        return (f for sid in r.securities for f in sql_by_security.get(sid, ()))

    out = Comparison(
        shadow_file=str(shadow_file),
        window_from=window_from,
        window_to=window_to,
        dorq_reports=len(reports),
        labelled=labels is not None,
        state_at=state_at,
        backfill_securities=len(backfilled),
    )
    by_security: dict[int, list[Report]] = {}
    for r in reports:
        for sid in r.securities:
            by_security.setdefault(sid, []).append(r)
        # A cohort row is one report, however many securities it names.
        entry = out.by_code.setdefault(r.code, {"reports": 0, "also_sql": 0})
        entry["reports"] += 1
        entry["also_sql"] += 1 if any(matches(r, f) for f in flags_of(r)) else 0
        if any(f["accepted"] and matches(r, f) for f in flags_of(r)):
            out.on_accepted += 1
        if labels is not None:
            outcome = judge(r, labels, slack_days)
            out.precision_warn.add(outcome)
            if r.severity == "error":
                out.precision_error.add(outcome)
            out.precision_by_code.setdefault(r.code, Precision()).add(outcome)
    out.by_code = dict(sorted(out.by_code.items()))
    out.precision_by_code = dict(sorted(out.precision_by_code.items()))

    for check in SUCCESSORS:
        entry = {
            "flags": 0,
            "also_dorq": 0,
            "holding": 0,
            "holding_also_dorq": 0,
            "unmatched": {"holding": 0, **{reason: 0 for reason in NOT_HOLDING}},
        }
        for f in sql_flags:
            if f["check_name"] != check:
                continue
            holding = f["state"] == "holding"
            matched = any(matches(r, f) for r in by_security.get(f["security_id"], ()))
            entry["flags"] += 1
            entry["also_dorq"] += matched
            entry["holding"] += holding
            entry["holding_also_dorq"] += holding and matched
            if not matched:
                entry["unmatched"][f["state"]] += 1
        out.by_sql_check[check] = entry
    return out


def latest_shadow_file(shadow_dir: str) -> Optional[Path]:
    files = sorted(Path(shadow_dir).glob("*.jsonl"))
    return files[-1] if files else None


def format_comparison(c: Comparison) -> str:
    def pct(x: Optional[float]) -> str:
        return "-" if x is None else f"{x:.3f}"

    def share(n: int, of: int) -> str:
        return f"{n:>6} ({n / of:.0%})" if of else f"{n:>6}"

    stamp = (
        f"as the run found them, {c.state_at:%Y-%m-%d %H:%M %Z}"
        if c.state_at
        else "as they stand now (no run recorded for this file)"
    )
    lines = [
        f"Shadow file : {c.shadow_file}",
        f"Window      : {c.window_from or '?'} .. {c.window_to or '?'}",
        f"dorq        : {c.dorq_reports} reports",
    ]
    if c.backfill_securities:
        lines.append(
            f"Backfill    : the whole history of {c.backfill_securities} securities,"
            " new or rewritten; their SQL flags are compared at every date"
        )
    lines += [
        f"SQL flags   : {stamp}",
        "",
        "SQL check   holding  also reported by dorq   all flags  also reported by dorq",
    ]
    for check, e in c.by_sql_check.items():
        lines.append(
            f"  {check:<9} {e['holding']:>7}  {share(e['holding_also_dorq'], e['holding']):<22}"
            f" {e['flags']:>9}  {share(e['also_dorq'], e['flags'])}"
        )
    sql_total = sum(e["flags"] for e in c.by_sql_check.values())
    if sql_total:
        change = (c.dorq_reports - sql_total) / sql_total
        lines.append(
            f"  dorq reports {c.dorq_reports} against {sql_total} SQL flags"
            f" ({change:+.0%})"
        )
    lines += [
        "  Recall is measured on the holding column: flags still open when dorq ran,",
        "  and for stale, on a security that had not traded since nor been delisted.",
        "",
        "Not reported by dorq, by why:",
    ]
    for check, e in c.by_sql_check.items():
        why = ", ".join(
            f"{n} {reason.replace('_', ' ')}"
            for reason, n in e["unmatched"].items()
            if n
        )
        lines.append(f"  {check:<9} {why or '-'}")
    lines += ["", "dorq code   reports  also an SQL flag"]
    for code, e in c.by_code.items():
        lines.append(f"  {code:<9} {e['reports']:>7}  {e['also_sql']:>7}")
    if c.on_accepted:
        lines += [
            "",
            f"Reports on an SQL flag accepted as a market fact: {c.on_accepted}.",
            "  With --labels, a DQ2xx report on that security and date counts as false;"
            " a cohort",
            "  row or a misdated split (DQ601, DQ7xx) stays unlabelled, as dorq-eval"
            " matches labels.",
        ]
    if c.labelled:
        lines += [
            "",
            "Against the labels (right / other fault / false / unlabelled, precision):",
        ]
        for name, p in (("warn+", c.precision_warn), ("error", c.precision_error)):
            lines.append(
                f"  {name:<9} {p.right:>5} {p.other:>5} {p.false:>5} {p.unlabelled:>6}"
                f"  {pct(p.precision)}"
            )
        for code, p in c.precision_by_code.items():
            lines.append(
                f"  {code:<9} {p.right:>5} {p.other:>5} {p.false:>5} {p.unlabelled:>6}"
                f"  {pct(p.precision)}"
            )
        lines.append("  Cutover needs 0.85 at warn+ and 0.95 at error (dorq plan §8).")
    return "\n".join(lines)
