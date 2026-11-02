"""dorq on the statements: export, run, ingest, shadow, compare, recheck, status
(dorq plan §5, DR-0912). Every statement row is synthetic.

Most of these run a *fake* dorq, which records each call and prints canned
fafnir-format rows: FAKE_DORQ_ROWS for a bars run, FAKE_DORQ_STATEMENT_ROWS for a
statements run (``--kind statements``). With FAKE_DORQ_FILTER it prints only the
rows a real dorq could: on a security it was sent rows or metadata for, or on none
(a cohort). The tests marked ``real_dorq`` run the binary named by
``FAFNIR_TEST_DORQ`` and are skipped without it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from datetime import timezone

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import compare as cmp
from fafnir.dq import compare_statements as cst
from fafnir.dq import dorq, dorq_statements
from fafnir.dq import recheck as rc
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

REAL_DORQ = os.environ.get("FAFNIR_TEST_DORQ", "")
real_dorq = pytest.mark.skipif(
    not REAL_DORQ, reason="set FAFNIR_TEST_DORQ to a dorq binary to run"
)
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = dt.datetime(2024, 1, 5, 12, tzinfo=timezone.utc)

_FAKE = """#!{python}
import csv, io, json, os, sys, pathlib
here = pathlib.Path(__file__).parent
if "--version" in sys.argv:
    print("9.9.9-fake")
    sys.exit(0)
calls = here / "calls.txt"
n = len(calls.read_text().splitlines()) if calls.exists() else 0
data = sys.stdin.buffer.read()
(here / f"stdin-{{n}}.csv").write_bytes(data)
(here / f"args-{{n}}.txt").write_text("\\n".join(sys.argv[1:]))
with calls.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
for name in ("--calendar-file", "--actions", "--meta", "--items"):
    if name in sys.argv:
        src = pathlib.Path(sys.argv[sys.argv.index(name) + 1])
        (here / f"{{name.strip('-')}}-{{n}}.csv").write_bytes(src.read_bytes())
statements = "--kind" in sys.argv and "statements" in sys.argv
rows = os.environ.get("FAKE_DORQ_STATEMENT_ROWS" if statements else "FAKE_DORQ_ROWS")
if not rows:
    sys.exit(0)
lines = pathlib.Path(rows).read_text().splitlines()
if os.environ.get("FAKE_DORQ_FILTER"):
    sent = {{r["security_id"] for r in csv.DictReader(io.StringIO(data.decode()))}}
    if "--meta" in sys.argv:
        meta = pathlib.Path(sys.argv[sys.argv.index("--meta") + 1]).read_text()
        sent |= {{r["security_id"] for r in csv.DictReader(io.StringIO(meta))}}
    lines = [
        ln for ln in lines
        if json.loads(ln)["security_id"] is None
        or str(json.loads(ln)["security_id"]) in sent
    ]
sys.stdout.write("".join(ln + "\\n" for ln in lines))
"""


@pytest.fixture()
def fake_dorq(tmp_path, monkeypatch):
    path = tmp_path / "dorq"
    path.write_text(_FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    for name, env in (
        ("rows.jsonl", "FAKE_DORQ_ROWS"),
        ("statement-rows.jsonl", "FAKE_DORQ_STATEMENT_ROWS"),
    ):
        (tmp_path / name).write_text("")
        monkeypatch.setenv(env, str(tmp_path / name))
    monkeypatch.delenv("FAKE_DORQ_FILTER", raising=False)
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DORQ_PATH", raising=False)
    return path


def _calls(fake) -> list[str]:
    path = fake.parent / "calls.txt"
    return path.read_text().splitlines() if path.exists() else []


def _emit(fake, rows, *, statements=True):
    name = "statement-rows.jsonl" if statements else "rows.jsonl"
    (fake.parent / name).write_text("".join(json.dumps(r) + "\n" for r in rows))


def _statement_row(
    sid,
    fiscal_date,
    *,
    check="dorq_identity_break",
    code="DQ903",
    severity="error",
    statement="balance_sheet",
    period_type="quarter",
    version=1,
    fiscal_year=None,
    extra_key=None,
    table=None,
    inner=None,
):
    key = {
        "statement": statement,
        "period_type": period_type,
        "fiscal_date": fiscal_date,
        "version_no": version,
        **(extra_key or {}),
    }
    return {
        "security_id": sid,
        "table_name": table or fm.TABLES[statement],
        "record_key": key,
        "check_name": check,
        "severity": severity,
        "detail": {
            "series": str(sid),
            "date": fiscal_date,
            "code": code,
            "p_error": 0.97,
            "statement": statement,
            "period_type": period_type,
            "fiscal_year": fiscal_year or int(fiscal_date[:4]),
            "version_no": version,
            "detail": inner or {},
            "dorq": {"version": "9.9.9-fake", "config_hash": "s"},
        },
    }


def _bars_row(sid, day):
    return {
        "security_id": sid,
        "table_name": "core.daily_price",
        "record_key": {"trade_date": day},
        "check_name": "dorq_bad_print",
        "severity": "error",
        "detail": {
            "series": str(sid),
            "date": day,
            "code": "DQ201",
            "p_error": 0.99,
            "dorq": {"version": "9.9.9-fake", "config_hash": "b"},
        },
    }


def _security(db, symbol, *, currency="USD", active=True, delisted=None):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NASDAQ",
        source="sharadar",
    )
    db.execute(
        "UPDATE core.security SET currency = %s, is_actively_trading = %s,"
        " delisted_date = %s WHERE security_id = %s",
        (currency, active, delisted, sid),
    )
    return sid


def _expect_statements(db, sid, vendor_key):
    """An open Sharadar link to a fundamentals master row with a first quarter:
    the security's statements are expected."""
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
        " first_quarter) VALUES ('sharadar', 'fundamentals', %s, %s, '2020-03-31')",
        (vendor_key, f"T{vendor_key}"),
    )
    db.execute(
        "INSERT INTO core.security_vendor_xref (security_id, vendor, vendor_key,"
        " valid_from, match_method, created_by) VALUES (%s, 'sharadar', %s,"
        " '2020-01-01', 'manual', 'test')",
        (sid, vendor_key),
    )


def _put(
    db,
    statement,
    sid,
    fiscal_date,
    *,
    label,
    known_from,
    version=1,
    known_to=None,
    loaded_at=T0,
    superseded_at=None,
    currency="USD",
    **items,
):
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900050",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version,
        "known_from": known_from,
        "known_to": known_to,
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": loaded_at,
        "superseded_at": superseded_at,
        "reported_currency": currency,
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _prices(db, sid, days, close=10.0):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": close + i * 0.01,
                "high": close + i * 0.01,
                "low": close + i * 0.01,
                "close": close + i * 0.01,
                "volume": 1000,
            }
            for i, d in enumerate(days)
        ],
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


def _company(db, symbol="STMA"):
    """A security with prices through 2024 and two quarters of statements, the
    first restated once and corrected once by the vendor."""
    sid = _security(db, symbol)
    days = _sessions(db)
    _prices(db, sid, days)
    q1, q2 = dt.date(2024, 3, 31), dt.date(2024, 6, 30)
    for statement, items in (
        ("income", {"revenue": 100, "net_income": 10}),
        ("balance_sheet", {"total_assets": 500, "total_liabilities": 300}),
        ("cash_flow", {"net_cash_operating": 20}),
    ):
        _put(
            db,
            statement,
            sid,
            q1,
            label="2024-Q1",
            known_from=dt.date(2024, 5, 2),
            known_to=dt.date(2024, 9, 3),
            **items,
        )
        _put(
            db,
            statement,
            sid,
            q1,
            label="2024-Q1",
            known_from=dt.date(2024, 9, 3),
            version=2,
            **items,
        )
        _put(
            db,
            statement,
            sid,
            q2,
            label="2024-Q2",
            known_from=dt.date(2024, 8, 1),
            superseded_at=T0 + dt.timedelta(days=1),
            **items,
        )
        _put(
            db,
            statement,
            sid,
            q2,
            label="2024-Q2",
            known_from=dt.date(2024, 8, 1),
            loaded_at=T0 + dt.timedelta(days=1),
            **items,
        )
    return sid, days


def _settings(fake, tmp_path, **kw):
    return dorq.DorqSettings(path=str(fake), shadow_dir=str(tmp_path / "sh"), **kw)


def _rc(tmp_path, db, fake, **dq):
    path = tmp_path / "fafnirrc"
    lines = [
        f'[database]\ndsn = "{db.dsn}"\n[dq]\n',
        f'dorq_path = "{fake}"\ndorq_shadow_dir = "{tmp_path / "sh"}"\n',
    ]
    for k, v in dq.items():
        lines.append(f"{k} = {json.dumps(v)}\n")
    path.write_text("".join(lines))
    return path


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_the_export_streams_current_versions_with_close_items_and_meta(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    expected = _security(
        db, "STMX", currency="EUR", active=False, delisted=dt.date(2024, 6, 28)
    )
    _expect_statements(db, expected, "777001")
    _prices(db, expected, days[:30], close=40.0)
    result = dorq_statements.run_statements(
        db, _settings(fake_dorq, tmp_path), dt.date(2024, 12, 31)
    )
    assert result.version == "9.9.9-fake" and result.records == []
    here = fake_dorq.parent
    args = (here / "args-0.txt").read_text().splitlines()
    for flag in ("--kind", "--items", "--actions", "--meta", "--show-info"):
        assert flag in args
    assert args[args.index("--kind") + 1] == "statements"
    assert args[args.index("--as-of") + 1] == "2024-12-31"
    assert "--isolated" in args and "--calendar-file" not in args

    lines = (here / "stdin-0.csv").read_text().splitlines()
    header = lines[0].split(",")
    items = dorq_statements.line_items(db)
    assert header == list(dorq_statements._FIELDS) + ["close"] + [
        i["column_name"] for i in items
    ]
    rows = [dict(zip(header, ln.split(","))) for ln in lines[1:]]
    # Current versions only: the superseded Q2 rows are not sent, the restated Q1's
    # two versions are. Ordered by statement, period type, fiscal date, version.
    assert [(r["statement"], r["fiscal_date"], r["version_no"]) for r in rows] == [
        (s, d, v)
        for s in ("balance_sheet", "cash_flow", "income")
        for d, v in (("2024-03-31", "1"), ("2024-03-31", "2"), ("2024-06-30", "1"))
    ]
    # The close on or before known_from: 2024-05-02 is a session, 2024-09-03 too.
    by = {(r["statement"], r["known_from"]): r for r in rows}
    closes = {
        r["trade_date"]: r["close"]
        for r in db.fetchall(
            "SELECT trade_date, close FROM core.daily_price WHERE security_id = %s",
            (sid,),
        )
    }
    assert float(by[("income", "2024-05-02")]["close"]) == float(
        closes[dt.date(2024, 5, 2)]
    )
    # Another statement's items are empty on a row.
    income = by[("income", "2024-05-02")]
    assert income["revenue"] == "100.00" and income["total_assets"] == ""
    assert by[("balance_sheet", "2024-05-02")]["total_assets"] == "500.00"
    assert income["loaded_on"] == "2024-01-05"

    item_lines = (here / "items-0.csv").read_text().splitlines()
    assert item_lines[0] == "column,statement,item_class,sign_convention,is_subtotal"
    assert len(item_lines) == 1 + len(items) == 59

    meta = {
        ln.split(",")[0]: ln.split(",")
        for ln in (here / "meta-0.csv").read_text().splitlines()
    }
    assert meta["security_id"] == [
        "security_id",
        "asset_type",
        "exchange",
        "currency",
        "first_trade_date",
        "last_trade_date",
        "active",
        "delisted_date",
        "sector",
        "industry",
        "statements_expected",
    ]
    assert meta[str(sid)][3:8] == ["USD", "2023-01-03", "2024-12-31", "true", ""]
    assert meta[str(sid)][-1] == "false"
    assert meta[str(expected)][3:8] == [
        "EUR",
        days[0].isoformat(),
        days[29].isoformat(),
        "false",
        "2024-06-28",
    ]
    assert meta[str(expected)][-1] == "true"


def test_a_recheck_export_is_scoped_to_its_securities(db, fake_dorq, tmp_path):
    a, _ = _company(db, "STMA")
    b, _ = _company(db, "STMB")
    dorq_statements.run_statements(
        db, _settings(fake_dorq, tmp_path), dt.date(2024, 12, 31), security_ids=[b]
    )
    here = fake_dorq.parent
    sent = {
        ln.split(",")[0] for ln in (here / "stdin-0.csv").read_text().splitlines()[1:]
    }
    assert sent == {str(b)}
    meta = (here / "meta-0.csv").read_text().splitlines()
    assert [ln.split(",")[0] for ln in meta[1:]] == [str(b)]
    assert a != b


# ---------------------------------------------------------------------------
# The night
# ---------------------------------------------------------------------------


def test_a_statements_night_in_shadow_writes_its_file_and_not_the_queue(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    _emit(
        fake_dorq,
        [
            _statement_row(sid, "2024-03-31", extra_key={"identity": "balance_sheet"}),
            # A finding on no one statement, as an --isolated run files it.
            {
                **_statement_row(
                    sid,
                    "2023-01-03",
                    check="dorq_statements_missing",
                    code="DQ943",
                    severity="warn",
                ),
                "table_name": "core.daily_price",
                "record_key": {"fiscal_date": "2023-01-03"},
            },
        ],
    )
    settings = _settings(fake_dorq, tmp_path)
    out = dorq_statements.run(db, settings, "NASDAQ", shadow=True)
    path = tmp_path / "sh" / "statements" / f"{days[-1].isoformat()}.jsonl"
    assert out.shadow_file == path
    assert out.detected == {"dorq_identity_break": 1, "dorq_statements_missing": 1}
    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == 0
    rows = dorq.read_shadow(path)
    assert [r["table_name"] for r in rows] == [
        "core.balance_sheet",
        "core.income_statement",
    ]
    # Never the shadow directory's top level, where `dq compare` reads bars.
    assert list((tmp_path / "sh").glob("*.jsonl")) == []
    run = db.fetchone(
        "SELECT source, endpoint, status, window_from, window_to, params"
        " FROM ops.ingestion_run ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert run["source"] == "dorq" and run["endpoint"] == "dq-statements-shadow"
    assert run["status"] == "success" and run["window_to"] == days[-1]
    assert run["window_from"] is None
    assert run["params"]["kind"] == "statements" and run["params"]["full"] is True
    assert (
        run["params"]["reports"] == 2 and run["params"]["dorq_version"] == "9.9.9-fake"
    )
    assert dorq.read_watermark(db, dorq_statements.ENDPOINT_SHADOW) == days[-1]
    # The bars run's watermarks are untouched.
    assert dorq.read_watermark(db, dorq.WATERMARK_SHADOW) is None

    # A night is judged once; --rerun judges it again.
    again = dorq_statements.run(db, settings, "NASDAQ", shadow=True)
    assert again.judged_by is not None and len(_calls(fake_dorq)) == 1
    dorq_statements.run(db, settings, "NASDAQ", shadow=True, rerun=True)
    assert len(_calls(fake_dorq)) == 2


def test_a_statements_night_leaves_dq_compare_on_bars_unchanged(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    _emit(fake_dorq, [_bars_row(sid, days[-3].isoformat())], statements=False)
    _emit(
        fake_dorq,
        [_statement_row(sid, "2024-03-31", extra_key={"identity": "balance_sheet"})],
    )
    settings = _settings(fake_dorq, tmp_path)
    bars = dorq.run(db, settings, "NASDAQ", shadow=True)
    before = cmp.compare(db, bars.shadow_file).as_dict()
    assert cmp.latest_shadow_file(settings.shadow_dir) == bars.shadow_file

    dorq_statements.run(db, settings, "NASDAQ", shadow=True)
    assert cmp.latest_shadow_file(settings.shadow_dir) == bars.shadow_file
    assert cmp.compare(db, bars.shadow_file).as_dict() == before


def test_the_bars_run_is_byte_identical_with_dorq_statements_on_and_off(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    _emit(fake_dorq, [_bars_row(sid, days[-3].isoformat())], statements=False)
    _emit(fake_dorq, [_statement_row(sid, "2024-03-31")])
    runner = CliRunner()
    bars_file = tmp_path / "sh" / f"{days[-1].isoformat()}.jsonl"
    outputs = {}
    for on in (False, True):
        # Back to the first night. (TRUNCATE would cascade to every table whose
        # rows name a run, prices included.)
        db.execute("DELETE FROM ops.load_watermark WHERE source = 'dorq'")
        db.execute("DELETE FROM ops.ingestion_run WHERE source = 'dorq'")
        rcfile = _rc(tmp_path, db, fake_dorq, engine="dorq", dorq_statements=on)
        start = len(_calls(fake_dorq))
        result = runner.invoke(cli.main, ["--config", str(rcfile), "dq", "run"])
        assert result.exit_code == 0, result.output
        here = fake_dorq.parent
        outputs[on] = (
            bars_file.read_bytes(),
            (here / f"stdin-{start}.csv").read_bytes(),
            re.sub(
                r"/fafnir-dorq-[^/]+/",
                "/tmp/",
                (here / f"args-{start}.txt").read_text(),
            ),
            (here / f"meta-{start}.csv").read_bytes(),
        )
        statements = "dorq statements 9.9.9-fake" in result.output
        assert statements == on
        assert len(_calls(fake_dorq)) - start == (2 if on else 1)
    assert outputs[True] == outputs[False]
    assert (tmp_path / "sh" / "statements" / f"{days[-1].isoformat()}.jsonl").is_file()


def test_cli_statements_only_runs_no_sql_checks_and_no_bars(db, fake_dorq, tmp_path):
    sid, days = _company(db)
    _emit(fake_dorq, [_statement_row(sid, "2024-03-31")])
    rcfile = _rc(tmp_path, db, fake_dorq)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rcfile), "dq", "run", "--statements-only"]
    )
    assert result.exit_code == 0, result.output
    assert "DQ flags written" not in result.output
    assert f"dorq statements 9.9.9-fake (as of {days[-1].isoformat()}," in result.output
    assert "1 violation written to the shadow file" in result.output
    (call,) = _calls(fake_dorq)
    assert "--kind statements" in call

    bad = CliRunner().invoke(
        cli.main, ["--config", str(rcfile), "dq", "run", "--statements-only", "--full"]
    )
    assert bad.exit_code == 2

    sql = CliRunner().invoke(
        cli.main,
        ["--config", str(rcfile), "dq", "run", "--engine", "sql", "--statements"],
    )
    assert sql.exit_code == 0, sql.output
    assert "dorq statements: not run, the engine is sql" in sql.output
    assert len(_calls(fake_dorq)) == 1


def test_ingest_writes_statement_flags_with_the_open_and_accepted_guards(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    rows = [
        _statement_row(sid, "2024-03-31", extra_key={"identity": "balance_sheet"}),
        _statement_row(
            sid,
            "2024-06-30",
            check="dorq_bad_value",
            code="DQ911",
            statement="income",
            extra_key={"item": "revenue"},
        ),
        # Two cohorts on one date, which dorq keys on the date alone.
        {
            **_statement_row(
                None,
                "2024-08-01",
                check="dorq_revision_cohort",
                code="DQ951",
                severity="info",
                inner={"members": "1,2"},
            ),
            "table_name": "core.income_statement",
            "record_key": {"trade_date": "2024-08-01"},
        },
        {
            **_statement_row(
                None,
                "2024-08-01",
                check="dorq_revision_cohort",
                code="DQ951",
                severity="warn",
                inner={"members": "1,3", "codes": "DQ903"},
            ),
            "table_name": "core.income_statement",
            "record_key": {"trade_date": "2024-08-01"},
        },
    ]
    _emit(fake_dorq, rows)
    settings = _settings(fake_dorq, tmp_path)
    out = dorq_statements.run(db, settings, "NASDAQ", shadow=False)
    assert out.flagged == {
        "dorq_bad_value": 1,
        "dorq_identity_break": 1,
        "dorq_revision_cohort": 2,
    }
    flags = db.fetchall(
        "SELECT check_name, table_name, record_key FROM ops.data_quality_flag"
        " ORDER BY check_name, dq_flag_id"
    )
    assert [f["table_name"] for f in flags] == [
        "core.income_statement",
        "core.balance_sheet",
        "core.income_statement",
        "core.income_statement",
    ]
    assert {f["record_key"].get("cohort") for f in flags[2:]} == {
        "revisions",
        "findings",
    }
    run = db.fetchone(
        "SELECT endpoint FROM ops.ingestion_run ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert run["endpoint"] == "dq-statements"

    # Open: the next night writes none again.
    assert (
        dorq_statements.run(db, settings, "NASDAQ", shadow=False, rerun=True).flagged
        == {}
    )
    # Accepted: closed, and not written again either.
    db.execute(
        "UPDATE ops.data_quality_flag SET accepted_at = now(), resolved_at = now(),"
        " resolved_by = 'test' WHERE check_name = 'dorq_bad_value'"
    )
    assert (
        dorq_statements.run(db, settings, "NASDAQ", shadow=False, rerun=True).flagged
        == {}
    )
    # Resolved (not accepted): written again, as any check's would be.
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 'test'"
        " WHERE check_name = 'dorq_identity_break'"
    )
    again = dorq_statements.run(db, settings, "NASDAQ", shadow=False, rerun=True)
    assert again.flagged == {"dorq_identity_break": 1}


def test_a_failed_statements_run_is_recorded_and_moves_no_watermark(
    db, fake_dorq, tmp_path, monkeypatch
):
    _company(db)
    (fake_dorq.parent / "statement-rows.jsonl").write_text("not json\n")
    with pytest.raises(Exception):
        dorq_statements.run(db, _settings(fake_dorq, tmp_path), "NASDAQ", shadow=True)
    run = db.fetchone(
        "SELECT endpoint, status, error_message FROM ops.ingestion_run"
        " ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert run["endpoint"] == "dq-statements-shadow" and run["status"] == "failed"
    assert dorq.read_watermark(db, dorq_statements.ENDPOINT_SHADOW) is None


# ---------------------------------------------------------------------------
# Recheck
# ---------------------------------------------------------------------------


def _flag(db, sid, row):
    db.execute(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detail, detected_at) VALUES (%s, %s, %s, %s, %s,"
        " %s, now())",
        (
            sid,
            row["table_name"],
            json.dumps(row["record_key"]),
            row["check_name"],
            row["severity"],
            json.dumps(row["detail"]),
        ),
    )


def test_recheck_closes_a_fixed_condition_and_leaves_statement_key_alone(
    db, fake_dorq, tmp_path, monkeypatch
):
    a, days = _company(db, "STMA")
    b, _ = _company(db, "STMB")
    fixed = _statement_row(
        a,
        "2024-06-30",
        check="dorq_bad_value",
        code="DQ911",
        statement="income",
        extra_key={"item": "revenue"},
    )
    holds = _statement_row(a, "2024-03-31", extra_key={"identity": "balance_sheet"})
    chain = _statement_row(a, "2024-03-31", check="dorq_statement_key", code="DQ901")
    bars = _bars_row(b, days[-3].isoformat())
    for sid, row in ((a, fixed), (a, holds), (a, chain), (b, bars)):
        _flag(db, sid, row)
    # The re-runs: the statements still break the identity (not the bad value),
    # and the bars still have the bad print.
    _emit(fake_dorq, [holds, chain])
    _emit(fake_dorq, [bars], statements=False)
    monkeypatch.setenv("FAKE_DORQ_FILTER", "1")

    assert set(rc.open_dorq_checks(db)) == {
        "dorq_bad_print",
        "dorq_bad_value",
        "dorq_identity_break",
    }
    results = {
        r.check_name: r for r in rc.recheck_dorq(db, _settings(fake_dorq, tmp_path))
    }
    assert set(results) == {"dorq_bad_print", "dorq_bad_value", "dorq_identity_break"}
    closed = {
        int(r["dq_flag_id"])
        for r in db.fetchall(
            "SELECT dq_flag_id FROM ops.data_quality_flag"
            " WHERE check_name = 'dorq_bad_value'"
        )
    }
    assert set(results["dorq_bad_value"].stale_flag_ids) == closed
    assert "statements history" in results["dorq_bad_value"].reason
    # Each flag is judged by a run of its own kind: a bars re-run never closes a
    # statement flag, nor a statements re-run a bars flag.
    assert results["dorq_identity_break"].stale_flag_ids == []
    assert results["dorq_bad_print"].stale_flag_ids == []
    calls = _calls(fake_dorq)
    assert len(calls) == 2
    statements_call = next(c for c in calls if "--kind statements" in c)
    n = calls.index(statements_call)
    meta = (fake_dorq.parent / f"meta-{n}.csv").read_text().splitlines()
    assert [ln.split(",")[0] for ln in meta[1:]] == [str(a)]

    with pytest.raises(ValueError, match="not re-evaluable"):
        rc.recheck_dorq(
            db, _settings(fake_dorq, tmp_path), checks=["dorq_statement_key"]
        )
    with pytest.raises(ValueError, match="not re-evaluable"):
        rc.recheck_dorq(
            db, _settings(fake_dorq, tmp_path), checks=["dorq_revision_cohort"]
        )


def test_cli_recheck_closes_the_fixed_statement_flag(db, fake_dorq, tmp_path):
    a, _ = _company(db, "STMA")
    fixed = _statement_row(
        a,
        "2024-06-30",
        check="dorq_bad_value",
        code="DQ911",
        statement="income",
        extra_key={"item": "revenue"},
    )
    chain = _statement_row(a, "2024-03-31", check="dorq_statement_key", code="DQ901")
    _flag(db, a, fixed)
    _flag(db, a, chain)
    rcfile = _rc(tmp_path, db, fake_dorq)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rcfile), "dq", "recheck", "--yes"]
    )
    assert result.exit_code == 0, result.output
    still = {
        r["check_name"]
        for r in db.fetchall(
            "SELECT check_name FROM ops.data_quality_flag WHERE resolved_at IS NULL"
        )
    }
    assert still == {"dorq_statement_key"}


# ---------------------------------------------------------------------------
# dq compare --statements
# ---------------------------------------------------------------------------


def _sql_flag(db, sid, check, table, key, severity="warn", *, accepted=False):
    db.execute(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detail, detected_at, accepted_at, resolved_at,"
        " resolved_by) VALUES (%s, %s, %s, %s, %s, '{}', now() - interval '1 day',"
        " %s, %s, %s)",
        (
            sid,
            table,
            json.dumps(key),
            check,
            severity,
            *(["now()", "now()", "test"] if accepted else [None, None, None]),
        ),
    )
    if accepted:
        db.execute(
            "UPDATE ops.data_quality_flag SET accepted_at = now() - interval '1 hour',"
            " resolved_at = now() - interval '1 hour' WHERE check_name = %s"
            " AND record_key = %s",
            (check, json.dumps(key)),
        )


def test_dq_compare_statements_matches_successors_by_security_period_and_statement(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    # The first night is the day before the last session; the second, the last.
    db.execute("DELETE FROM core.daily_price WHERE trade_date = %s", (days[-1],))
    other = _security(db, "STMO")
    _sql_flag(
        db,
        sid,
        "balance_sheet_identity",
        "core.balance_sheet",
        {"period_type": "quarter", "fiscal_date": "2024-03-31"},
        "error",
    )
    _sql_flag(
        db,
        sid,
        "fundamentals_gap",
        "core.income_statement",
        {"from": "2022-12-31", "to": "2024-03-31"},
        "info",
    )
    _sql_flag(
        db,
        sid,
        "fundamentals_ytd_suspect",
        "core.cash_flow_statement",
        {"fiscal_year": 2024, "item": "net_cash_operating"},
    )
    _sql_flag(db, other, "fundamentals_coverage", "core.income_statement", {}, "info")
    _sql_flag(
        db,
        sid,
        "cash_flow_tie",
        "core.cash_flow_statement",
        {"period_type": "quarter", "fiscal_date": "2024-06-30"},
    )
    reports = [
        # Covers balance_sheet_identity: same period, statement and identity.
        _statement_row(sid, "2024-03-31", extra_key={"identity": "balance_sheet"}),
        # Another identity on the same period covers nothing.
        _statement_row(sid, "2024-03-31", extra_key={"identity": "debt_split"}),
        # In the gap's span.
        _statement_row(
            sid,
            "2023-06-30",
            check="dorq_period_gap",
            code="DQ941",
            statement="income",
            severity="warn",
        ),
        # DQ916 on the cash flow, in the fiscal year.
        _statement_row(
            sid,
            "2024-06-30",
            check="dorq_cumulative_quarters",
            code="DQ916",
            statement="cash_flow",
        ),
        # DQ903 on the income statement does not cover the cash flow's tie.
        _statement_row(
            sid, "2024-06-30", statement="income", extra_key={"identity": "cash_flow"}
        ),
    ]
    _emit(fake_dorq, reports)
    settings = _settings(fake_dorq, tmp_path)
    night = dorq_statements.run(db, settings, "NASDAQ", shadow=True)
    c = cst.compare(db, night.shadow_file)
    by = c.by_sql_check
    assert by["balance_sheet_identity"]["flags"] == 1
    assert by["balance_sheet_identity"]["also_dorq"] == 1
    assert by["fundamentals_gap"]["also_dorq"] == 1
    assert by["fundamentals_ytd_suspect"]["also_dorq"] == 1
    assert by["fundamentals_coverage"] == {
        "codes": ["DQ943"],
        "flags": 1,
        "also_dorq": 0,
        "severe": 0,
        "severe_also_dorq": 0,
    }
    assert by["cash_flow_tie"]["also_dorq"] == 0
    assert c.by_code["DQ903"] == {
        "reports": 3,
        "severe": 3,
        "also_sql": 1,
        "dorq_only": 2,
    }
    assert c.reports == 5 and c.conditions == 5 and c.run_id is not None
    # Standing at warn and above: dorq's 5 conditions against 3 SQL flags.
    assert c.standing["dorq"] == 5 and c.standing["sql"] == 3
    assert c.new_per_night is None
    text = cst.format_comparison(c)
    assert "balance_sheet_identity" in text and "Standing at warn+" in text

    # The next night: one condition new, against the SQL flags written since.
    db.execute(
        "UPDATE ops.ingestion_run SET started_at = started_at - interval '2 days'"
    )
    _prices(db, sid, [days[-1]])
    _emit(
        fake_dorq,
        reports
        + [
            _statement_row(
                sid,
                "2024-06-30",
                check="dorq_bad_value",
                code="DQ911",
                statement="income",
                extra_key={"item": "revenue"},
            )
        ],
    )
    _sql_flag(
        db,
        sid,
        "fundamentals_stale",
        "core.income_statement",
        {"latest_fiscal_date": "2024-06-30"},
    )
    second = dorq_statements.run(db, settings, "NASDAQ", shadow=True)
    n = cst.compare(db, second.shadow_file).new_per_night
    assert n["previous"] == night.shadow_file.name
    assert n["dorq"] == 1 and n["sql"] == 6


def test_dq_compare_statements_precision_uses_statement_labels_only(
    db, fake_dorq, tmp_path
):
    sid, _ = _company(db)
    reports = [
        _statement_row(
            sid,
            "2024-06-30",
            check="dorq_unit_error",
            code="DQ912",
            statement="income",
            extra_key={"item": "revenue"},
        ),
        _statement_row(sid, "2024-03-31", extra_key={"identity": "balance_sheet"}),
    ]
    _emit(fake_dorq, reports)
    night = dorq_statements.run(
        db, _settings(fake_dorq, tmp_path), "NASDAQ", shadow=True
    )
    labels = cmp.parse_labels(
        iter(
            [
                json.dumps(
                    {
                        "series": str(sid),
                        "date": "2024-06-30",
                        "class": "data_error",
                        "expect": "DQ912",
                        "statement": "income",
                        "item": "revenue",
                    }
                ),
                json.dumps(
                    {
                        "series": str(sid),
                        "date": "2024-03-31",
                        "class": "market_fact",
                        "codes": "DQ9",
                        "statement": "balance_sheet",
                    }
                ),
                # A bars label on the same dates judges no statement report.
                json.dumps(
                    {
                        "series": str(sid),
                        "date": "2024-06-30",
                        "class": "data_error",
                        "expect": "DQ201",
                    }
                ),
            ]
        )
    )
    c = cst.compare(db, night.shadow_file, labels=labels)
    assert c.labels_used == 2
    assert c.precision_warn.right == 1 and c.precision_warn.false == 1
    assert c.precision_by_code["DQ912"].precision == 1.0
    assert c.precision_by_code["DQ903"].precision == 0.0
    # And a statements label judges no bars report.
    assert [lab.is_statements for lab in labels] == [True, True, False]


def test_cli_dq_compare_statements_reads_the_statements_directory(
    db, fake_dorq, tmp_path
):
    sid, days = _company(db)
    _emit(fake_dorq, [_statement_row(sid, "2024-03-31")])
    rcfile = _rc(tmp_path, db, fake_dorq)
    runner = CliRunner()
    none = runner.invoke(
        cli.main, ["--config", str(rcfile), "dq", "compare", "--statements"]
    )
    assert none.exit_code == 1 and "statements" in none.output
    assert (
        runner.invoke(
            cli.main, ["--config", str(rcfile), "dq", "run", "--statements-only"]
        ).exit_code
        == 0
    )
    result = runner.invoke(
        cli.main, ["--config", str(rcfile), "dq", "compare", "--statements", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "statements" and payload["dorq_reports"] == 1
    assert payload["as_of"] == days[-1].isoformat()
    # Without --statements, there is no bars night to compare.
    bars = runner.invoke(cli.main, ["--config", str(rcfile), "dq", "compare"])
    assert bars.exit_code == 1 and "No shadow file" in bars.output


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_has_a_dorq_statements_line(db, fake_dorq, tmp_path):
    runner = CliRunner()
    off = _rc(tmp_path, db, fake_dorq, engine="both")
    result = runner.invoke(cli.main, ["--config", str(off), "status"])
    assert result.exit_code == 0, result.output
    assert "dorq       : 9.9.9-fake (engine both, shadow)" in result.output
    assert "             statements" not in result.output

    on = _rc(tmp_path, db, fake_dorq, engine="both", dorq_statements=True)
    result = runner.invoke(cli.main, ["--config", str(on), "status"])
    assert "             statements on (shadow); no run yet" in result.output

    sid, days = _company(db)
    _emit(fake_dorq, [_statement_row(sid, "2024-03-31")])
    assert (
        runner.invoke(
            cli.main, ["--config", str(on), "dq", "run", "--statements-only"]
        ).exit_code
        == 0
    )
    result = runner.invoke(cli.main, ["--config", str(on), "status"])
    line = next(ln for ln in result.output.splitlines() if "statements on" in ln)
    assert f"last as of {days[-1].isoformat()}, 1 report in " in line
    assert line.endswith(")")


# ---------------------------------------------------------------------------
# The real binary
# ---------------------------------------------------------------------------


def _quarters(db, sid, *, revenue_at=None, scale=1000.0):
    """Sixteen quarters of a steady company, 2020-2023: income, a balance sheet
    that balances, and a cash flow. ``revenue_at`` stores that quarter's revenue at
    ``scale`` times its value: a unit error."""
    rows = []
    for i in range(16):
        year, q = 2020 + i // 4, i % 4 + 1
        end = [
            dt.date(year, 3, 31),
            dt.date(year, 6, 30),
            dt.date(year, 9, 30),
            dt.date(year, 12, 31),
        ][q - 1]
        known = end + dt.timedelta(days=40)
        revenue = 1_000_000_000 * (1 + 0.01 * i)
        if revenue_at == end:
            revenue *= scale
        label = f"{year}-Q{q}"
        _put(
            db,
            "income",
            sid,
            end,
            label=label,
            known_from=known,
            revenue=revenue,
            cost_of_revenue=600_000_000 * (1 + 0.01 * i),
            net_income=100_000_000 * (1 + 0.01 * i),
            weighted_average_shares=50_000_000,
        )
        assets = 5_000_000_000 * (1 + 0.005 * i)
        _put(
            db,
            "balance_sheet",
            sid,
            end,
            label=label,
            known_from=known,
            total_assets=assets,
            total_liabilities=assets * 0.6,
            total_equity_parent=assets * 0.4,
            shares_outstanding=50_000_000,
            cash_and_equivalents=400_000_000 * (1 + 0.005 * i),
        )
        _put(
            db,
            "cash_flow",
            sid,
            end,
            label=label,
            known_from=known,
            net_cash_operating=150_000_000 * (1 + 0.01 * i),
        )
        rows.append(end)
    return rows


@real_dorq
def test_real_dorq_finds_a_unit_error_and_is_deterministic(db, tmp_path):
    sid = _security(db, "STMR")
    _prices(db, sid, _sessions(db, dt.date(2023, 1, 3)), close=30.0)
    ends = _quarters(db, sid, revenue_at=dt.date(2021, 9, 30))
    settings = dorq.DorqSettings(path=REAL_DORQ, shadow_dir=str(tmp_path / "sh"))
    first = dorq_statements.run_statements(db, settings, dt.date(2024, 12, 31))
    second = dorq_statements.run_statements(db, settings, dt.date(2024, 12, 31))
    assert first.records == second.records
    hits = [
        r
        for r in first.records
        if r["record_key"].get("fiscal_date") == "2021-09-30"
        and r["record_key"].get("statement") == "income"
        and r["severity"] in ("warn", "error")
    ]
    assert "dorq_unit_error" in {r["check_name"] for r in hits}, first.records
    assert {r["table_name"] for r in first.records} <= set(fm.TABLES.values())
    assert ends[-1] == dt.date(2023, 12, 31)


@real_dorq
def test_real_dorq_recheck_closes_a_unit_error_the_vendor_corrected(db, tmp_path):
    sid = _security(db, "STMC")
    _prices(db, sid, _sessions(db, dt.date(2023, 1, 3)), close=30.0)
    bad = dt.date(2021, 9, 30)
    _quarters(db, sid, revenue_at=bad)
    settings = dorq.DorqSettings(path=REAL_DORQ, shadow_dir=str(tmp_path / "sh"))
    out = dorq_statements.run(db, settings, "NASDAQ", shadow=False)
    flagged = db.fetchall(
        "SELECT dq_flag_id, check_name FROM ops.data_quality_flag"
        " WHERE resolved_at IS NULL AND record_key->>'fiscal_date' = '2021-09-30'"
        " AND record_key->>'statement' = 'income'"
    )
    assert flagged, out.detected
    # The vendor corrects the row: the old one is superseded, the right one loaded.
    db.execute(
        "UPDATE core.income_statement SET superseded_at = %s"
        " WHERE security_id = %s AND fiscal_date = %s",
        (T0 + dt.timedelta(days=2), sid, bad),
    )
    _put(
        db,
        "income",
        sid,
        bad,
        label="2021-Q3",
        known_from=bad + dt.timedelta(days=40),
        loaded_at=T0 + dt.timedelta(days=2),
        revenue=1_000_000_000 * 1.06,
        cost_of_revenue=600_000_000 * 1.06,
        net_income=100_000_000 * 1.06,
        weighted_average_shares=50_000_000,
    )
    results = rc.recheck_dorq(db, settings)
    closed = {i for r in results for i in r.stale_flag_ids}
    assert {int(f["dq_flag_id"]) for f in flagged} <= closed
