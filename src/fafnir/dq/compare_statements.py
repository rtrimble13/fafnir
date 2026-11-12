"""`fafnir dq compare --statements`: a statements night of dorq against the SQL
fundamentals checks and the labels (dorq plan §5.5, §6; DR-0912).

The statements run (:mod:`fafnir.dq.dorq_statements`) judges every current
version every night, so a night's file is dorq's standing view of the statements,
as the SQL fundamentals checks' open flags are theirs. A night's comparison
measures what the plan's cutover criteria (§6) ask of it:

  * **Overlap.** Of the thirteen SQL checks dorq is to succeed (§3), the flags
    open when the night's run started, and how many a successor report also
    covers. A report covers a flag when it is on the same security and of a
    successor code, and agrees with the flag's key on what the key names: the
    statement, the period (period type and fiscal date), the fiscal year, the
    span, the identity or the tie (:data:`SUCCESSORS`). And per dorq code: its
    reports, of which an open SQL flag covers, and how many no SQL check made.
  * **Standing.** The conditions at warn and above in dorq's file against the
    SQL checks' open flags at warn and above. The bar is 30% or less.
  * **New flags per night.** The conditions in this night's file absent from the
    previous night's, against the SQL flags written between the two runs' starts.
    The bar is 70% fewer.
  * **As written.** Both figures again on what the statements ingest would write
    to the queue: less the reports an SQL flag accepted before the run carries
    (:class:`AcceptedPredecessors`), which the ingest withholds. The queue judged
    those conditions already, under the SQL check's name.
  * **Precision.** With labels, the share of reports that were right (strict) or
    some fault, at warn and above, at error, and per code, judged as dorq-eval
    judges a statements label: on the same security, its statement, period type,
    version and item agreeing where both name one, and its fiscal dates
    overlapping the report's within the slack. Only labels about statements
    judge (:attr:`fafnir.dq.compare.Label.is_statements`).
  * **Recall, both engines'.** With labels, of the confirmed errors (the
    ``data_error`` statements labels on one security that apply at the night),
    how many dorq found, a report at warn and above as precision matches it, and
    how many the thirteen SQL checks found: a flag of theirs at warn and above,
    open or closed, detected by the night's start, placed as `dq export-labels`
    places it (:func:`fafnir.dq.labels.place_statement_flags`). The set pools both
    engines' confirmations, the EDGAR sample of dorq's reports and the closures and
    corrections of the SQL checks' flags, so neither engine's recall is 1 by
    construction. ``found`` needs a code the label expects (an SQL flag: its
    successor's); ``any`` takes any code. The bar: dorq's at least the SQL checks'.

Reads only.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from fafnir.db.connection import Database
from fafnir.dq import dorq as engine
from fafnir.dq import dorq_statements
from fafnir.dq.compare import (
    DEFAULT_SLACK_DAYS,
    NEW_FLAGS_BAR,
    Label,
    Precision,
    _date,
    applicable,
    statement_name,
)

#: The bar for the standing count at warn and above: dorq's at most this share of
#: the SQL checks' (plan §6).
STANDING_BAR = 0.30


@dataclass
class Recall:
    """Confirmed errors, and how many each engine found: with a code the label
    expects (``dorq``, ``sql``), or with any code (``dorq_any``, ``sql_any``)."""

    labels: int = 0
    dorq: int = 0
    dorq_any: int = 0
    sql: int = 0
    sql_any: int = 0

    def add(self, dorq: bool, dorq_any: bool, sql: bool, sql_any: bool) -> None:
        self.labels += 1
        self.dorq += dorq
        self.dorq_any += dorq_any
        self.sql += sql
        self.sql_any += sql_any

    def _of(self, n: int) -> Optional[float]:
        return n / self.labels if self.labels else None

    def as_dict(self) -> dict:
        return {
            "labels": self.labels,
            "dorq": {
                "found": self.dorq,
                "any": self.dorq_any,
                "recall": self._of(self.dorq),
                "recall_any": self._of(self.dorq_any),
            },
            "sql": {
                "found": self.sql,
                "any": self.sql_any,
                "recall": self._of(self.sql),
                "recall_any": self._of(self.sql_any),
            },
        }


@dataclass(frozen=True)
class Rule:
    """How a successor report covers one SQL check's flag.

    ``codes``: the dorq codes that succeed it. ``match``: what of the flag's key
    the report must agree with -- ``period`` (its period type and fiscal date),
    ``fiscal_year``, ``fiscal_year_or_before`` (that year or the one before: a
    trailing four broken in one year can miss a quarter of the last),
    ``span`` (a fiscal date from its ``from`` to its ``to``), ``currency`` or
    ``security`` alone. ``statement``: ``"key"`` (the key's), ``"table"`` (the
    flag's table's), a statement name, or None for any. ``names``: the
    identities (DQ903) or ties (DQ904) that are the SQL check's, by
    ``priors/fafnir-statements.toml``'s names.
    """

    codes: tuple[str, ...]
    match: str
    statement: Optional[str] = None
    names: tuple[str, ...] = ()


#: The thirteen SQL checks dorq succeeds at the statements cutover (plan §3).
#: ``fundamentals_unmapped_fields`` and the loader's own flags stay (§3.7).
SUCCESSORS: dict[str, Rule] = {
    "fundamentals_version_chain": Rule(("DQ901",), "period", "key"),
    "fundamentals_negative_impossible": Rule(("DQ902",), "period", "key"),
    "balance_sheet_identity": Rule(
        ("DQ903",), "period", "balance_sheet", ("balance_sheet",)
    ),
    "cash_flow_tie": Rule(("DQ903",), "period", "cash_flow", ("cash_flow",)),
    "cash_balance_tie": Rule(("DQ904",), "period", None, ("cash",)),
    "fundamentals_suspicious_zero": Rule(("DQ915",), "period", "key"),
    "fundamentals_ytd_suspect": Rule(("DQ916",), "fiscal_year", "table"),
    "fundamentals_implausible_filing_lag": Rule(("DQ931",), "period"),
    "fundamentals_gap": Rule(("DQ941",), "span"),
    "fundamentals_ttm_incomplete": Rule(("DQ941",), "fiscal_year_or_before"),
    "fundamentals_stale": Rule(("DQ942",), "security"),
    "fundamentals_coverage": Rule(("DQ943",), "security"),
    "fundamentals_currency_mismatch": Rule(("DQ944",), "currency"),
}

_SEVERE = ("warn", "error")


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StatementReport:
    """One dorq statements report, reduced to what matching needs."""

    security_id: Optional[int]
    code: str
    check_name: str
    severity: str
    first: dt.date
    last: dt.date
    statement: Optional[str] = None
    period_type: Optional[str] = None
    fiscal_year: Optional[int] = None
    version_no: Optional[int] = None
    item: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    members: tuple[int, ...] = ()
    key: tuple = ()

    @classmethod
    def of(cls, record: dict) -> Optional["StatementReport"]:
        detail = record.get("detail") or {}
        key = record.get("record_key") or {}
        first = _date(key.get("fiscal_date")) or engine.report_date(record)
        if first is None:
            return None
        inner = detail.get("detail")
        inner = inner if isinstance(inner, dict) else {}
        sid = record.get("security_id")
        return cls(
            security_id=_int(sid),
            code=engine.code_of(record),
            check_name=record.get("check_name", ""),
            severity=record.get("severity", "warn"),
            first=first,
            last=max(first, _date(detail.get("end_date")) or first),
            statement=statement_name(key.get("statement") or detail.get("statement")),
            period_type=key.get("period_type") or detail.get("period_type"),
            fiscal_year=_int(detail.get("fiscal_year")),
            version_no=_int(key.get("version_no") or detail.get("version_no")),
            item=key.get("item") or detail.get("item"),
            name=key.get("identity") or key.get("tie"),
            currency=key.get("currency") or inner.get("reported_currency"),
            members=(
                tuple(engine.member_ids(inner.get("members"))) if sid is None else ()
            ),
            key=engine.condition_key(record),
        )


def _flag_statement(rule: Rule, flag: dict) -> Optional[str]:
    if rule.statement == "key":
        return statement_name((flag["record_key"] or {}).get("statement"))
    if rule.statement == "table":
        return statement_name(flag["table_name"])
    return rule.statement


def covers(rule: Rule, flag: dict, r: StatementReport) -> bool:
    """Whether successor report ``r`` covers SQL ``flag`` under ``rule``."""
    if r.security_id is None or r.security_id != flag["security_id"]:
        return False
    if r.code not in rule.codes:
        return False
    statement = _flag_statement(rule, flag)
    if statement and r.statement and statement != r.statement:
        return False
    if rule.names and r.name not in rule.names:
        return False
    key = flag["record_key"] or {}
    if rule.match == "period":
        return r.period_type == key.get("period_type") and r.first == _date(
            key.get("fiscal_date")
        )
    if rule.match in ("fiscal_year", "fiscal_year_or_before"):
        year = _int(key.get("fiscal_year"))
        if year is None or r.fiscal_year is None:
            return False
        years = (year - 1, year) if rule.match == "fiscal_year_or_before" else (year,)
        return r.fiscal_year in years
    if rule.match == "span":
        lo, hi = _date(key.get("from")), _date(key.get("to"))
        return lo is not None and hi is not None and lo <= r.first <= hi
    if rule.match == "currency":
        wanted = key.get("reported_currency")
        return not wanted or not r.currency or r.currency.upper() == wanted.upper()
    return rule.match == "security"


#: The SQL checks one flag of which names several items of its period in its
#: ``detail``, and how to read them: such a flag covers only a report on one of its
#: items (or, naming none, the whole period).
ITEMIZED: dict[str, Callable[[dict], set]] = {
    # {"items": [column, ...]}
    "fundamentals_suspicious_zero": lambda d: set(d.get("items") or ()),
    # {column: the negative value, ...}
    "fundamentals_negative_impossible": lambda d: set(d),
}


def accepted_covers(flag: dict, r: StatementReport) -> bool:
    """Whether accepted SQL ``flag`` carries report ``r`` into the queue's
    acceptance: it covers the report (:func:`covers`, under its check's rule) and,
    for an :data:`ITEMIZED` check that names items, the report is on one of them.
    A report on no item (the whole row) is carried only by a flag naming none:
    accepting two negative items is no verdict on the row's others."""
    rule = SUCCESSORS.get(flag["check_name"])
    if rule is None or not covers(rule, flag, r):
        return False
    items_of = ITEMIZED.get(flag["check_name"])
    if items_of is None:
        return True
    items = items_of(flag.get("detail") or {})
    return not items or r.item in items


def _slim(flag: dict) -> dict:
    """An SQL flag as :func:`accepted_covers` reads it, its repeated strings (the
    check, the table, the key's names, period types and dates) interned so that
    164k flags share them."""

    def intern(v):
        return sys.intern(v) if isinstance(v, str) else v

    key = flag.get("record_key") or {}
    return {
        "check_name": intern(flag["check_name"]),
        "security_id": int(flag["security_id"]),
        "table_name": intern(flag["table_name"]),
        "record_key": {intern(k): intern(v) for k, v in key.items()},
        "detail": flag.get("detail") if flag["check_name"] in ITEMIZED else None,
    }


class AcceptedPredecessors:
    """The accepted flags of the thirteen SQL checks dorq succeeds, by security:
    the queue's verdict on a condition, which carries to the successor report that
    covers it (:func:`accepted_covers`).

    dorq's write guard (:func:`fafnir.dq.dorq.ingest`) knows only dorq's own
    ``check_name`` and ``record_key``. On 2026-11-11 175,748 of a statements night's
    reports were on conditions the queue had accepted under an SQL check's name
    (about 164k flags, SF1 as published), and at the cutover each would have
    entered the queue again. The statements ingest withholds them through
    :meth:`withhold`, and ``dq compare --statements`` counts what it would write.

    Each flag is held as the few fields matching needs (no ``detail`` but an
    itemized check's), its strings interned (:func:`_slim`): the ~164k accepted
    on the host cost about 80 MB.
    """

    def __init__(self, flags: Iterable[dict]):
        self.by_security: dict[int, list[dict]] = {}
        for f in flags:
            if f["security_id"] is not None:
                self.by_security.setdefault(int(f["security_id"]), []).append(_slim(f))
        self.flags = sum(len(v) for v in self.by_security.values())
        self.withheld = 0

    @classmethod
    def load(
        cls, db: Database, at: Optional[dt.datetime] = None
    ) -> "AcceptedPredecessors":
        """The flags accepted by ``at`` (by now when None), read a row at a time:
        only their slim form is ever held."""
        from psycopg.rows import dict_row

        with db.conn.cursor(row_factory=dict_row) as cur:
            return cls(
                cur.stream(
                    """
                SELECT check_name, security_id, table_name, record_key,
                       CASE WHEN check_name = ANY(%(itemized)s) THEN detail END
                           AS detail
                  FROM ops.data_quality_flag
                 WHERE check_name = ANY(%(checks)s)
                   AND security_id IS NOT NULL
                   AND accepted_at IS NOT NULL
                   AND accepted_at <= coalesce(%(t)s, now())
                """,
                    {"checks": list(SUCCESSORS), "itemized": list(ITEMIZED), "t": at},
                )
            )

    def carries(self, r: StatementReport) -> bool:
        if r.security_id is None:
            return False
        return any(
            accepted_covers(f, r) for f in self.by_security.get(r.security_id, ())
        )

    def withhold(self, records: Iterable[dict]) -> Iterator[dict]:
        """``records`` less those an accepted SQL flag carries, one at a time;
        :attr:`withheld` counts what was left out."""
        for record in records:
            r = StatementReport.of(record)
            if r is not None and self.carries(r):
                self.withheld += 1
                continue
            yield record


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _same(label_value, report_value) -> bool:
    return label_value is None or report_value is None or label_value == report_value


def _label_overlaps(r: StatementReport, lab: Label, slack: int) -> bool:
    if not (
        _same(lab.statement, r.statement)
        and _same(lab.period_type, r.period_type)
        and _same(lab.version_no, r.version_no)
        and _same(lab.item, r.item)
    ):
        return False
    if lab.series == "*":
        if lab.members:
            if r.security_id is not None or not set(lab.members) & set(r.members):
                return False
        elif lab.is_fault and r.security_id is not None:
            return False
    elif r.security_id is None or lab.series != str(r.security_id):
        return False
    pad = dt.timedelta(days=slack)
    return r.first <= lab.last + pad and lab.first <= r.last + pad


def judge(r: StatementReport, labels: Sequence[Label], slack: int) -> str:
    """``right``, ``other`` (a different fault), ``false`` or ``unlabelled``, as
    dorq-eval judges a statements report."""
    right = other = false = False
    for lab in labels:
        if lab.is_fault and _label_overlaps(r, lab, slack):
            if not lab.expect or r.code in lab.expect:
                right = True
            else:
                other = True
        elif (
            not lab.is_fault
            and _label_overlaps(r, lab, 0)
            and r.code.startswith(lab.codes)
        ):
            false = True
    if right:
        return "right"
    if other:
        return "other"
    return "false" if false else "unlabelled"


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass
class StatementsComparison:
    shadow_file: str
    as_of: Optional[dt.date]
    run_id: Optional[int] = None
    state_at: Optional[dt.datetime] = None
    dorq_version: str = ""
    reports: int = 0
    conditions: int = 0
    # per SQL check: flags open at the run's start, and of those the ones a
    # successor report covers; and the same at warn and above
    by_sql_check: dict[str, dict] = field(default_factory=dict)
    # per dorq code: reports, at warn and above, covered by an open SQL flag of a
    # check it succeeds, and those no SQL check made
    by_code: dict[str, dict] = field(default_factory=dict)
    # reports covered by an SQL flag accepted before the run: the queue judged
    # the condition real or a fact
    on_accepted: int = 0
    # reports the statements ingest withholds: an accepted SQL flag carries them
    # (accepted_covers, which adds the itemized checks' item rule to covers)
    withheld: int = 0
    # conditions at warn and above: dorq's against the SQL checks' open flags
    standing: dict = field(default_factory=dict)
    new_per_night: Optional[dict] = None
    labelled: bool = False
    labels_used: int = 0
    labels_time_scoped: bool = False
    labels_verified_only: bool = False
    precision_warn: Precision = field(default_factory=Precision)
    precision_error: Precision = field(default_factory=Precision)
    precision_by_code: dict[str, Precision] = field(default_factory=dict)
    recall: Recall = field(default_factory=Recall)
    #: by the label's kind: where the confirmation came from
    recall_by_kind: dict[str, Recall] = field(default_factory=dict)

    def as_dict(self) -> dict:
        warn = self.precision_warn
        return {
            "shadow_file": self.shadow_file,
            "kind": "statements",
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "run_id": self.run_id,
            "state_at": self.state_at.isoformat() if self.state_at else None,
            "dorq_version": self.dorq_version,
            "dorq_reports": self.reports,
            "conditions": self.conditions,
            "by_sql_check": self.by_sql_check,
            "by_code": self.by_code,
            "on_accepted": self.on_accepted,
            "withheld": self.withheld,
            "standing": self.standing,
            "new_per_night": self.new_per_night,
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
            "recall": (
                {
                    **self.recall.as_dict(),
                    "by_kind": {k: v.as_dict() for k, v in self.recall_by_kind.items()},
                }
                if self.labelled
                else None
            ),
        }


def _sql_flags(db: Database, state_at: Optional[dt.datetime]) -> list[dict]:
    """The SQL successors' flags open at ``state_at`` (now when None), and those
    accepted by then (``accepted``)."""
    return db.fetchall(
        """
        SELECT dq_flag_id, check_name, security_id, table_name, record_key,
               severity,
               CASE WHEN check_name = ANY(%(itemized)s) THEN detail END AS detail,
               accepted_at IS NOT NULL
                   AND accepted_at <= coalesce(%(t)s, now()) AS accepted
          FROM ops.data_quality_flag
         WHERE check_name = ANY(%(checks)s)
           AND detected_at <= coalesce(%(t)s, now())
           AND (resolved_at IS NULL OR resolved_at > coalesce(%(t)s, now())
                OR (accepted_at IS NOT NULL
                    AND accepted_at <= coalesce(%(t)s, now())))
         ORDER BY dq_flag_id
        """,
        {"t": state_at, "checks": list(SUCCESSORS), "itemized": list(ITEMIZED)},
    )


def _written_between(
    db: Database, after: Optional[dt.datetime], until: Optional[dt.datetime]
) -> int:
    return int(
        db.fetchval(
            """
            SELECT count(*) FROM ops.data_quality_flag
             WHERE check_name = ANY(%s) AND detected_at > %s
               AND detected_at <= coalesce(%s, now())
            """,
            (list(SUCCESSORS), after, until),
        )
    )


def _sql_flags_found(
    db: Database, state_at: Optional[dt.datetime], securities: Sequence[int]
) -> list[dict]:
    """Every flag of the thirteen at warn and above on ``securities`` detected by
    ``state_at`` (now when None), open or closed: what the SQL checks found."""
    if not securities:
        return []
    return db.fetchall(
        """
        SELECT dq_flag_id, check_name, security_id, table_name, record_key, detail,
               COALESCE(accepted_at, resolved_at, %(t)s, now()) AS decided_at
          FROM ops.data_quality_flag
         WHERE check_name = ANY(%(checks)s) AND security_id = ANY(%(sids)s)
           AND severity IN ('warn', 'error')
           AND detected_at <= coalesce(%(t)s, now())
         ORDER BY dq_flag_id
        """,
        {"t": state_at, "checks": list(SUCCESSORS), "sids": list(securities)},
    )


def _flag_finds(placed: tuple, lab: Label, slack: int) -> tuple[bool, bool]:
    """Whether a placed SQL flag (:func:`fafnir.dq.labels.place_statement_flags`)
    found confirmed error ``lab``: with a code it expects, and at all. It agrees
    with the label's scope where both name one, and its dates overlap the label's
    within the slack, as a report must."""
    code, scope, first, last = placed
    if not (
        _same(lab.statement, scope.get("statement"))
        and _same(lab.period_type, scope.get("period_type"))
        and _same(lab.version_no, scope.get("version_no"))
        and _same(lab.item, scope.get("item"))
    ):
        return False, False
    pad = dt.timedelta(days=slack)
    if not (first <= lab.last + pad and lab.first <= last + pad):
        return False, False
    return (not lab.expect or code in lab.expect), True


def _recall(
    db: Database,
    out: "StatementsComparison",
    used: Sequence[Label],
    by_security: dict[int, list[StatementReport]],
    slack: int,
) -> None:
    """Both engines' recall on the confirmed errors among ``used``. ``by_security``
    holds dorq's reports at warn and above on the securities those errors name;
    the pass over the night keeps no other."""
    from fafnir.dq import labels as lb

    errors = [
        lab for lab in used if lab.label_class == "data_error" and lab.series.isdigit()
    ]
    sids = sorted({int(lab.series) for lab in errors})
    placed: dict[int, list[tuple]] = {}
    for flag, where in lb.place_statement_flags(
        db, _sql_flags_found(db, out.state_at, sids)
    ):
        placed.setdefault(int(flag["security_id"]), []).append(where)
    for lab in errors:
        sid = int(lab.series)
        dorq = dorq_any = False
        for r in by_security.get(sid, ()):
            if r.severity in _SEVERE and _label_overlaps(r, lab, slack):
                dorq_any = True
                dorq = dorq or not lab.expect or r.code in lab.expect
        sql = sql_any = False
        for where in placed.get(sid, ()):
            strict, overlaps = _flag_finds(where, lab, slack)
            sql, sql_any = sql or strict, sql_any or overlaps
        out.recall.add(dorq, dorq_any, sql, sql_any)
        out.recall_by_kind.setdefault(lab.kind or "?", Recall()).add(
            dorq, dorq_any, sql, sql_any
        )
    out.recall_by_kind = dict(sorted(out.recall_by_kind.items()))


def latest_shadow_file(settings: engine.DorqSettings) -> Optional[Path]:
    files = sorted(dorq_statements.shadow_dir(settings).glob("*.jsonl"))
    return files[-1] if files else None


def compare(
    db: Database,
    shadow_file: Path,
    *,
    labels: Optional[Sequence[Label]] = None,
    slack_days: int = DEFAULT_SLACK_DAYS,
    labels_ignore_time: bool = False,
    verified_only: bool = False,
) -> StatementsComparison:
    from fafnir.dq.compare import previous_shadow_file

    as_of = _date(Path(shadow_file).stem)
    night = (
        engine.night_run(db, dorq_statements.ENDPOINT_SHADOW, as_of) if as_of else None
    )
    out = StatementsComparison(
        shadow_file=str(shadow_file),
        as_of=as_of,
        run_id=(night or {}).get("ingestion_run_id"),
        state_at=(night or {}).get("started_at"),
        dorq_version=str(((night or {}).get("params") or {}).get("dorq_version") or ""),
        labelled=labels is not None,
    )
    flags = _sql_flags(db, out.state_at)
    open_flags = [f for f in flags if not f["accepted"]]
    flags_by_security: dict[int, list[dict]] = {}
    for f in open_flags:
        flags_by_security.setdefault(f["security_id"], []).append(f)
    # The accepted flags, as the statements ingest's guard holds them: slim, and
    # the rows they came from let go.
    accepted = AcceptedPredecessors(f for f in flags if f["accepted"])
    accepted_by_security = accepted.by_security
    del flags

    index = None
    if labels is not None:
        used = [
            lab
            for lab in applicable(
                labels,
                out.state_at,
                ignore_time=labels_ignore_time,
                verified_only=verified_only,
            )
            if lab.is_statements
        ]
        out.labels_used = len(used)
        out.labels_time_scoped = not labels_ignore_time and out.state_at is not None
        out.labels_verified_only = verified_only
        index = used
    by_series: dict[str, list[Label]] = {}
    star: list[Label] = []
    for lab in index or ():
        (star if lab.series == "*" else by_series.setdefault(lab.series, [])).append(
            lab
        )
    # The securities the confirmed errors name: the only ones whose reports the
    # recall needs after the pass, and only at warn and above.
    error_sids = {
        int(lab.series)
        for lab in index or ()
        if lab.label_class == "data_error" and lab.series.isdigit()
    }

    # One pass over the night's file, a report at a time (#219: the file is dorq's
    # whole standing view, 693 MB on the host on 2026-11-10, and reading it whole
    # cost 6.5 times that and the OOM killer). What the pass keeps is small against
    # the file: the condition keys as hashes, the open flags a report covers by id,
    # the counters, and the severe reports on the securities the recall asks about.
    keys: set[int] = set()
    severe: set[int] = set()
    # the same, less the reports the ingest withholds: the queue's view
    written: set[int] = set()
    written_severe: set[int] = set()
    covered: set[int] = set()
    recall_reports: dict[int, list[StatementReport]] = {}
    for record in engine.read_records(shadow_file):
        r = StatementReport.of(record)
        if r is None:
            continue
        out.reports += 1
        h = _key_hash(r.key)
        keys.add(h)
        entry = out.by_code.setdefault(
            r.code, {"reports": 0, "severe": 0, "also_sql": 0, "dorq_only": 0}
        )
        entry["reports"] += 1
        if r.severity in _SEVERE:
            entry["severe"] += 1
            severe.add(h)
        matched = False
        withheld = False
        if r.security_id is not None:
            for f in flags_by_security.get(r.security_id, ()):
                if covers(SUCCESSORS[f["check_name"]], f, r):
                    covered.add(f["dq_flag_id"])
                    matched = True
            if any(
                covers(SUCCESSORS[f["check_name"]], f, r)
                for f in accepted_by_security.get(r.security_id, ())
            ):
                out.on_accepted += 1
                withheld = accepted.carries(r)
                out.withheld += withheld
            if r.security_id in error_sids and r.severity in _SEVERE:
                recall_reports.setdefault(r.security_id, []).append(r)
        if not withheld:
            written.add(h)
            if r.severity in _SEVERE:
                written_severe.add(h)
        entry["also_sql" if matched else "dorq_only"] += 1
        if index is not None:
            own = by_series.get(str(r.security_id), []) if r.security_id else []
            outcome = judge(r, [*own, *star], slack_days)
            if r.severity in _SEVERE:
                out.precision_warn.add(outcome)
                out.precision_by_code.setdefault(r.code, Precision()).add(outcome)
            if r.severity == "error":
                out.precision_error.add(outcome)
    out.conditions = len(keys)
    out.by_code = dict(sorted(out.by_code.items()))
    out.precision_by_code = dict(sorted(out.precision_by_code.items()))

    for check, rule in SUCCESSORS.items():
        entry = {"codes": list(rule.codes), "flags": 0, "also_dorq": 0}
        entry.update({"severe": 0, "severe_also_dorq": 0})
        for f in open_flags:
            if f["check_name"] != check:
                continue
            matched = f["dq_flag_id"] in covered
            entry["flags"] += 1
            entry["also_dorq"] += matched
            if f["severity"] in _SEVERE:
                entry["severe"] += 1
                entry["severe_also_dorq"] += matched
        out.by_sql_check[check] = entry

    if index is not None:
        _recall(db, out, index, recall_reports, slack_days)

    sql_severe = sum(e["severe"] for e in out.by_sql_check.values())
    out.standing = {
        "dorq": len(severe),
        "sql": sql_severe,
        "ratio": len(severe) / sql_severe if sql_severe else None,
        "bar": STANDING_BAR,
        # what the queue would hold: less the conditions an accepted SQL flag
        # carries, which the statements ingest does not write
        "written": {
            "dorq": len(written_severe),
            "ratio": len(written_severe) / sql_severe if sql_severe else None,
        },
    }

    previous = previous_shadow_file(Path(shadow_file))
    if previous is not None:
        before = set(_condition_hashes(previous))
        new = keys - before
        new_written = len(written - before)
        prev_night = engine.night_run(
            db, dorq_statements.ENDPOINT_SHADOW, _date(previous.stem)
        )
        sql_new = (
            _written_between(db, prev_night["started_at"], out.state_at)
            if prev_night is not None
            else None
        )
        out.new_per_night = {
            "previous": previous.name,
            "dorq": len(new),
            "sql": sql_new,
            "change": (len(new) - sql_new) / sql_new if sql_new else None,
            "bar": NEW_FLAGS_BAR,
            # the new conditions the statements ingest would write
            "written": {
                "dorq": new_written,
                "change": (new_written - sql_new) / sql_new if sql_new else None,
            },
        }
    return out


def _key_hash(key: tuple) -> int:
    """A condition key as one integer, so a night's distinct conditions cost a set
    of ints rather than of tuples holding a JSON string each. ``hash`` is salted
    per process, and both nights of a comparison are hashed in the same one."""
    return hash(key)


def _condition_hashes(shadow_file: Path) -> Iterator[int]:
    """The condition keys of a night's reports, hashed, one at a time."""
    for record in engine.read_records(shadow_file):
        r = StatementReport.of(record)
        if r is not None:
            yield _key_hash(r.key)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _ratio(n: int, d: int) -> str:
    return f"{n}/{d}" + (f" ({100 * n / d:.0f}%)" if d else "")


def _precision_line(label: str, p: Precision, fmt: Callable[[Optional[float]], str]):
    return (
        f"  {label:<14} {p.reports:>5} reports, {p.judged:>4} judged: "
        f"strict {fmt(p.precision)}, fault {fmt(p.fault_precision)}"
    )


def format_comparison(c: StatementsComparison) -> str:
    def fmt(x: Optional[float]) -> str:
        return "n/a" if x is None else f"{x:.3f}"

    head = f"Statements night {c.as_of or '?'}  ({c.shadow_file})"
    lines = [head]
    if c.run_id is not None:
        at = c.state_at.astimezone(dt.timezone.utc) if c.state_at else None
        lines.append(
            f"  run {c.run_id}"
            + (f", started {at:%Y-%m-%d %H:%M} UTC" if at else "")
            + (f", dorq {c.dorq_version}" if c.dorq_version else "")
        )
    else:
        lines.append("  no run recorded for this file: the SQL flags are as of now")
    lines.append(f"  dorq: {c.reports} reports, {c.conditions} conditions")
    lines.append("")
    lines.append("SQL check (open at the run)       flags  also dorq   at warn+  also")
    for check, e in c.by_sql_check.items():
        lines.append(
            f"  {check:<32} {e['flags']:>5}  {_ratio(e['also_dorq'], e['flags']):<14}"
            f" {e['severe']:>5}  {e['severe_also_dorq']:>5}"
            f"   [{', '.join(e['codes'])}]"
        )
    lines.append("")
    lines.append("dorq code    reports  warn+  on an SQL flag  dorq only")
    for code, e in c.by_code.items():
        lines.append(
            f"  {code:<10} {e['reports']:>7} {e['severe']:>6} {e['also_sql']:>15}"
            f" {e['dorq_only']:>10}"
        )
    if c.on_accepted:
        lines.append(
            f"  {c.on_accepted} on an SQL flag the queue accepted, of which the"
            f" ingest withholds {c.withheld}"
        )
    s = c.standing
    lines.append("")
    lines.append(
        f"Standing at warn+: dorq {s['dorq']} against SQL {s['sql']}"
        f" ({_pct(s['ratio'])}; the bar is {_pct(s['bar'])} or less)"
    )
    w = s.get("written")
    if w is not None:
        lines.append(
            f"  as written to the queue: dorq {w['dorq']} ({_pct(w['ratio'])}),"
            " less the conditions an accepted SQL flag carries"
        )
    n = c.new_per_night
    if n is None:
        lines.append("New per night: n/a (no previous night)")
    else:
        lines.append(
            f"New per night: dorq {n['dorq']} against SQL "
            f"{'n/a' if n['sql'] is None else n['sql']} since {n['previous']}"
            + (
                f" ({'+' if n['change'] >= 0 else ''}{100 * n['change']:.0f}%;"
                f" the bar is {100 * n['bar']:.0f}%)"
                if n["change"] is not None
                else ""
            )
        )
        w = n.get("written")
        if w is not None:
            lines.append(
                f"  as written to the queue: dorq {w['dorq']}"
                + (
                    f" ({'+' if w['change'] >= 0 else ''}{100 * w['change']:.0f}%)"
                    if w["change"] is not None
                    else ""
                )
            )
    if c.labelled:
        scope = ("time-scoped" if c.labels_time_scoped else "every label") + (
            ", verified only" if c.labels_verified_only else ""
        )
        lines.append("")
        lines.append(f"Precision ({c.labels_used} statements labels, {scope})")
        lines.append(_precision_line("warn and above", c.precision_warn, fmt))
        lines.append(_precision_line("error", c.precision_error, fmt))
        for code, p in c.precision_by_code.items():
            lines.append(_precision_line(code, p, fmt))
        rc = c.recall
        lines.append("")
        lines.append(
            f"Recall at warn+ ({rc.labels} confirmed errors: the data_error labels on "
            "one security, both engines' confirmations)"
        )
        for name, found, any_ in (
            ("dorq", rc.dorq, rc.dorq_any),
            ("SQL checks", rc.sql, rc.sql_any),
        ):
            lines.append(
                f"  {name:<14} found {_ratio(found, rc.labels):<16}"
                f" with any code {_ratio(any_, rc.labels)}"
            )
        lines.append("  the bar: dorq's recall at least the SQL checks'")
        if c.recall_by_kind:
            lines.append("  by label kind                     labels   dorq    SQL")
            for kind, k in c.recall_by_kind.items():
                lines.append(f"    {kind:<32} {k.labels:>6} {k.dorq:>6} {k.sql:>6}")
    return "\n".join(lines)
