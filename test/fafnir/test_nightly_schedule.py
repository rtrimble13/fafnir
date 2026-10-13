"""DQ runs once a night, after the load.

On 2026-10-12 it ran twice: `daily_update.sh` ended with `fafnir dq run`, and the
`fafnir-dq` unit, ordered after the daily, ran `run_dq_checks.sh` again. The second
dorq run reported over a narrower window and replaced the first's shadow file. The
cron example was worse: its DQ entry had a clock of its own, so it judged a night
the load was still writing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _commands(path: Path) -> list[str]:
    """The lines of a shell script or crontab that are not comments."""
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_daily_load_does_not_run_dq():
    lines = _commands(REPO_ROOT / "scripts" / "daily_update.sh")
    assert not [line for line in lines if re.search(r"\bdq\s+run\b", line)]


def test_the_dq_unit_runs_after_the_daily():
    unit = (REPO_ROOT / "etc" / "systemd" / "fafnir-dq.service.in").read_text()
    after = [line for line in unit.splitlines() if line.startswith("After=")]
    assert any("fafnir-daily.service" in line for line in after)
    assert "scripts/run_dq_checks.sh" in unit


def test_cron_chains_dq_after_the_load_in_one_entry():
    lines = _commands(REPO_ROOT / "etc" / "crontab.example")
    dq = [line for line in lines if "run_dq_checks.sh" in line]
    assert len(dq) == 1
    (entry,) = dq
    between = entry[entry.index("daily_update.sh") : entry.index("run_dq_checks.sh")]
    # `;`, as systemd's After= without Requires=: DQ runs after a failed load too.
    assert ";" in between and "&&" not in between
