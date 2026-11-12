"""An SQL fundamentals flag the queue accepted carries to its dorq successor.

dorq's write guard knows only dorq's own ``check_name`` and ``record_key``. About
164k flags of the thirteen SQL checks dorq succeeds were accepted by rule (SF1 as
published), and on 2026-11-11 175,748 of a statements night's reports were on
those conditions: at the cutover each would have entered the queue again. The
statements ingest withholds a report an accepted predecessor flag covers, by
``dq compare --statements``'s own rule, and the compare counts what it would write.
These run with no database.
"""

from __future__ import annotations

import datetime as dt

from fafnir.dq import compare_statements as cst


def _report(
    sid=7,
    fiscal="2024-03-31",
    code="DQ903",
    check="dorq_identity_break",
    statement="balance_sheet",
    key=None,
    item=None,
):
    record_key = {
        "statement": statement,
        "period_type": "quarter",
        "fiscal_date": fiscal,
        "version_no": 1,
        **(key or {}),
    }
    if item is not None:
        record_key["item"] = item
    return {
        "security_id": sid,
        "table_name": f"core.{statement}",
        "record_key": record_key,
        "check_name": check,
        "severity": "error",
        "detail": {
            "code": code,
            "date": fiscal,
            "statement": statement,
            "period_type": "quarter",
            "fiscal_year": int(fiscal[:4]),
        },
    }


def _flag(check, key, *, sid=7, table="core.balance_sheet", detail=None):
    return {
        "check_name": check,
        "security_id": sid,
        "table_name": table,
        "record_key": key,
        "detail": detail,
    }


PERIOD = {"period_type": "quarter", "fiscal_date": "2024-03-31"}
IDENTITY = _flag("balance_sheet_identity", PERIOD)


def _r(record):
    return cst.StatementReport.of(record)


def test_an_accepted_identity_flag_carries_its_dq903():
    assert cst.accepted_covers(IDENTITY, _r(_report(key={"identity": "balance_sheet"})))
    # Another identity, period, security or code is another condition.
    assert not cst.accepted_covers(
        IDENTITY, _r(_report(key={"identity": "debt_split"}))
    )
    assert not cst.accepted_covers(
        IDENTITY, _r(_report(fiscal="2024-06-30", key={"identity": "balance_sheet"}))
    )
    assert not cst.accepted_covers(
        IDENTITY, _r(_report(sid=8, key={"identity": "balance_sheet"}))
    )
    assert not cst.accepted_covers(
        IDENTITY, _r(_report(code="DQ904", key={"tie": "cash"}))
    )


def test_an_itemized_flag_carries_only_its_items():
    """Accepting two negative items of a period is no verdict on a third, nor on
    the row as a whole."""
    negatives = _flag(
        "fundamentals_negative_impossible",
        {"statement": "income", **PERIOD},
        table="core.income_statement",
        detail={"revenue": -5, "cost_of_revenue": -2},
    )

    def dq902(item):
        return _r(
            _report(
                code="DQ902",
                check="dorq_impossible_sign",
                statement="income",
                item=item,
            )
        )

    assert cst.accepted_covers(negatives, dq902("revenue"))
    assert cst.accepted_covers(negatives, dq902("cost_of_revenue"))
    assert not cst.accepted_covers(negatives, dq902("sga"))
    assert not cst.accepted_covers(negatives, dq902(None))
    # A flag naming no item is the whole period's.
    whole = {**negatives, "detail": {}}
    assert cst.accepted_covers(whole, dq902(None))
    assert cst.accepted_covers(whole, dq902("sga"))

    zeros = _flag(
        "fundamentals_suspicious_zero",
        {"statement": "income", **PERIOD},
        table="core.income_statement",
        detail={"items": ["revenue"]},
    )

    def dq915(item):
        return _r(
            _report(
                code="DQ915",
                check="dorq_suspicious_zero",
                statement="income",
                item=item,
            )
        )

    assert cst.accepted_covers(zeros, dq915("revenue"))
    assert not cst.accepted_covers(zeros, dq915("net_income"))


def test_the_guard_withholds_what_an_accepted_flag_carries_as_a_stream():
    guard = cst.AcceptedPredecessors(
        [IDENTITY, {**IDENTITY, "security_id": None}]  # no security: never held
    )
    assert guard.flags == 1
    carried = _report(key={"identity": "balance_sheet"})
    other = _report(key={"identity": "debt_split"})
    cohort = {**_report(sid=None), "record_key": {"trade_date": "2024-08-01"}}
    rows = iter([carried, other, cohort, carried])
    out = guard.withhold(rows)
    assert next(out) is other  # lazy: rows pass one at a time
    assert list(out) == [cohort]
    assert guard.withheld == 2


def test_the_text_says_what_the_queue_would_take():
    c = cst.StatementsComparison(shadow_file="x.jsonl", as_of=dt.date(2026, 11, 11))
    c.reports, c.conditions, c.on_accepted, c.withheld = 10, 10, 6, 5
    c.standing = {
        "dorq": 9,
        "sql": 30,
        "ratio": 0.3,
        "bar": cst.STANDING_BAR,
        "written": {"dorq": 4, "ratio": 4 / 30},
    }
    c.new_per_night = {
        "previous": "y.jsonl",
        "dorq": 8,
        "sql": 10,
        "change": -0.2,
        "bar": -0.7,
        "written": {"dorq": 2, "change": -0.8},
    }
    text = cst.format_comparison(c)
    assert (
        "6 on an SQL flag the queue accepted, of which the ingest withholds 5" in text
    )
    assert "as written to the queue: dorq 4 (13%)" in text
    assert "as written to the queue: dorq 2 (-80%)" in text
    d = c.as_dict()
    assert d["withheld"] == 5 and d["standing"]["written"]["dorq"] == 4


def test_carrying_holds_to_the_sql_checks_own_condition():
    """covers() matches loosely for the compare; a carry withholds a write, so it
    is held to the condition the SQL check itself keys."""
    # A restated version that still fails is a new condition.
    v1 = _flag(
        "fundamentals_negative_impossible",
        {**PERIOD, "version_no": 1, "statement": "income"},
        table="core.income_statement",
        detail={"revenue": -5},
    )

    def neg(version):
        return _r(
            _report(
                code="DQ902",
                check="dorq_negative_impossible",
                statement="income",
                item="revenue",
                key={"version_no": version},
            )
        )

    assert cst.accepted_covers(v1, neg(1))
    assert not cst.accepted_covers(v1, neg(3))
    # A stale flag is about its latest period: a later filing is a new occurrence.
    stale = _flag(
        "fundamentals_stale",
        {"latest_fiscal_date": "2022-12-31"},
        table="core.income_statement",
    )

    def dq942(fiscal):
        return _r(_report(fiscal=fiscal, code="DQ942", check="dorq_stale_filing"))

    assert cst.accepted_covers(stale, dq942("2022-12-31"))
    assert not cst.accepted_covers(stale, dq942("2025-06-30"))
    # A broken trailing four is the income statement's: no verdict on a cash flow
    # gap the year before.
    ttm = _flag(
        "fundamentals_ttm_incomplete",
        {"fiscal_year": 2024},
        table="core.income_statement",
    )

    def dq941(statement):
        return _r(
            _report(
                fiscal="2023-09-30",
                code="DQ941",
                check="dorq_period_gap",
                statement=statement,
            )
        )

    assert cst.accepted_covers(ttm, dq941("income"))
    assert not cst.accepted_covers(ttm, dq941("cash_flow"))
