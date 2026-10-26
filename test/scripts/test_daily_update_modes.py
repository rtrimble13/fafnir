"""
`daily_update.sh` in both modes, and the rollback between them (plan SA-1301, #111).

The script runs for real under bash, against a stub `fafnir` first on PATH that
records each call and answers `config get` from the environment. What is asserted
is the night's sequence: which commands run, in what order, with which flags. The
guard that keeps FMP's loaders from writing after the cutover is tested through the
real CLI, and the rollback's "FMP fills only what Sharadar did not" through the
upsert the FMP loader calls (the integration test needs FAFNIR_TEST_DSN).
"""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "daily_update.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$*" in
    "config get primary_price_vendor") echo "$STUB_PRIMARY" ;;
    "config get fmp_secondary")
        [ "$STUB_SECONDARY" = "bad" ] && { echo "bad setting" >&2; exit 1; }
        echo "$STUB_SECONDARY"
        ;;
esac
[ -n "${STUB_FAIL:-}" ] && [ "$*" = "$STUB_FAIL" ] && exit 1
exit 0
"""

FMP_NIGHT = [
    "config get primary_price_vendor",
    "db ensure-horizon",
    "ingest symbol-changes",
    "ingest securities",
    "ingest tracked",
    "ingest delisted",
    "ingest prices",
    "vendor link --vendor sharadar --new",
    "ingest actions",
    "adjust --changed",
    "db refresh-marts",
    "status",
]
SHARADAR_NIGHT = [
    "config get primary_price_vendor",
    "db ensure-horizon",
    "ingest sharadar",
    "ingest fundamentals",
    "config get fmp_secondary",
    "ingest tracked --allow-secondary",
    "ingest prices --allow-secondary",
    "ingest actions --allow-secondary",
    "adjust --changed",
    "db refresh-marts",
    "status",
]


def _night(tmp_path, primary, *, secondary="true", fail=None):
    """Run the script once; the completed process and the calls it made."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fafnir"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_PRIMARY=primary,
        STUB_SECONDARY=secondary,
        STUB_FAIL=fail or "",
    )
    done = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    return done, log.read_text().splitlines()


def test_fmp_mode_is_the_night_it_always_was(tmp_path):
    done, calls = _night(tmp_path, "fmp")
    assert done.returncode == 0, done.stderr
    assert calls == FMP_NIGHT
    assert "(primary vendor: fmp)" in done.stdout


def test_sharadar_mode_runs_the_sharadar_night_and_fmp_as_secondary(tmp_path):
    done, calls = _night(tmp_path, "sharadar")
    assert done.returncode == 0, done.stderr
    assert calls == SHARADAR_NIGHT
    # FMP's universe sweeps and its link step stay off: Sharadar's universe step
    # replaces them.
    for gone in ("symbol-changes", "ingest securities", "delisted", "vendor link"):
        assert not [c for c in calls if gone in c]


def test_sharadar_mode_without_fmp(tmp_path):
    done, calls = _night(tmp_path, "sharadar", secondary="false")
    assert done.returncode == 0, done.stderr
    assert calls == [
        "config get primary_price_vendor",
        "db ensure-horizon",
        "ingest sharadar",
        "ingest fundamentals",
        "config get fmp_secondary",
        "db refresh-marts",
        "status",
    ]
    assert "fmp_secondary = false" in done.stdout


def test_rollback_is_the_setting_flipped_back(tmp_path):
    first, before = _night(tmp_path, "sharadar")
    assert first.returncode == 0 and before == SHARADAR_NIGHT
    back, after = _night(tmp_path, "fmp")
    assert back.returncode == 0, back.stderr
    # FMP's own night, with nothing left over from the Sharadar one.
    assert after == FMP_NIGHT
    assert "ingest sharadar" not in after
    assert not [c for c in after if "--allow-secondary" in c]


def test_a_failed_sharadar_night_stops_and_a_failed_secondary_does_not(tmp_path):
    done, calls = _night(tmp_path, "sharadar", fail="ingest sharadar")
    assert done.returncode != 0
    assert calls[-1] == "ingest sharadar"  # no marts over a half-loaded night

    done, calls = _night(tmp_path, "sharadar", fail="ingest prices --allow-secondary")
    assert done.returncode == 0, done.stderr
    assert calls == SHARADAR_NIGHT
    assert "'fafnir ingest prices --allow-secondary' failed" in done.stdout


def test_a_failed_fundamentals_load_never_costs_the_night(tmp_path):
    # #106: the statements come after the Sharadar step, from the SF1 delta it
    # fetched, and before the marts that read them. A failure is upkeep.
    done, calls = _night(tmp_path, "sharadar", fail="ingest fundamentals")
    assert done.returncode == 0, done.stderr
    assert calls == SHARADAR_NIGHT
    assert "'fafnir ingest fundamentals' failed" in done.stdout
    # FMP's night has no fundamentals: no SF1 delta was fetched.
    done, calls = _night(tmp_path, "fmp")
    assert not [c for c in calls if "fundamentals" in c]


@pytest.mark.parametrize("bad", ["config get primary_price_vendor", "secondary"])
def test_a_setting_that_does_not_validate_stops_the_night(tmp_path, bad):
    if bad == "secondary":
        done, calls = _night(tmp_path, "sharadar", secondary="bad")
        assert calls[-1] == "config get fmp_secondary"
    else:
        done, calls = _night(tmp_path, "fmp", fail=bad)
        assert calls == [bad]
    assert done.returncode != 0
    assert "db refresh-marts" not in calls


def test_an_unknown_mode_stops_the_night(tmp_path):
    done, calls = _night(tmp_path, "tiingo")
    assert done.returncode != 0 and "unknown primary vendor 'tiingo'" in done.stdout
    assert calls == ["config get primary_price_vendor"]


# ---------------------------------------------------------------------------
# The CLI side: `config get`, and FMP's loaders after the cutover
# ---------------------------------------------------------------------------


def _invoke(tmp_path, rc_text, *args):
    rc = tmp_path / "fafnirrc"
    rc.write_text(rc_text)
    return CliRunner().invoke(
        cli.main, ["--config", str(rc), *args], env={"FAFNIR_DSN": "dbname=none"}
    )


def test_config_get_prints_what_the_script_reads(tmp_path):
    rc = '[general]\nprimary_price_vendor = "sharadar"\nfmp_secondary = false\n'
    out = _invoke(tmp_path, rc, "config", "get", "primary_price_vendor")
    assert (out.exit_code, out.output) == (0, "sharadar\n")
    out = _invoke(tmp_path, rc, "config", "get", "fmp_secondary")
    assert (out.exit_code, out.output) == (0, "false\n")
    assert _invoke(tmp_path, "", "config", "get", "fmp_secondary").output == "true\n"
    assert _invoke(tmp_path, "", "config", "get", "primary_price_vendor").output == (
        "fmp\n"
    )

    out = _invoke(tmp_path, rc, "config", "get", "fmp_key")
    assert out.exit_code != 0 and "unknown key 'fmp_key'" in out.output
    out = _invoke(
        tmp_path, "[general]\nfmp_secondary = 1\n", "config", "get", "fmp_secondary"
    )
    assert out.exit_code != 0 and "is not true or false" in out.output
    rc = '[general]\nprimary_price_vendor = "sharadr"\n'
    out = _invoke(tmp_path, rc, "config", "get", "primary_price_vendor")
    assert out.exit_code != 0 and "'sharadr' is not one of" in out.output


@pytest.mark.parametrize(
    "command",
    [
        ["ingest", "symbol-changes"],
        ["ingest", "securities"],
        ["ingest", "tracked"],
        ["ingest", "delisted"],
        ["ingest", "prices"],
        ["ingest", "actions"],
    ],
)
def test_fmps_loaders_refuse_after_the_cutover(tmp_path, command):
    rc = '[general]\nprimary_price_vendor = "sharadar"\n'
    out = _invoke(tmp_path, rc, *command)
    assert out.exit_code != 0
    assert f"`fafnir {' '.join(command)}` is FMP's" in out.output
    assert "--allow-secondary" in out.output


@pytest.mark.integration
def test_after_a_rollback_fmp_fills_only_what_sharadar_did_not(db):
    """The FMP loader's upsert, as it runs after the flip back: a bar Sharadar wrote
    during its nights stays, and FMP adds the dates it lacks."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol="ZZRB",
        company_name="ZZRB Co",
        exchange_code="NYSE",
        source="fmp",
    )

    def bar(day, close):
        c = Decimal(close)
        return {
            "security_id": sid,
            "trade_date": day,
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 100,
        }

    repo.upsert_daily_prices(db, [bar(date(2026, 12, 1), "10")], source="sharadar")
    result = repo.upsert_daily_prices(
        db, [bar(date(2026, 12, 1), "10.5"), bar(date(2026, 12, 2), "11")]
    )
    assert (result.inserted, result.skipped_foreign) == (1, 1)
    rows = {
        r["trade_date"]: (r["close"], r["source"])
        for r in db.fetchall(
            "SELECT trade_date, close, source FROM core.daily_price "
            "WHERE security_id = %s",
            (sid,),
        )
    }
    assert rows == {
        date(2026, 12, 1): (Decimal(10), "sharadar"),
        date(2026, 12, 2): (Decimal(11), "fmp"),
    }
