"""
`scripts/monitor.sh` in a multi-vendor warehouse (plan SA-1404, #118; needs
FAFNIR_TEST_DSN and psql).

The script runs for real, with its SQL against the test database. Only `fafnir` is
a stub, first on PATH, answering `config get` from the environment. Checked here:
- the `sharadar` section warns only in Sharadar mode, and only when the last
  weeknight's night did not succeed;
- the `bandwidth` section charges FMP's budget with FMP's bytes alone.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("psql") is None, reason="needs psql"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "monitor.sh"

STUB = """#!/usr/bin/env bash
case "$*" in
    "config get primary_price_vendor") echo "$STUB_PRIMARY" ;;
    "config get sharadar_bulk_dir") echo "$STUB_BULK_DIR" ;;
    *) exit 1 ;;
esac
"""

STALE = "No successful Sharadar night"


def _monitor(tmp_path, dsn, primary, *sections):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fafnir"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    bulk = tmp_path / "bulk"
    bulk.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        FAFNIR_DSN=dsn,
        FAFNIR_ENV_FILE=str(tmp_path / "absent.env"),
        STUB_PRIMARY=primary,
        STUB_BULK_DIR=str(bulk),
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *sections],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run(db, source, endpoint, *, ago="0 hours", status="success", nbytes=0):
    db.execute(
        "INSERT INTO ops.ingestion_run "
        "(source, endpoint, status, started_at, finished_at, bytes_downloaded) "
        "VALUES (%s, %s, %s, now() - %s::interval, now() - %s::interval, %s)",
        (source, endpoint, status, ago, ago, nbytes),
    )


def test_the_sharadar_section_warns_only_on_a_missed_sharadar_night(db, tmp_path):
    _run(db, "sharadar", "delta:stocks", ago="5 days")
    _run(db, "sharadar", "prices:nightly", ago="5 days")
    _run(db, "sharadar", "prices:nightly", status="failed")
    db.execute(
        "INSERT INTO landing.sharadar_file (vendor_table, kind, path, sha256, bytes, "
        "rows) VALUES ('stocks', 'delta', 'a.csv', %s, 1200, 10)",
        ("0" * 64,),
    )

    out = _monitor(tmp_path, db.dsn, "sharadar", "sharadar")
    assert out.returncode == 1, out.stdout
    assert STALE in out.stdout
    assert "primary vendor: sharadar" in out.stdout
    assert "prices:nightly" in out.stdout and "delta:stocks" in out.stdout
    assert "stocks" in out.stdout and "bulk_dir" in out.stdout

    # Before the cutover the same evidence is reported, never a warning.
    out = _monitor(tmp_path, db.dsn, "fmp", "sharadar")
    assert out.returncode == 0, out.stdout
    assert STALE not in out.stdout

    _run(db, "sharadar", "prices:nightly")  # tonight's
    out = _monitor(tmp_path, db.dsn, "sharadar", "sharadar")
    assert out.returncode == 0, out.stdout


def test_fmps_budget_counts_fmps_bytes_alone(db, tmp_path):
    gib = 1 << 30
    _run(db, "sharadar", "bulk:stocks", nbytes=45 * gib)
    out = _monitor(tmp_path, db.dsn, "fmp", "bandwidth")
    assert out.returncode == 0, out.stdout
    assert "sharadar" in out.stdout  # shown, by vendor

    _run(db, "fmp", "prices", nbytes=41 * gib)
    out = _monitor(tmp_path, db.dsn, "fmp", "bandwidth")
    assert out.returncode == 1
    assert "41.0 GB downloaded from FMP this month" in out.stdout
