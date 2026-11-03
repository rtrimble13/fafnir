"""A stratified sample of a statements night, to verify on EDGAR (DR-0914).

The statements cutover is judged on a full-history run, sampled and verified
against the filings (dorq plan §6; fafnir#221, step 3): at least 200 reports, and
at least 10 of every code. Each is checked on EDGAR and its verdict stated with
``dq label add --verified``; ``dq compare --statements --labels-from-db
--verified-only`` then measures precision on them.

The sample is drawn from the night's distinct conditions (one per
:func:`fafnir.dq.dorq.condition_key`, as the queue would write them) at warn and
above, about one security each. A DQ951 cohort row is about many securities and
is left out: a cohort is verified as a load, not on a filing.

Every code gets ``per_code`` conditions, or all it has; the rest of ``size`` goes
to the conditions left, in one order. The order is a hash of the seed and the
condition's key, so the same night and seed give the same sample on any host, and
a larger ``size`` keeps a smaller one's rows.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shlex
from dataclasses import dataclass, field
from typing import Optional, Sequence, TextIO

from fafnir.db.connection import Database
from fafnir.dq import dorq as engine
from fafnir.dq.compare_statements import StatementReport

DEFAULT_SIZE = 200
DEFAULT_PER_CODE = 10
_SEVERE = ("warn", "error")
#: A company's 10-K and 10-Q filings, newest first.
EDGAR_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    "&type=10-&dateb=&owner=include&count=40"
)
COLUMNS = (
    "n",
    "code",
    "check_name",
    "severity",
    "p_error",
    "security_id",
    "ticker",
    "company",
    "cik",
    "statement",
    "period_type",
    "fiscal_date",
    "last_date",
    "version_no",
    "item",
    "message",
    "edgar",
    "confirm",
    "refute",
)


@dataclass
class Sample:
    """The sampled conditions, in code order, with what they were drawn from."""

    seed: int
    size: int
    per_code: int
    conditions: int = 0  # at warn and above, about one security
    cohort_conditions: int = 0  # left out
    available: dict[str, int] = field(default_factory=dict)  # per code
    records: list[dict] = field(default_factory=list)

    def sampled(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            code = engine.code_of(r)
            out[code] = out.get(code, 0) + 1
        return out


def _rank(seed: int, key: tuple) -> str:
    return hashlib.sha256(f"{seed}|{json.dumps(key)}".encode()).hexdigest()


def draw(
    records: Sequence[dict],
    *,
    size: int = DEFAULT_SIZE,
    per_code: int = DEFAULT_PER_CODE,
    seed: int = 1,
) -> Sample:
    """The sample of ``records`` (a statements night, fafnir format)."""
    out = Sample(seed=seed, size=size, per_code=per_code)
    conditions: dict[tuple, dict] = {}
    cohorts: set[tuple] = set()
    for r in records:
        if r.get("severity") not in _SEVERE:
            continue
        key = engine.condition_key(r)
        if r.get("security_id") is None:
            cohorts.add(key)
            continue
        conditions.setdefault(key, r)
    out.conditions = len(conditions)
    out.cohort_conditions = len(cohorts)
    ranked = sorted(conditions, key=lambda k: _rank(seed, k))
    by_code: dict[str, list[tuple]] = {}
    for key in ranked:
        by_code.setdefault(engine.code_of(conditions[key]), []).append(key)
    out.available = {code: len(keys) for code, keys in sorted(by_code.items())}
    chosen = {key for keys in by_code.values() for key in keys[:per_code]}
    for key in ranked:
        if len(chosen) >= size:
            break
        chosen.add(key)
    picked = sorted(
        chosen, key=lambda k: (engine.code_of(conditions[k]), _rank(seed, k))
    )
    out.records = [conditions[k] for k in picked]
    return out


def _label_command(r: StatementReport, verdict: str) -> str:
    """``dq label add`` for one report, with its scope: ``confirm`` states it right
    (a data error with the code it reported), ``refute`` states it wrong (a market
    fact on that code). The note is left for what EDGAR showed."""
    args = ["fafnir", "dq", "label", "add", "--security-id", str(r.security_id)]
    if r.last == r.first:
        args += ["--fiscal-date", r.first.isoformat()]
    else:
        args += ["--first", r.first.isoformat(), "--last", r.last.isoformat()]
    for option, value in (
        ("--statement", r.statement),
        ("--period-type", r.period_type),
        ("--version-no", r.version_no),
        ("--item", r.item),
    ):
        if value is not None:
            args += [option, str(value)]
    if verdict == "confirm":
        args += ["--class", "data_error", "--kind", "edgar_confirmed"]
        args += ["--expect", r.code]
    else:
        args += ["--class", "market_fact", "--kind", "edgar_refuted"]
        args += ["--codes", r.code]
    args += ["--verified", "-m", "EDGAR: "]
    return shlex.join(args)


def _securities(db: Database, ids: Sequence[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = db.fetchall(
        "SELECT security_id, primary_symbol, company_name, cik FROM core.security"
        " WHERE security_id = ANY(%s)",
        (list(ids),),
    )
    return {int(r["security_id"]): r for r in rows}


def rows(db: Database, sample: Sample) -> list[dict]:
    """The sample as CSV rows: each report's scope, its message, the company's
    filings on EDGAR, and the two ``dq label add`` commands to state its verdict."""
    reports = [StatementReport.of(r) for r in sample.records]
    known = _securities(
        db, sorted({r.security_id for r in reports if r and r.security_id})
    )
    out = []
    for n, (record, report) in enumerate(zip(sample.records, reports), start=1):
        if report is None:
            continue
        detail = record.get("detail") or {}
        security = known.get(report.security_id) or {}
        cik: Optional[str] = security.get("cik")
        out.append(
            {
                "n": n,
                "code": report.code,
                "check_name": report.check_name,
                "severity": report.severity,
                "p_error": detail.get("p_error"),
                "security_id": report.security_id,
                "ticker": security.get("primary_symbol"),
                "company": security.get("company_name"),
                "cik": cik,
                "statement": report.statement,
                "period_type": report.period_type,
                "fiscal_date": report.first.isoformat(),
                "last_date": (
                    report.last.isoformat() if report.last != report.first else None
                ),
                "version_no": report.version_no,
                "item": report.item,
                "message": detail.get("message"),
                "edgar": EDGAR_URL.format(cik=cik) if cik else None,
                "confirm": _label_command(report, "confirm"),
                "refute": _label_command(report, "refute"),
            }
        )
    return out


def write_csv(out: TextIO, sample_rows: Sequence[dict]) -> None:
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sample_rows:
        writer.writerow({k: "" if row.get(k) is None else row[k] for k in COLUMNS})


def summary(sample: Sample, source: str) -> str:
    """What was drawn from what, per code."""
    sampled = sample.sampled()
    lines = [
        f"{source}: {len(sample.records)} of {sample.conditions} conditions at warn "
        f"and above sampled (seed {sample.seed}; {sample.per_code} a code, "
        f"{sample.size} in all)"
        + (
            f"; {sample.cohort_conditions} cohort rows left out"
            if sample.cohort_conditions
            else ""
        )
    ]
    for code, available in sample.available.items():
        lines.append(f"  {code}  {sampled.get(code, 0):>4} of {available}")
    return "\n".join(lines)
