"""Integration tests for the company summary against a real warehouse.

Needs FAFNIR_TEST_DSN. What is under test here is the SQL half: that resolution
finds the right security by ticker and by name, that the numbers the views
produce are the numbers the report claims, and -- the one most likely to rot --
that a RESOLVED data-quality flag never reaches the summary.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from duk import company_summary as cs
from duk.datasource import db as ds_db
from fafnir.db import repository as repo
from fafnir.ingest import adjustments

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


def _company(db, symbol, name, **kwargs):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code="NASDAQ",
        **kwargs,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _prices(db, sid, start, days, close=100):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": start + dt.timedelta(days=n),
                "open": close,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1000 if n else 0,  # one zero-volume bar, to be counted
            }
            for n in range(days)
        ],
    )


def test_resolves_by_ticker_and_summarises(db):
    sid = _company(db, "SUMM", "Summary Test Inc")
    _prices(db, sid, dt.date(2024, 1, 1), 40)

    candidates = ds_db.resolve_company(dsn=DSN, query="SUMM")
    assert [c["security_id"] for c in candidates] == [sid]

    report = cs.build_summary(ds_db.company_summary(dsn=DSN, security_id=sid))
    assert report["meta"]["symbol"] == "SUMM"
    assert report["meta"]["company_name"] == "Summary Test Inc"
    assert report["prices"]["bar_count"] == 40
    assert report["prices"]["first_trade_date"] == "2024-01-01"
    assert report["prices"]["last_trade_date"] == "2024-02-09"
    assert report["prices"]["zero_volume_bars"] == 1
    assert report["prices"]["last_close"] == 100.0
    # No corporate actions loaded -- the section must say zero, not blow up.
    assert report["actions"]["split_count"] == 0
    assert report["actions"]["dividend_count"] == 0
    assert report["dq"] == []
    assert report["fundamentals"] is None


def test_resolves_by_company_name_case_insensitively(db):
    sid = _company(db, "NAMED", "Distinctive Holdings PLC")
    assert [
        c["security_id"]
        for c in ds_db.resolve_company(dsn=DSN, query="distinctive holdings plc")
    ] == [sid]
    # A substring is enough, as long as it is unambiguous.
    assert [
        c["security_id"] for c in ds_db.resolve_company(dsn=DSN, query="Distinctive")
    ] == [sid]


def test_an_ambiguous_name_returns_every_candidate(db):
    _company(db, "ONE", "Ambiguous Alpha Corp")
    _company(db, "TWO", "Ambiguous Beta Corp")
    candidates = ds_db.resolve_company(dsn=DSN, query="Ambiguous")
    assert {c["symbol"] for c in candidates} == {"ONE", "TWO"}


def test_an_exact_name_match_beats_a_substring_match(db):
    exact = _company(db, "EXACT", "Delta")
    _company(db, "LONGER", "Delta Industries Group")
    # Both match the substring; the exact one must come first, so the CLI's
    # "one candidate" path is not reached but the ordering is still meaningful.
    candidates = ds_db.resolve_company(dsn=DSN, query="Delta")
    assert candidates[0]["security_id"] == exact


def test_a_ticker_never_loses_to_a_name_containing_it(db):
    ticker = _company(db, "CAT", "Caterpillar Inc")
    _company(db, "OTHER", "Cat Grooming Holdings")
    # "CAT" is the ticker of one and a substring of the other's name. The ladder
    # is a precedence, not a search: the ticker wins outright and alone.
    candidates = ds_db.resolve_company(dsn=DSN, query="CAT")
    assert [c["security_id"] for c in candidates] == [ticker]


def test_a_former_ticker_resolves_and_is_reported_as_such(db):
    sid = _company(db, "NEWNAME", "Renamed Corp")
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to, "
        "is_primary) VALUES (%s, 'OLDNAME', '1990-01-01', '2022-05-05', false)",
        (sid,),
    )
    candidates = ds_db.resolve_company(dsn=DSN, query="OLDNAME")
    assert len(candidates) == 1
    assert candidates[0]["security_id"] == sid
    assert candidates[0]["matched_former_symbol"] == "OLDNAME"
    assert candidates[0]["matched_former_valid_to"] == dt.date(2022, 5, 5)

    # Reaching a company through its old ticker must be stated, not silent: the
    # report is otherwise about a company the user did not name.
    profile = dict(candidates[0])
    raw = ds_db.company_summary(dsn=DSN, security_id=sid)
    raw["profile"].update(
        matched_former_symbol=profile["matched_former_symbol"],
        matched_former_valid_to=profile["matched_former_valid_to"],
    )
    assert "Matched a former ticker" in cs.render_text(cs.build_summary(raw))


def test_corporate_actions_and_ttm_dividends(db):
    sid = _company(db, "DIVS", "Dividend Payer Inc")
    _prices(db, sid, dt.date(2024, 1, 1), 10)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2024, 1, 5),
        split_numerator=2,
        split_denominator=1,
    )
    for month, amount in ((3, "0.10"), (6, "0.20"), (9, "0.30")):
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="dividend",
            ex_date=dt.date(2024, month, 1),
            dividend_amount=Decimal(amount),
        )
    # Outside the trailing year measured from the latest ex-date (2024-09-01).
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2022, 1, 1),
        dividend_amount=Decimal("99.00"),
    )
    adjustments.compute_for_security(db, sid)

    report = cs.build_summary(ds_db.company_summary(dsn=DSN, security_id=sid))
    actions = report["actions"]
    assert actions["split_count"] == 1
    assert actions["last_split_numerator"] == 2.0
    assert actions["dividend_count"] == 4
    assert actions["last_dividend_date"] == "2024-09-01"
    # 0.10 + 0.20 + 0.30 -- the 99.00 from two years earlier is out of window.
    assert actions["ttm_dividend_amount"] == pytest.approx(0.60)


def test_resolved_dq_flags_never_reach_the_summary(db):
    sid = _company(db, "FLAGGED", "Flagged Inc")
    _prices(db, sid, dt.date(2024, 1, 1), 5)
    repo.add_dq_flag(
        db,
        check_name="gap",
        severity="warn",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2024-01-03"},
    )
    repo.add_dq_flag(
        db,
        check_name="outlier",
        severity="warn",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"trade_date": "2024-01-04"},
    )
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 'rob', "
        "resolution_note = 'real move, not bad data' WHERE check_name = 'outlier'"
    )

    report = cs.build_summary(ds_db.company_summary(dsn=DSN, security_id=sid))
    assert [g["check_name"] for g in report["dq"]] == ["gap"]
    assert report["dq"][0]["keys"] == ["2024-01-03"]

    # The resolution note is human-written text and must not appear anywhere in
    # the rendered report -- the open-only filter is what keeps it off the seam.
    text = cs.render_text(report)
    assert "real move, not bad data" not in text
    assert "rob" not in text


def test_summary_of_a_security_with_no_prices_at_all(db):
    sid = _company(db, "EMPTY", "No Prices Inc")
    report = cs.build_summary(ds_db.company_summary(dsn=DSN, security_id=sid))
    assert report["prices"]["bar_count"] is None
    assert report["prices"]["last_close"] is None
    assert "No price history loaded" in cs.render_text(report)


def test_unknown_security_id_is_an_error_not_an_empty_report(db):
    with pytest.raises(ds_db.DataSourceError):
        ds_db.company_summary(dsn=DSN, security_id=999_999_999)
