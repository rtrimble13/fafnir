"""`fafnir dq compare`: a shadow night of dorq against the SQL checks and the labels.

The shadow period (dorq plan §7.4) exists to answer two questions before dorq's
flags reach the queue:

  * **Overlap.** Of the `outlier`, `gap` and `stale` flags the SQL checks wrote
    over the window, how many does dorq also report -- and how many of dorq's
    reports are conditions the SQL checks never saw? A successor that misses what
    its predecessor caught, or that reports ten times as much, is not ready.
  * **Precision.** Against the labelled decisions (`fafnir dq export-labels`), the
    share of dorq's reports that were real data errors, at warn and at error. The
    cutover bar is 0.85 at warn and 0.95 at error (dorq plan §8).

Matching follows dorq's own evaluation tool (dorq-eval): a report matches a fault
label on the same security whose dates overlap it within ``slack`` calendar days,
and is *right* when its code is one the label expects. It matches a market fact
on an overlapping date, with no slack, when its code starts with the fact's
``codes`` -- and is then *false*.

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
    "outlier": ("DQ2", "DQ7"),
    "gap": ("DQ301", "DQ303"),
    "stale": ("DQ304", "DQ501"),
}

DEFAULT_SLACK_DAYS = 3


def _date(text) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Report:
    """One dorq report, reduced to what matching needs."""

    security_id: Optional[int]
    code: str
    check_name: str
    severity: str
    first: dt.date
    last: dt.date

    @classmethod
    def of(cls, record: dict) -> Optional["Report"]:
        detail = record.get("detail") or {}
        first = _date(
            (record.get("record_key") or {}).get("trade_date") or detail.get("date")
        )
        if first is None:
            return None
        last = _date(detail.get("end_date")) or first
        sid = record.get("security_id")
        return cls(
            security_id=int(sid) if sid is not None else None,
            code=dorq_engine.code_of(record),
            check_name=record.get("check_name", ""),
            severity=record.get("severity", "warn"),
            first=first,
            last=last,
        )


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
    # per SQL check: flags keyed in the window, of which dorq also reports
    by_sql_check: dict[str, dict[str, int]] = field(default_factory=dict)
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


def _shadow_window(db: Database, as_of: Optional[dt.date]) -> tuple:
    """The window the shadow run that wrote ``as_of`` reported over."""
    if as_of is None:
        return None, None
    row = db.fetchone(
        """
        SELECT window_from, params->>'since' AS since FROM ops.ingestion_run
         WHERE source = %s AND endpoint = %s AND window_to = %s AND status = 'success'
         ORDER BY ingestion_run_id DESC LIMIT 1
        """,
        (dorq_engine.WATERMARK_SOURCE, dorq_engine.WATERMARK_SHADOW, as_of),
    )
    if row is None:
        return None, as_of
    since = _date(row["since"]) if row["since"] else None
    return since or row["window_from"], as_of


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
    window_from, window_to = _shadow_window(db, as_of)
    if reports:
        window_from = window_from or min(r.first for r in reports)
        window_to = window_to or max(r.last for r in reports)
    slack = dt.timedelta(days=slack_days)

    sql_flags = []
    if window_from and window_to:
        sql_flags = db.fetchall(
            """
            SELECT check_name, security_id,
                   COALESCE(record_key->>'trade_date', record_key->>'last_date')::date AS d
              FROM ops.data_quality_flag
             WHERE check_name = ANY(%s)
               AND COALESCE(record_key->>'trade_date', record_key->>'last_date')::date
                   BETWEEN %s AND %s
            """,
            (list(SUCCESSORS), window_from, window_to),
        )
    sql_by_security: dict[int, list[tuple[str, dt.date]]] = {}
    for f in sql_flags:
        sql_by_security.setdefault(f["security_id"], []).append(
            (f["check_name"], f["d"])
        )

    def sql_covers(r: Report) -> bool:
        for check, d in sql_by_security.get(r.security_id, ()):
            if (
                r.code.startswith(SUCCESSORS[check])
                and r.first - slack <= d <= r.last + slack
            ):
                return True
        return False

    out = Comparison(
        shadow_file=str(shadow_file),
        window_from=window_from,
        window_to=window_to,
        dorq_reports=len(reports),
        labelled=labels is not None,
    )
    by_security: dict[Optional[int], list[Report]] = {}
    for r in reports:
        by_security.setdefault(r.security_id, []).append(r)
        entry = out.by_code.setdefault(r.code, {"reports": 0, "also_sql": 0})
        entry["reports"] += 1
        entry["also_sql"] += 1 if sql_covers(r) else 0
        if labels is not None:
            outcome = judge(r, labels, slack_days)
            out.precision_warn.add(outcome)
            if r.severity == "error":
                out.precision_error.add(outcome)
            out.precision_by_code.setdefault(r.code, Precision()).add(outcome)
    out.by_code = dict(sorted(out.by_code.items()))
    out.precision_by_code = dict(sorted(out.precision_by_code.items()))

    for check in SUCCESSORS:
        entry = {"flags": 0, "also_dorq": 0}
        for f in sql_flags:
            if f["check_name"] != check:
                continue
            entry["flags"] += 1
            if any(
                r.code.startswith(SUCCESSORS[check])
                and r.first - slack <= f["d"] <= r.last + slack
                for r in by_security.get(f["security_id"], ())
            ):
                entry["also_dorq"] += 1
        out.by_sql_check[check] = entry
    return out


def latest_shadow_file(shadow_dir: str) -> Optional[Path]:
    files = sorted(Path(shadow_dir).glob("*.jsonl"))
    return files[-1] if files else None


def format_comparison(c: Comparison) -> str:
    def pct(x: Optional[float]) -> str:
        return "-" if x is None else f"{x:.3f}"

    lines = [
        f"Shadow file : {c.shadow_file}",
        f"Window      : {c.window_from or '?'} .. {c.window_to or '?'}",
        f"dorq        : {c.dorq_reports} reports",
        "",
        "SQL check   flags  also reported by dorq",
    ]
    for check, e in c.by_sql_check.items():
        share = f" ({e['also_dorq'] / e['flags']:.0%})" if e["flags"] else ""
        lines.append(f"  {check:<9} {e['flags']:>6}  {e['also_dorq']:>6}{share}")
    sql_total = sum(e["flags"] for e in c.by_sql_check.values())
    if sql_total:
        change = (c.dorq_reports - sql_total) / sql_total
        lines.append(
            f"  dorq reports {c.dorq_reports} against {sql_total} SQL flags"
            f" ({change:+.0%})"
        )
    lines += ["", "dorq code   reports  also an SQL flag"]
    for code, e in c.by_code.items():
        lines.append(f"  {code:<9} {e['reports']:>7}  {e['also_sql']:>7}")
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
