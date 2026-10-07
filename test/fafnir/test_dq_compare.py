"""`fafnir dq compare`: dorq's shadow night against the SQL checks and the labels."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import compare as cmp
from fafnir.dq import dorq


def _row(sid, day, code="DQ201", check="dorq_bad_print", severity="error", end=None):
    return {
        "security_id": sid,
        "table_name": "core.daily_price",
        "record_key": {"trade_date": day},
        "check_name": check,
        "severity": severity,
        "detail": {"code": code, "date": day, "end_date": end},
    }


def _labels(tmp_path, *rows):
    path = tmp_path / "labels.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cmp.read_labels(path)


# ---------------------------------------------------------------------------
# Matching: the same rules as dorq-eval
# ---------------------------------------------------------------------------


def test_a_report_on_a_fault_with_its_code_is_right(tmp_path):
    labels = _labels(
        tmp_path,
        {
            "series": "7",
            "date": "2024-03-04",
            "class": "data_error",
            "expect": "DQ201|DQ204",
        },
    )
    r = cmp.Report.of(_row(7, "2024-03-06"))
    assert cmp.judge(r, labels, 3) == "right"
    assert (
        cmp.judge(cmp.Report.of(_row(7, "2024-03-06", code="DQ202")), labels, 3)
        == "other"
    )
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-12")), labels, 3) == "unlabelled"
    assert cmp.judge(cmp.Report.of(_row(8, "2024-03-04")), labels, 3) == "unlabelled"


def test_a_report_on_a_market_fact_is_false_only_on_its_date_and_codes(tmp_path):
    labels = _labels(
        tmp_path, {"series": "7", "date": "2024-03-04", "class": "market_fact"}
    )
    assert labels[0].codes == "DQ2"
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-04")), labels, 3) == "false"
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-05")), labels, 3) == "unlabelled"
    gap = cmp.Report.of(_row(7, "2024-03-04", code="DQ301", check="dorq_missing_run"))
    assert cmp.judge(gap, labels, 3) == "unlabelled"


def test_a_cross_sectional_fault_matches_only_the_cohort_row(tmp_path):
    labels = _labels(
        tmp_path,
        {"series": "*", "date": "2024-03-04", "class": "data_error", "expect": "DQ303"},
    )
    cohort = cmp.Report.of(
        _row(None, "2024-03-04", code="DQ303", check="dorq_cohort_gap")
    )
    member = cmp.Report.of(
        _row(7, "2024-03-04", code="DQ301", check="dorq_missing_run")
    )
    assert cmp.judge(cohort, labels, 3) == "right"
    assert cmp.judge(member, labels, 3) == "unlabelled"


def test_a_bad_label_names_its_line(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"series":"1","date":"2024-01-02","class":"data_error"}\n{"x":1}\n'
    )
    with pytest.raises(ValueError, match="line 2"):
        cmp.read_labels(path)


# ---------------------------------------------------------------------------
# Against the queue
# ---------------------------------------------------------------------------


def _mk(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


@pytest.mark.integration
def test_overlap_counts_both_directions(db, tmp_path):
    a = _mk(db, "CMPA")
    b = _mk(db, "CMPB")
    for sid, check, key in (
        (a, "outlier", {"trade_date": "2024-03-04"}),  # dorq reports it too
        (b, "outlier", {"trade_date": "2024-03-05"}),  # dorq does not
        (a, "gap", {"trade_date": "2024-03-07"}),  # dorq reports a missing run
        (a, "stale", {"last_date": "2024-01-02"}),  # outside the window
    ):
        repo.add_dq_flag_once(db, check_name=check, security_id=sid, record_key=key)
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [
            _row(a, "2024-03-05"),
            _row(
                a, "2024-03-07", code="DQ301", check="dorq_missing_run", severity="warn"
            ),
            _row(b, "2024-03-01", code="DQ202", check="dorq_scale_shift"),
        ],
        shadow,
    )
    labels = _labels(
        tmp_path,
        {
            "series": str(a),
            "date": "2024-03-05",
            "class": "data_error",
            "expect": "DQ201",
        },
        {"series": str(b), "date": "2024-03-01", "class": "market_fact"},
    )
    c = cmp.compare(db, shadow, labels=labels)
    assert c.window_from == dt.date(2024, 3, 1)
    assert c.window_to == dt.date(2024, 3, 8)
    assert c.by_sql_check == {
        "outlier": {"flags": 2, "also_dorq": 1},
        "gap": {"flags": 1, "also_dorq": 1},
        "stale": {"flags": 0, "also_dorq": 0},
    }
    assert c.by_code == {
        "DQ201": {"reports": 1, "also_sql": 1},
        "DQ202": {"reports": 1, "also_sql": 0},
        "DQ301": {"reports": 1, "also_sql": 1},
    }
    # The missing run two days from the bad print is near a fault, with another
    # code: an "other fault", as dorq-eval counts it.
    p = c.precision_warn
    assert (p.right, p.other, p.false, p.unlabelled) == (1, 1, 1, 0)
    assert p.precision == pytest.approx(1 / 3)
    assert p.fault_precision == pytest.approx(2 / 3)
    assert c.precision_error.judged == 2
    assert c.precision_error.precision == 0.5
    text = cmp.format_comparison(c)
    assert "outlier" in text and "0.333" in text


@pytest.mark.integration
def test_cli_compare_reads_the_newest_shadow_file(db, tmp_path, monkeypatch):
    sid = _mk(db, "CMPC")
    shadow_dir = tmp_path / "shadow"
    dorq.write_shadow([_row(sid, "2024-02-01")], shadow_dir / "2024-02-02.jsonl")
    dorq.write_shadow([_row(sid, "2024-03-01")], shadow_dir / "2024-03-04.jsonl")
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{shadow_dir}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "compare", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["shadow_file"].endswith("2024-03-04.jsonl")
    assert payload["dorq_reports"] == 1
    assert payload["precision"] is None

    empty = tmp_path / "none"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{empty}"\n')
    result = CliRunner().invoke(cli.main, ["--config", str(rc), "dq", "compare"])
    assert result.exit_code != 0
    assert "No shadow file" in result.output
