"""`fafnir dq export-labels`: repairs and judged flags as dorq labels (DR-0701)."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import labels as lb

D = dt.date


def _mk(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


def _bars(db, sid, days, close=100.0):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1000,
            }
            for d in days
        ],
    )


def _days(db, n, start=D(2024, 3, 1)):
    return [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
            " AND is_open AND trade_date >= %s ORDER BY trade_date LIMIT %s",
            (start, n),
        )
    ]


def _flag(db, sid, check, day, detail, *, note, accepted=False):
    repo.add_dq_flag_once(
        db,
        check_name=check,
        security_id=sid,
        record_key={"trade_date": day.isoformat()},
        detail=detail,
    )
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 't',"
        " resolution_note = %s, accepted_at = CASE WHEN %s THEN now() END,"
        " accepted_by = CASE WHEN %s THEN 't' END"
        " WHERE security_id = %s AND check_name = %s",
        (note, accepted, accepted, sid, check),
    )


@pytest.mark.integration
def test_repairs_become_faults_with_the_bars_as_they_stood(db):
    days = _days(db, 30)
    deleted = _mk(db, "LBDEL")
    _bars(db, deleted, days)
    db.execute(
        "UPDATE core.daily_price SET close = 950, high = 951 WHERE security_id = %s"
        " AND trade_date = %s",
        (deleted, days[10]),
    )
    repo.delete_operator_bars(
        db,
        security_id=deleted,
        trade_dates=[days[10]],
        note="bad print",
        created_by="t",
    )

    rescaled = _mk(db, "LBRSC")
    _bars(db, rescaled, days)
    olds = repo.stored_bars(db, rescaled, days[5], days[7])
    changes = [
        (
            o,
            {
                **o,
                "open": o["open"] * 10,
                "high": o["high"] * 10,
                "low": o["low"] * 10,
                "close": o["close"] * 10,
                "volume": o["volume"] // 10,
            },
        )
        for o in olds
    ]
    repo.replace_operator_bars(
        db,
        security_id=rescaled,
        kind="rescale",
        params={"price_factor": "10", "volume_factor": "0.1"},
        changes=changes,
        note="cents era",
        created_by="t",
    )

    shifted = _mk(db, "LBSHF")
    _bars(db, shifted, days[:5])
    olds = repo.stored_bars(db, shifted, days[0], days[4])
    changes = [(o, {**o, "trade_date": days[i + 1]}) for i, o in enumerate(olds)]
    repo.replace_operator_bars(
        db,
        security_id=shifted,
        kind="shift",
        params={"days": 1},
        changes=changes,
        note="dated a day early",
        created_by="t",
    )

    split = _mk(db, "LBSPL")
    _bars(db, split, days)
    repo.add_operator_action(
        db,
        security_id=split,
        action_type="split",
        ex_date=days[12],
        split_numerator=Decimal(2),
        split_denominator=Decimal(1),
        note="vendor missed it",
        created_by="t",
    )

    export = lb.build(db)
    by_kind = {lab["kind"]: lab for lab in export.labels}
    assert export.by_kind() == {
        "bar_deleted": 1,
        "date_shift": 1,
        "scale_era": 1,
        "split_added": 1,
    }
    assert by_kind["bar_deleted"] == {
        "series": str(deleted),
        "first": days[10].isoformat(),
        "last": days[10].isoformat(),
        "class": "data_error",
        "kind": "bar_deleted",
        "source": "operator_override",
        "note": "bad print",
    }
    assert by_kind["scale_era"]["expect"] == "DQ202"
    assert (by_kind["scale_era"]["first"], by_kind["scale_era"]["last"]) == (
        days[5].isoformat(),
        days[7].isoformat(),
    )
    shift = by_kind["date_shift"]
    assert shift["expect"] == "DQ206"
    assert (shift["first"], shift["last"]) == (days[0].isoformat(), days[5].isoformat())
    # The shift added a bar on a date the vendor's history never had.
    assert shift["remove"] == days[5].isoformat()
    assert by_kind["split_added"]["class"] == "context_gap"
    assert by_kind["split_added"]["expect"] == "DQ203"

    restore = {(r["series"], r["date"]): r for r in export.restore}
    assert Decimal(str(restore[(str(deleted), days[10].isoformat())]["close"])) == 950
    # The rescaled era is restored at the scale the vendor stored it.
    assert Decimal(str(restore[(str(rescaled), days[6].isoformat())]["close"])) == 100
    assert {d for s, d in restore if s == str(shifted)} == {
        d.isoformat() for d in days[:5]
    }


@pytest.mark.integration
def test_judged_flags_become_market_facts_or_faults(db):
    days = _days(db, 20)
    real = _mk(db, "LBREAL")
    _bars(db, real, days)
    _flag(
        db,
        real,
        "outlier",
        days[5],
        {"close": 100.0, "prev_close": 60.0},
        note="earnings: a real move",
    )
    accepted = _mk(db, "LBACC")
    _bars(db, accepted, days)
    _flag(db, accepted, "outlier", days[6], {"close": 1.0}, note="real", accepted=True)
    corrected = _mk(db, "LBCOR")
    _bars(db, corrected, days)
    _flag(db, corrected, "outlier", days[7], {"close": 55.0}, note="vendor fixed it")
    loaded = _mk(db, "LBLOAD")
    _bars(db, loaded, days)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " split_numerator, split_denominator) VALUES (%s, 'split', %s, 2, 1)",
        (loaded, days[8]),
    )
    _flag(
        db,
        loaded,
        "outlier",
        days[8],
        {"close": 50.0},
        note="Re-checked: the close-to-close move is no longer over the threshold, "
        "or a split explaining it has since been loaded.",
    )
    dq_err = _mk(db, "LBDQE")
    _bars(db, dq_err, days)
    _flag(
        db,
        dq_err,
        "dorq_history_segment",
        days[9],
        {"code": "DQ205"},
        note="unfixable",
        accepted=True,
    )
    dq_ok = _mk(db, "LBDQO")
    _bars(db, dq_ok, days)
    _flag(
        db, dq_ok, "dorq_bad_print", days[10], {"code": "DQ201"}, note="it traded there"
    )

    kinds = {lab["series"]: lab for lab in lb.build(db).labels}
    assert kinds[str(real)]["class"] == "market_fact"
    assert kinds[str(real)]["kind"] == "outlier_resolved"
    assert kinds[str(real)]["source"].startswith("dq_flag:")
    assert kinds[str(accepted)]["kind"] == "outlier_accepted"
    assert str(corrected) not in kinds  # the bar changed: no evidence left
    assert kinds[str(loaded)]["class"] == "context_gap"
    assert kinds[str(loaded)]["expect"] == "DQ203"
    assert kinds[str(dq_err)]["class"] == "data_error"
    assert kinds[str(dq_err)]["expect"] == "DQ205"
    assert kinds[str(dq_ok)]["class"] == "market_fact"
    assert kinds[str(dq_ok)]["codes"] == "DQ201"


@pytest.mark.integration
def test_a_flag_on_a_repair_is_the_repair_not_a_judgement(db):
    days = _days(db, 20)
    sid = _mk(db, "LBBOTH")
    _bars(db, sid, days)
    _flag(db, sid, "outlier", days[5], {"close": 100.0}, note="deleted the bar")
    repo.delete_operator_bars(
        db, security_id=sid, trade_dates=[days[5]], note="bad print", created_by="t"
    )
    assert [lab["kind"] for lab in lb.build(db).labels] == ["bar_deleted"]


@pytest.mark.integration
def test_cli_writes_both_files(db, tmp_path, monkeypatch):
    days = _days(db, 10)
    sid = _mk(db, "LBCLI")
    _bars(db, sid, days)
    repo.delete_operator_bars(
        db, security_id=sid, trade_dates=[days[3]], note="bad", created_by="t"
    )
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    out = tmp_path / "labels.jsonl"
    before = tmp_path / "before.csv"
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(rc),
            "dq",
            "export-labels",
            "--out",
            str(out),
            "--restore",
            str(before),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 label" in result.output and "bar_deleted 1" in result.output
    assert json.loads(out.read_text())["series"] == str(sid)
    lines = before.read_text().splitlines()
    assert lines[0] == "series,date,open,high,low,close,volume"
    assert lines[1].startswith(f"{sid},{days[3].isoformat()},")

    dorq_bin = os.environ.get("FAFNIR_TEST_DORQ")
    if dorq_bin:
        # dorq reads both files: the restored bar is back in the series it checks.
        bars = tmp_path / "bars.csv"
        rows = db.fetchall(
            "SELECT security_id, trade_date, open, high, low, close, volume"
            " FROM core.daily_price WHERE security_id = %s ORDER BY trade_date",
            (sid,),
        )
        bars.write_text(
            "security_id,trade_date,open,high,low,close,volume\n"
            + "".join(
                f"{r['security_id']},{r['trade_date']},{r['open']},{r['high']},"
                f"{r['low']},{r['close']},{r['volume']}\n"
                for r in rows
            )
        )
        done = subprocess.run(
            [
                dorq_bin,
                str(bars),
                "--isolated",
                "--labels",
                str(out),
                "--restore",
                str(before),
                "--format",
                "jsonl",
                "--show-info",
                "--exit-zero",
            ],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        for line in done.stdout.splitlines():
            assert json.loads(line)["series"] == str(sid)
