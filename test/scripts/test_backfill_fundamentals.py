"""
`scripts/backfill_fundamentals.sh` (#106): the fundamentals backfill's steps for a
re-run, in order, with the options that skip or narrow them.

The script runs for real under bash, against a stub `fafnir` first on PATH that
records each call: no database, no network, no vendor data. CI also runs its
`--help` as a smoke test.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backfill_fundamentals.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
[ -n "${STUB_FAIL:-}" ] && [ "$*" = "$STUB_FAIL" ] && exit 1
exit 0
"""

WHOLE = [
    "source sharadar-bulk --tables SF1 --force",
    "ingest fundamentals --backfill",
    "db refresh-marts",
    "status",
]


def _backfill(tmp_path, *args, fail=None):
    """Run the script once; the completed process and the calls it made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fafnir"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if k != "FAFNIR_DSN"}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_FAIL=fail or "",
    )
    done = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, log.read_text().splitlines()


def test_it_explains_itself_and_refuses_what_it_does_not_know(tmp_path):
    done, calls = _backfill(tmp_path, "--help")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Usage: scripts/backfill_fundamentals.sh")
    assert "--keep-file" in done.stdout and calls == []
    done, calls = _backfill(tmp_path, "--bogus")
    assert done.returncode == 2 and "Unknown argument: --bogus" in done.stderr
    assert calls == []


def test_a_whole_run_starts_from_a_fresh_file(tmp_path):
    done, calls = _backfill(tmp_path)
    assert done.returncode == 0, done.stderr
    assert calls == WHOLE
    # Without FAFNIR_DSN the report is a command to run, not a failure.
    assert "skipped (needs psql and FAFNIR_DSN)" in done.stdout


@pytest.mark.parametrize(
    "args, expected",
    [
        (["--keep-file"], WHOLE[1:]),
        (
            ["--symbols", "ZZA,ZZB"],
            ["ingest fundamentals --backfill --symbols ZZA,ZZB", *WHOLE[2:]],
        ),
        (["--dry-run"], ["ingest fundamentals --backfill --dry-run"]),
        (
            ["--dry-run", "--symbols=ZZA"],
            ["ingest fundamentals --backfill --symbols ZZA --dry-run"],
        ),
    ],
)
def test_the_options(tmp_path, args, expected):
    done, calls = _backfill(tmp_path, *args)
    assert done.returncode == 0, done.stderr
    assert calls == expected


def test_a_backfill_that_fails_stops_it_before_the_marts(tmp_path):
    done, calls = _backfill(tmp_path, fail="ingest fundamentals --backfill")
    assert done.returncode != 0
    assert calls == WHOLE[:2]
