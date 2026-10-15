"""`fafnir dq compare`: a shadow night of dorq against the SQL checks and the labels.

The shadow period (dorq plan §7.4) exists to show that dorq meets the cutover bar
(doc/dorq.md) before its flags reach the queue: precision of 0.85 at warn and 0.95
at error, recall no lower than the SQL checks', and at least 70% fewer new flags a
night, over 20 sessions. A night's comparison measures all three:

  * **Overlap and recall.** Of the `outlier`, `gap` and `stale` flags the SQL
    checks wrote over the window, how many does dorq also report -- and how many of
    dorq's reports are conditions the SQL checks never saw?

    Recall is measured on the flags whose condition still **held** when dorq ran:
    open then, and for `stale`, on a security that had not traded since and was
    not delisted. The state is read as of the run, not of the day `compare` is
    run, so a night's figures do not drift as the queue is worked -- and from the
    **holding snapshot** the run records (``params.holding``), so they do not drift
    as late bars arrive either: ``core.daily_price`` as it stands now holds bars
    the night could not have seen, and the re-read overwrites their load stamps.

    A `stale` flag keyed before the window is a condition that may still hold: a
    feed that stopped three weeks ago is still stopped. Those that held at the
    run's start are fetched whatever their date and counted on a line of their own
    (``pre_window``). dorq up to 0.9 keys its stale-feed report on the first
    missing session and stops reporting once that date leaves ``--since``; the
    line shows the misses until dorq keeps reporting a stale feed while it holds.
  * **New flags per night.** The conditions in this night's file that the
    previous night's did not hold, against the SQL flags written between the two
    runs. The cutover bar is 70% fewer.
  * **Precision.** Against the labels (`fafnir dq export-labels`, or the
    warehouse's own with ``--labels-from-db``), the share of dorq's reports that
    were right (``right / judged``, strict) and that were *some* fault
    (``(right + other) / judged``), at warn and above, at error, and per code --
    with the coverage: how many reports a label judged at all. A label applies to
    a night only when the night's run started before the label's
    ``applies_until`` (a repair label stops applying once the data dorq reads was
    corrected).

An SQL flag counts as "also reported" when a report of a successor code is about
its security within ``slack`` calendar days of its date. A report is about the
dates it spans, and the move a misdated split explains (DQ701's ``move_date``); a
cross-sectional row is about each security it names (``members``, from dorq 0.9:
a comma-separated string of series ids, as dorq writes it; a list is read too).

Matching against labels follows dorq's own evaluation tool (dorq-eval): a report
matches a fault label on the same security whose dates overlap it within
``slack`` calendar days, and is *right* when its code is one the label expects. It
matches a market fact on an overlapping date, with no slack, when its code starts
with the fact's ``codes`` -- and is then *false*. A label on ``"*"`` is
cross-sectional: a fault there wants the cohort row, and a market fact covers every
report -- unless it names ``members``, when it covers only the cohort rows naming
one of them.

Reads only.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional, Sequence

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

#: The SQL checks whose condition can still hold after its key date leaves the
#: window. A stale feed stays stopped; a missing session in the middle of a history
#: is a fact about one past date, which no nightly window re-reports by design.
PRE_WINDOW_CHECKS = ("stale",)

DEFAULT_SLACK_DAYS = 3

#: The cutover bar on new flags a night (dorq plan §8): at least this much fewer.
NEW_FLAGS_BAR = -0.70

#: Why an SQL flag's condition no longer held when dorq ran; anything else held.
NOT_HOLDING = ("closed", "traded_since", "inactive")

#: How far before the window the holding snapshot reaches (calendar days). It
#: covers every slack anyone passes in practice; a flag keyed further back, and
#: not stale, falls back to the state computed now.
SNAPSHOT_REACH_DAYS = 31

#: Above this many flags the snapshot is not stored (a --full run on a warehouse
#: with a deep open queue): ``params`` is not the place for it, and that run's
#: comparison is recomputed instead, saying so.
SNAPSHOT_MAX_FLAGS = 20_000


def _date(text) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _stamp(text) -> Optional[dt.datetime]:
    if text in (None, ""):
        return None
    if isinstance(text, dt.datetime):
        return text if text.tzinfo else text.replace(tzinfo=dt.timezone.utc)
    try:
        value = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _ids(values) -> tuple[int, ...]:
    return tuple(dorq_engine.member_ids(values))


@dataclass(frozen=True)
class Report:
    """One dorq report, reduced to what matching needs.

    ``also`` holds the other dates it is about: a misdated split (DQ701) is keyed
    at the split's ex-date, and the move it explains is ``detail.detail.move_date``.
    ``members`` are the securities a cross-sectional row (no ``security_id``)
    names, from ``detail.detail.members``: a comma-separated string of series ids,
    as dorq writes it (``"9161,10640"``), or a list. Without them it is about no
    one. ``key`` is the condition (:func:`fafnir.dq.dorq.condition_key`).
    """

    security_id: Optional[int]
    code: str
    check_name: str
    severity: str
    first: dt.date
    last: dt.date
    also: tuple[dt.date, ...] = ()
    members: tuple[int, ...] = ()
    key: tuple = ()
    p_error: Optional[float] = None
    message: str = ""

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
        try:
            p_error = float(detail.get("p_error"))
        except (TypeError, ValueError):
            p_error = None
        return cls(
            security_id=int(sid) if sid is not None else None,
            code=dorq_engine.code_of(record),
            check_name=record.get("check_name", ""),
            severity=record.get("severity", "warn"),
            first=first,
            last=last,
            also=(move,) if move is not None else (),
            members=_ids(inner.get("members")) if sid is None else (),
            key=dorq_engine.condition_key(record) if "check_name" in record else (),
            p_error=p_error,
            message=str(detail.get("message") or ""),
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
    decided_at: Optional[dt.datetime] = None
    applies_until: Optional[dt.datetime] = None
    verified: Optional[bool] = None
    members: tuple[int, ...] = ()

    @property
    def is_fault(self) -> bool:
        return self.label_class != "market_fact"

    def applies_at(self, started_at: Optional[dt.datetime]) -> bool:
        """Whether the label is about what a run starting at ``started_at`` read.

        A repair label stops applying when the repair was made: a night that ran
        after it judged the corrected data. Unknown on either side, it applies.
        """
        if self.applies_until is None or started_at is None:
            return True
        return started_at < self.applies_until


def parse_labels(fh: IO[str], name: str = "labels") -> list[Label]:
    """Labels in dorq's JSONL format (dorq's doc/labels.md), with fafnir's
    additions: ``decided_at``, ``applies_until``, ``verified``, ``members``."""
    out = []
    for n, line in enumerate(fh, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            first = _date(obj.get("first") or obj.get("date"))
            last = _date(obj.get("last")) or first
            cls = obj["class"]
            series = str(obj["series"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"{name} line {n}: not a label ({exc})") from exc
        if first is None:
            raise ValueError(f"{name} line {n}: a label needs first (or date)")
        expect = tuple(c for c in str(obj.get("expect") or "").split("|") if c)
        codes = str(obj.get("codes") or ("DQ2" if cls == "market_fact" else ""))
        verified = obj.get("verified")
        out.append(
            Label(
                series,
                first,
                last,
                cls,
                expect,
                codes,
                str(obj.get("kind") or ""),
                decided_at=_stamp(obj.get("decided_at")),
                applies_until=_stamp(obj.get("applies_until")),
                verified=verified if isinstance(verified, bool) else None,
                members=_ids(obj.get("members")),
            )
        )
    return out


def read_labels(path: Path) -> list[Label]:
    """The labels file `fafnir dq export-labels` writes (dorq's doc/labels.md)."""
    with Path(path).open(encoding="utf-8") as fh:
        return parse_labels(fh, str(path))


def labels_from_db(db: Database, *, verified_only: bool = False) -> list[Label]:
    """The labels `dq export-labels` would write, read in process: no file."""
    from fafnir.dq import labels as lb

    export = lb.build(db, verified_only=verified_only)
    lines = [json.dumps(lab, default=str) + "\n" for lab in export.labels]
    return parse_labels(iter(lines), "export-labels")


def _overlaps(r: Report, lab: Label, slack: int) -> bool:
    if lab.series == "*":
        # A cross-sectional fault wants the cross-sectional row; a market-wide fact
        # covers every security -- unless it names its members, when it covers only
        # the cohort rows naming one of them.
        if lab.members:
            if r.security_id is not None or not set(lab.members) & set(r.members):
                return False
        elif lab.is_fault and r.security_id is not None:
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


class LabelIndex:
    """Labels by series, so a night's reports are judged against their own
    security's labels and the cross-sectional ones -- not all 90,000."""

    def __init__(self, labels: Sequence[Label]) -> None:
        self.by_series: dict[str, list[Label]] = {}
        self.star: list[Label] = []
        for lab in labels:
            if lab.series == "*":
                self.star.append(lab)
            else:
                self.by_series.setdefault(lab.series, []).append(lab)

    def judge(self, r: Report, slack: int) -> str:
        own = self.by_series.get(str(r.security_id), ()) if r.security_id else ()
        return judge(r, [*own, *self.star], slack)


def applicable(
    labels: Sequence[Label],
    started_at: Optional[dt.datetime],
    *,
    ignore_time: bool = False,
    verified_only: bool = False,
) -> list[Label]:
    """The labels that judge a night whose run started at ``started_at``."""
    out = []
    for lab in labels:
        if verified_only and lab.verified is not True:
            continue
        if not ignore_time and not lab.applies_at(started_at):
            continue
        out.append(lab)
    return out


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
    def reports(self) -> int:
        return self.judged + self.unlabelled

    @property
    def precision(self) -> Optional[float]:
        return self.right / self.judged if self.judged else None

    @property
    def fault_precision(self) -> Optional[float]:
        return (self.right + self.other) / self.judged if self.judged else None

    def as_dict(self) -> dict:
        return {
            "right": self.right,
            "other_fault": self.other,
            "false": self.false,
            "unlabelled": self.unlabelled,
            "precision": self.precision,
            "fault_precision": self.fault_precision,
        }


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
    # where the state came from: the run's snapshot, or computed now
    holding_source: str = "now"
    # securities whose whole history the run judged (params.backfill)
    backfill_securities: int = 0
    # dorq reports that match an SQL flag accepted as a market fact
    on_accepted: int = 0
    # distinct conditions among the reports (fafnir.dq.dorq.condition_key)
    conditions: int = 0
    # SQL flags keyed before the window whose condition still held at the run
    pre_window: dict[str, dict] = field(default_factory=dict)
    # new conditions against the previous night (None: no previous night)
    new_per_night: Optional[dict] = None
    # precision against labels, overall at warn and at error, and per code
    precision_warn: Precision = field(default_factory=Precision)
    precision_error: Precision = field(default_factory=Precision)
    precision_by_code: dict[str, Precision] = field(default_factory=dict)
    labelled: bool = False
    labels_time_scoped: bool = False
    labels_verified_only: bool = False
    labels_used: int = 0
    dorq_eval: Optional[dict] = None
    # the night's conditions, and each one's outcome against the labels, for the
    # all-nights summary (not in as_dict)
    keys: set = field(default_factory=set)
    outcomes: dict[tuple, tuple[str, str, str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        warn = self.precision_warn
        return {
            "shadow_file": self.shadow_file,
            "window": [
                self.window_from.isoformat() if self.window_from else None,
                self.window_to.isoformat() if self.window_to else None,
            ],
            "dorq_reports": self.dorq_reports,
            "conditions": self.conditions,
            "by_code": self.by_code,
            "by_sql_check": self.by_sql_check,
            "pre_window": self.pre_window,
            "new_per_night": self.new_per_night,
            "state_at": self.state_at.isoformat() if self.state_at else None,
            "holding_source": self.holding_source,
            "backfill_securities": self.backfill_securities,
            "on_accepted": self.on_accepted,
            "precision": (
                {
                    "warn": warn.as_dict(),
                    "error": self.precision_error.as_dict(),
                    "by_code": {
                        c: v.as_dict() for c, v in self.precision_by_code.items()
                    },
                    "coverage": {
                        "judged": warn.judged,
                        "reports": warn.reports,
                        "share": warn.judged / warn.reports if warn.reports else None,
                    },
                    "labels": self.labels_used,
                    "time_scoped": self.labels_time_scoped,
                    "verified_only": self.labels_verified_only,
                }
                if self.labelled
                else None
            ),
            "dorq_eval": self.dorq_eval,
        }


# ---------------------------------------------------------------------------
# The SQL side
# ---------------------------------------------------------------------------

#: The state of an SQL flag as of a run: ``t`` (its start) and ``as_of`` (its
#: date). Shared by the comparison and the run's holding snapshot, so the snapshot
#: freezes exactly what the comparison would have computed.
_STATE_SQL = """
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
END
"""

_FLAGS_CTE = """
WITH f AS (
    SELECT f.*,
           COALESCE(f.record_key->>'trade_date',
                    f.record_key->>'last_date')::date AS d
      FROM ops.data_quality_flag f
     WHERE f.check_name = ANY(%(checks)s)
       AND f.detected_at <= COALESCE(%(t)s::timestamptz, 'infinity')
)
"""


def _sql_flags(
    db: Database,
    lo: dt.date,
    hi: dt.date,
    backfilled: Sequence[int],
    as_of: Optional[dt.date],
    state_at: Optional[dt.datetime],
) -> list[dict]:
    """The flags of the SQL checks dorq is to replace that its night covers: keyed
    from ``lo`` to ``hi``, every one on a security whose whole history the run
    judged, and -- marked ``pre_window`` -- every :data:`PRE_WINDOW_CHECKS` flag
    keyed before ``lo`` that was open at the run's start. Each comes with its
    ``state`` when dorq ran (``state_at``, as of the night's ``as_of``):
    ``holding``, or one of :data:`NOT_HOLDING`. A flag written after the run is not
    one the night could have matched, and is left out.

    ``inactive`` reads the delisting date, so it holds for the night whenever the
    delisting was recorded; a security out of scope with no date counts too.
    """
    return db.fetchall(
        _FLAGS_CTE + f"""
        SELECT f.dq_flag_id, f.check_name, f.security_id, f.d,
               f.accepted_at IS NOT NULL AS accepted,
               NOT (f.d BETWEEN %(lo)s AND %(hi)s
                    OR f.security_id = ANY(%(backfilled)s)) AS pre_window,
               {_STATE_SQL} AS state
          FROM f
          LEFT JOIN core.security s ON s.security_id = f.security_id
         WHERE f.d BETWEEN %(lo)s AND %(hi)s
            OR f.security_id = ANY(%(backfilled)s)
            OR (f.check_name = ANY(%(pre)s) AND f.d < %(lo)s
                AND (f.resolved_at IS NULL
                     OR f.resolved_at > COALESCE(%(t)s::timestamptz, 'infinity'))
                AND (f.accepted_at IS NULL
                     OR f.accepted_at > COALESCE(%(t)s::timestamptz, 'infinity')))
        """,
        {
            "checks": list(SUCCESSORS),
            "pre": list(PRE_WINDOW_CHECKS),
            "t": state_at,
            "as_of": as_of,
            "lo": lo,
            "hi": hi,
            "backfilled": list(backfilled),
        },
    )


def holding_snapshot(
    db: Database,
    *,
    window_from: Optional[dt.date],
    as_of: Optional[dt.date],
    started_at: Optional[dt.datetime],
    backfilled: Sequence[int] = (),
) -> Optional[dict]:
    """The state of the SQL flags `compare` will fetch for a run, frozen at the run.

    Every flag of the successor checks that was open at ``started_at`` and that a
    comparison of the night can fetch -- keyed from :data:`SNAPSHOT_REACH_DAYS`
    before the window, any stale flag, any flag on a backfilled security -- with its
    state by the comparison's own rule (:data:`_STATE_SQL`). A flag the comparison
    fetches that is not in it was closed when the run started. Returns
    ``{"since": date, "states": {dq_flag_id: state}}`` for the run's
    ``params.holding``, or None when there would be more than
    :data:`SNAPSHOT_MAX_FLAGS`.
    """
    since = (
        window_from - dt.timedelta(days=SNAPSHOT_REACH_DAYS)
        if window_from is not None
        else None
    )
    rows = db.fetchall(
        _FLAGS_CTE + f"""
        SELECT f.dq_flag_id, {_STATE_SQL} AS state
          FROM f
          LEFT JOIN core.security s ON s.security_id = f.security_id
         WHERE (f.resolved_at IS NULL
                OR f.resolved_at > COALESCE(%(t)s::timestamptz, 'infinity'))
           AND (f.accepted_at IS NULL
                OR f.accepted_at > COALESCE(%(t)s::timestamptz, 'infinity'))
           AND (%(since)s::date IS NULL OR f.d >= %(since)s::date
                OR f.check_name = ANY(%(pre)s)
                OR f.security_id = ANY(%(backfilled)s))
         LIMIT %(cap)s
        """,
        {
            "checks": list(SUCCESSORS),
            "pre": list(PRE_WINDOW_CHECKS),
            "t": started_at,
            "as_of": as_of,
            "since": since,
            "backfilled": list(backfilled),
            "cap": SNAPSHOT_MAX_FLAGS + 1,
        },
    )
    if len(rows) > SNAPSHOT_MAX_FLAGS:
        return None
    return {
        "since": since.isoformat() if since else None,
        "states": {str(r["dq_flag_id"]): r["state"] for r in rows},
    }


def _apply_snapshot(
    flags: list[dict], snapshot: dict, backfilled: Sequence[int]
) -> list[dict]:
    """Replace the computed states with the run's own, where the snapshot covers."""
    states = snapshot.get("states") or {}
    since = _date(snapshot.get("since"))
    backfill = set(backfilled)
    out = []
    for f in flags:
        state = states.get(str(f["dq_flag_id"]))
        if state is None:
            covered = (
                since is None
                or (f["d"] is not None and f["d"] >= since)
                or f["check_name"] in PRE_WINDOW_CHECKS
                or f["security_id"] in backfill
            )
            state = "closed" if covered else f["state"]
        out.append({**f, "state": state})
    return out


def _sql_new_flags(
    db: Database, after: Optional[dt.datetime], until: Optional[dt.datetime]
) -> Optional[int]:
    """The SQL successor flags written between two runs' starts."""
    if after is None or until is None:
        return None
    return int(
        db.fetchval(
            """
            SELECT count(*) FROM ops.data_quality_flag
             WHERE check_name = ANY(%s) AND detected_at > %s AND detected_at <= %s
            """,
            (list(SUCCESSORS), after, until),
        )
        or 0
    )


# ---------------------------------------------------------------------------
# The night
# ---------------------------------------------------------------------------


def previous_shadow_file(path: Path) -> Optional[Path]:
    """The newest shadow file dated before ``path``'s, in the same directory."""
    as_of = _date(Path(path).stem)
    if as_of is None:
        return None
    earlier = [
        p
        for p in Path(path).parent.glob("*.jsonl")
        if (d := _date(p.stem)) is not None and d < as_of
    ]
    return max(earlier, key=lambda p: p.stem) if earlier else None


def _in_backfill(r: Report, backfill: dict) -> bool:
    """Whether a report is the backfill pass's: on a security whose whole history
    the run judged, dated before the window."""
    before = _date(backfill.get("before"))
    ids = set(backfill.get("security_ids") or ())
    return bool(before is not None and r.security_id in ids and r.first < before)


def compare(
    db: Database,
    shadow_file: Path,
    *,
    labels: Optional[Sequence[Label]] = None,
    slack_days: int = DEFAULT_SLACK_DAYS,
    labels_ignore_time: bool = False,
    verified_only: bool = False,
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
    backfill: dict = {}
    snapshot: Optional[dict] = None
    params = (night or {}).get("params") or {}
    if night is not None:
        window_from = _date(night["since"]) or night["window_from"]
        state_at = night["started_at"]
        backfill = params.get("backfill") or {}
        backfilled = list(backfill.get("security_ids") or [])
        snapshot = (
            params.get("holding") if isinstance(params.get("holding"), dict) else None
        )
    if reports:
        window_from = window_from or min(r.first for r in reports)
        window_to = window_to or max(r.last for r in reports)
    slack = dt.timedelta(days=slack_days)

    sql_flags: list[dict] = []
    if window_from and window_to:
        sql_flags = _sql_flags(
            db, window_from - slack, window_to + slack, backfilled, as_of, state_at
        )
        if snapshot is not None:
            sql_flags = _apply_snapshot(sql_flags, snapshot, backfilled)
    in_window = [f for f in sql_flags if not f["pre_window"]]
    pre_window = [f for f in sql_flags if f["pre_window"]]
    sql_by_security: dict[int, list[dict]] = {}
    for f in in_window:
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
        holding_source=(
            "snapshot"
            if snapshot is not None
            else "now" if night is None else "recomputed"
        ),
        backfill_securities=len(backfilled),
        keys={r.key for r in reports},
    )
    out.conditions = len(out.keys)
    index = None
    if labels is not None:
        used = applicable(
            labels,
            state_at,
            ignore_time=labels_ignore_time,
            verified_only=verified_only,
        )
        out.labels_used = len(used)
        out.labels_time_scoped = not labels_ignore_time and state_at is not None
        out.labels_verified_only = verified_only
        index = LabelIndex(used)
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
        if index is not None:
            outcome = index.judge(r, slack_days)
            out.precision_warn.add(outcome)
            if r.severity == "error":
                out.precision_error.add(outcome)
            out.precision_by_code.setdefault(r.code, Precision()).add(outcome)
            out.outcomes.setdefault(r.key, (outcome, r.severity, r.code))
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
        for f in in_window:
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

    # Conditions that started before the window and still held at the run: a stale
    # feed reported by a successor report on that security anywhere from the flag's
    # date on (dorq keys DQ304 on the first missing session, or later).
    for check in PRE_WINDOW_CHECKS:
        held = [
            f
            for f in pre_window
            if f["check_name"] == check and f["state"] == "holding"
        ]
        out.pre_window[check] = {
            "holding": len(held),
            "also_dorq": sum(
                1
                for f in held
                if any(
                    r.code.startswith(SUCCESSORS[check]) and r.last >= f["d"] - slack
                    for r in by_security.get(f["security_id"], ())
                )
            ),
        }

    out.new_per_night = _new_per_night(
        db, shadow_file, reports, as_of, state_at, backfill
    )
    return out


def _new_per_night(
    db: Database,
    shadow_file: Path,
    reports: Sequence[Report],
    as_of: Optional[dt.date],
    state_at: Optional[dt.datetime],
    backfill: dict,
) -> Optional[dict]:
    """New conditions against the previous night, both engines (C1). None when there
    is no previous shadow file to compare with."""
    previous = previous_shadow_file(Path(shadow_file))
    if previous is None:
        return None
    before = {
        dorq_engine.condition_key(x)
        for x in dorq_engine.read_shadow(previous)
        if "check_name" in x
    }
    new = {}
    for r in reports:
        if r.key not in before:
            new.setdefault(r.key, r)
    history = sum(1 for r in new.values() if _in_backfill(r, backfill))
    prev_run = dorq_engine.night_run(
        db, dorq_engine.WATERMARK_SHADOW, _date(previous.stem)
    )
    sql = _sql_new_flags(db, (prev_run or {}).get("started_at"), state_at)
    return {
        "previous_file": str(previous),
        "dorq": len(new),
        "dorq_backfill": history,
        "sql": sql,
        "change": (len(new) - sql) / sql if sql else None,
    }


def latest_shadow_file(shadow_dir: str) -> Optional[Path]:
    files = sorted(Path(shadow_dir).glob("*.jsonl"))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Several nights
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    """Several nights, as distinct conditions: the shadow period, not one night.

    A condition reported on several nights counts once, judged as the night that
    first reported it judged it: that is the report the queue would have written
    (a later night finds it open and writes nothing).
    """

    nights: list[Comparison]
    conditions: int = 0
    precision_warn: Precision = field(default_factory=Precision)
    precision_error: Precision = field(default_factory=Precision)
    precision_by_code: dict[str, Precision] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "nights": [n.as_dict() for n in self.nights],
            "all": {
                "conditions": self.conditions,
                "precision": (
                    {
                        "warn": self.precision_warn.as_dict(),
                        "error": self.precision_error.as_dict(),
                        "by_code": {
                            c: v.as_dict() for c, v in self.precision_by_code.items()
                        },
                    }
                    if any(n.labelled for n in self.nights)
                    else None
                ),
            },
        }


def summarize(nights: Sequence[Comparison]) -> Summary:
    """Distinct conditions over ``nights``, each judged by the first night that
    reported it."""
    ordered = sorted(nights, key=lambda c: Path(c.shadow_file).stem)
    first: dict[tuple, tuple[str, str, str]] = {}
    for c in ordered:
        for key, outcome in c.outcomes.items():
            first.setdefault(key, outcome)
    out = Summary(nights=list(ordered))
    out.conditions = len(set().union(*(c.keys for c in ordered)))
    for outcome, severity, code in first.values():
        out.precision_warn.add(outcome)
        if severity == "error":
            out.precision_error.add(outcome)
        out.precision_by_code.setdefault(code, Precision()).add(outcome)
    out.precision_by_code = dict(sorted(out.precision_by_code.items()))
    return out


# ---------------------------------------------------------------------------
# dorq-eval, for the same night and labels
# ---------------------------------------------------------------------------

_EVAL_COLUMNS = ("series", "date", "end_date", "code", "severity", "p_error", "message")


def run_dorq_eval(
    dorq_eval: str,
    shadow_file: Path,
    labels: Sequence[Label],
    started_at: Optional[dt.datetime],
    *,
    ignore_time: bool = False,
    verified_only: bool = False,
) -> dict:
    """dorq-eval's figures for the same night and the same labels.

    dorq-eval reads ``dorq check --format csv``, not the fafnir rows, so the
    shadow file is written out in that shape; and the labels that apply to the
    night (time scope, ``verified_only``) in dorq's own format. Returns its per-code
    precision (warn and above) and its error row, parsed from its markdown, or
    ``{"error": ...}`` when it could not run.

    Two differences are by design and do not count as disagreement: dorq-eval's
    "warn" row is warn only (compare's is warn and above, so it is not compared),
    and it reads no ``members`` on a ``*`` label.
    """
    used = applicable(
        labels, started_at, ignore_time=ignore_time, verified_only=verified_only
    )
    with tempfile.TemporaryDirectory(prefix="fafnir-eval-") as tmp:
        results = Path(tmp) / "results.csv"
        label_path = Path(tmp) / "labels.jsonl"
        with results.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(_EVAL_COLUMNS)
            for rec in dorq_engine.read_shadow(shadow_file):
                r = Report.of(rec)
                if r is None:
                    continue
                end = (rec.get("detail") or {}).get("end_date") or ""
                writer.writerow(
                    [
                        "" if r.security_id is None else str(r.security_id),
                        r.first.isoformat(),
                        str(end)[:10],
                        r.code,
                        r.severity,
                        "" if r.p_error is None else r.p_error,
                        r.message,
                    ]
                )
        with label_path.open("w", encoding="utf-8") as fh:
            for lab in used:
                obj = {
                    "series": lab.series,
                    "first": lab.first.isoformat(),
                    "last": lab.last.isoformat(),
                    "class": lab.label_class,
                    "kind": lab.kind,
                }
                if lab.expect:
                    obj["expect"] = "|".join(lab.expect)
                if lab.label_class == "market_fact":
                    obj["codes"] = lab.codes
                fh.write(json.dumps(obj) + "\n")
        try:
            done = subprocess.run(
                [dorq_eval, "--labels", str(label_path), "--results", str(results)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": f"{dorq_eval}: {exc}"}
    if done.returncode != 0:
        return {"error": f"exit {done.returncode}: {done.stderr.strip()[-500:]}"}
    return parse_dorq_eval(done.stdout)


def _table_rows(markdown: str, heading: str) -> list[list[str]]:
    rows: list[list[str]] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().startswith(heading)
            continue
        if inside and line.startswith("|") and not line.startswith("|---"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:] if rows else []  # less the header


def _number(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def parse_dorq_eval(markdown: str) -> dict:
    """dorq-eval's per-check and by-severity tables, from its markdown report."""
    by_code = {}
    for row in _table_rows(markdown, "Per check"):
        if len(row) < 8:
            continue
        by_code[row[0]] = {
            "right": int(row[2]),
            "other_fault": int(row[3]),
            "false": int(row[4]),
            "unlabelled": int(row[5]),
            "precision": _number(row[6]),
            "fault_precision": _number(row[7]),
        }
    by_severity = {}
    for row in _table_rows(markdown, "By severity"):
        if len(row) >= 4:
            by_severity[row[0]] = {
                "reported": int(row[1]),
                "precision": _number(row[2]),
                "fault_precision": _number(row[3]),
            }
    return {"by_code": by_code, "by_severity": by_severity}


def eval_disagreements(c: Comparison, ev: dict) -> list[str]:
    """Where dorq-eval and compare judged the same night differently."""
    if "error" in ev:
        return []
    out = []
    for code, p in c.precision_by_code.items():
        theirs = ev["by_code"].get(code)
        mine = (p.right, p.other, p.false)
        if theirs is None:
            if p.judged:
                out.append(f"{code}: compare judged {p.judged}, dorq-eval has no row")
            continue
        if mine != (theirs["right"], theirs["other_fault"], theirs["false"]):
            out.append(
                f"{code}: compare {mine[0]}/{mine[1]}/{mine[2]}, dorq-eval "
                f"{theirs['right']}/{theirs['other_fault']}/{theirs['false']}"
                " (right/other/false)"
            )
    err = ev["by_severity"].get("error")
    mine_err = c.precision_error.precision
    if err is not None and mine_err is not None and err["precision"] is not None:
        if abs(err["precision"] - mine_err) > 0.0005:
            out.append(
                f"error precision: compare {mine_err:.3f}, dorq-eval "
                f"{err['precision']:.3f}"
            )
    return out


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.3f}"


def _precision_lines(
    warn: Precision, error: Precision, by_code: dict[str, Precision]
) -> list[str]:
    lines = [
        "            right other false unlabelled  strict  fault",
    ]
    for name, p in (("warn+", warn), ("error", error), *by_code.items()):
        lines.append(
            f"  {name:<9} {p.right:>5} {p.other:>5} {p.false:>5} {p.unlabelled:>10}"
            f"  {_pct(p.precision):>6}  {_pct(p.fault_precision):>5}"
        )
    return lines


def format_comparison(c: Comparison) -> str:
    def share(n: int, of: int) -> str:
        return f"{n:>6} ({n / of:.0%})" if of else f"{n:>6}"

    if c.state_at:
        how = {
            "snapshot": "the run's own snapshot",
            "recomputed": "recomputed now as of the run's start (no snapshot: may "
            "drift as late bars arrive)",
        }.get(c.holding_source, "")
        stamp = f"as the run found them, {c.state_at:%Y-%m-%d %H:%M %Z}; {how}"
    else:
        stamp = "as they stand now (no run recorded for this file)"
    reports = f"{c.dorq_reports} reports"
    if c.conditions and c.conditions != c.dorq_reports:
        reports += f" ({c.conditions} conditions)"
    lines = [
        f"Shadow file : {c.shadow_file}",
        f"Window      : {c.window_from or '?'} .. {c.window_to or '?'}",
        f"dorq        : {reports}",
    ]
    if c.backfill_securities:
        lines.append(
            f"Backfill    : the whole history of {c.backfill_securities} securities,"
            " new or rewritten; their SQL flags are compared at every date"
        )
    lines += [
        f"SQL flags   : {stamp}",
        "",
    ]
    n = c.new_per_night
    if n is None:
        lines.append("New flags   : n/a (no previous shadow night to compare with)")
    else:
        sql = "?" if n["sql"] is None else str(n["sql"])
        change = (
            f"change {n['change']:+.0%}" if n["change"] is not None else "change n/a"
        )
        lines.append(
            f"New flags   : dorq {n['dorq']} (history pass {n['dorq_backfill']}) vs "
            f"SQL {sql} ({change}; the bar is {NEW_FLAGS_BAR:+.0%})"
        )
    lines += [
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
    ]
    for check, e in c.pre_window.items():
        lines.append(
            f"  {check} before the window, still holding at the run: "
            f"{share(e['also_dorq'], e['holding']).strip()} of {e['holding']} "
            "also reported by dorq"
        )
    lines += ["", "Not reported by dorq, by why:"]
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
        w = c.precision_warn
        scope = []
        if c.labels_time_scoped:
            scope.append("those that applied when the run started")
        if c.labels_verified_only:
            scope.append("verified only")
        lines += [
            "",
            f"Against {c.labels_used} labels"
            + (f" ({'; '.join(scope)})" if scope else "")
            + ":",
            f"  judged {w.judged} of {w.reports} reports"
            + (f" ({w.judged / w.reports:.0%})" if w.reports else ""),
        ]
        lines += _precision_lines(w, c.precision_error, c.precision_by_code)
        lines.append(
            "  strict = right / judged; fault = (right + other fault) / judged."
        )
        lines.append("  Cutover needs 0.85 at warn+ and 0.95 at error (dorq plan §8).")
    if c.dorq_eval is not None:
        lines += ["", "dorq-eval on the same night and labels:"]
        if "error" in c.dorq_eval:
            lines.append(f"  did not run: {c.dorq_eval['error']}")
        else:
            err = c.dorq_eval["by_severity"].get("error") or {}
            lines.append(
                f"  error: strict {_pct(err.get('precision'))}, fault "
                f"{_pct(err.get('fault_precision'))}"
            )
            diffs = c.dorq_eval.get("disagreements") or []
            lines.append(
                "  agrees with compare, code by code"
                if not diffs
                else "  DISAGREES with compare:"
            )
            lines += [f"    {d}" for d in diffs]
    return "\n".join(lines)


def format_summary(s: Summary) -> str:
    lines = [
        f"All {len(s.nights)} nights, as distinct conditions "
        f"({s.conditions}), each judged by the night that first reported it:",
    ]
    if not any(n.labelled for n in s.nights):
        lines.append("  (no labels: nothing to judge)")
        return "\n".join(lines)
    w = s.precision_warn
    lines.append(
        f"  judged {w.judged} of {w.reports} conditions"
        + (f" ({w.judged / w.reports:.0%})" if w.reports else "")
    )
    lines += _precision_lines(w, s.precision_error, s.precision_by_code)
    return "\n".join(lines)
