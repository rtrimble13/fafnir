"""
`scripts/reset_data.sh --scope vendor:sharadar` (plan SA-0505, #70; needs
FAFNIR_TEST_DSN and psql).

Plan v2 withdrew per-vendor excision: under precedence one vendor's rows cannot be
deleted without rebuilding the series from the other's. What remains is a report of
what Sharadar supplied and where its data is held, for the day its licence ends. It
must count the right rows, and never delete. Every key and price is synthetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fafnir.db import repository as repo

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("psql") is None, reason="needs psql"),
]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reset_data.sh"


def _run(dsn, *args, env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "FAFNIR_DSN": dsn, **(env or {})},
        check=False,
    )


def _line(out: str, label: str) -> str:
    """The value printed after ``label`` in the report."""
    (line,) = [x for x in out.splitlines() if x.strip().startswith(label)]
    return line.strip()[len(label) :].strip()


def _seed(db, tmp_path):
    """One FMP security, and one Sharadar minted, with rows of each kind."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    fmp = repo.upsert_security(
        db, primary_symbol="ZZF", company_name="ZZF Co", exchange_code="NYSE"
    )
    dead = repo.upsert_security(
        db,
        primary_symbol="ZZD1",
        company_name="ZZD Co",
        exchange_code="NYSE",
        is_actively_trading=False,
        delisted_date=date(2009, 6, 30),
        source="sharadar",
    )
    repo.insert_symbol_period(
        db,
        security_id=dead,
        symbol="ZZD1",
        valid_from=date(2005, 1, 3),
        valid_to=date(2009, 6, 30),
        source="sharadar",
    )
    bar = {"open": Decimal("10"), "high": Decimal("10"), "low": Decimal("10")}
    repo.upsert_daily_prices(
        db,
        [dict(bar, security_id=fmp, trade_date=date(2020, 1, 2), close=Decimal("10"))],
    )
    repo.upsert_daily_prices(
        db,
        [
            dict(bar, security_id=dead, trade_date=d, close=Decimal("10"), volume=1)
            for d in (date(2009, 6, 29), date(2009, 6, 30))
        ],
        source="sharadar",
    )
    for key, ticker in (("900001", "ZZD1"), ("900002", "ZZF")):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, "
            "ticker) VALUES ('sharadar', 'stocks', %s, %s)",
            (key, ticker),
        )
    repo.insert_vendor_link(
        db,
        security_id=dead,
        vendor="sharadar",
        vendor_key="900001",
        valid_from=date(2005, 1, 3),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    repo.add_dq_flag_once(
        db,
        check_name="vendor_xref_unmatched",
        security_id=fmp,
        record_key={"vendor": "sharadar"},
        detail={"reason": "price_disagrees"},
    )
    # An operator edit on the minted security: a person decides before any removal.
    db.execute(
        "INSERT INTO ops.operator_override (security_id, target, key_date, operation, "
        "note, created_by) VALUES (%s, 'daily_price', '2009-06-29', 'delete', "
        "'test', 'tester')",
        (dead,),
    )
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    (bulk / "abc.zip").write_bytes(b"x" * 10)
    return fmp, dead, bulk


def _counts(db):
    return db.fetchone(
        "SELECT (SELECT count(*) FROM core.security) AS securities, "
        "(SELECT count(*) FROM core.daily_price) AS bars, "
        "(SELECT count(*) FROM core.security_vendor_xref) AS links, "
        "(SELECT count(*) FROM core.vendor_security) AS master"
    )


def test_the_report_counts_what_sharadar_supplied_and_deletes_nothing(
    db, migrated_dsn, tmp_path
):
    _, _, bulk = _seed(db, tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "fafnir_20261019T000000Z.dump").write_bytes(b"d")
    before = _counts(db)
    out = _run(
        migrated_dsn,
        "--scope",
        "vendor:sharadar",
        "--bulk-dir",
        str(bulk),
        env={"FAFNIR_BACKUP_DIR": str(backups)},
    )
    assert out.returncode == 0, out.stderr
    o = out.stdout
    assert _line(o, "core.daily_price") == "2"
    assert _line(o, "core.security (minted by Sharadar)") == "1"
    assert _line(o, "core.symbol_xref") == "1"
    assert _line(o, "core.vendor_security (its master)") == "2"
    assert _line(o, "core.security_vendor_xref (links; evidence: closes)") == "1"
    assert _line(o, "ops.data_quality_flag (vendor_*; detail: closes)") == "1"
    assert _line(o, "ops.vendor_correction (Sharadar replaced; active)") == (
        "0 (0 active)"
    )
    assert _line(o, "operator edits on Sharadar-minted securities") == "1"
    assert _line(o, str(bulk)).startswith("1 files, ")
    assert _line(o, f"logical dumps in {backups}").startswith("1 dumps, oldest ")
    assert "REPORT ONLY" in o
    assert "password" not in o
    assert _counts(db) == before


def test_the_report_refuses_to_delete_and_names_no_other_vendor(
    db, migrated_dsn, tmp_path
):
    _seed(db, tmp_path)
    before = _counts(db)
    out = _run(migrated_dsn, "--scope", "vendor:sharadar", "--yes")
    assert out.returncode == 2 and "never deletes" in out.stderr
    out = _run(migrated_dsn, "--scope", "vendor:fmp")
    assert out.returncode == 2 and "only vendor:sharadar" in out.stderr
    assert _counts(db) == before


def test_a_missing_bulk_dir_is_said_not_failed(db, migrated_dsn, tmp_path):
    out = _run(
        migrated_dsn,
        "--scope",
        "vendor:sharadar",
        "--bulk-dir",
        str(tmp_path / "absent"),
        env={"FAFNIR_BACKUP_DIR": str(tmp_path / "none")},
    )
    assert out.returncode == 0, out.stderr
    assert "not found or not readable (pass --bulk-dir" in out.stdout
    assert "not found or not readable (set FAFNIR_BACKUP_DIR)" in out.stdout
