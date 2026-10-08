"""dorq as a DQ engine: export, run, ingest, shadow (dorq plan DR-0702).

Most of these run a *fake* dorq -- a script that records what it was given and
prints canned fafnir-format rows -- so the export, the guards and the shadow file
are tested without the C++ binary. The tests marked ``real_dorq`` run the binary
named by ``FAFNIR_TEST_DORQ`` and are skipped without it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import stat
import sys

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.config import FafnirConfig
from fafnir.db import repository as repo
from fafnir.dq import dorq

REAL_DORQ = os.environ.get("FAFNIR_TEST_DORQ", "")
real_dorq = pytest.mark.skipif(
    not REAL_DORQ, reason="set FAFNIR_TEST_DORQ to a dorq binary to run"
)

# A fake dorq: `--version` prints a version; `check` saves its arguments and stdin
# beside itself and prints the rows in FAKE_DORQ_ROWS (a file), or fails with
# FAKE_DORQ_FAIL.
_FAKE = """#!{python}
import os, sys, pathlib
here = pathlib.Path(__file__).parent
if "--version" in sys.argv:
    print("9.9.9-fake")
    sys.exit(0)
data = sys.stdin.buffer.read()
(here / "stdin.csv").write_bytes(data)
(here / "args.txt").write_text("\\n".join(sys.argv[1:]))
for name in ("--calendar-file", "--actions", "--meta"):
    if name in sys.argv:
        src = pathlib.Path(sys.argv[sys.argv.index(name) + 1])
        (here / (name.strip("-") + ".csv")).write_bytes(src.read_bytes())
if os.environ.get("FAKE_DORQ_FAIL"):
    sys.stderr.write(os.environ["FAKE_DORQ_FAIL"] + "\\n")
    sys.exit(2)
rows = os.environ.get("FAKE_DORQ_ROWS")
if rows:
    sys.stdout.write(pathlib.Path(rows).read_text())
"""


@pytest.fixture()
def fake_dorq(tmp_path, monkeypatch):
    path = tmp_path / "dorq"
    path.write_text(_FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    rows = tmp_path / "rows.jsonl"
    rows.write_text("")
    monkeypatch.setenv("FAKE_DORQ_ROWS", str(rows))
    monkeypatch.delenv("FAKE_DORQ_FAIL", raising=False)
    return path


def _row(sid, day, check="dorq_bad_print", severity="error", code="DQ201", p=0.99):
    return {
        "security_id": sid,
        "table_name": "core.daily_price",
        "record_key": {"trade_date": day},
        "check_name": check,
        "severity": severity,
        "detail": {
            "series": str(sid),
            "date": day,
            "code": code,
            "p_error": p,
            "dorq": {"version": "9.9.9-fake", "config_hash": "x"},
        },
    }


def _emit(fake, rows):
    (fake.parent / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_settings_default_to_an_isolated_run(tmp_path):
    cfg = FafnirConfig(str(tmp_path / "missing.toml"))
    s = dorq.settings_from(cfg)
    assert s.path == "/opt/dorq/bin/dorq"
    assert s.base_args() == ["/opt/dorq/bin/dorq", "check", "--isolated"]
    assert cfg.dq_engine == "sql"
    assert cfg.dorq_shadow is True
    assert cfg.dorq_lookback_sessions == 260


def test_settings_from_the_dq_section(tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        '[dq]\nengine = "both"\ndorq_path = "/x/dorq"\ndorq_config = "/etc/dorq.toml"\n'
        "dorq_threads = 4\ndorq_shadow = false\ndorq_lookback_sessions = 300\n"
    )
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    cfg = FafnirConfig(str(rc))
    s = dorq.settings_from(cfg)
    assert cfg.dq_engine == "both"
    assert cfg.dorq_shadow is False
    assert s.base_args() == [
        "/x/dorq",
        "check",
        "--config",
        "/etc/dorq.toml",
        "--threads",
        "4",
    ]
    assert s.lookback_sessions == 300
    monkeypatch.setenv("FAFNIR_DORQ_PATH", "/env/dorq")
    assert dorq.settings_from(cfg).path == "/env/dorq"


def test_an_unknown_engine_is_an_error(tmp_path):
    rc = tmp_path / "fafnirrc"
    rc.write_text('[dq]\nengine = "dork"\n')
    with pytest.raises(ValueError, match="engine"):
        FafnirConfig(str(rc)).dq_engine


def test_a_missing_binary_says_how_to_fix_it(tmp_path):
    with pytest.raises(dorq.DorqError, match="FAFNIR_DORQ_PATH"):
        dorq.dorq_version(dorq.DorqSettings(path=str(tmp_path / "nope")))


def test_the_bars_are_ordered_by_security_then_date():
    """dorq streams a large input, which needs each series' rows together."""
    stmt = dorq.bars_copy_statement(dt.date(2024, 1, 2), [3, 1]).as_string(None)
    assert "ORDER BY security_id, trade_date" in stmt
    assert "trade_date >= '2024-01-02'" in stmt
    assert "ARRAY[3, 1]" in stmt or "'{3,1}'" in stmt


def test_shadow_files_are_named_by_as_of_date(tmp_path):
    path = dorq.shadow_path(str(tmp_path), dt.date(2024, 6, 3))
    assert path.name == "2024-06-03.jsonl"
    rows = [_row(1, "2024-06-03")]
    dorq.write_shadow(rows, path)
    assert dorq.read_shadow(path) == rows
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# With a database
# ---------------------------------------------------------------------------


def _mk(db, symbol, asset_type="equity", is_fund=False):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type=asset_type,
        exchange_code="NASDAQ",
    )
    if is_fund:
        db.execute(
            "UPDATE core.security SET is_fund = true WHERE security_id = %s", (sid,)
        )
    return sid


def _sessions(db, start=dt.date(2023, 1, 3), end=dt.date(2024, 12, 31)):
    return [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
            " AND is_open AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )
    ]


def _walk(db, sid, days, start=50.0, seed=1, bad=None):
    """A quiet random walk on ~1M shares a day. ``bad`` maps a date to a factor
    the whole bar is multiplied by: a bad print, which the series reverts from."""
    state = seed
    price = start
    rows = []
    for d in days:
        state = (state * 1103515245 + 12345) % (1 << 31)
        prev = price
        price *= math.exp(((state / (1 << 31)) - 0.5) * 0.03)
        f = (bad or {}).get(d, 1.0)
        o, c = prev * f, price * f
        rows.append(
            {
                "security_id": sid,
                "trade_date": d,
                "open": round(o, 2),
                "high": round(max(o, c) * 1.004, 2),
                "low": round(min(o, c) * 0.996, 2),
                "close": round(c, 2),
                "volume": 1_000_000 + (state % 200_000),
            }
        )
    repo.upsert_daily_prices(db, rows)


@pytest.mark.integration
def test_export_writes_calendar_actions_and_nav_metadata(db, fake_dorq):
    eq = _mk(db, "DQEQ")
    fund = _mk(db, "DQFD", is_fund=True)
    days = _sessions(db)
    _walk(db, eq, days[:30])
    _walk(db, fund, days[:30], seed=2)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " split_numerator, split_denominator) VALUES (%s, 'split', %s, 2, 1)",
        (eq, days[10]),
    )
    settings = dorq.DorqSettings(path=str(fake_dorq))
    result = dorq.run_dorq(db, settings, "NASDAQ", dorq.Window(as_of=days[29]))
    assert result.version == "9.9.9-fake"
    assert result.records == []

    here = fake_dorq.parent
    args = (here / "args.txt").read_text().splitlines()
    assert args[:2] == ["check", "--isolated"]
    assert "--format" in args and args[args.index("--format") + 1] == "fafnir"
    assert args[args.index("--as-of") + 1] == days[29].isoformat()
    assert "--since" not in args
    bars = (here / "stdin.csv").read_text().splitlines()
    assert bars[0] == "security_id,trade_date,open,high,low,close,volume"
    assert len(bars) == 61
    assert [int(b.split(",")[0]) for b in bars[1:]] == sorted(
        int(b.split(",")[0]) for b in bars[1:]
    )
    meta = (here / "meta.csv").read_text().splitlines()
    assert meta[0] == "security_id,asset_type,nav_priced,exchange"
    assert f"{eq},equity,false,NASDAQ" in meta
    assert f"{fund},equity,true,NASDAQ" in meta
    actions = (here / "actions.csv").read_text().splitlines()
    assert actions[1].startswith(f"{eq},{days[10].isoformat()},split,2")
    calendar = (here / "calendar-file.csv").read_text().splitlines()
    assert calendar[0] == "exchange_code,trade_date,is_open"


@pytest.mark.integration
def test_the_nightly_window_reads_the_lookback_and_reports_from_the_last_run(db):
    sid = _mk(db, "DQWIN")
    days = _sessions(db)
    _walk(db, sid, days[:300])
    first = dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_QUEUE)
    assert first.as_of == days[299]
    assert first.bars_from == days[299 - 259]
    assert first.since is None  # first run: the whole lookback

    dorq.write_watermark(db, dorq.WATERMARK_QUEUE, days[290])
    later = dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_QUEUE)
    assert later.since == days[290 - dorq.SINCE_OVERLAP_SESSIONS]
    # The shadow's watermark is its own.
    assert dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW).since is None


@pytest.mark.integration
def test_ingest_carries_the_open_and_accepted_guards(db):
    a = _mk(db, "DQIA")
    b = _mk(db, "DQIB")
    rows = [
        _row(a, "2024-03-01"),
        _row(a, "2024-03-01"),  # the same condition twice in one run: one flag
        _row(b, "2024-03-04", check="dorq_scale_shift", code="DQ202"),
        _row(None, "2024-03-05", check="dorq_cohort_gap", code="DQ303"),
    ]
    assert dorq.ingest(db, rows) == {
        "dorq_bad_print": 1,
        "dorq_cohort_gap": 1,
        "dorq_scale_shift": 1,
    }
    # Open: not written again, including the cross-sectional row (NULL security).
    assert dorq.ingest(db, rows) == {}

    # Resolved: written again, because resolving is judged against the data.
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 't',"
        " resolution_note = 'n' WHERE check_name = 'dorq_bad_print'"
    )
    assert dorq.ingest(db, rows) == {"dorq_bad_print": 1}

    # Accepted: never written again.
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 't',"
        " resolution_note = 'n', accepted_at = now(), accepted_by = 't'"
        " WHERE check_name = 'dorq_scale_shift'"
    )
    assert dorq.ingest(db, rows) == {}
    detail = db.fetchval(
        "SELECT detail FROM ops.data_quality_flag WHERE check_name = 'dorq_bad_print'"
        " LIMIT 1"
    )
    assert detail["code"] == "DQ201"


@pytest.mark.integration
def test_dq_run_shadow_writes_the_file_and_not_the_queue(db, fake_dorq, tmp_path):
    sid = _mk(db, "DQSH")
    days = _sessions(db)
    _walk(db, sid, days[:40])
    _emit(fake_dorq, [_row(sid, days[20].isoformat())])
    settings = dorq.DorqSettings(
        path=str(fake_dorq), shadow_dir=str(tmp_path / "shadow")
    )
    out = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert out.shadow_file == tmp_path / "shadow" / f"{days[39].isoformat()}.jsonl"
    assert out.detected == {"dorq_bad_print": 1}
    assert out.flagged == {}
    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == 0
    assert len(dorq.read_shadow(out.shadow_file)) == 1
    run = db.fetchone(
        "SELECT source, endpoint, status, window_to FROM ops.ingestion_run"
        " ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert run == {
        "source": "dorq",
        "endpoint": "dq-shadow",
        "status": "success",
        "window_to": days[39],
    }
    assert dorq.read_watermark(db, dorq.WATERMARK_SHADOW) == days[39]
    assert dorq.read_watermark(db, dorq.WATERMARK_QUEUE) is None

    # To the queue: written with the run as its lineage.
    out = dorq.run(db, settings, "NASDAQ", shadow=False)
    assert out.flagged == {"dorq_bad_print": 1}
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag f JOIN ops.ingestion_run r"
            " USING (ingestion_run_id) WHERE r.endpoint = 'dq-run'"
        )
        == 1
    )


@pytest.mark.integration
def test_a_dorq_error_names_dorqs_own_message(db, fake_dorq, monkeypatch):
    sid = _mk(db, "DQERR")
    _walk(db, sid, _sessions(db)[:10])
    monkeypatch.setenv("FAKE_DORQ_FAIL", "dorq: error: dorq.toml:3: unknown key")
    settings = dorq.DorqSettings(path=str(fake_dorq))
    with pytest.raises(dorq.DorqError, match="unknown key"):
        dorq.run(db, settings, "NASDAQ", shadow=True)


@pytest.mark.integration
def test_cli_engine_both_runs_sql_and_dorq(db, fake_dorq, tmp_path, monkeypatch):
    sid = _mk(db, "DQCLI")
    days = _sessions(db)
    _walk(db, sid, days[:40])
    _emit(fake_dorq, [_row(sid, days[20].isoformat())])
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n'
        f'[dq]\ndorq_path = "{fake_dorq}"\ndorq_shadow_dir = "{tmp_path / "sh"}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["--config", str(rc), "dq", "run", "--engine", "both"]
    )
    assert result.exit_code == 0, result.output
    assert "DQ flags written:" in result.output
    assert "dorq 9.9.9-fake" in result.output
    assert "shadow" in result.output
    assert (tmp_path / "sh" / f"{days[39].isoformat()}.jsonl").is_file()
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE check_name LIKE 'dorq%'"
        )
        == 0
    )

    result = runner.invoke(
        cli.main,
        ["--config", str(rc), "dq", "run", "--engine", "dorq", "--no-shadow"],
    )
    assert result.exit_code == 0, result.output
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE check_name LIKE 'dorq%'"
        )
        == 1
    )


@pytest.mark.integration
def test_status_reports_the_dorq_version(db, fake_dorq, tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\nengine = "both"\n'
        f'dorq_path = "{fake_dorq}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    result = CliRunner().invoke(cli.main, ["--config", str(rc), "status"])
    assert result.exit_code == 0, result.output
    assert "dorq       : 9.9.9-fake (engine both, shadow)" in result.output


# ---------------------------------------------------------------------------
# The real binary
# ---------------------------------------------------------------------------


@real_dorq
@pytest.mark.integration
def test_real_dorq_finds_a_bad_print_and_is_deterministic(db, tmp_path):
    days = _sessions(db)
    sids = [_mk(db, f"RD{i}") for i in range(4)]
    for i, sid in enumerate(sids):
        bad = {days[200]: 1.6} if i == 0 else None
        _walk(db, sid, days, start=40.0 + i, seed=i + 7, bad=bad)
    one = dorq.DorqSettings(path=REAL_DORQ, threads=1)
    many = dorq.DorqSettings(path=REAL_DORQ, threads=4)
    window = dorq.full_window(db, "NASDAQ")
    a = dorq.run_dorq(db, one, "NASDAQ", window)
    b = dorq.run_dorq(db, many, "NASDAQ", window)
    assert a.records == b.records
    hits = [
        r
        for r in a.records
        if r["security_id"] == sids[0]
        and r["record_key"] == {"trade_date": days[200].isoformat()}
    ]
    assert [r["check_name"] for r in hits] == ["dorq_bad_print"]
    assert dorq.ingest(db, a.records)["dorq_bad_print"] >= 1
    assert dorq.ingest(db, a.records) == {}


# ---------------------------------------------------------------------------
# dq recheck: rerun and negate (DR-0705)
# ---------------------------------------------------------------------------


def test_recheck_never_closes_the_never_three_or_a_cohort():
    from fafnir.dq import recheck as rc
    from fafnir_mcp.tools import NEVER_AUTO_RESOLVE

    excluded = rc.dorq_recheck_excluded()
    assert {c for c in NEVER_AUTO_RESOLVE if c.startswith("dorq_")} <= excluded
    assert {"dorq_cohort_gap", "dorq_cohort_move"} <= excluded
    for name in ("dorq_scale_shift", "dorq_cohort_gap", "outlier"):
        with pytest.raises(ValueError, match="not re-evaluable"):
            rc.recheck_dorq(None, dorq.DorqSettings(), checks=[name])


@pytest.mark.integration
def test_recheck_closes_what_dorq_no_longer_emits(db, fake_dorq):
    from fafnir.dq import recheck as rc

    still = _mk(db, "RCKA")
    gone = _mk(db, "RCKB")
    days = _sessions(db)
    _walk(db, still, days[:50])
    _walk(db, gone, days[:50], seed=3)
    old = _row(gone, days[20].isoformat())
    old["detail"]["dorq"]["version"] = "0.5.0"
    dorq.ingest(
        db,
        [
            _row(still, days[10].isoformat()),
            old,
            _row(gone, days[30].isoformat(), check="dorq_scale_shift", code="DQ202"),
        ],
    )
    assert rc.open_dorq_checks(db) == ["dorq_bad_print"]  # scale_shift is Never

    _emit(fake_dorq, [_row(still, days[10].isoformat())])
    settings = dorq.DorqSettings(path=str(fake_dorq))
    (result,) = rc.recheck_dorq(db, settings)
    assert result.check_name == "dorq_bad_print"
    assert result.open_flags == 2
    gone_id = db.fetchval(
        "SELECT dq_flag_id FROM ops.data_quality_flag WHERE security_id = %s"
        " AND check_name = 'dorq_bad_print'",
        (gone,),
    )
    assert list(result.stale_flag_ids) == [gone_id]
    assert "re-running dorq 9.9.9-fake" in result.reason
    assert "written by dorq 0.5.0" in result.reason
    # The run read only the securities with open flags, over their full history.
    args = (fake_dorq.parent / "args.txt").read_text().splitlines()
    assert "--since" not in args
    sent = {
        int(line.split(",")[0])
        for line in (fake_dorq.parent / "stdin.csv").read_text().splitlines()[1:]
    }
    assert sent == {still, gone}


@pytest.mark.integration
def test_cli_recheck_includes_dorq_and_skips_it_when_absent(
    db, fake_dorq, tmp_path, monkeypatch
):
    sid = _mk(db, "RCKC")
    days = _sessions(db)
    _walk(db, sid, days[:50])
    dorq.ingest(db, [_row(sid, days[10].isoformat())])
    rc_file = tmp_path / "fafnirrc"
    rc_file.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_path = "{fake_dorq}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["--config", str(rc_file), "dq", "recheck", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "dorq_bad_print" in result.output and "1 of       1" in result.output

    result = runner.invoke(
        cli.main,
        [
            "--config",
            str(rc_file),
            "dq",
            "recheck",
            "--check",
            "dorq_bad_print",
            "--yes",
            "--by",
            "test",
        ],
    )
    assert result.exit_code == 0, result.output
    note = db.fetchval(
        "SELECT resolution_note FROM ops.data_quality_flag WHERE check_name = 'dorq_bad_print'"
    )
    assert note.startswith("Re-checked: re-running dorq 9.9.9-fake")

    # No binary: the SQL checks are still rechecked, and dorq is reported skipped.
    dorq.ingest(db, [_row(sid, days[11].isoformat())])
    rc_file.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_path = "{tmp_path / "nope"}"\n'
    )
    result = runner.invoke(
        cli.main, ["--config", str(rc_file), "dq", "recheck", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "dorq_*: skipped" in result.output
    result = runner.invoke(
        cli.main,
        [
            "--config",
            str(rc_file),
            "dq",
            "recheck",
            "--check",
            "dorq_bad_print",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0


@real_dorq
@pytest.mark.integration
def test_real_dorq_recheck_closes_a_bad_print_once_it_is_deleted(db):
    from fafnir.dq import recheck as rc

    days = _sessions(db)
    sid = _mk(db, "RCKREAL")
    _walk(db, sid, days, seed=11, bad={days[200]: 1.6})
    settings = dorq.DorqSettings(path=REAL_DORQ)
    run = dorq.run_dorq(db, settings, "NASDAQ", dorq.full_window(db, "NASDAQ"))
    assert dorq.ingest(db, run.records).get("dorq_bad_print") == 1
    (before,) = rc.recheck_dorq(db, settings)
    assert before.stale == 0  # the bar is still bad

    repo.delete_operator_bars(
        db, security_id=sid, trade_dates=[days[200]], note="bad print", created_by="t"
    )
    (after,) = rc.recheck_dorq(db, settings)
    assert after.stale == 1
