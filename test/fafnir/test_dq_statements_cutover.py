"""
The statements cutover (DR-0914): the retired SQL fundamentals checks' open flags
close by recheck when a dorq successor flag carries their condition. Every row is
synthetic.
"""

from __future__ import annotations

import json
from datetime import date
from test.fafnir import test_dq_dorq_statements as base
from test.fafnir import test_dq_fundamentals as fund

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.dq import recheck as rc

pytestmark = pytest.mark.integration


def _sql_flag(db, sid, check, table, key):
    return db.fetchval(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detail, detected_at)"
        " VALUES (%s, %s, %s, %s, 'warn', '{}', now()) RETURNING dq_flag_id",
        (sid, table, json.dumps(key), check),
    )


def _dorq_flag(db, sid, fiscal_date, *, state="open", **row):
    base._flag(db, sid, base._statement_row(sid, fiscal_date, **row))
    if state == "accepted":
        db.execute(
            "UPDATE ops.data_quality_flag SET accepted_at = now(), resolved_at = now()"
            " WHERE security_id = %s AND check_name LIKE 'dorq%%'",
            (sid,),
        )
    elif state == "resolved":
        db.execute(
            "UPDATE ops.data_quality_flag SET resolved_at = now()"
            " WHERE security_id = %s AND check_name LIKE 'dorq%%'",
            (sid,),
        )


def test_a_retired_flag_closes_when_a_dorq_successor_carries_it(db):
    period = {"period_type": "annual", "fiscal_date": "2010-12-31"}
    identity = {
        "check": "dorq_identity_break",
        "code": "DQ903",
        "period_type": "annual",
    }
    # Carried: an open DQ903 on the balance sheet's identity, that period.
    carried = base._security(db, "CUTA")
    held = _sql_flag(
        db, carried, "balance_sheet_identity", "core.balance_sheet", period
    )
    _dorq_flag(
        db,
        carried,
        "2010-12-31",
        **identity,
        statement="balance_sheet",
        extra_key={"identity": "balance_sheet"},
    )
    # Not carried: the DQ903 is the balance sheet's, the flag the cash flow's.
    other = base._security(db, "CUTB")
    _sql_flag(db, other, "cash_flow_tie", "core.cash_flow_statement", period)
    _dorq_flag(
        db,
        other,
        "2010-12-31",
        **identity,
        statement="balance_sheet",
        extra_key={"identity": "balance_sheet"},
    )
    # Carried by an accepted successor: the condition was judged a fact.
    accepted = base._security(db, "CUTC")
    stale = _sql_flag(
        db,
        accepted,
        "fundamentals_stale",
        "core.income_statement",
        {"latest_fiscal_date": "2024-12-31"},
    )
    _dorq_flag(
        db,
        accepted,
        "2024-12-31",
        check="dorq_statements_stale",
        code="DQ942",
        statement="income",
        state="accepted",
    )
    # Not carried: the successor was resolved, not accepted.
    resolved = base._security(db, "CUTD")
    _sql_flag(
        db,
        resolved,
        "fundamentals_gap",
        "core.income_statement",
        {"from": "2005-12-31", "to": "2008-12-31"},
    )
    _dorq_flag(
        db,
        resolved,
        "2007-12-31",
        check="dorq_period_gap",
        code="DQ941",
        statement="income",
        state="resolved",
    )
    results = {
        r.check_name: r
        for r in rc.recheck_successors(
            db,
            checks=[
                "balance_sheet_identity",
                "cash_flow_tie",
                "fundamentals_stale",
                "fundamentals_gap",
            ],
        )
    }
    assert results["balance_sheet_identity"].stale_flag_ids == [held]
    assert results["cash_flow_tie"].stale_flag_ids == []
    assert results["fundamentals_stale"].stale_flag_ids == [stale]
    assert results["fundamentals_gap"].stale_flag_ids == []
    assert "DQ903" in results["balance_sheet_identity"].reason
    # A flag another rule closes is not listed twice.
    again = rc.recheck_successors(db, checks=["balance_sheet_identity"], exclude=[held])
    assert again[0].stale_flag_ids == []
    with pytest.raises(ValueError, match="not a retired fundamentals check"):
        rc.recheck_successors(db, checks=["stale"])


def test_cli_recheck_closes_a_held_condition_dorq_carries(db, tmp_path):
    """The SQL condition still holds, so its own rule keeps the flag open; the dorq
    successor flag on the same period closes it, with the cutover's reason."""
    sid = fund._security(db, "CUTLAG")
    fund._put(
        db,
        "income",
        sid,
        date(2010, 3, 31),
        label="2010-Q1",
        known_from=date(2010, 4, 2),
    )
    from fafnir.dq import checks

    assert checks.RETIRED_FUNDAMENTALS_CHECKS["fundamentals_implausible_filing_lag"](db)
    _dorq_flag(
        db,
        sid,
        "2010-03-31",
        check="dorq_filing_date_implausible",
        code="DQ931",
        statement="income",
    )
    rc_file = base._rc(tmp_path, db, tmp_path / "dorq")  # never run
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(rc_file),
            "dq",
            "recheck",
            "--check",
            "fundamentals_implausible_filing_lag",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "0 of       1 no longer hold" in result.output
    assert "1 of       1 carried by dorq" in result.output
    note = db.fetchval(
        "SELECT resolution_note FROM ops.data_quality_flag"
        " WHERE check_name = 'fundamentals_implausible_filing_lag'"
    )
    assert "DR-0914" in note and "DQ931" in note
