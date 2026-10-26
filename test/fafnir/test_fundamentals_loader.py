"""
The SF1 loader's logic (#90): unit tests, no database. Every row is synthetic.

What the plan's §10 asks of the unit suite: the map from a row, fiscal labels,
``known_from`` by every rule of §4.5, share-term recovery, the hash at 1,000-share
precision, and routing (a first-reported change is a correction, a restated one a
restatement).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from fafnir.ingest import fundamentals as fu

D = Decimal
KEY = "199059"
TODAY = date(2026, 10, 25)

#: A quarter of a company, in SF1's spelling, as sharadar_files types it.
BASE = {
    "revenue": D("1000"),
    "cor": D("400"),
    "gp": D("600"),
    "netinc": D("100"),
    "netinccmn": D("100"),
    "ebt": D("130"),
    "shareswa": D("1000000"),
    "shareswadil": D("1010000"),
    "rnd": D("0"),
    "intexp": None,
    "assets": D("5000"),
    "equity": D("2000"),
    "sharesbas": D("1000000"),
    "ncfo": D("200"),
    "capex": D("-50"),
    "eps": D("0.1"),
    "pe": D("12.345"),
    "fxusd": D("1"),
}


def sf1(
    dimension="ARQ",
    reportperiod=date(2020, 3, 31),
    filed=date(2020, 5, 1),
    fiscalperiod="2020-Q1",
    ticker="ZZA",
    **values,
):
    row = {
        "ticker": ticker,
        "dimension": dimension,
        "calendardate": reportperiod,
        "date": filed if dimension.startswith("AR") else reportperiod,
        "reportperiod": reportperiod,
        "fiscalperiod": fiscalperiod,
        "lastupdated": TODAY,
    }
    row.update(BASE)
    row.update(values)
    return row


def run(rows, book=None, *, splits=(), as_of=TODAY):
    book = book or fu.Book()
    ops, events = fu.plan(
        [fu.parse(r) for r in rows],
        book,
        vendor_key=KEY,
        currency="USD",
        splits=list(splits),
        as_of=as_of,
    )
    return ops, events, book


def inserted(ops, statement="income"):
    return [
        o.version
        for o in ops
        if o.kind == "insert" and o.version.statement == statement
    ]


# ---------------------------------------------------------------------------
# A row
# ---------------------------------------------------------------------------


def test_a_first_reported_row_is_typed_by_statement():
    r = fu.parse(sf1(unknowncol=D("7")))
    assert (r.period_type, r.fiscal_date, r.fiscal_year, r.fiscal_period) == (
        "quarter",
        date(2020, 3, 31),
        2020,
        "Q1",
    )
    assert r.filing_date == date(2020, 5, 1) and r.first_reported
    assert r.values["income"]["revenue"] == D("1000")
    assert r.values["income"]["income_before_tax"] == D("130")  # ebt is typed
    assert r.values["income"]["interest_expense"] is None  # NULL stays NULL
    assert r.values["income"]["research_and_development"] == 0  # and 0 stays 0
    assert r.values["balance_sheet"]["shares_outstanding"] == D("1000000")
    assert r.values["cash_flow"]["capital_expenditure"] == D("-50")
    # Not typed: metrics, per-share values and conversions go to extra.
    assert r.vendor_metric == {"eps": D("0.1"), "pe": D("12.345"), "fxusd": D("1")}
    assert r.unmapped == {"unknowncol": D("7")}


def test_a_restated_row_has_no_filing_date():
    r = fu.parse(sf1("MRY", date(2015, 6, 30), fiscalperiod="2015-FY"))
    assert (r.period_type, r.fiscal_period, r.filing_date) == ("annual", "FY", None)
    assert not r.first_reported


@pytest.mark.parametrize(
    "reportperiod, fiscalperiod, year",
    [
        # AAPL's year ends in September: its first quarter ends in December.
        (date(2019, 12, 28), "2020-Q1", 2020),
        (date(2020, 9, 26), "2020-FY", 2020),
        # MSFT's ends in June.
        (date(2015, 6, 30), "2015-FY", 2015),
    ],
)
def test_the_fiscal_year_is_sf1s_label_not_the_calendar(
    reportperiod, fiscalperiod, year
):
    dim = "ARY" if fiscalperiod.endswith("FY") else "ARQ"
    r = fu.parse(sf1(dim, reportperiod, reportperiod + timedelta(40), fiscalperiod))
    assert r.fiscal_year == year


@pytest.mark.parametrize(
    "change, check",
    [
        ({"reportperiod": None}, fu.MISSING_LABEL),
        ({"calendardate": None}, fu.MISSING_LABEL),
        ({"fiscalperiod": "2020Q1"}, fu.MISSING_LABEL),
        ({"fiscalperiod": None}, fu.MISSING_LABEL),
        ({"fiscalperiod": "2020-FY"}, fu.MISSING_LABEL),  # an FY label on ARQ
        ({"date": None}, fu.MISSING_LABEL),
        ({"date": date(2020, 3, 30)}, fu.FILED_EARLY),
        ({"revenue": D("1E+22")}, fu.OUT_OF_RANGE),
        ({"shareswa": D("1E+20")}, fu.OUT_OF_RANGE),
    ],
)
def test_a_bad_row_is_quarantined_under_its_check(change, check):
    row = sf1()
    row.update(change)
    with pytest.raises(fu.Quarantined) as caught:
        fu.parse(row)
    assert caught.value.check == check


def test_a_quarterly_label_on_an_annual_row_is_refused():
    with pytest.raises(fu.Quarantined):
        fu.parse(sf1("MRY", fiscalperiod="2020-Q4"))


# ---------------------------------------------------------------------------
# Share terms, the hash, the comparison
# ---------------------------------------------------------------------------


def test_the_split_product_is_exact_and_after_the_day():
    splits = [
        (date(2014, 6, 9), Fraction(7)),
        (date(2020, 8, 31), Fraction(4)),
        (date(2022, 1, 3), Fraction(1, 30)),
    ]
    assert fu.split_product(splits, date(2014, 1, 1)) == Fraction(28, 30)
    assert fu.split_product(splits, date(2020, 8, 31)) == Fraction(1, 30)  # its ex-date
    assert fu.split_product(splits, date(2023, 1, 1)) == 1


def test_share_counts_are_recovered_to_the_nearest_share_and_money_is_untouched():
    values = {
        "revenue": D("1000.004"),
        "weighted_average_shares": D("28000003"),
        "weighted_average_shares_diluted": None,
    }
    out = fu.in_terms("income", values, Fraction(28))
    assert out["revenue"] == D("1000.00")
    assert out["weighted_average_shares"] == D("1000000.0000")  # 1,000,000.107
    assert out["weighted_average_shares_diluted"] is None
    # A reverse split multiplies: 1-for-30 after the day.
    assert fu.in_terms(
        "income", {"weighted_average_shares": D("33333")}, Fraction(1, 30)
    )["weighted_average_shares"] == D("999990")
    # Nothing divides: as given.
    assert fu.in_terms("income", {"weighted_average_shares": D("12.5")}, Fraction(1))[
        "weighted_average_shares"
    ] == D("12.5000")


def _income(**changes):
    values = fu.in_terms("income", fu.parse(sf1()).values["income"], Fraction(1))
    values.update(changes)
    return values


def test_the_hash_sees_share_counts_to_1000_shares():
    base = fu.content_hash("income", _income(), date(2020, 5, 1))
    assert base == fu.content_hash("income", _income(), date(2020, 5, 1))
    near = _income(weighted_average_shares=D("1000400.0000"))
    assert fu.content_hash("income", near, date(2020, 5, 1)) == base
    far = _income(weighted_average_shares=D("1001000.0000"))
    assert fu.content_hash("income", far, date(2020, 5, 1)) != base
    # None is told from 0, a cent is a change, and so is the filing date.
    assert fu.content_hash("income", _income(interest_expense=D("0.00")), None) != (
        fu.content_hash("income", _income(), None)
    )
    assert (
        fu.content_hash("income", _income(revenue=D("1000.01")), date(2020, 5, 1))
        != base
    )
    assert fu.content_hash("income", _income(), date(2020, 5, 2)) != base
    # Negative zero is zero.
    assert fu.content_hash("income", _income(revenue=D("-0.00")), None) == (
        fu.content_hash("income", _income(revenue=D("0.00")), None)
    )


def test_moved_tolerates_a_recoverys_rounding_only():
    a = _income()
    assert fu.moved("income", a, _income(weighted_average_shares=D("1000999"))) == {}
    assert "weighted_average_shares" in fu.moved(
        "income", a, _income(weighted_average_shares=D("1001001"))
    )
    big = _income(weighted_average_shares=D("5000000000"))
    # A millionth of five billion is 5,000 shares: a deep reverse split's rounding.
    assert (
        fu.moved("income", big, _income(weighted_average_shares=D("5000004000"))) == {}
    )
    assert fu.moved("income", a, _income(interest_expense=D("0.00"))) == {
        "interest_expense": (None, D("0.00"))
    }
    assert fu.moved("income", a, _income(revenue=D("1")), skip=("revenue",)) == {}


# ---------------------------------------------------------------------------
# known_from, by every rule of §4.5
# ---------------------------------------------------------------------------


def test_rule_1_a_first_reported_row_is_known_from_its_filing():
    ops, events, book = run([sf1()])
    (v,) = inserted(ops)
    assert (v.version_no, v.known_from, v.known_to, v.knowledge_source) == (
        1,
        date(2020, 5, 1),
        None,
        "filed",
    )
    assert v.filing_date == date(2020, 5, 1) and v.reported_currency == "USD"
    assert v.extra == {
        "vendor_metric": {"eps": D("0.1"), "pe": D("12.345"), "fxusd": D("1")}
    }
    assert inserted(ops, "balance_sheet")[0].extra == {}
    assert {e.kind for e in events} == {"inserted"}
    assert len(ops) == 3  # one row per statement


def test_rule_1_share_counts_are_in_the_terms_of_the_filing():
    splits = [(date(2020, 8, 31), Fraction(4))]  # after the filing
    ops, _, _ = run(
        [sf1(shareswa=D("4000000"), shareswadil=D("4040000"), sharesbas=D("4000004"))],
        splits=splits,
    )
    assert inserted(ops)[0].values["weighted_average_shares"] == D("1000000")
    assert inserted(ops, "balance_sheet")[0].values["shares_outstanding"] == D(
        "1000001"
    )


def test_a_twin_that_agrees_adds_no_version():
    ops, events, _ = run([sf1(), sf1("MRQ")])
    assert len(inserted(ops)) == 1
    assert [e.kind for e in events if e.statement == "income"] == [
        "inserted",
        "unchanged",
    ]


def test_rule_2_a_restatement_is_known_from_the_load_day_and_chains():
    ops, events, book = run([sf1(), sf1("MRQ", revenue=D("990"), sharesbas=D("7"))])
    v1, v2 = inserted(ops)
    # The planned version 1 is closed in its own insert: no update is needed.
    assert v1.known_to == TODAY and not [o for o in ops if o.kind == "close"]
    assert (v2.version_no, v2.known_from, v2.known_to, v2.knowledge_source) == (
        2,
        TODAY,
        None,
        "observed",
    )
    assert v2.filing_date is None and v2.values["revenue"] == D("990.00")
    restated = [e for e in events if e.kind == "restated"]
    assert [e.statement for e in restated] == ["income"]  # each table on its own
    assert restated[0].moved == {"revenue": (D("1000.00"), D("990.00"))}
    # The cover count is the filing's, carried into the new version's terms.
    (b1,) = inserted(ops, "balance_sheet")
    assert b1.values["shares_outstanding"] == D("1000000")


def test_a_restated_cover_count_alone_is_not_a_restatement():
    ops, events, _ = run([sf1(), sf1("MRQ", sharesbas=D("1200000"))])
    assert len(inserted(ops, "balance_sheet")) == 1
    assert not [e for e in events if e.kind == "restated"]


def test_a_restatement_carries_the_cover_count_across_a_split_between():
    splits = [(date(2024, 6, 10), Fraction(10))]
    v1 = fu.Version(
        statement="balance_sheet",
        period_type="quarter",
        fiscal_date=date(2020, 3, 31),
        version_no=1,
        known_from=date(2020, 5, 1),
        known_to=None,
        knowledge_source="filed",
        filing_date=date(2020, 5, 1),
        values={
            **fu.in_terms(
                "balance_sheet", fu.parse(sf1()).values["balance_sheet"], Fraction(1)
            ),
        },
        content_hash="0" * 64,
        vendor_key=KEY,
    )
    ops, events, book = run(
        [
            sf1(
                "MRQ",
                assets=D("4999"),
                shareswa=D("10000000"),
                shareswadil=D("10100000"),
            )
        ],
        fu.Book([v1]),
        splits=splits,
    )
    close, insert = [o for o in ops if o.version.statement == "balance_sheet"]
    assert (close.kind, close.known_to) == ("close", TODAY)
    # Stored in 2020's terms (1,000,000), in today's terms after the 10-for-1.
    assert insert.version.values["shares_outstanding"] == D("10000000")
    # The restated share counts, recovered into version 1's terms, did not move
    # anything: only total assets did.
    (e,) = [e for e in events if e.kind == "restated"]
    assert (e.statement, list(e.moved)) == ("balance_sheet", ["total_assets"])


def test_rule_3_a_period_with_no_first_reported_row_is_known_from_the_next_filing():
    q1 = sf1("MRQ", date(2020, 3, 31), sharesbas=D("5"))
    q2 = sf1("ARQ", date(2020, 6, 30), date(2020, 8, 4), "2020-Q2")
    fy = sf1("ARY", date(2020, 12, 31), date(2021, 2, 20), "2020-FY")
    ops, events, _ = run([q1, q2, fy])
    est = [v for v in inserted(ops) if v.fiscal_date == date(2020, 3, 31)][0]
    assert (est.known_from, est.knowledge_source, est.filing_date) == (
        date(2020, 8, 4),
        "estimated",
        None,
    )
    bal = [
        v for v in inserted(ops, "balance_sheet") if v.fiscal_date == date(2020, 3, 31)
    ]
    assert bal[0].values["shares_outstanding"] is None  # first-reported rows only


def test_rule_3_a_filing_for_the_same_period_end_counts():
    q4 = sf1("MRQ", date(2020, 12, 31), fiscalperiod="2020-Q4")
    fy = sf1("ARY", date(2020, 12, 31), date(2021, 2, 20), "2020-FY")
    ops, _, _ = run([q4, fy])
    (q,) = [v for v in inserted(ops) if v.period_type == "quarter"]
    assert q.known_from == date(2021, 2, 20)


def test_rule_3_with_no_later_filing_it_is_known_from_the_load_day():
    ops, _, _ = run([sf1("MRQ")])
    (v,) = inserted(ops)
    assert (v.known_from, v.knowledge_source) == (TODAY, "observed")


def test_rule_4_never_a_restated_rows_date_nor_lastupdated():
    # The MR row's `date` is the period end and its stamp is today: neither is used.
    ops, _, _ = run([sf1(), sf1("MRQ", revenue=D("1"), lastupdated=date(2021, 1, 1))])
    assert [v.known_from for v in inserted(ops)] == [date(2020, 5, 1), TODAY]


def test_a_restatement_of_a_version_known_from_today_is_known_from_tomorrow():
    ops, _, _ = run(
        [sf1(filed=TODAY), sf1("MRQ", revenue=D("1"))],
        as_of=TODAY,
    )
    v1, v2 = inserted(ops)
    assert (v1.known_from, v1.known_to, v2.known_from) == (
        TODAY,
        TODAY + timedelta(1),
        TODAY + timedelta(1),
    )


# ---------------------------------------------------------------------------
# What a load meets: versions already held
# ---------------------------------------------------------------------------


def _held_from(rows, *, splits=(), as_of=TODAY):
    """The book a first load of ``rows`` would leave, as the database returns it."""
    ops, _, _ = run(rows, splits=splits, as_of=as_of)
    out = []
    for o in ops:
        v = o.version
        out.append(
            fu.Version(
                statement=v.statement,
                period_type=v.period_type,
                fiscal_date=v.fiscal_date,
                version_no=v.version_no,
                known_from=v.known_from,
                known_to=v.known_to,
                knowledge_source=v.knowledge_source,
                filing_date=v.filing_date,
                values=dict(v.values),
                content_hash=v.content_hash,
                vendor_key=v.vendor_key,
            )
        )
    return fu.Book(out)


def test_a_rerun_writes_nothing():
    rows = [sf1(), sf1("MRQ", revenue=D("990"))]
    book = _held_from(rows, as_of=date(2026, 1, 5))
    ops, events, _ = run(rows, book)
    assert ops == []
    assert {e.kind for e in events} == {"unchanged"}


def test_a_split_after_load_writes_nothing():
    """SF1 restates its history in the new terms; recovered, nothing moved."""
    rows = [sf1(), sf1("MRQ", revenue=D("990"))]
    book = _held_from(rows, as_of=date(2026, 1, 5))
    split = [(date(2026, 3, 2), Fraction(3, 2))]
    after = [
        sf1(shareswa=D("1500000"), shareswadil=D("1515000"), sharesbas=D("1500000")),
        sf1("MRQ", revenue=D("990"), shareswa=D("1500000"), shareswadil=D("1515000")),
    ]
    ops, events, _ = run(after, book, splits=split)
    assert ops == []


def test_a_changed_first_reported_row_is_a_vendor_correction():
    book = _held_from([sf1(), sf1("MRQ", revenue=D("990"))], as_of=date(2026, 1, 5))
    ops, events, _ = run([sf1(netinc=D("101"))], book)
    sup, ins = [o for o in ops if o.version.statement == "income"]
    assert (sup.kind, sup.version.version_no) == ("supersede", 1)
    assert (ins.kind, ins.version.version_no) == ("insert", 1)
    # Same knowledge interval: the filing did not change, the vendor's record did.
    assert (ins.version.known_from, ins.version.known_to) == (
        date(2020, 5, 1),
        date(2026, 1, 5),
    )
    (e,) = [e for e in events if e.kind == "corrected"]
    assert e.moved == {"net_income": (D("100.00"), D("101.00"))}


def test_a_corrected_filing_date_moves_known_from():
    book = _held_from([sf1()])
    ops, events, _ = run([sf1(filed=date(2020, 5, 4))], book)
    (ins,) = inserted(ops)
    assert ins.known_from == date(2020, 5, 4)
    assert [e for e in events if e.kind == "corrected"][0].moved == {
        "filing_date": (date(2020, 5, 1), date(2020, 5, 4))
    }


def test_a_correction_past_a_dated_restatement_is_refused():
    book = _held_from([sf1(), sf1("MRQ", revenue=D("990"))], as_of=date(2020, 6, 1))
    ops, events, _ = run([sf1(filed=date(2020, 6, 2))], book)
    assert not inserted(ops)
    assert [e.kind for e in events if e.statement == "income"][0] == "refused"


def test_an_estimated_version_becomes_filed_when_the_filing_appears():
    q1 = sf1("MRQ", date(2020, 3, 31))
    q2 = sf1("ARQ", date(2020, 6, 30), date(2020, 8, 4), "2020-Q2")
    book = _held_from([q1, q2])
    ops, events, _ = run([sf1(), q1, q2], book)
    sup, ins = [o for o in ops if o.version.statement == "income"]
    assert (sup.kind, ins.version.knowledge_source, ins.version.known_from) == (
        "supersede",
        "filed",
        date(2020, 5, 1),
    )


def test_a_later_restatement_closes_the_version_in_force():
    book = _held_from([sf1(), sf1("MRQ", revenue=D("990"))], as_of=date(2026, 1, 5))
    ops, events, _ = run([sf1("MRQ", revenue=D("980"))], book)
    close, ins = ops
    assert (close.kind, close.version.version_no, close.known_to) == ("close", 2, TODAY)
    assert (ins.version.version_no, ins.version.known_from) == (3, TODAY)
    # Back to the first-reported value is a restatement too.
    ops, _, _ = run([sf1("MRQ")], book)
    assert ops[-1].version.values["revenue"] == D("1000.00")


def test_a_period_held_from_another_key_is_left():
    book = _held_from([sf1()])
    for v in book.versions("income", ("quarter", date(2020, 3, 31))).values():
        v.vendor_key = "other"
    ops, events, _ = run([sf1(netinc=D("5"))], book)
    assert not [o for o in ops if o.version.statement == "income"]
    assert "other_key" in {e.kind for e in events}


# ---------------------------------------------------------------------------
# Identity, extra
# ---------------------------------------------------------------------------


def test_a_period_resolves_through_the_link_that_covers_it_else_the_open_one():
    links = fu.Links(
        [
            {
                "security_id": 1,
                "vendor_key": "A",
                "valid_from": date(1998, 1, 2),
                "valid_to": date(2005, 6, 30),
            },
            {
                "security_id": 2,
                "vendor_key": "A",
                "valid_from": date(2010, 1, 4),
                "valid_to": None,
            },
            {
                "security_id": 3,
                "vendor_key": "B",
                "valid_from": date(1999, 1, 4),
                "valid_to": date(2003, 1, 2),
            },
            {
                "security_id": 2,
                "vendor_key": "C",
                "valid_from": date(1995, 1, 3),
                "valid_to": date(2009, 12, 31),
            },
        ]
    )
    assert links.resolve("A", date(2000, 3, 31)) == (1, "covered")
    assert links.resolve("A", date(2012, 3, 31)) == (2, "covered")
    assert links.resolve("A", date(1990, 3, 31)) == (2, "open")
    # Key C's link to security 2 covers 2007: the period is C's, not A's.
    assert links.resolve("A", date(2007, 3, 31)) == (None, "other_key")
    assert links.resolve("C", date(2007, 3, 31)) == (2, "covered")
    assert links.resolve("B", date(2010, 3, 31)) == (None, "unlinked")
    assert links.resolve("Z", date(2010, 3, 31)) == (None, "outside_universe")


def test_extra_is_json_with_the_vendors_numbers_exact():
    text = fu._json(
        {"vendor_metric": {"pe": D("12.345"), "big": D("1E+3"), "neg": D("-0.0")}}
    )
    assert text == '{"vendor_metric":{"pe":12.345,"big":1000,"neg":0.0}}'
    import json

    assert json.loads(text)["vendor_metric"]["pe"] == 12.345
