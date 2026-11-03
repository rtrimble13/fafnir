"""
`fafnir dq sample` (DR-0914): the stratified sample of a statements night that the
statements cutover verifies on EDGAR. Every row is synthetic.
"""

from __future__ import annotations

import csv
import io
import json
import shlex
from test.fafnir import test_dq_dorq_statements as base

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.dq import sample as smp


def _night(per_code: dict[str, int], *, first_sid: int = 1) -> list[dict]:
    """``per_code[code]`` conditions of each code, on securities from
    ``first_sid``, a quarter each."""
    rows = []
    sid = first_sid
    for code, n in per_code.items():
        for i in range(n):
            rows.append(
                base._statement_row(
                    sid,
                    f"{2001 + i}-03-31",
                    check=f"dorq_{code.lower()}",
                    code=code,
                    severity="warn",
                )
            )
        sid += 1
    return rows


def test_every_code_gets_its_minimum_and_the_rest_fills_to_size():
    night = _night({"DQ903": 50, "DQ912": 30, "DQ931": 4})
    drawn = smp.draw(night, size=40, per_code=10, seed=1)
    assert drawn.conditions == 84
    assert drawn.available == {"DQ903": 50, "DQ912": 30, "DQ931": 4}
    sampled = drawn.sampled()
    assert sum(sampled.values()) == 40
    assert sampled["DQ931"] == 4  # all it has
    assert sampled["DQ903"] >= 10 and sampled["DQ912"] >= 10
    codes = [r["detail"]["code"] for r in drawn.records]
    assert codes == sorted(codes)  # in code order


def test_the_minimum_wins_over_size():
    night = _night({f"DQ9{n}0": 12 for n in range(3)})
    drawn = smp.draw(night, size=10, per_code=5, seed=1)
    assert len(drawn.records) == 15
    assert set(drawn.sampled().values()) == {5}


def test_the_seed_fixes_the_sample_and_a_larger_size_keeps_it():
    night = _night({"DQ903": 60, "DQ912": 60})
    key = smp.engine.condition_key
    one = {key(r) for r in smp.draw(night, size=40, seed=7).records}
    again = {key(r) for r in smp.draw(list(reversed(night)), size=40, seed=7).records}
    other = {key(r) for r in smp.draw(night, size=40, seed=8).records}
    larger = {key(r) for r in smp.draw(night, size=80, seed=7).records}
    assert one == again  # the input's order does not matter
    assert one != other
    assert one < larger


def test_info_reports_cohort_rows_and_repeats_are_left_out():
    night = _night({"DQ903": 3})
    info = base._statement_row(9, "2001-03-31", code="DQ919", severity="info")
    cohort = {**base._statement_row(9, "2002-03-31", code="DQ951"), "security_id": None}
    drawn = smp.draw(night + night[:1] + [info, cohort], size=200, seed=1)
    assert drawn.conditions == 3
    assert drawn.cohort_conditions == 1
    assert drawn.available == {"DQ903": 3}


def test_label_commands_carry_the_report_scope():
    row = base._statement_row(
        12,
        "2020-06-30",
        check="dorq_unit_error",
        code="DQ912",
        statement="income",
        extra_key={"item": "revenue"},
    )
    report = smp.StatementReport.of(row)
    confirm = shlex.split(smp._label_command(report, "confirm"))
    refute = shlex.split(smp._label_command(report, "refute"))
    for args in (confirm, refute):
        assert args[:4] == ["fafnir", "dq", "label", "add"]
        assert args[args.index("--fiscal-date") + 1] == "2020-06-30"
        assert args[args.index("--statement") + 1] == "income"
        assert args[args.index("--period-type") + 1] == "quarter"
        assert args[args.index("--version-no") + 1] == "1"
        assert args[args.index("--item") + 1] == "revenue"
        assert "--verified" in args
    assert confirm[confirm.index("--expect") + 1] == "DQ912"
    assert refute[refute.index("--codes") + 1] == "DQ912"


@pytest.mark.integration
def test_cli_writes_the_sample_and_its_commands_state_a_verified_label(db, tmp_path):
    sid = base._security(db, "SMPL")
    db.execute(
        "UPDATE core.security SET cik = '0000320193' WHERE security_id = %s", (sid,)
    )
    other = base._security(db, "SMPM")
    night = [
        base._statement_row(sid, "2020-06-30", code="DQ903"),
        base._statement_row(
            other,
            "2021-03-31",
            check="dorq_unit_error",
            code="DQ912",
            statement="income",
            extra_key={"item": "revenue"},
        ),
    ]
    statements = tmp_path / "sh" / "statements"
    statements.mkdir(parents=True)
    (statements / "2026-11-20.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in night)
    )
    rc = base._rc(tmp_path, db, tmp_path / "dorq")  # never run
    out = tmp_path / "sample.csv"
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "sample", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "2 of 2 conditions at warn and above sampled" in result.output
    rows = list(csv.DictReader(io.StringIO(out.read_text())))
    assert [r["code"] for r in rows] == ["DQ903", "DQ912"]
    first = rows[0]
    assert (first["ticker"], first["cik"]) == ("SMPL", "0000320193")
    assert "CIK=0000320193" in first["edgar"]
    assert rows[1]["edgar"] == ""  # no CIK on file
    # The confirm command, run as written (with a note and --yes), states a
    # verified label with the report's scope.
    args = shlex.split(rows[1]["confirm"])[1:]
    args[-1] = "EDGAR: the 10-Q reports revenue in thousands"
    result = CliRunner().invoke(cli.main, ["--config", str(rc), *args, "--yes"])
    assert result.exit_code == 0, result.output
    label = db.fetchone(
        "SELECT class AS label_class, expect, verified, statement, period_type,"
        " version_no, item, first_date FROM ops.dq_label WHERE security_id = %s",
        (other,),
    )
    assert label["label_class"] == "data_error"
    assert label["expect"] == "DQ912"
    assert label["verified"] is True
    assert (label["statement"], label["period_type"], label["version_no"]) == (
        "income",
        "quarter",
        1,
    )
    assert label["item"] == "revenue"
    assert str(label["first_date"]) == "2021-03-31"


@pytest.mark.integration
def test_cli_says_where_it_looked_when_there_is_no_night(db, tmp_path):
    rc = base._rc(tmp_path, db, tmp_path / "dorq")  # never run
    result = CliRunner().invoke(cli.main, ["--config", str(rc), "dq", "sample"])
    assert result.exit_code != 0
    assert "No shadow file in" in result.output
    assert "statements" in result.output
