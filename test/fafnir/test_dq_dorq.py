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
from fafnir.db.connection import Database
from fafnir.dq import dorq

REAL_DORQ = os.environ.get("FAFNIR_TEST_DORQ", "")
real_dorq = pytest.mark.skipif(
    not REAL_DORQ, reason="set FAFNIR_TEST_DORQ to a dorq binary to run"
)

# A fake dorq: `--version` prints a version; `check` saves its arguments and stdin
# beside itself and prints the rows in FAKE_DORQ_ROWS (a file), or fails with
# FAKE_DORQ_FAIL. FAKE_DORQ_SLEEP makes the check take that many seconds. Each
# check's arguments are also appended to calls.txt, one line a call. With
# FAKE_DORQ_FILTER it prints only the rows a real dorq could: on a security it was
# sent bars for (or on none: a cohort row), dated from its --since.
_FAKE = """#!{python}
import csv, io, json, os, sys, pathlib, time
here = pathlib.Path(__file__).parent
if "--version" in sys.argv:
    print("9.9.9-fake")
    sys.exit(0)
time.sleep(float(os.environ.get("FAKE_DORQ_SLEEP") or 0))
data = sys.stdin.buffer.read()
(here / "stdin.csv").write_bytes(data)
(here / "args.txt").write_text("\\n".join(sys.argv[1:]))
with (here / "calls.txt").open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
for name in ("--calendar-file", "--actions", "--meta"):
    if name in sys.argv:
        src = pathlib.Path(sys.argv[sys.argv.index(name) + 1])
        (here / (name.strip("-") + ".csv")).write_bytes(src.read_bytes())
if os.environ.get("FAKE_DORQ_FAIL"):
    sys.stderr.write(os.environ["FAKE_DORQ_FAIL"] + "\\n")
    sys.exit(2)
rows = os.environ.get("FAKE_DORQ_ROWS")
if rows and os.environ.get("FAKE_DORQ_FILTER"):
    sent = {{r["security_id"] for r in csv.DictReader(io.StringIO(data.decode()))}}
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else ""
    for line in pathlib.Path(rows).read_text().splitlines():
        r = json.loads(line)
        if r["security_id"] is not None and str(r["security_id"]) not in sent:
            continue
        if r["record_key"]["trade_date"] < since:
            continue
        sys.stdout.write(line + "\\n")
elif rows:
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
    monkeypatch.delenv("FAKE_DORQ_SLEEP", raising=False)
    monkeypatch.delenv("FAKE_DORQ_FILTER", raising=False)
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


def _stop_trading(db, sid):
    db.execute(
        "UPDATE core.security SET is_actively_trading = false WHERE security_id = %s",
        (sid,),
    )


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
def test_export_keeps_a_zero_dividend(db, fake_dorq):
    """The feed sends dividends of 0 and the schema keeps them. They go to dorq as
    stored, and dorq (0.8.0 on) reports them as DQ705 rather than refusing the file."""
    sid = _mk(db, "DQZD")
    days = _sessions(db)
    _walk(db, sid, days[:30])
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " dividend_amount) VALUES (%s, 'dividend', %s, 0), (%s, 'dividend', %s, 0.25)",
        (sid, days[10], sid, days[20]),
    )
    settings = dorq.DorqSettings(path=str(fake_dorq))
    dorq.run_dorq(db, settings, "NASDAQ", dorq.Window(as_of=days[29]))
    actions = (fake_dorq.parent / "actions.csv").read_text().splitlines()
    assert [a.split(",")[1] for a in actions[1:]] == [
        days[10].isoformat(),
        days[20].isoformat(),
    ]


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
def test_a_failed_run_is_recorded_and_moves_no_watermark(db, fake_dorq, monkeypatch):
    """On 2026-10-08 dorq 0.7.0 refused the actions file, and nothing in
    ops.ingestion_run said a run had happened: the row was written only once dorq
    had succeeded. The agent has no journal, so the row is its only record."""
    sid = _mk(db, "DQFAIL")
    _walk(db, sid, _sessions(db)[:10])
    monkeypatch.setenv(
        "FAKE_DORQ_FAIL",
        'dorq: error: actions.csv line 84466: amount "0.000000" is not a positive'
        " number",
    )
    settings = dorq.DorqSettings(path=str(fake_dorq))
    # Not autocommit, as the CLI runs it: the connection rolls back on the way out.
    with pytest.raises(dorq.DorqError, match="dorq 9.9.9-fake exited 2"):
        with Database(db.dsn) as conn:
            dorq.run(conn, settings, "NASDAQ", shadow=True)
    (run,) = db.fetchall(
        "SELECT status, error_message, finished_at FROM ops.ingestion_run"
        " WHERE source = 'dorq'"
    )
    assert run["status"] == "failed"
    assert run["error_message"].startswith("DorqError: dorq 9.9.9-fake exited 2: ")
    assert "is not a positive number" in run["error_message"]
    assert run["finished_at"] is not None
    assert dorq.read_watermark(db, dorq.WATERMARK_SHADOW) is None


def _stale_feed_night(db, fake_dorq):
    """dorq reports a stale feed on a security still trading and on one that has
    stopped, and a bad print in the stopped one's history. Returns the two."""
    active = _mk(db, "DQSFA")
    stopped = _mk(db, "DQSFD")
    days = _sessions(db)
    _walk(db, active, days[:40])
    _walk(db, stopped, days[:40], seed=2)
    _stop_trading(db, stopped)
    _emit(
        fake_dorq,
        [
            _row(active, days[30].isoformat(), check="dorq_stale_feed", code="DQ304"),
            _row(stopped, days[30].isoformat(), check="dorq_stale_feed", code="DQ304"),
            _row(stopped, days[20].isoformat()),
        ],
    )
    return active, stopped


@pytest.mark.integration
def test_stale_feed_on_a_security_not_trading_is_left_out_of_the_shadow(
    db, fake_dorq, tmp_path
):
    """The SQL `stale` check judges only securities still trading. DQ304 cannot
    tell a delisting from an outage, so fafnir holds it to the same rule. The rest
    of the stopped security's history is still judged."""
    active, stopped = _stale_feed_night(db, fake_dorq)
    settings = dorq.DorqSettings(path=str(fake_dorq), shadow_dir=str(tmp_path / "sh"))
    out = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert out.detected == {"dorq_bad_print": 1, "dorq_stale_feed": 1}
    assert {
        (r["check_name"], r["security_id"]) for r in dorq.read_shadow(out.shadow_file)
    } == {("dorq_stale_feed", active), ("dorq_bad_print", stopped)}


@pytest.mark.integration
def test_stale_feed_on_a_security_not_trading_never_reaches_the_queue(db, fake_dorq):
    active, stopped = _stale_feed_night(db, fake_dorq)
    out = dorq.run(db, dorq.DorqSettings(path=str(fake_dorq)), "NASDAQ", shadow=False)
    assert out.flagged == {"dorq_bad_print": 1, "dorq_stale_feed": 1}
    assert {
        (r["check_name"], r["security_id"])
        for r in db.fetchall(
            "SELECT check_name, security_id FROM ops.data_quality_flag"
        )
    } == {("dorq_stale_feed", active), ("dorq_bad_print", stopped)}


@pytest.mark.integration
def test_a_run_records_its_hash_what_was_left_out_and_how_long_it_took(
    db, fake_dorq, tmp_path, monkeypatch
):
    """The hash is dorq's own (the config file, its options and the calendar), so
    `dorq config show` cannot recompute it; the run keeps it. finished_at was now(),
    the transaction's start, which made every run take no time at all."""
    _stale_feed_night(db, fake_dorq)
    monkeypatch.setenv("FAKE_DORQ_SLEEP", "0.3")
    settings = dorq.DorqSettings(path=str(fake_dorq), shadow_dir=str(tmp_path / "sh"))
    with Database(db.dsn) as conn:
        dorq.run(conn, settings, "NASDAQ", shadow=True)
    run = db.fetchone(
        "SELECT status, params, finished_at - started_at AS took"
        " FROM ops.ingestion_run WHERE source = 'dorq'"
    )
    assert run["status"] == "success"
    backfill = run["params"].pop("backfill")
    # No SQL flags open: the snapshot `dq compare` reads is empty, not absent.
    assert run["params"].pop("holding")["states"] == {}
    assert run["params"] == {
        "dorq_version": "9.9.9-fake",
        "dorq_config_hash": "x",
        "dropped": {"dorq_stale_feed": 1},
        "since": None,
        "shadow": True,
        "full": False,
    }
    # The first night has no previous one to have missed anything since.
    assert backfill["securities"] == 0 and backfill["reports"] == 0
    assert run["took"] >= dt.timedelta(seconds=0.3)


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
def test_cli_says_a_judged_night_is_skipped_and_still_runs_the_sql_checks(
    db, fake_dorq, tmp_path, monkeypatch
):
    sid = _mk(db, "DQSKP")
    days = _sessions(db)
    _walk(db, sid, days[:40])
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n'
        f'[dq]\ndorq_path = "{fake_dorq}"\ndorq_shadow_dir = "{tmp_path / "sh"}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    runner = CliRunner()
    args = ["--config", str(rc), "dq", "run", "--engine", "both"]
    assert runner.invoke(cli.main, args).exit_code == 0

    again = runner.invoke(cli.main, args)
    assert again.exit_code == 0, again.output
    assert "DQ flags written:" in again.output
    assert f"dorq: {days[39].isoformat()} was judged by run" in again.output
    assert "--rerun" in again.output

    rerun = runner.invoke(cli.main, args + ["--rerun"])
    assert rerun.exit_code == 0, rerun.output
    assert "written to the shadow file" in rerun.output

    both = runner.invoke(cli.main, args + ["--rerun", "--full"])
    assert both.exit_code == 2
    assert "--full always runs" in both.output


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


@real_dorq
@pytest.mark.integration
def test_real_dorq_reports_a_zero_dividend(db):
    """On 2026-10-08 the first run on the host stopped at exit 3 over a dividend of
    0 (dorq 0.7.0). From 0.8.0 dorq reads it and reports it at warn (DQ705), and a
    dividend above the close is still an error."""
    days = _sessions(db)
    sid = _mk(db, "RDZD")
    _walk(db, sid, days, seed=5)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " dividend_amount) VALUES (%s, 'dividend', %s, 0), (%s, 'dividend', %s, 500)",
        (sid, days[100], sid, days[300]),
    )
    settings = dorq.DorqSettings(path=REAL_DORQ)
    run = dorq.run_dorq(db, settings, "NASDAQ", dorq.full_window(db, "NASDAQ"))
    implausible = {
        r["record_key"]["trade_date"]: r["severity"]
        for r in run.records
        if r["check_name"] == "dorq_dividend_implausible"
    }
    assert implausible == {
        days[100].isoformat(): "warn",
        days[300].isoformat(): "error",
    }


@real_dorq
@pytest.mark.integration
def test_real_dorq_stale_feed_is_left_out_for_a_security_not_trading(db):
    """The first shadow night reported DQ304 on 1,195 securities; 1,127 of them had
    stopped trading, such as CMCSV, delisted 192 sessions before the as-of date."""
    days = _sessions(db)
    live = _mk(db, "RDSL")
    quiet = _mk(db, "RDSQ")
    delisted = _mk(db, "RDSD")
    _walk(db, live, days, seed=21)
    _walk(db, quiet, days[:-60], seed=22)
    _walk(db, delisted, days[:-80], seed=23)
    _stop_trading(db, delisted)
    run = dorq.run_dorq(
        db,
        dorq.DorqSettings(path=REAL_DORQ),
        "NASDAQ",
        dorq.full_window(db, "NASDAQ"),
    )
    stale = {
        r["security_id"] for r in run.records if r["check_name"] == "dorq_stale_feed"
    }
    assert stale == {quiet}
    assert run.dropped == {"dorq_stale_feed": 1}


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
def test_recheck_closes_a_stale_feed_flag_once_the_security_stops_trading(
    db, fake_dorq
):
    """dorq still reports the stale feed; fafnir's rule leaves it out, as the SQL
    `stale` check would, so the flag is no longer emitted."""
    from fafnir.dq import recheck as rc

    active = _mk(db, "RCKSA")
    stopped = _mk(db, "RCKSD")
    days = _sessions(db)
    _walk(db, active, days[:50])
    _walk(db, stopped, days[:50], seed=3)
    rows = [
        _row(sid, days[40].isoformat(), check="dorq_stale_feed", code="DQ304")
        for sid in (active, stopped)
    ]
    dorq.ingest(db, rows)
    _stop_trading(db, stopped)
    _emit(fake_dorq, rows)
    (result,) = rc.recheck_dorq(db, dorq.DorqSettings(path=str(fake_dorq)))
    assert result.check_name == "dorq_stale_feed"
    assert result.open_flags == 2
    assert list(result.stale_flag_ids) == [
        db.fetchval(
            "SELECT dq_flag_id FROM ops.data_quality_flag WHERE security_id = %s",
            (stopped,),
        )
    ]
    # The note must not claim dorq stopped reporting it.
    assert "on a security no longer actively trading" in result.reason


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


# ---------------------------------------------------------------------------
# A second run for a night already judged
#
# On 2026-10-12 the daily ran `dq run` and fafnir-dq ran it again. The second
# dorq run took its --since from the watermark the first had just moved, so it
# reported from five sessions before *that* night, and its shadow file replaced
# the first's: `dq compare` saw a narrower night.
# ---------------------------------------------------------------------------


def _night(db, fake_dorq, tmp_path, symbol="DQRR"):
    sid = _mk(db, symbol)
    days = _sessions(db)
    _walk(db, sid, days[:300])
    _emit(fake_dorq, [_row(sid, days[296].isoformat())])
    settings = dorq.DorqSettings(path=str(fake_dorq), shadow_dir=str(tmp_path / "sh"))
    return sid, days, settings


@pytest.mark.integration
def test_a_second_run_for_the_same_night_judges_the_same_window(
    db, fake_dorq, tmp_path
):
    sid, days, settings = _night(db, fake_dorq, tmp_path)
    dorq.write_watermark(db, dorq.WATERMARK_SHADOW, days[290])  # the last night
    first = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert first.window.since == days[290 - dorq.SINCE_OVERLAP_SESSIONS]
    night = first.shadow_file.read_bytes()

    again = dorq.run(db, settings, "NASDAQ", shadow=True, rerun=True)
    assert again.window == first.window
    assert again.shadow_file == first.shadow_file
    assert again.shadow_file.read_bytes() == night

    # A new night reports from five sessions before this one, as it always did.
    _walk(db, sid, days[:301])
    later = dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW)
    assert later.as_of == days[300]
    assert later.since == days[299 - dorq.SINCE_OVERLAP_SESSIONS]


@pytest.mark.integration
def test_a_rerun_of_the_first_night_reports_the_whole_lookback_again(
    db, fake_dorq, tmp_path
):
    _, _, settings = _night(db, fake_dorq, tmp_path)
    first = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert first.window.since is None
    again = dorq.run(db, settings, "NASDAQ", shadow=True, rerun=True)
    assert again.window == first.window


@pytest.mark.integration
def test_the_queue_run_reproduces_its_own_night(db, fake_dorq, tmp_path):
    _, days, settings = _night(db, fake_dorq, tmp_path)
    dorq.write_watermark(db, dorq.WATERMARK_QUEUE, days[290])
    first = dorq.run(db, settings, "NASDAQ", shadow=False)
    assert first.window.since == days[290 - dorq.SINCE_OVERLAP_SESSIONS]
    again = dorq.run(db, settings, "NASDAQ", shadow=False, rerun=True)
    assert again.window == first.window
    # The shadow has had no night of its own.
    assert dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW).since is None


@pytest.mark.integration
def test_a_full_run_does_not_set_a_nightly_reruns_window(db, fake_dorq, tmp_path):
    """A --full run reports from no date at all. A nightly re-run of the same date
    takes the nightly's window, not that."""
    _, days, settings = _night(db, fake_dorq, tmp_path)
    dorq.write_watermark(db, dorq.WATERMARK_SHADOW, days[290])
    nightly = dorq.run(db, settings, "NASDAQ", shadow=True)
    full = dorq.run(db, settings, "NASDAQ", shadow=True, full=True)
    assert full.window.since is None and full.backfill is None
    assert db.fetchval(
        "SELECT params->'full' FROM ops.ingestion_run ORDER BY ingestion_run_id DESC"
        " LIMIT 1"
    )
    assert dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW) == (
        nightly.window
    )


@pytest.mark.integration
def test_a_night_already_judged_is_not_judged_again(db, fake_dorq, tmp_path):
    """2026-10-31: Sharadar's SFP delta answered 503, the load stopped with no new
    session, and fafnir-dq still ran. dorq re-judged 10-29, replaced that night's
    file with one carrying the day's repairs, and its history pass doubled.

    Without --rerun a night is judged once: no run row, no file written, no
    watermark moved, no call to dorq. The SQL checks are the CLI's, not this."""
    _, days, settings = _night(db, fake_dorq, tmp_path)
    dorq.write_watermark(db, dorq.WATERMARK_SHADOW, days[290])
    first = dorq.run(db, settings, "NASDAQ", shadow=True)
    night = first.shadow_file.read_bytes()
    runs = db.fetchval("SELECT count(*) FROM ops.ingestion_run WHERE source = 'dorq'")
    calls = (fake_dorq.parent / "calls.txt").read_text().splitlines()

    _emit(fake_dorq, [])  # a re-run would now write an empty file
    again = dorq.run(db, settings, "NASDAQ", shadow=True)

    run_id = db.fetchval(
        "SELECT max(ingestion_run_id) FROM ops.ingestion_run WHERE source = 'dorq'"
    )
    assert again.judged_by["ingestion_run_id"] == run_id
    assert again.shadow_file is None and again.detected == {}
    assert first.shadow_file.read_bytes() == night
    assert (
        db.fetchval("SELECT count(*) FROM ops.ingestion_run WHERE source = 'dorq'")
        == runs
    )
    assert dorq.read_watermark(db, dorq.WATERMARK_SHADOW) == days[299]
    assert (fake_dorq.parent / "calls.txt").read_text().splitlines() == calls

    # --rerun judges it again, over the span that night's run used.
    rerun = dorq.run(db, settings, "NASDAQ", shadow=True, rerun=True)
    assert rerun.judged_by is None and rerun.window == first.window
    assert dorq.read_shadow(first.shadow_file) == []


@pytest.mark.integration
def test_a_skipped_night_leaves_nothing_out_of_the_next_nights_pass(
    db, fake_dorq, tmp_path, monkeypatch
):
    """The skip costs nothing: a security minted after the judged night, while the
    next run had no session to judge, is in the following night's history pass,
    which is measured from the last run that judged a night."""
    old, new, days, settings = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    # The judged night is days[299]. Take days[300] away for a moment, as a failed
    # load would, so the next run has nothing newer.
    db.execute("DELETE FROM core.daily_price WHERE trade_date = %s", (days[300],))
    skipped = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert skipped.judged_by is not None

    _walk(db, old, days[:301])
    _walk(db, new, days[:301], seed=3, bad={days[100]: 3.0})
    out = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert out.judged_by is None and out.window.as_of == days[300]
    assert out.backfill["security_ids"] == [new]


@pytest.mark.integration
def test_a_night_only_a_full_run_judged_is_not_judged_again(db, fake_dorq, tmp_path):
    """A --full run's file stands for its night too: a nightly on the same date,
    with nothing new loaded, leaves it in place."""
    _, days, settings = _night(db, fake_dorq, tmp_path)
    full = dorq.run(db, settings, "NASDAQ", shadow=True, full=True)
    whole = full.shadow_file.read_bytes()
    nightly = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert nightly.judged_by is not None
    assert full.shadow_file.read_bytes() == whole
    # --full itself always runs.
    assert dorq.run(db, settings, "NASDAQ", shadow=True, full=True).judged_by is None


@pytest.mark.integration
def test_a_night_only_a_full_run_judged_reports_from_five_sessions_back(
    db, fake_dorq, tmp_path
):
    _, days, settings = _night(db, fake_dorq, tmp_path)
    dorq.run(db, settings, "NASDAQ", shadow=True, full=True)
    window = dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW)
    assert window.since == days[299 - dorq.SINCE_OVERLAP_SESSIONS]


# ---------------------------------------------------------------------------
# Backfill: history behind the window that is new or was rewritten
#
# CELG-RI was minted on 2026-10-13 with bars back to 2022. The SQL checks, which
# have no window, wrote ten outliers on its history that night. dorq read bars
# from 2025-09-30 and reported from 10-05, so it never judged them.
# ---------------------------------------------------------------------------


def _backfill_world(db, fake_dorq, tmp_path, monkeypatch):
    """One night judged (shadow and queue), then a security minted with years of
    history and a x3 print in it. Returns the old security, the new one, the
    sessions, and the settings."""
    monkeypatch.setenv("FAKE_DORQ_FILTER", "1")
    days = _sessions(db)
    old = _mk(db, "DQBFO")
    _walk(db, old, days[:300])
    settings = dorq.DorqSettings(path=str(fake_dorq), shadow_dir=str(tmp_path / "sh"))
    for endpoint, shadow in (
        (dorq.WATERMARK_SHADOW, True),
        (dorq.WATERMARK_QUEUE, False),
    ):
        dorq.write_watermark(db, endpoint, days[290])
        dorq.run(db, settings, "NASDAQ", shadow=shadow)

    new = _mk(db, "DQBFN")
    _walk(db, new, days[:301], seed=3, bad={days[100]: 3.0})
    _walk(db, old, days[:301])
    _emit(
        fake_dorq,
        [
            _row(new, days[100].isoformat()),  # the x3 print, long behind the window
            _row(new, days[150].isoformat(), check="dorq_stale_feed", code="DQ304"),
            _row(None, days[100].isoformat(), check="dorq_cohort_move", code="DQ601"),
            _row(new, days[298].isoformat()),  # inside the window: the nightly's
            _row(old, days[100].isoformat()),  # old history, not new: never judged
        ],
    )
    (fake_dorq.parent / "calls.txt").unlink(missing_ok=True)
    return old, new, days, settings


def _reports(path):
    return sorted(
        (r["check_name"], r["security_id"], r["record_key"]["trade_date"])
        for r in dorq.read_shadow(path)
    )


@pytest.mark.integration
def test_a_new_securitys_history_behind_the_window_is_judged(
    db, fake_dorq, tmp_path, monkeypatch
):
    old, new, days, settings = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    out = dorq.run(db, settings, "NASDAQ", shadow=True)
    since = days[299 - dorq.SINCE_OVERLAP_SESSIONS]
    assert out.window.since == since
    # The x3 print is reported; the pass's cohort and stale-feed rows are not.
    assert _reports(out.shadow_file) == [
        ("dorq_bad_print", new, days[100].isoformat()),
        ("dorq_bad_print", new, days[298].isoformat()),
    ]
    assert out.detected == {"dorq_bad_print": 2}
    expected = {
        "securities": 1,
        "security_ids": [new],
        "before": since.isoformat(),
        "reports": 1,
    }
    assert out.backfill == expected
    assert (
        db.fetchval(
            "SELECT params->'backfill' FROM ops.ingestion_run"
            " ORDER BY ingestion_run_id DESC LIMIT 1"
        )
        == expected
    )
    # The pass is the whole history of the new security alone, with no --since.
    calls = (fake_dorq.parent / "calls.txt").read_text().splitlines()
    assert len(calls) == 2
    assert "--since" in calls[0] and "--since" not in calls[1]
    sent = {
        line.split(",")[0]
        for line in (fake_dorq.parent / "stdin.csv").read_text().splitlines()[1:]
    }
    assert sent == {str(new)}

    # The next night it is not new.
    _walk(db, old, days[:302])
    nxt = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert nxt.backfill["securities"] == 0
    assert len((fake_dorq.parent / "calls.txt").read_text().splitlines()) == 3


@pytest.mark.integration
def test_a_rerun_of_the_night_keeps_the_pass(db, fake_dorq, tmp_path, monkeypatch):
    """A re-run measures "new" from the night before, not from the first run of
    the same night, which would leave the pass empty and the file without it."""
    _, new, _, settings = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    first = dorq.run(db, settings, "NASDAQ", shadow=True)
    night = first.shadow_file.read_bytes()
    again = dorq.run(db, settings, "NASDAQ", shadow=True, rerun=True)
    assert again.backfill["security_ids"] == [new]
    assert again.shadow_file.read_bytes() == night


@pytest.mark.integration
def test_the_queue_run_takes_the_pass_too(db, fake_dorq, tmp_path, monkeypatch):
    _, new, days, settings = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    out = dorq.run(db, settings, "NASDAQ", shadow=False)
    assert out.flagged == {"dorq_bad_print": 2}
    assert out.backfill["security_ids"] == [new]
    assert {
        r["d"]
        for r in db.fetchall(
            "SELECT record_key->>'trade_date' AS d FROM ops.data_quality_flag"
            " WHERE security_id = %s",
            (new,),
        )
    } == {days[100].isoformat(), days[298].isoformat()}


@pytest.mark.integration
def test_over_the_cap_the_pass_is_skipped_with_a_warning(
    db, fake_dorq, tmp_path, monkeypatch, caplog
):
    import dataclasses

    _, new, days, settings = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    settings = dataclasses.replace(settings, backfill_max=0)
    monkeypatch.setattr(dorq.logger, "propagate", True)
    with caplog.at_level("WARNING"):
        out = dorq.run(db, settings, "NASDAQ", shadow=True)
    assert "dorq_backfill_max" in caplog.text
    assert out.backfill["securities"] == 1
    assert out.backfill["security_ids"] == []
    assert out.backfill["skipped"] == "over dorq_backfill_max (0)"
    assert _reports(out.shadow_file) == [("dorq_bad_print", new, days[298].isoformat())]
    assert len((fake_dorq.parent / "calls.txt").read_text().splitlines()) == 1


@pytest.mark.integration
def test_an_operator_rewrite_behind_the_window_puts_its_security_in_the_pass(
    db, fake_dorq, tmp_path
):
    days = _sessions(db)
    names = ("DQOPR", "DQOPD", "DQOPS", "DQOPT", "DQOPV", "DQOPQ")
    rescaled, deleted, source, dest, revoked, quiet = (_mk(db, n) for n in names)
    for i, sid in enumerate((rescaled, deleted, source, dest, revoked, quiet)):
        _walk(db, sid, days[:300], seed=i + 1)

    def override(sid, day, detail="{}"):
        return db.fetchval(
            "INSERT INTO ops.operator_override (security_id, target, key_date,"
            " operation, detail, note, created_by)"
            " VALUES (%s, 'daily_price', %s, 'delete', %s::jsonb, 'n', 't')"
            " RETURNING override_id",
            (sid, day, detail),
        )

    # Made before the last night: already judged.
    undone = override(revoked, days[20])
    override(quiet, days[21])
    settings = dorq.DorqSettings(path=str(fake_dorq), shadow_dir=str(tmp_path / "sh"))
    dorq.write_watermark(db, dorq.WATERMARK_SHADOW, days[290])
    dorq.run(db, settings, "NASDAQ", shadow=True)

    # Since: an era rescaled, long behind the window.
    olds = repo.stored_bars(db, rescaled, days[10], days[12])
    repo.replace_operator_bars(
        db,
        security_id=rescaled,
        kind="rescale",
        params={"price_factor": "10", "volume_factor": "0.1"},
        changes=[
            (o, {**o, **{k: o[k] * 10 for k in ("open", "high", "low", "close")}})
            for o in olds
        ],
        note="cents era",
        created_by="t",
    )
    # A bar deleted inside the window: the nightly run judges that itself.
    repo.delete_operator_bars(
        db, security_id=deleted, trade_dates=[days[298]], note="bad", created_by="t"
    )
    # History moved from one security to another: both sides changed.
    override(source, days[30], json.dumps({"split": {"destination_security_id": dest}}))
    # An older edit taken back.
    db.execute(
        "UPDATE ops.operator_override SET revoked_at = now(), revoked_by = 't'"
        " WHERE override_id = %s",
        (undone,),
    )
    _walk(db, quiet, days[:301], seed=6)  # the next session

    window = dorq.nightly_window(db, "NASDAQ", 260, dorq.WATERMARK_SHADOW)
    assert dorq.backfill_securities(db, dorq.WATERMARK_SHADOW, window) == sorted(
        [rescaled, source, dest, revoked]
    )
    # Its own endpoint: the queue has had no night, so nothing is "since" it.
    assert dorq.backfill_securities(db, dorq.WATERMARK_QUEUE, window) == []


@pytest.mark.integration
def test_cli_says_what_the_pass_judged(db, fake_dorq, tmp_path, monkeypatch):
    _, _, _, _ = _backfill_world(db, fake_dorq, tmp_path, monkeypatch)
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n'
        f'[dq]\ndorq_path = "{fake_dorq}"\ndorq_shadow_dir = "{tmp_path / "sh"}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "run", "--engine", "dorq"]
    )
    assert result.exit_code == 0, result.output
    assert "backfill: 1 security with history before" in result.output
    assert "1 violation, included above" in result.output
