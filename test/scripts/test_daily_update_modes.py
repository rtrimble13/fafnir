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
if [ -n "${STUB_FAIL:-}" ] && [ "$*" = "$STUB_FAIL" ]; then
    n=$(( $(cat "$STUB_LOG.fails" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_LOG.fails"
    if [ -z "${STUB_FAIL_TIMES:-}" ] || [ "$n" -le "$STUB_FAIL_TIMES" ]; then
        exit "${STUB_FAIL_CODE:-1}"
    fi
fi
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


def _night(
    tmp_path,
    primary,
    *,
    secondary="true",
    fail=None,
    fail_code=1,
    fail_times=None,
    retry=None,
):
    """Run the script once; the completed process and the calls it made.

    ``fail`` exits ``fail_code`` (``fail_times`` times, then succeeds; every time
    by default). ``retry`` is the FAFNIR_RETRY_* environment."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "fafnir"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    (tmp_path / "calls.log.fails").unlink(missing_ok=True)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_PRIMARY=primary,
        STUB_SECONDARY=secondary,
        STUB_FAIL=fail or "",
        STUB_FAIL_CODE=str(fail_code),
        STUB_FAIL_TIMES="" if fail_times is None else str(fail_times),
        **(retry or {}),
    )
    for name in ("FAFNIR_RETRY_DELAYS", "FAFNIR_RETRY_WINDOW"):
        if name not in (retry or {}):
            env.pop(name, None)
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


# ---------------------------------------------------------------------------
# A vendor outage is waited out (exit 75, EX_TEMPFAIL); a fault is not
# ---------------------------------------------------------------------------

NO_WAIT = {"FAFNIR_RETRY_DELAYS": "0 0 0"}


def test_a_vendor_outage_is_retried_and_the_night_then_loads(tmp_path):
    """2026-10-31: SFP answered 503 and the night stopped at 02:31 UTC with no
    prices. Now the Sharadar step is run again until it loads."""
    done, calls = _night(
        tmp_path,
        "sharadar",
        fail="ingest sharadar",
        fail_code=75,
        fail_times=2,
        retry=NO_WAIT,
    )
    assert done.returncode == 0, done.stderr
    # Two attempts stopped by the outage, then the one that loads.
    assert calls == SHARADAR_NIGHT[:3] + ["ingest sharadar"] * 2 + SHARADAR_NIGHT[3:]
    assert calls.count("ingest sharadar") == 3
    assert "stopped on a vendor outage (attempt 1, exit 75)" in done.stdout
    assert "stopped on a vendor outage (attempt 2, exit 75)" in done.stdout


def test_a_fault_is_not_retried(tmp_path):
    done, calls = _night(
        tmp_path, "sharadar", fail="ingest sharadar", fail_code=1, retry=NO_WAIT
    )
    assert done.returncode == 1
    assert calls.count("ingest sharadar") == 1 and calls[-1] == "ingest sharadar"
    assert "vendor outage" not in done.stdout


def test_an_outage_that_outlasts_the_delays_stops_the_night_with_75(tmp_path):
    done, calls = _night(
        tmp_path,
        "sharadar",
        fail="ingest sharadar",
        fail_code=75,
        retry={"FAFNIR_RETRY_DELAYS": "0 0"},
    )
    assert done.returncode == 75
    assert calls.count("ingest sharadar") == 3 and calls[-1] == "ingest sharadar"
    assert "still unavailable after 3 attempts" in done.stdout


def test_no_retry_starts_past_the_window(tmp_path):
    """The window keeps the night inside fafnir-daily's TimeoutStartSec, and 0
    turns retrying off."""
    for window in ("0", "60"):
        done, calls = _night(
            tmp_path,
            "sharadar",
            fail="ingest sharadar",
            fail_code=75,
            retry={"FAFNIR_RETRY_DELAYS": "3600", "FAFNIR_RETRY_WINDOW": window},
        )
        assert done.returncode == 75
        assert calls.count("ingest sharadar") == 1
        assert f"past FAFNIR_RETRY_WINDOW ({window}s)" in done.stdout


def test_a_window_that_is_not_seconds_stops_retrying(tmp_path):
    """`4h` is not a number of seconds: no retry, rather than retries unbounded by
    any window (`[` errors, and `if` reads the error as false)."""
    done, calls = _night(
        tmp_path,
        "sharadar",
        fail="ingest sharadar",
        fail_code=75,
        retry={"FAFNIR_RETRY_DELAYS": "0 0", "FAFNIR_RETRY_WINDOW": "4h"},
    )
    assert done.returncode == 75
    assert calls.count("ingest sharadar") == 1
    assert "FAFNIR_RETRY_WINDOW holds '4h', not seconds -- no retry." in done.stdout
    assert "integer expression expected" not in done.stderr


def test_the_defaults_wait_out_an_outage_within_the_units_timeout():
    """15 min, 30 min, then hourly, inside a 4 h window: the night ends inside
    fafnir-daily's TimeoutStartSec=6h even when the last attempt loads."""
    text = SCRIPT.read_text()
    assert "FAFNIR_RETRY_DELAYS:-900 1800 3600 3600 3600" in text
    assert "FAFNIR_RETRY_WINDOW:-14400" in text
    unit = (REPO_ROOT / "etc" / "systemd" / "fafnir-daily.service.in").read_text()
    assert "TimeoutStartSec=6h" in unit
    assert 14400 < 6 * 3600


def test_fmps_night_is_not_retried(tmp_path):
    done, calls = _night(
        tmp_path, "fmp", fail="ingest prices", fail_code=75, retry=NO_WAIT
    )
    assert done.returncode == 75
    assert calls.count("ingest prices") == 1 and calls[-1] == "ingest prices"


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
