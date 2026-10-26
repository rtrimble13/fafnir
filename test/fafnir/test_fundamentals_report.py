"""
`scripts/fundamentals_report.sql` (#92; needs FAFNIR_TEST_DSN).

The script is what the host runs after the SF1 backfill, and its output is posted on
#92 as it is. So it is tested here: every statement runs, the numbers are the ones
the load wrote, and nothing in it is a vendor value. Every row is synthetic.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import fundamentals as fu

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fundamentals_report.sql"
COLUMNS = (
    "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
    "revenue,netinc,netinccmn,netincnci,assets,liabilities,equity,ncfo,shareswa"
).split(",")


def _statements() -> list[str]:
    """The script's SQL statements, as psql sends them: comments and psql's own
    backslash commands left out."""
    out = []
    for chunk in re.split(r";\s*$", SCRIPT.read_text(), flags=re.M):
        code = "\n".join(
            line
            for line in chunk.splitlines()
            if not line.lstrip().startswith(("--", "\\"))
        )
        if code.strip():
            out.append(code)
    return out


def _row(dim, period, filed, label, **values):
    vals = {
        "revenue": "1000",
        "netinc": "100",
        "netinccmn": "100",
        "netincnci": "0",
        "assets": "5000000000",
        "liabilities": "3000000000",
        "equity": "2000000000",
        "ncfo": "200",
        "shareswa": "1000",
        **values,
    }
    head = {
        "ticker": "ZZR",
        "dimension": dim,
        "calendardate": period,
        "date": filed if dim.startswith("AR") else period,
        "reportperiod": period,
        "fiscalperiod": label,
        "lastupdated": "2026-01-02",
    }
    return ",".join({**head, **vals}.get(c, "") for c in COLUMNS) + "\n"


def _load(db, tmp_path):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol="ZZR",
        company_name="ZZR Co",
        exchange_code="NYSE",
        source="sharadar",
    )
    for table in ("stocks", "fundamentals"):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
            " currency) VALUES ('sharadar', %s, '900020', 'ZZR', 'USD')",
            (table,),
        )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key="900020",
        valid_from=date(1990, 1, 2),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    body = (
        ",".join(COLUMNS)
        + "\n"
        + "".join(
            [
                # Holds.
                _row("ARQ", "2020-03-31", "2020-05-01", "2020-Q1"),
                _row("MRQ", "2020-03-31", "", "2020-Q1"),
                # Fails, with noncontrolling interest to explain it.
                _row(
                    "ARQ",
                    "2020-06-30",
                    "2020-08-01",
                    "2020-Q2",
                    equity="1500000000",
                    netincnci="5",
                ),
                # Fails with none; restated income.
                _row(
                    "ARY",
                    "1999-12-31",
                    "2000-03-01",
                    "1999-FY",
                    liabilities="1000000000",
                ),
                _row(
                    "MRY",
                    "1999-12-31",
                    "",
                    "1999-FY",
                    liabilities="1000000000",
                    revenue="990",
                ),
                # Quarantined: a malformed label.
                _row("ARQ", "2020-09-30", "2020-11-01", "2020Q3"),
            ]
        )
    )
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    repo.record_sharadar_file(
        db,
        vendor_table="fundamentals",
        kind="full",
        filters={"years": "full"},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=6,
        columns=None,
        ingestion_run_id=None,
    )
    return fu.load(db, tmp_path, backfill=True, as_of=date(2026, 1, 5))


def test_the_report_counts_what_the_load_wrote(db, tmp_path):
    result = _load(db, tmp_path)
    runs, rows, decades, restated, flags, identity, invariants, sizes = [
        db.fetchall(s) for s in _statements()
    ]
    assert [(r["run"], r["status"], r["rows_quarantined"]) for r in runs] == [
        (result.run_id, "partial", 1)
    ]
    by_table = {r["statement"]: r for r in rows}
    assert by_table["income_statement"]["rows"] == 4  # three periods, one restated
    assert by_table["income_statement"]["later_versions"] == 1
    assert by_table["balance_sheet"]["periods"] == 3
    assert {
        (r["statement"], r["decade"]): (r["filed"], r["observed"], r["estimated"])
        for r in decades
    }[("income_statement", "1990s")] == (1, 1, 0)
    pct = {(r["statement"], r["period_type"]): r["restated_pct"] for r in restated}
    assert pct[("income_statement", "annual")] == 100
    assert pct[("balance_sheet", "quarter")] == 0
    assert {(r["check_name"], r["open_flags"]) for r in flags} >= {
        (fu.MISSING_LABEL, 1),
        (fu.RESTATEMENT, 1),
    }
    (bs,) = identity
    assert (bs["rows"], bs["holds"], bs["fails_with_nci"], bs["fails_without_nci"]) == (
        3,
        1,
        1,
        1,
    )
    assert float(bs["pass_pct_nci_aside"]) == pytest.approx(66.67)
    assert all(
        r["known_before_period_end"] == 0 and r["chain_breaks"] == 0 for r in invariants
    )
    assert len(invariants) == 3 and len(sizes) == 3


def test_the_report_shows_no_vendor_value(db, tmp_path):
    """Every column is a label, a count, a share, a time or a size."""
    _load(db, tmp_path)
    allowed = re.compile(
        r"run|endpoint|status|started|took|rows.*|keys|securities|statement|"
        r"current_rows|superseded|later_versions|periods|decade|filed|observed|"
        r"estimated|period_type|restated(_pct)?|check_name|severity|open_flags|"
        r"incomplete|holds|fails_with(out)?_nci|pass_pct_nci_aside|"
        r"known_before_period_end|chain_breaks|total|heap|indexes|toast"
    )
    for statement in _statements():
        for row in db.fetchall(statement):
            assert all(allowed.fullmatch(c) for c in row), list(row)


RESET = Path(__file__).resolve().parents[2] / "scripts" / "reset_data.sh"


@pytest.mark.skipif(shutil.which("psql") is None, reason="needs psql")
def test_reset_scope_fundamentals_undoes_the_load_and_nothing_else(db, tmp_path):
    _load(db, tmp_path)
    repo.set_watermark(
        db, "fmp", "historical-price-eod/non-split-adjusted", date(2020, 1, 2)
    )

    def run(*args):
        return subprocess.run(
            [str(RESET), "--scope", "fundamentals", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "FAFNIR_DSN": db.dsn},
            check=False,
        )

    preview = run()
    assert preview.returncode == 0, preview.stderr
    lines = {
        x.split()[0]: x.split()[-1]
        for x in preview.stdout.splitlines()
        if "core." in x or "ops.load_watermark" in x
    }
    assert lines["core.income_statement"] == "4"
    assert "ops.load_watermark (SF1)" in preview.stdout
    assert "DRY RUN" in preview.stdout
    assert db.fetchval("SELECT count(*) FROM core.income_statement") == 4

    done = run("--yes")
    assert done.returncode == 0, done.stderr
    for table in repo.STATEMENT_TABLES:
        assert db.fetchval(f"SELECT count(*) FROM {table}") == 0
    endpoints = {
        r["endpoint"] for r in db.fetchall("SELECT endpoint FROM ops.load_watermark")
    }
    assert endpoints == {"historical-price-eod/non-split-adjusted"}
    # The runs and flags stay as the record; the security and its links too.
    assert db.fetchval("SELECT count(*) FROM ops.ingestion_run") == 1
    assert db.fetchval("SELECT count(*) FROM core.security_vendor_xref") == 1
