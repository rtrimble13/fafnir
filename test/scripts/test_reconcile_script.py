"""
`scripts/reconcile.sh` keeps its interface (#117): `SYM1,SYM2,... [FROM_DATE]`.

The script runs for real under bash, against a stub `fafnir` first on PATH that
records its arguments and exits with a chosen status. The comparison itself is
`fafnir source reconcile`, tested in test/fafnir/test_reconcile.py.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reconcile.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
exit "${STUB_EXIT:-0}"
"""


def _reconcile(tmp_path, *args, exit_code=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fafnir"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_EXIT=str(exit_code),
    )
    done = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return done, calls


def test_the_symbols_and_the_date_pass_through(tmp_path):
    done, calls = _reconcile(tmp_path, "AAPL,MSFT", "2026-10-01")
    assert done.returncode == 0, done.stderr
    assert calls == ["source reconcile AAPL,MSFT --from 2026-10-01"]


def test_the_window_defaults_to_30_days(tmp_path):
    done, calls = _reconcile(tmp_path, "AAPL")
    assert done.returncode == 0, done.stderr
    expected = (
        dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=30)
    ).isoformat()
    assert calls == [f"source reconcile AAPL --from {expected}"]


def test_no_symbols_is_a_usage_error(tmp_path):
    done, calls = _reconcile(tmp_path)
    assert done.returncode != 0
    assert "usage: reconcile.sh SYM1,SYM2,... [FROM_DATE]" in done.stderr
    assert calls == []


def test_the_commands_failure_is_the_scripts(tmp_path):
    # The timer's unit fails when nothing could be compared.
    done, _ = _reconcile(tmp_path, "NOPE", exit_code=1)
    assert done.returncode == 1
