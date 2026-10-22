"""
`scripts/collect_facts.sh`, the nightly report's evidence, in a multi-vendor
warehouse (plan SA-1404, #118).

A smoke test: the script runs for real under bash, with stubs first on PATH for
`sudo` (which runs the rest of its command line), `systemctl`, `journalctl` and
`fafnir`. What is checked is the plumbing: the vendor mode is read as fafnir and
reaches monitor.sh's `sharadar` section, which the report's prompt reads.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "collect_facts.sh"

STUBS = {
    # sudo -n -u fafnir BIN ARGS... -> BIN ARGS...
    "sudo": 'while [[ "$1" == -* ]]; do [[ "$1" == -u ]] && shift; shift; done\n'
    'exec "$@"\n',
    "systemctl": "exit 0\n",
    "journalctl": "exit 0\n",
    # What `sudo -u fafnir` runs: fafnir's own rc file, so the real mode.
    "fafnir-as-fafnir": 'case "$*" in\n'
    '    "config get primary_price_vendor") echo "$STUB_PRIMARY" ;;\n'
    '    "config get fmp_secondary") echo true ;;\n'
    '    "config get sharadar_bulk_dir") exit 1 ;;\n'
    '    status) echo "Primary    : $STUB_PRIMARY" ;;\n'
    "esac\n",
    # What the agent itself would read: no rc file of its own, so the default.
    "fafnir": 'case "$*" in "config get primary_price_vendor") echo fmp ;; esac\n',
}


def _collect(tmp_path, primary):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\n" + body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("FAFNIR_DSN", "FAFNIR_PRIMARY_VENDOR")
    }
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        FAFNIR_HOME=str(REPO_ROOT),
        FAFNIR_BIN=str(bin_dir / "fafnir-as-fafnir"),
        FAFNIR_ENV_FILE=str(tmp_path / "absent.env"),
        STUB_PRIMARY=primary,
    )
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_evidence_carries_the_vendor_mode_into_the_sharadar_section(tmp_path):
    text = _collect(tmp_path, "sharadar").stdout
    assert "===== VENDOR MODE =====" in text
    assert "primary_price_vendor: sharadar" in text
    assert "fmp_secondary: true" in text
    assert "===== DISK / RUNS / BANDWIDTH / SHARADAR =====" in text
    # monitor.sh got the mode read as fafnir, not the agent's default ("fmp").
    assert "primary vendor: sharadar" in text
    # No DSN: said outright, not left to read as a clean night.
    assert "`runs`, `bandwidth` and `sharadar` checks below" in text
