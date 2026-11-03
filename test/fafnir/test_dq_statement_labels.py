"""The statements' labels for dorq (DR-0913; dorq fundamentals plan §5.7).

SF1's corrections (the superseded rows) and the closed statement flags become
labels in `dq export-labels`; `dq label add` states one with a statement scope.
Every row is synthetic. The round trip at the end runs the real dorq and dorq-eval
(``FAFNIR_TEST_DORQ``; dorq-eval beside it, or ``FAFNIR_TEST_DORQ_EVAL``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from test.fafnir import test_dq_dorq_statements as base

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.dq import compare as cmp
from fafnir.dq import compare_statements as cst
from fafnir.dq import dorq_statements
from fafnir.dq import labels as lb

# The statements fixtures' helpers (synthetic rows, prices, sessions).
T0, _put, _quarters = base.T0, base._put, base._quarters
_security, _sessions, _bars = base._security, base._sessions, base._prices

REAL_DORQ = os.environ.get("FAFNIR_TEST_DORQ", "")
EVAL = os.environ.get("FAFNIR_TEST_DORQ_EVAL", "")
if not EVAL and REAL_DORQ:
    for candidate in (
        Path(REAL_DORQ).with_name("dorq-eval"),
        Path(REAL_DORQ).parent.parent / "tools" / "eval" / "dorq-eval",
    ):
        if candidate.is_file():
            EVAL = str(candidate)
            break

T1 = T0 + dt.timedelta(days=30)
Q2 = dt.date(2024, 6, 30)


# ---------------------------------------------------------------------------
# How a correction moved (no database)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old, new, shares, move",
    [
        (412_300_000, 412_300_000_000, False, "power"),
        (412_300_000, -412_300, False, "power_sign"),
        (55_000_000, -55_000_000, False, "sign"),
        (0, 7_000_000, False, "zero"),
        (0, 200, False, "zero"),
        (100_000_000, 130_000_000, False, "other"),
        (None, 9_000_000, False, "other"),
        (None, 900, False, None),  # below the floor
        (100_000_000, 100_200_000, False, None),  # 0.2%: within tolerance
        (1_500_000, 1_520_000, False, None),  # 1.3%, but 20k: below the floor
        (50_000_000, 50_400_000, True, "other"),  # shares: a 1,000-share floor
        (50_000_000, 50_000_000, True, None),
    ],
)
def test_a_correction_is_classified_by_how_it_moved(old, new, shares, move):
    assert lb.classify_move(old, new, shares=shares) == move


def test_a_statement_label_is_told_from_a_bars_label():
    assert lb.is_statements({"statement": "income"})
    assert lb.is_statements({"expect": "DQ201|DQ912"})
    assert lb.is_statements({"codes": "DQ9"})
    assert not lb.is_statements({"expect": "DQ201", "codes": ""})
    assert not lb.is_statements({"codes": "DQ2"})


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def _correct(db, statement, sid, fiscal_date, *, at=T1, **changes):
    """SF1 corrects a first-reported row: the loader supersedes it and inserts the
    corrected one with the same version (fafnir.ingest.fundamentals)."""
    table = {
        "income": "core.income_statement",
        "balance_sheet": "core.balance_sheet",
        "cash_flow": "core.cash_flow_statement",
    }[statement]
    old = db.fetchone(
        f"SELECT * FROM {table} WHERE security_id = %s AND fiscal_date = %s"
        " AND version_no = 1 AND superseded_at IS NULL",
        (sid, fiscal_date),
    )
    db.execute(
        f"UPDATE {table} SET superseded_at = %s WHERE security_id = %s"
        " AND fiscal_date = %s AND version_no = 1 AND superseded_at IS NULL",
        (at, sid, fiscal_date),
    )
    row = {**dict(old), **changes, "loaded_at": at, "superseded_at": None}
    if "filing_date" in changes:
        row["known_from"] = changes["filing_date"]
    cols = list(row)
    db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
        f"({', '.join(['%s'] * len(cols))})",
        [json.dumps(row[c]) if c == "extra" else row[c] for c in cols],
    )


def _company(db, symbol="STL"):
    sid = _security(db, symbol)
    _bars(db, sid, _sessions(db), close=20.0)
    for fd, label, known in (
        (dt.date(2024, 3, 31), "2024-Q1", dt.date(2024, 5, 2)),
        (Q2, "2024-Q2", dt.date(2024, 8, 1)),
    ):
        _put(
            db,
            "income",
            sid,
            fd,
            label=label,
            known_from=known,
            revenue=412_300_000,
            cost_of_revenue=250_000_000,
            net_income=55_000_000,
            preferred_dividends=0,
        )
        _put(
            db,
            "balance_sheet",
            sid,
            fd,
            label=label,
            known_from=known,
            total_assets=5_000_000_000,
            total_liabilities=3_000_000_000,
            cash_and_equivalents=400_000_000,
        )
    return sid


def _labels(db, **kw):
    return [lab for lab in lb.build(db, **kw).labels if lb.is_statements(lab)]


def test_a_unit_correction_is_a_dq912_label_on_its_item_until_it_loaded(db):
    sid = _company(db)
    _correct(db, "income", sid, Q2, revenue=412_300_000_000)
    (label,) = _labels(db)
    assert label == {
        "series": str(sid),
        "first": "2024-06-30",
        "last": "2024-06-30",
        "class": "data_error",
        "kind": "corrected_unit",
        "source": "vendor_correction",
        "expect": "DQ912",
        "note": "SF1 corrected income quarter 2024-06-30 v1: revenue",
        "decided_at": T1.isoformat(),
        "applies_until": T1.isoformat(),
        "statement": "income",
        "period_type": "quarter",
        "version_no": 1,
        "item": "revenue",
    }


def test_a_row_corrected_alike_is_one_label_with_no_item(db):
    sid = _company(db)
    _correct(
        db,
        "income",
        sid,
        Q2,
        revenue=412_300_000_000,
        cost_of_revenue=250_000_000_000,
        net_income=55_000_000_000,
    )
    (label,) = _labels(db)
    assert label["kind"] == "corrected_unit" and "item" not in label
    # In the taxonomy's order.
    assert label["note"].endswith(": revenue, cost_of_revenue, net_income")


def test_each_kind_of_move_is_its_own_label(db):
    sid = _company(db)
    _correct(
        db,
        "income",
        sid,
        Q2,
        net_income=-55_000_000,
        preferred_dividends=4_000_000,
        cost_of_revenue=300_000_000,
        filing_date=dt.date(2024, 8, 6),
    )
    _correct(db, "balance_sheet", sid, Q2, total_assets=5_000_100_000)  # 0.002%
    by_kind = {lab["kind"]: lab for lab in _labels(db)}
    assert set(by_kind) == {
        "corrected_sign",
        "corrected_zero",
        "corrected_value",
        "corrected_filing_date",
    }
    assert by_kind["corrected_sign"]["expect"] == "DQ913"
    assert by_kind["corrected_sign"]["item"] == "net_income"
    assert by_kind["corrected_zero"]["expect"] == "DQ915"
    assert by_kind["corrected_value"]["expect"] == lb.VALUE_CODES
    assert by_kind["corrected_filing_date"]["expect"] == "DQ931"
    assert "item" not in by_kind["corrected_filing_date"]
    assert "2024-08-01 to 2024-08-06" in by_kind["corrected_filing_date"]["note"]


def test_restore_statements_writes_the_rows_as_they_stood(db, tmp_path):
    sid = _company(db)
    _correct(db, "income", sid, Q2, revenue=412_300_000_000)
    _correct(db, "balance_sheet", sid, dt.date(2024, 3, 31), total_assets=5_000_000_001)
    export = lb.build(db)
    assert len(export.restore_statements) == 1  # the cent move is no label
    items = [str(i["column_name"]) for i in dorq_statements.line_items(db)]
    path = tmp_path / "statements-before.csv"
    lb.write(export, tmp_path / "labels.jsonl", None, None, path, items)
    lines = path.read_text().splitlines()
    assert lines[0].split(",") == list(dorq_statements._FIELDS) + ["close"] + items
    row = dict(zip(lines[0].split(","), lines[1].split(",")))
    assert row["security_id"] == str(sid) and row["statement"] == "income"
    assert row["fiscal_date"] == "2024-06-30" and row["version_no"] == "1"
    assert row["revenue"] == "412300000.00"  # as it stood, not as corrected
    assert row["loaded_on"] == T0.date().isoformat()
    assert row["close"] not in ("", None)  # 2024-08-01's close
    assert row["total_assets"] == ""

    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(_rc(tmp_path, db)),
            "dq",
            "export-labels",
            "--out",
            str(tmp_path / "cli.jsonl"),
            "--restore-statements",
            str(tmp_path / "cli-before.csv"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 1 statement row as they stood" in result.output
    assert (tmp_path / "cli-before.csv").read_text() == path.read_text()


def _rc(tmp_path, db):
    path = tmp_path / "fafnirrc"
    path.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    return path


# ---------------------------------------------------------------------------
# Closed statement flags
# ---------------------------------------------------------------------------


def _flag(
    db,
    sid,
    check,
    table,
    key,
    *,
    code=None,
    detected=T0,
    closed=T1,
    accepted=False,
    basis="per_flag",
    note="checked",
):
    detail = {"code": code} if code else {}
    return db.fetchval(
        """
        INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,
            check_name, severity, detail, detected_at, resolved_at, resolved_by,
            accepted_at, resolution_note, judgement_basis)
        VALUES (%s, %s, %s, %s, 'warn', %s, %s, %s, 'test', %s, %s, %s)
        RETURNING dq_flag_id
        """,
        (
            sid,
            table,
            json.dumps(key),
            check,
            json.dumps(detail),
            detected,
            closed,
            closed if accepted else None,
            note,
            basis,
        ),
    )


def test_an_accepted_statement_flag_is_a_filing_fact_on_its_successor(db):
    sid = _company(db)
    _flag(
        db,
        sid,
        "balance_sheet_identity",
        "core.balance_sheet",
        {"period_type": "quarter", "fiscal_date": "2024-06-30"},
        accepted=True,
        note="10-Q: temporary equity, as filed",
    )
    _flag(
        db,
        sid,
        "dorq_absent_as_zero",
        "core.income_statement",
        {
            "statement": "income",
            "period_type": "quarter",
            "fiscal_date": "2024-03-31",
            "version_no": 1,
            "item": "preferred_dividends",
        },
        code="DQ915",
        accepted=True,
        basis="by_rule",
    )
    _flag(
        db,
        sid,
        "fundamentals_currency_mismatch",
        "core.income_statement",
        {"reported_currency": "EUR"},
        accepted=True,
    )
    by_kind = {lab["kind"]: lab for lab in _labels(db)}
    bs = by_kind["balance_sheet_identity_accepted"]
    assert bs["class"] == "market_fact" and bs["codes"] == "DQ903"
    assert (bs["statement"], bs["period_type"], bs["first"]) == (
        "balance_sheet",
        "quarter",
        "2024-06-30",
    )
    assert bs["verified"] is True and bs["applies_until"] is None
    zero = by_kind["dorq_absent_as_zero_accepted"]
    assert zero["codes"] == "DQ915" and zero["item"] == "preferred_dividends"
    assert zero["verified"] is False
    fx = by_kind["fundamentals_currency_mismatch_accepted"]
    assert fx["codes"] == "DQ944" and fx["statement"] == "income"
    assert fx["first"] == "1998-01-01"


def test_a_flag_resolved_after_the_vendor_fixed_it_is_a_data_error(db):
    sid = _company(db)
    # The identity broke; SF1 corrected the row in between; recheck closed it.
    _flag(
        db,
        sid,
        "dorq_identity_break",
        "core.balance_sheet",
        {
            "statement": "balance_sheet",
            "period_type": "quarter",
            "fiscal_date": "2024-06-30",
            "version_no": 1,
            "identity": "balance_sheet",
        },
        code="DQ903",
        detected=T0,
        closed=T1 + dt.timedelta(days=1),
        note="Re-checked: no longer reported",
    )
    _correct(db, "balance_sheet", sid, Q2, total_assets=5_000_000_000_000)
    # A cent move SF1 made under an SQL flag's watch: a vendor fix all the same.
    _flag(
        db,
        sid,
        "fundamentals_negative_impossible",
        "core.income_statement",
        {
            "statement": "income",
            "period_type": "quarter",
            "fiscal_date": "2024-03-31",
            "version_no": 1,
        },
        detected=T0,
        closed=T1 + dt.timedelta(days=2),
    )
    _correct(db, "income", sid, dt.date(2024, 3, 31), revenue=412_300_000.01)
    # Resolved with no correction in between: says nothing.
    _flag(
        db,
        sid,
        "dorq_bad_value",
        "core.income_statement",
        {
            "statement": "income",
            "period_type": "quarter",
            "fiscal_date": "2024-06-30",
            "version_no": 1,
            "item": "revenue",
        },
        code="DQ911",
        detected=T1 + dt.timedelta(days=5),
        closed=T1 + dt.timedelta(days=6),
    )
    export = lb.build(db)
    labels = [lab for lab in export.labels if lb.is_statements(lab)]
    unit = next(lab for lab in labels if lab["kind"] == "corrected_unit")
    assert unit["expect"] == "DQ912|DQ903"
    assert unit["source"].startswith("vendor_correction,dq_flag:")
    fixed = next(lab for lab in labels if lab["kind"].endswith("_vendor_fixed"))
    assert fixed["kind"] == "fundamentals_negative_impossible_vendor_fixed"
    assert fixed["expect"] == "DQ902" and fixed["first"] == "2024-03-31"
    assert fixed["applies_until"] == T1.isoformat()
    assert len(labels) == 2
    assert {r["fiscal_date"] for r in export.restore_statements} == {
        dt.date(2024, 3, 31),
        Q2,
    }


def test_statement_labels_leave_the_bars_labels_as_they_were(db):
    sid = _company(db)
    before = [lab for lab in lb.build(db).labels if not lb.is_statements(lab)]
    _correct(db, "income", sid, Q2, revenue=412_300_000_000)
    _flag(
        db,
        sid,
        "balance_sheet_identity",
        "core.balance_sheet",
        {"period_type": "quarter", "fiscal_date": "2024-06-30"},
        accepted=True,
    )
    after = [lab for lab in lb.build(db).labels if not lb.is_statements(lab)]
    assert after == before


# ---------------------------------------------------------------------------
# Stating one
# ---------------------------------------------------------------------------


def test_label_add_states_a_statements_label_with_its_scope(db, tmp_path):
    sid = _company(db)
    rcfile = _rc(tmp_path, db)
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "--config",
            str(rcfile),
            "dq",
            "label",
            "add",
            "--security-id",
            str(sid),
            "--fiscal-date",
            "2024-06-30",
            "--statement",
            "income",
            "--period-type",
            "quarter",
            "--version-no",
            "1",
            "--item",
            "revenue",
            "--kind",
            "unit_error",
            "--verified",
            "-m",
            "10-Q: 412.3M; SF1 had 412,300M",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SCOPE" in result.output and "income quarter v1 revenue" in result.output
    (label,) = [lab for lab in _labels(db) if lab["source"] == "cli"]
    assert label["class"] == "data_error" and label["expect"] == "DQ912"
    assert (label["first"], label["last"]) == ("2024-06-30", "2024-06-30")
    assert (
        label["statement"],
        label["period_type"],
        label["version_no"],
        label["item"],
    ) == ("income", "quarter", 1, "revenue")
    listed = runner.invoke(cli.main, ["--config", str(rcfile), "dq", "label", "list"])
    assert "income quarter v1 revenue" in listed.output

    for kind, cls, field, code in (
        ("sign_error", "data_error", "expect", "DQ913"),
        ("misdated_filing", "data_error", "expect", "DQ931"),
        ("structural_gap", "market_fact", "codes", "DQ903"),
        ("fiscal_year_change", "market_fact", "codes", "DQ941"),
        ("real_zero", "market_fact", "codes", "DQ915"),
    ):
        (row,) = lb.stated_rows(
            security_id=sid,
            members=None,
            first=Q2,
            last=None,
            label_class=None,
            kind=kind,
            expect=None,
            codes=None,
            note="n",
            verified=True,
            source="t",
            decided_by="t",
            statement="balance_sheet",
        )
        assert row["class"] == cls and row[field] == code
        assert row["statement"] == "balance_sheet"

    refused = [
        ["--fiscal-date", "2024-06-30", "--first", "2024-06-30", "--kind", "real_zero"],
        ["--first", "2024-06-30", "--statement", "income", "--kind", "bad_print"],
        ["--kind", "unit_error"],
    ]
    for extra in refused:
        bad = runner.invoke(
            cli.main,
            [
                "--config",
                str(rcfile),
                "dq",
                "label",
                "add",
                "--security-id",
                str(sid),
                *extra,
                "-m",
                "n",
                "--yes",
            ],
        )
        assert bad.exit_code != 0, extra


def test_accept_with_a_label_states_it_on_the_statement_flags_scope(db, tmp_path):
    sid = _company(db)
    flag = db.fetchval(
        """
        INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,
            check_name, severity, detail, detected_at)
        VALUES (%s, 'core.income_statement', %s, 'dorq_absent_as_zero', 'warn',
                %s, now())
        RETURNING dq_flag_id
        """,
        (
            sid,
            json.dumps(
                {
                    "statement": "income",
                    "period_type": "quarter",
                    "fiscal_date": "2024-06-30",
                    "version_no": 1,
                    "item": "preferred_dividends",
                }
            ),
            json.dumps({"code": "DQ915", "date": "2024-06-30"}),
        ),
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(_rc(tmp_path, db)),
            "dq",
            "accept",
            str(flag),
            "--per-flag",
            "--label",
            "market_fact",
            "-m",
            "10-Q: no preferred dividend this quarter",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    (row,) = db.fetchall("SELECT * FROM ops.dq_label")
    assert (
        row["first_date"],
        row["codes"],
        row["statement"],
        row["period_type"],
        row["version_no"],
        row["item"],
    ) == (Q2, "DQ915", "income", "quarter", 1, "preferred_dividends")


def test_a_stated_statements_label_does_not_replace_a_bars_label(db):
    bars = {
        "series": "7",
        "first": "2024-06-28",
        "last": "2024-07-02",
        "class": "data_error",
        "kind": "bar_deleted",
        "expect": "",
    }
    stated = {
        "series": "7",
        "first": "2024-06-30",
        "last": "2024-06-30",
        "class": "data_error",
        "kind": "unit_error",
        "expect": "DQ912",
        "statement": "income",
    }
    assert not lb._same_decision(bars, stated)
    other_item = {**stated, "item": "revenue"}
    derived = {**stated, "item": "net_income", "kind": "corrected_unit"}
    assert not lb._same_decision(derived, other_item)
    assert lb._same_decision({**derived, "item": "revenue"}, other_item)


def test_import_reads_the_statement_scope(db, tmp_path):
    sid = _company(db)
    path = tmp_path / "hand.jsonl"
    path.write_text(
        json.dumps(
            {
                "series": str(sid),
                "date": "2024-06-30",
                "class": "data_error",
                "expect": "DQ912",
                "kind": "unit_error",
                "statement": "core.income_statement",
                "period_type": "quarter",
                "version_no": 1,
                "item": "revenue",
                "note": "10-Q",
            }
        )
        + "\n"
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(_rc(tmp_path, db)),
            "dq",
            "label",
            "import",
            str(path),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    (label,) = [lab for lab in _labels(db) if lab["kind"] == "unit_error"]
    assert label["statement"] == "income" and label["item"] == "revenue"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "series": str(sid),
                "date": "2024-06-30",
                "class": "data_error",
                "statement": "ledger",
                "note": "n",
            }
        )
        + "\n"
    )
    with pytest.raises(lb.LabelRefused, match="line 1"):
        lb.read_label_file(bad.open())


# ---------------------------------------------------------------------------
# --verified-only and applies_until, as for bars
# ---------------------------------------------------------------------------


def test_verified_only_and_applies_until_behave_as_for_bars(db, tmp_path):
    sid = _company(db)
    _correct(db, "income", sid, Q2, revenue=412_300_000_000)
    _flag(
        db,
        sid,
        "balance_sheet_identity",
        "core.balance_sheet",
        {"period_type": "quarter", "fiscal_date": "2024-06-30"},
        accepted=True,
    )
    _flag(
        db,
        sid,
        "cash_flow_tie",
        "core.cash_flow_statement",
        {"period_type": "quarter", "fiscal_date": "2024-06-30"},
        accepted=True,
        basis="by_rule",
    )
    kinds = {lab["kind"] for lab in _labels(db, verified_only=True)}
    # The correction is the vendor's, which nobody verified; the bulk acceptance
    # is by rule.
    assert kinds == {"balance_sheet_identity_accepted"}

    labels = cmp.labels_from_db(db)
    unit = next(lab for lab in labels if lab.kind == "corrected_unit")
    assert unit.is_statements and unit.applies_until == T1
    assert not unit.applies_at(T1 + dt.timedelta(seconds=1))
    assert unit.applies_at(T1 - dt.timedelta(days=1))
    used = cmp.applicable(labels, T1 + dt.timedelta(days=1))
    assert "corrected_unit" not in {lab.kind for lab in used}
    assert "balance_sheet_identity_accepted" in {lab.kind for lab in used}


def test_dq_compare_statements_judges_a_night_by_the_labels_that_applied(db, tmp_path):
    sid = _company(db)
    night = tmp_path / "2024-12-31.jsonl"
    night.write_text(
        json.dumps(
            {
                "security_id": sid,
                "table_name": "core.income_statement",
                "record_key": {
                    "statement": "income",
                    "period_type": "quarter",
                    "fiscal_date": "2024-06-30",
                    "version_no": 1,
                    "item": "revenue",
                },
                "check_name": "dorq_unit_error",
                "severity": "error",
                "detail": {
                    "code": "DQ912",
                    "date": "2024-06-30",
                    "statement": "income",
                    "period_type": "quarter",
                    "fiscal_year": 2024,
                    "version_no": 1,
                },
            }
        )
        + "\n"
    )
    _correct(db, "income", sid, Q2, revenue=412_300_000_000)
    labels = cmp.labels_from_db(db)
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, params, window_to, status,"
        " started_at) VALUES ('dorq', 'dq-statements-shadow', '{}', '2024-12-31',"
        " 'success', %s)",
        (T1 - dt.timedelta(days=1),),
    )
    before = cst.compare(db, night, labels=labels)
    assert before.precision_warn.right == 1 and before.labels_used >= 1
    db.execute(
        "UPDATE ops.ingestion_run SET started_at = %s", (T1 + dt.timedelta(days=1),)
    )
    after = cst.compare(db, night, labels=labels)
    assert after.precision_warn.right == 0 and after.precision_warn.unlabelled == 1
    ignored = cst.compare(db, night, labels=labels, labels_ignore_time=True)
    assert ignored.precision_warn.right == 1


# ---------------------------------------------------------------------------
# The round trip: dorq's label reader and dorq-eval
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (REAL_DORQ and EVAL),
    reason="set FAFNIR_TEST_DORQ (and dorq-eval beside it, or FAFNIR_TEST_DORQ_EVAL)",
)
def test_round_trip_through_dorq_and_dorq_eval(db, tmp_path):
    sid = _security(db, "STRT")
    _bars(db, sid, _sessions(db, dt.date(2023, 1, 3)), close=30.0)
    bad = dt.date(2021, 9, 30)
    _quarters(db, sid, revenue_at=bad)

    # The night, before SF1's correction: dorq's own report, as jsonl.
    items = dorq_statements.line_items(db)
    paths = dorq_statements.export_context(db, tmp_path, items)
    stream = tmp_path / "statements.csv"
    with db.conn.cursor() as cur, stream.open("wb") as fh:
        with cur.copy(dorq_statements.statements_copy_statement(items)) as copy:
            for chunk in copy:
                fh.write(chunk)
    results = tmp_path / "results.jsonl"
    with stream.open("rb") as stdin, results.open("wb") as out:
        subprocess.run(
            [
                REAL_DORQ,
                "check",
                "--isolated",
                "-",
                "--kind",
                "statements",
                "--format",
                "jsonl",
                "--exit-zero",
                "--show-info",
                "--items",
                str(paths["items"]),
                "--meta",
                str(paths["meta"]),
                "--as-of",
                "2024-12-31",
            ],
            stdin=stdin,
            stdout=out,
            check=True,
        )

    # SF1 corrects the quarter; the warehouse's labels say what was wrong.
    _correct(db, "income", sid, bad, revenue=1_000_000_000 * 1.06)
    labels_path = tmp_path / "labels.jsonl"
    lb.write(lb.build(db), labels_path, None)
    report = tmp_path / "eval.md"
    done = subprocess.run(
        [
            EVAL,
            "--labels",
            str(labels_path),
            "--results",
            str(results),
            "--markdown",
            str(report),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    text = report.read_text()
    row = next(ln for ln in text.splitlines() if ln.startswith("| DQ912 "))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # The label is read (dorq's reader takes every field fafnir writes), and
    # dorq's unit error is the fault it labels.
    assert "1.000" in cells, row
