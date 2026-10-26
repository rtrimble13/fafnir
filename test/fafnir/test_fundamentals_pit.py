"""
Point-in-time access and TTM (#95, migration 0036): integration tests
(FAFNIR_TEST_DSN). Every row is synthetic.

The plan's §5.1-5.2 (doc/plans/fundamentals-sf1.md) and ADR 0018: a read as of a date
sees only what was known then, each statement at its own version in force, joined on
fiscal_date; the two-axis read sees what fafnir held; TTM sums four contiguous
quarters of a flow or is NULL, never sums a stock, and is known from its last
constituent.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

D = date
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


@pytest.fixture()
def sid(db):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    return repo.upsert_security(
        db,
        primary_symbol="ZZPIT",
        company_name="ZZ Point In Time Corp",
        exchange_code="NYSE",
        source="sharadar",
    )


def _put(
    db,
    statement,
    sid,
    fiscal_date,
    *,
    label,
    known_from,
    version_no=1,
    known_to=None,
    source="filed",
    loaded_at=T0,
    superseded_at=None,
    **items,
):
    """One version row, as the loader would write it."""
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900030",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version_no,
        "known_from": known_from,
        "known_to": known_to,
        "knowledge_source": source,
        "filing_date": known_from if source == "filed" else None,
        "loaded_at": loaded_at,
        "superseded_at": superseded_at,
        "reported_currency": "USD",
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _as_of(db, as_of, load_as_of=None, period_type="annual"):
    args = (as_of,) if load_as_of is None else (as_of, load_as_of)
    marks = ", ".join(["%s"] * len(args))
    return db.fetchone(
        f"SELECT * FROM mart.fn_statements_as_of({marks}) WHERE period_type = %s",
        (*args, period_type),
    )


# ---------------------------------------------------------------------------
# The shape: every object's columns are the map's
# ---------------------------------------------------------------------------


def _columns(db, relation):
    with db.conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {relation} LIMIT 0")
        return [d.name for d in cur.description]


@pytest.mark.parametrize("statement", list(fm.STATEMENTS))
def test_the_versions_views_carry_the_maps_items_and_nothing_vendor(db, statement):
    view = f"mart.v_{fm.TABLES[statement].split('.')[1]}_versions"
    cols = _columns(db, view)
    assert cols[-len(fm.STATEMENTS[statement]) :] == [
        i.column for i in fm.STATEMENTS[statement]
    ]
    # Never through mart: SF1's metrics (ADR 0019), the vendor key, the hash.
    assert not {"extra", "vendor_key", "content_hash", "ingestion_run_id"} & set(cols)


def test_the_functions_carry_every_line_item_once(db):
    every = [i.column for items in fm.STATEMENTS.values() for i in items]
    for relation in (
        "mart.fn_statements_as_of(current_date)",
        "mart.fn_statements_as_of(current_date, now())",
        "mart.v_statements_current",
    ):
        cols = _columns(db, relation)
        assert cols[-len(every) :] == every, relation
        assert len(cols) == len(set(cols))
    ttm = _columns(db, "mart.fn_fundamentals_ttm(current_date)")
    assert ttm[-len(every) :] == every
    assert _columns(db, "mart.v_fundamentals_ttm") == ttm


# ---------------------------------------------------------------------------
# fn_statements_as_of
# ---------------------------------------------------------------------------


def test_a_read_as_of_a_date_sees_only_what_was_known(db, sid):
    _put(
        db,
        "income",
        sid,
        D(2019, 12, 31),
        label="2019-FY",
        known_from=D(2020, 2, 20),
        revenue=4000,
    )
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2021, 2, 18),
        known_to=D(2026, 1, 5),
        revenue=5000,
    )
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2026, 1, 5),
        version_no=2,
        source="observed",
        revenue=4900,
    )

    # Before FY2020's 10-K: FY2019 is the latest known.
    row = _as_of(db, D(2021, 2, 17))
    assert (row["fiscal_date"], row["revenue"]) == (D(2019, 12, 31), 4000)
    # From the filing to the restatement: FY2020 as filed.
    row = _as_of(db, D(2021, 2, 18))
    assert (row["fiscal_date"], row["income_version_no"], row["revenue"]) == (
        D(2020, 12, 31),
        1,
        5000,
    )
    assert _as_of(db, D(2026, 1, 4))["revenue"] == 5000
    # From the day it was observed: the restatement.
    row = _as_of(db, D(2026, 1, 5))
    assert (row["income_version_no"], row["revenue"], row["knowledge_source"]) == (
        2,
        4900,
        "observed",
    )
    # Nothing is known before the first filing.
    assert _as_of(db, D(2020, 2, 19)) is None


def test_the_statements_join_on_fiscal_date_each_at_its_own_version(db, sid):
    fy = D(2020, 12, 31)
    _put(
        db, "income", sid, fy, label="2020-FY", known_from=D(2021, 2, 18), revenue=5000
    )
    _put(
        db,
        "balance_sheet",
        sid,
        fy,
        label="2020-FY",
        known_from=D(2021, 2, 18),
        known_to=D(2026, 1, 5),
        total_assets=900,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        fy,
        label="2020-FY",
        known_from=D(2026, 1, 5),
        version_no=2,
        source="observed",
        total_assets=950,
    )
    row = _as_of(db, D(2026, 3, 1))
    assert (row["has_income"], row["has_balance_sheet"], row["has_cash_flow"]) == (
        True,
        True,
        False,
    )
    assert (row["income_version_no"], row["balance_sheet_version_no"]) == (1, 2)
    assert row["cash_flow_version_no"] is None and row["net_cash_operating"] is None
    assert (row["revenue"], row["total_assets"]) == (5000, 950)
    # The row was knowable only when its last statement was, and is as weak as its
    # weakest.
    assert (row["known_from"], row["knowledge_source"]) == (D(2026, 1, 5), "observed")
    assert (row["income_known_from"], row["balance_sheet_known_from"]) == (
        D(2021, 2, 18),
        D(2026, 1, 5),
    )


def test_a_statement_ahead_of_the_others_makes_its_period_the_latest(db, sid):
    """The latest period is the latest any statement knows: the others are NULL for
    it rather than reaching back to an older period."""
    _put(db, "income", sid, D(2019, 12, 31), label="2019-FY", known_from=D(2020, 2, 20))
    _put(
        db,
        "balance_sheet",
        sid,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2021, 2, 18),
        source="estimated",
    )
    row = _as_of(db, D(2021, 3, 1))
    assert (row["fiscal_date"], row["has_income"], row["has_balance_sheet"]) == (
        D(2020, 12, 31),
        False,
        True,
    )
    assert row["knowledge_source"] == "estimated"


def test_annual_and_quarterly_are_separate_rows(db, sid):
    _put(db, "income", sid, D(2020, 12, 31), label="2020-FY", known_from=D(2021, 2, 18))
    _put(db, "income", sid, D(2021, 3, 31), label="2021-Q1", known_from=D(2021, 5, 3))
    rows = db.fetchall(
        "SELECT period_type, fiscal_date FROM mart.fn_statements_as_of(%s)"
        " ORDER BY period_type",
        (D(2021, 6, 1),),
    )
    assert [(r["period_type"], r["fiscal_date"]) for r in rows] == [
        ("annual", D(2020, 12, 31)),
        ("quarter", D(2021, 3, 31)),
    ]


def test_the_two_axis_read_reproduces_what_fafnir_held(db, sid):
    fy = D(2020, 12, 31)
    later = T0 + timedelta(days=200)
    # Version 1 as first loaded, then corrected by the vendor at `later`, and a
    # restatement loaded at `later` too.
    _put(
        db,
        "income",
        sid,
        fy,
        label="2020-FY",
        known_from=D(2021, 2, 18),
        known_to=D(2026, 7, 24),
        superseded_at=later,
        revenue=5000,
    )
    _put(
        db,
        "income",
        sid,
        fy,
        label="2020-FY",
        known_from=D(2021, 2, 18),
        known_to=D(2026, 7, 24),
        loaded_at=later,
        revenue=5001,
    )
    _put(
        db,
        "income",
        sid,
        fy,
        label="2020-FY",
        known_from=D(2026, 7, 24),
        version_no=2,
        source="observed",
        loaded_at=later,
        revenue=4900,
    )

    # Held before `later`: the first load, with no version 2, so it is open.
    held = _as_of(db, D(2026, 12, 1), T0 + timedelta(days=1))
    assert (held["income_version_no"], held["revenue"]) == (1, 5000)
    # Held after: the correction, and the restatement from its day.
    now = _as_of(db, D(2026, 12, 1), later + timedelta(days=1))
    assert (now["income_version_no"], now["revenue"]) == (2, 4900)
    assert _as_of(db, D(2026, 7, 1), later + timedelta(days=1))["revenue"] == 5001
    # And the one-axis read is the two-axis read at now.
    assert _as_of(db, D(2026, 12, 1))["revenue"] == 4900
    # Before anything was loaded, nothing was held.
    assert _as_of(db, D(2026, 12, 1), T0 - timedelta(days=1)) is None


def test_the_current_view_is_today(db, sid):
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2021, 2, 18),
        revenue=5000,
    )
    tomorrow = date.today() + timedelta(days=1)
    _put(
        db,
        "income",
        sid,
        D(2021, 12, 31),
        label="2021-FY",
        known_from=tomorrow,
        revenue=6000,
    )
    (row,) = db.fetchall(
        "SELECT * FROM mart.v_statements_current WHERE security_id = %s", (sid,)
    )
    assert row["revenue"] == 5000  # tomorrow's filing is not in force today


def test_a_filter_on_security_reaches_the_indexes(db, sid):
    """The functions are inlined: a caller's filter goes to the statement tables, so
    one security's read probes an index rather than reading every row."""
    db.execute("SET enable_seqscan = off")
    try:
        for query in (
            "SELECT * FROM mart.fn_statements_as_of(current_date) WHERE security_id = 1",
            "SELECT * FROM mart.v_statements_current WHERE security_id = 1",
            "SELECT * FROM mart.fn_fundamentals_ttm(current_date) WHERE security_id = 1",
        ):
            plan = "\n".join(r["QUERY PLAN"] for r in db.fetchall(f"EXPLAIN {query}"))
            assert "Function Scan" not in plan, plan
            assert "Index" in plan, plan
    finally:
        db.execute("RESET enable_seqscan")


# ---------------------------------------------------------------------------
# fn_fundamentals_ttm
# ---------------------------------------------------------------------------

QUARTERS = [
    # (period end, label, filed)
    (D(2020, 3, 31), "2020-Q1", D(2020, 5, 1)),
    (D(2020, 6, 30), "2020-Q2", D(2020, 8, 1)),
    (D(2020, 9, 30), "2020-Q3", D(2020, 11, 1)),
    (D(2020, 12, 31), "2020-Q4", D(2021, 2, 18)),
    (D(2021, 3, 31), "2021-Q1", D(2021, 5, 3)),
]


def _quarters(
    db, sid, quarters=QUARTERS, *, statements=("income", "cash_flow", "balance_sheet")
):
    for n, (end, label, filed) in enumerate(quarters, start=1):
        if "income" in statements:
            _put(
                db,
                "income",
                sid,
                end,
                label=label,
                known_from=filed,
                revenue=100 * n,
                weighted_average_shares=1000 + n,
            )
        if "cash_flow" in statements:
            _put(
                db,
                "cash_flow",
                sid,
                end,
                label=label,
                known_from=filed,
                net_cash_operating=10 * n,
            )
        if "balance_sheet" in statements:
            _put(
                db,
                "balance_sheet",
                sid,
                end,
                label=label,
                known_from=filed,
                total_assets=5000 + n,
            )


def _ttm(db, as_of, fiscal_date):
    return db.fetchone(
        "SELECT * FROM mart.fn_fundamentals_ttm(%s) WHERE fiscal_date = %s",
        (as_of, fiscal_date),
    )


def test_ttm_sums_four_contiguous_quarters_of_a_flow(db, sid):
    _quarters(db, sid)
    row = _ttm(db, D(2021, 6, 1), D(2021, 3, 31))
    assert row["ttm_quarters_used"] == 4 and row["cash_flow_quarters_used"] == 4
    assert row["revenue"] == 200 + 300 + 400 + 500
    assert row["net_cash_operating"] == 20 + 30 + 40 + 50
    # Stocks and share counts take the latest quarter: never summed.
    assert row["total_assets"] == 5005
    assert row["weighted_average_shares"] == 1005
    # Known when its last constituent was.
    assert row["known_from"] == D(2021, 5, 3)
    assert (row["income_known_from"], row["balance_sheet_known_from"]) == (
        D(2021, 5, 3),
        D(2021, 5, 3),
    )


def test_a_three_quarter_window_is_null_not_a_partial_sum(db, sid):
    _quarters(db, sid)
    row = _ttm(db, D(2021, 6, 1), D(2020, 9, 30))
    assert row["ttm_quarters_used"] == 3
    assert row["revenue"] is None and row["net_cash_operating"] is None
    assert row["total_assets"] == 5003  # a stock needs no window


def test_a_missing_quarter_breaks_the_window(db, sid):
    _quarters(db, sid, [q for q in QUARTERS if q[1] != "2020-Q3"])
    row = _ttm(db, D(2021, 6, 1), D(2021, 3, 31))
    assert row["ttm_quarters_used"] == 2 and row["revenue"] is None


def test_a_flow_null_in_one_quarter_is_null(db, sid):
    _quarters(db, sid)
    db.execute(
        "UPDATE core.income_statement SET revenue = NULL"
        " WHERE security_id = %s AND fiscal_period = 'Q3'",
        (sid,),
    )
    row = _ttm(db, D(2021, 6, 1), D(2021, 3, 31))
    assert row["ttm_quarters_used"] == 4 and row["revenue"] is None


def test_a_long_first_quarter_is_contiguous(db, sid):
    """A 16-week first quarter (a retailer's 16-12-12-12 year) is 112 days."""
    q = [
        (D(2020, 5, 23), "2020-Q1", D(2020, 6, 20)),
        (D(2020, 8, 15), "2020-Q2", D(2020, 9, 12)),
        (D(2020, 11, 7), "2020-Q3", D(2020, 12, 5)),
        (D(2021, 1, 30), "2020-Q4", D(2021, 3, 30)),
        (D(2021, 5, 22), "2021-Q1", D(2021, 6, 19)),
    ]
    _quarters(db, sid, q)
    assert _ttm(db, D(2021, 7, 1), D(2021, 5, 22))["ttm_quarters_used"] == 4


def test_ttm_is_point_in_time(db, sid):
    _quarters(db, sid)
    # Before 2021-Q1 was filed, the window ends at 2020-Q4.
    assert _ttm(db, D(2021, 5, 2), D(2021, 3, 31)) is None
    assert _ttm(db, D(2021, 5, 2), D(2020, 12, 31))["revenue"] == 1000
    # An older quarter restated later makes the TTM known only from then.
    db.execute(
        "UPDATE core.income_statement SET known_to = '2021-08-02'"
        " WHERE security_id = %s AND fiscal_period = 'Q2' AND fiscal_year = 2020",
        (sid,),
    )
    _put(
        db,
        "income",
        sid,
        D(2020, 6, 30),
        label="2020-Q2",
        known_from=D(2021, 8, 2),
        version_no=2,
        source="observed",
        revenue=150,
        weighted_average_shares=1002,
    )
    # Every window holding 2020-Q2 reads its restated value from its day on.
    row = _ttm(db, D(2021, 9, 1), D(2021, 3, 31))
    assert row["revenue"] == 150 + 300 + 400 + 500
    assert row["known_from"] == D(2021, 8, 2)
    row = _ttm(db, D(2021, 9, 1), D(2020, 12, 31))
    assert row["revenue"] == 100 + 150 + 300 + 400
    assert (row["known_from"], row["knowledge_source"]) == (D(2021, 8, 2), "observed")
    assert _ttm(db, D(2021, 8, 1), D(2020, 12, 31))["revenue"] == 1000


def test_cash_flow_is_counted_on_its_own(db, sid):
    _quarters(db, sid, statements=("income", "balance_sheet"))
    _quarters(db, sid, QUARTERS[2:], statements=("cash_flow",))
    row = _ttm(db, D(2021, 6, 1), D(2021, 3, 31))
    assert (row["ttm_quarters_used"], row["cash_flow_quarters_used"]) == (4, 3)
    assert row["revenue"] == 1400 and row["net_cash_operating"] is None


def test_annual_rows_are_not_quarters(db, sid):
    _quarters(db, sid)
    _put(
        db,
        "income",
        sid,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2021, 2, 18),
        revenue=99999,
    )
    assert _ttm(db, D(2021, 6, 1), D(2021, 3, 31))["revenue"] == 1400
