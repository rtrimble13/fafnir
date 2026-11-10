"""`dq compare --statements` never holds the night (#219).

On 2026-11-10 the first statements night that reached a shadow file on the host
(run 809: 505,793 reports, 693 MB) killed the compare that read it: the file was
read whole into a list, at about 6.5 times its size, on a host with 1.2 GB free.
The compare now streams the night, and the previous one, a report at a time.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from fafnir.db import repository as repo
from fafnir.dq import compare_statements as cst

pytestmark = pytest.mark.integration

T0 = dt.datetime(2024, 12, 30, 3, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2024, 12, 31, 3, 0, tzinfo=dt.timezone.utc)


def _report(sid: int, i: int, severity: str = "warn") -> dict:
    """One DQ903 identity-break report, on its own period so its condition is its own."""
    fiscal = dt.date(1990, 1, 1) + dt.timedelta(days=i)
    return {
        "security_id": sid,
        "table_name": "core.balance_sheet",
        "record_key": {
            "statement": "balance_sheet",
            "period_type": "quarter",
            "fiscal_date": fiscal.isoformat(),
            "version_no": 1,
            "identity": "balance_sheet",
        },
        "check_name": "dorq_identity_break",
        "severity": severity,
        "detail": {
            "code": "DQ903",
            "date": fiscal.isoformat(),
            "statement": "balance_sheet",
            "period_type": "quarter",
            "fiscal_year": fiscal.year,
            "version_no": 1,
            # About what a real report carries (1.4 KB a report on the host).
            "message": (
                "balance sheet identity: assets 100 against liabilities and equity "
                "90, a 10% break on the quarter " + fiscal.isoformat()
            ),
            "detail": {
                "assets": 100,
                "liabilities": 60,
                "equity": 30,
                "liabilities_and_equity": 90,
                "break": 10,
                "break_share": 0.1,
                "reported_currency": "USD",
                "filing": f"0000000000-{i:02d}-000001",
                "row": i,
                "members": "",
                "context": {"prior": {"assets": 95, "liabilities_and_equity": 95}},
            },
        },
    }


def _night(path, sid, count):
    with path.open("w") as fh:
        for i in range(count):
            fh.write(json.dumps(_report(sid, i)) + "\n")
    return path.stat().st_size


def _run_row(db, as_of, started_at):
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, params, window_to, status,"
        " started_at) VALUES ('dorq', 'dq-statements-shadow', '{}', %s, 'success', %s)",
        (as_of, started_at),
    )


def test_the_compare_streams_both_nights(db, tmp_path):
    import tracemalloc

    sid = repo.upsert_security(
        db, primary_symbol="BIG", company_name="Big Filer", asset_type="equity"
    )
    db.commit()
    n, overlap = 20_000, 15_000
    # The previous night holds the first 15,000 of this night's 20,000 conditions.
    _night(tmp_path / "2024-12-30.jsonl", sid, overlap)
    size = _night(tmp_path / "2024-12-31.jsonl", sid, n)
    _run_row(db, dt.date(2024, 12, 30), T0)
    _run_row(db, dt.date(2024, 12, 31), T1)

    tracemalloc.start()
    try:
        out = cst.compare(db, tmp_path / "2024-12-31.jsonl", labels=[])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert out.reports == n and out.conditions == n
    assert out.by_code["DQ903"] == {
        "reports": n,
        "severe": n,
        "also_sql": 0,
        "dorq_only": n,
    }
    assert out.standing["dorq"] == n
    # The previous night held 15,000 of this night's conditions: 5,000 are new.
    assert out.new_per_night["previous"] == "2024-12-30.jsonl"
    assert out.new_per_night["dorq"] == n - overlap
    assert out.new_per_night["sql"] == 0
    assert peak < size / 4, (peak, size)


def test_a_report_covering_an_open_flag_is_counted_once_each_side(db, tmp_path):
    """The pass marks the open flags a report covers; by_sql_check reads the
    marks. One flag, two reports on its period: the flag is covered once, both
    reports are also_sql."""
    sid = repo.upsert_security(
        db, primary_symbol="ONE", company_name="One", asset_type="equity"
    )
    repo.add_dq_flag_once(
        db,
        check_name="balance_sheet_identity",
        security_id=sid,
        table_name="core.balance_sheet",
        record_key={
            "statement": "balance_sheet",
            "period_type": "quarter",
            "fiscal_date": "1990-01-01",
            "version_no": 1,
            "identity": "balance_sheet",
        },
        severity="error",
    )
    db.commit()
    night = tmp_path / "2024-12-31.jsonl"
    with night.open("w") as fh:
        fh.write(json.dumps(_report(sid, 0, "error")) + "\n")
        fh.write(json.dumps({**_report(sid, 0), "severity": "warn"}) + "\n")
        fh.write(json.dumps(_report(sid, 1)) + "\n")
    _run_row(db, dt.date(2024, 12, 31), T1)

    out = cst.compare(db, night)
    entry = out.by_sql_check["balance_sheet_identity"]
    assert (entry["flags"], entry["also_dorq"]) == (1, 1)
    assert (entry["severe"], entry["severe_also_dorq"]) == (1, 1)
    assert out.by_code["DQ903"]["also_sql"] == 2
    assert out.by_code["DQ903"]["dorq_only"] == 1
    assert out.reports == 3 and out.conditions == 2
