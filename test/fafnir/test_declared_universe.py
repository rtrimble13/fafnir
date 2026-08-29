"""
Integration tests for the declared universe (ADR 0006; needs FAFNIR_TEST_DSN).

The screened universe cannot reach a mutual fund -- it has no listing venue -- so
``ref.tracked_symbol`` declares one and ``ingest tracked`` mints it. What has to be
true, and is SQL-shaped enough to need a real database:

  * a declaration becomes exactly ONE security, and stays one across re-runs;
  * a rename underneath a declaration is followed, not forked;
  * a fund's NAV history loads, and a distribution back-adjusts through the
    ordinary dividend factor -- no fund-specific arithmetic anywhere;
  * the nightly equity upkeep leaves funds alone.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import adjustments, tracked
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

FUND = "VFIAX"


class _FakeFMP:
    """FMP stand-in: profiles, NAV bars, and distributions. No network."""

    bytes_downloaded = 0

    def __init__(self, *, profiles=None, bars=None, dividends=None, splits=None):
        self._profiles = profiles or {}
        self._bars = bars or []
        self._dividends = dividends or []
        self._splits = splits or []
        self.profile_calls: list[str] = []

    def profile(self, symbol):
        self.profile_calls.append(symbol)
        return self._profiles.get(symbol)

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return self._bars

    def dividends(self, symbol):
        return self._dividends

    def splits(self, symbol):
        return self._splits


def _fund_profile(symbol=FUND, name="Vanguard 500 Index Fund Admiral Shares"):
    # Note what this deliberately does NOT carry: an exchange, and any hint that it
    # is a fund. FMP reports many funds as plain equities, which is exactly why the
    # declaration is authoritative over the profile.
    return {symbol: {"companyName": name, "currency": "USD", "country": "US"}}


def _nav_bar(day: str, nav: float) -> dict:
    """The shape FMP returns for a NAV-priced security: a close and nothing else."""
    return {"date": day, "close": nav}


def _declare(db, symbol=FUND, **kwargs):
    kwargs.setdefault("asset_type", "fund")
    kwargs.setdefault("exchange_code", tracked.FUND_EXCHANGE)
    added = repo.upsert_tracked_symbol(db, symbol=symbol, **kwargs)
    db.commit()
    return added


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_declaring_a_symbol_is_idempotent_and_preserves_added_at(db):
    assert _declare(db, note="core sleeve") is True
    first = repo.list_tracked_symbols(db)[0]

    # Re-declaring is not an error and must not reset the "since when".
    assert _declare(db) is False
    again = repo.list_tracked_symbols(db)[0]
    assert again["added_at"] == first["added_at"]
    # ... nor erase the reason the row exists.
    assert again["note"] == "core sleeve"


def test_untracking_keeps_the_row_and_the_security(db):
    _declare(db)
    fmp = _FakeFMP(profiles=_fund_profile())
    tracked.load_tracked(db, fmp)
    sec_id = repo.resolve_security_id(db, FUND)
    assert sec_id is not None

    assert repo.untrack_symbol(db, symbol=FUND) is True
    db.commit()
    assert repo.untrack_symbol(db, symbol=FUND) is False, "must be idempotent"

    # Gone from the working list, still on the record, and the security untouched.
    assert repo.list_tracked_symbols(db, tracked_only=True) == []
    row = repo.list_tracked_symbols(db, tracked_only=False)[0]
    assert row["is_tracked"] is False and row["untracked_at"] is not None
    assert repo.resolve_security_id(db, FUND) == sec_id


def test_untracked_symbols_are_not_loaded(db):
    _declare(db)
    repo.untrack_symbol(db, symbol=FUND)
    db.commit()
    fmp = _FakeFMP(profiles=_fund_profile())
    result = tracked.load_tracked(db, fmp)
    assert result.total == 0
    assert fmp.profile_calls == [], "an untracked symbol must cost no request"


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def test_a_declaration_mints_one_fund_security(db):
    _declare(db, note="core sleeve")
    result = tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))

    assert result.total == 1
    assert result.minted == [FUND]
    row = db.fetchone(
        "SELECT security_id, asset_type, is_fund, exchange_code, company_name "
        "FROM core.security WHERE primary_symbol = %s",
        (FUND,),
    )
    # The declaration wins over the profile, which said nothing about being a fund.
    assert row["asset_type"] == "fund"
    assert row["is_fund"] is True
    assert row["exchange_code"] == tracked.FUND_EXCHANGE
    assert repo.resolve_security_id(db, FUND) == row["security_id"]


def test_reloading_a_declaration_refreshes_rather_than_forking(db):
    _declare(db)
    fmp = _FakeFMP(profiles=_fund_profile())
    first = tracked.load_tracked(db, fmp)
    second = tracked.load_tracked(db, fmp)

    assert first.minted == [FUND] and second.minted == []
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    assert (
        db.fetchval("SELECT count(*) FROM core.symbol_xref WHERE symbol = %s", (FUND,))
        == 1
    )


def test_a_symbol_unknown_to_the_source_is_flagged_not_fatal(db):
    _declare(db, symbol="NOSUCHX")
    _declare(db)
    result = tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))

    # The good declaration still loaded; the bad one is in the review queue.
    assert result.total == 1 and result.missing == ["NOSUCHX"]
    assert repo.resolve_security_id(db, FUND) is not None
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name = 'tracked_symbol_unknown_to_source' "
            "AND resolved_at IS NULL"
        )
        == 1
    )


def test_a_repeat_run_does_not_re_flag_the_same_unknown_symbol(db):
    """Nightly. One standing problem must stay one row in the queue."""
    _declare(db, symbol="NOSUCHX")
    fmp = _FakeFMP()
    tracked.load_tracked(db, fmp)
    tracked.load_tracked(db, fmp)
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name = 'tracked_symbol_unknown_to_source'"
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Renames: the failure that would otherwise be silent
# ---------------------------------------------------------------------------


def test_a_rename_under_a_declaration_is_followed_not_forked(db):
    """`ingest symbol-changes` can rename the security a declaration names.

    ref.tracked_symbol goes on naming the old ticker forever, so upserting on it
    would mint a SECOND security_id and strand the fund's bars, watermark and
    actions on the first -- the fork ADR 0005 exists to prevent, except the
    screener cannot rescue this one.
    """
    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))
    sec_id = repo.resolve_security_id(db, FUND)

    # The fund converts to a new share-class ticker.
    repo.retarget_symbol(
        db,
        security_id=sec_id,
        old_symbol=FUND,
        new_symbol="VFIAY",
        change_date=dt.date(2026, 6, 10),
    )
    db.commit()

    result = tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile("VFIAY")))

    assert result.renamed == [(FUND, "VFIAY")]
    assert db.fetchval("SELECT count(*) FROM core.security") == 1, "forked identity"
    assert repo.resolve_security_id(db, "VFIAY") == sec_id
    # The declaration moved with it, so tomorrow's run asks for the right ticker.
    assert [r["symbol"] for r in repo.list_tracked_symbols(db)] == ["VFIAY"]


def test_a_reused_ticker_mints_a_fresh_security(db):
    """A delisted issuer's ticker must never be resurrected by a declaration."""
    dead = repo.upsert_security(
        db, primary_symbol=FUND, company_name="Dead Fund", asset_type="fund"
    )
    repo.upsert_symbol_xref(db, security_id=dead, symbol=FUND)
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2020, 1, 1))
    db.commit()

    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))

    live = db.fetchval(
        "SELECT security_id FROM core.security "
        "WHERE primary_symbol = %s AND delisted_date IS NULL",
        (FUND,),
    )
    assert live is not None and live != dead


# ---------------------------------------------------------------------------
# Prices and adjustment: no fund-specific arithmetic anywhere
# ---------------------------------------------------------------------------


def _load_nav_history(db, bars):
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT) as run:
        written = load_symbol_prices(db, _FakeFMP(bars=bars), FUND, run=run)
    db.commit()
    return written


def test_nav_history_loads_for_a_fund_and_not_for_an_equity(db):
    """One payload, two verdicts -- decided by the security, not the payload."""
    bars = [_nav_bar("2026-06-01", 400.0), _nav_bar("2026-06-02", 402.0)]

    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))
    assert _load_nav_history(db, bars) == 2

    row = db.fetchone(
        "SELECT open, high, low, close, volume FROM core.daily_price p "
        "JOIN core.security s USING (security_id) "
        "WHERE s.primary_symbol = %s AND trade_date = %s",
        (FUND, dt.date(2026, 6, 2)),
    )
    assert row["open"] == row["high"] == row["low"] == row["close"]
    assert row["close"] == Decimal("402.000000")
    assert row["volume"] == 0

    # The same bars against an equity quarantine, every one of them.
    equity = repo.upsert_security(
        db, primary_symbol="AAA", company_name="Test", asset_type="equity"
    )
    repo.upsert_symbol_xref(db, security_id=equity, symbol="AAA")
    db.commit()
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT) as run:
        assert load_symbol_prices(db, _FakeFMP(bars=bars), "AAA", run=run) == 0
    db.commit()
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE security_id = %s AND check_name = "
            "'price_missing_or_nonnumeric_ohlc'",
            (equity,),
        )
        == 2
    )


def test_a_distribution_back_adjusts_nav_through_the_ordinary_dividend_factor(db):
    """The whole point of loading funds into core.daily_price.

    A capital-gain distribution drops NAV by the distributed amount, which is
    arithmetically a cash dividend. If the adjusted view returns
    ``nav x (nav - D) / nav`` for the day before the ex-date, then nothing in the
    adjustment path needed a fund-specific case.
    """
    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))
    sec_id = repo.resolve_security_id(db, FUND)

    ex_date = dt.date(2026, 6, 2)
    nav_before = Decimal("400.000000")
    distribution = Decimal("20.000000")
    _load_nav_history(
        db,
        [
            _nav_bar("2026-06-01", float(nav_before)),
            _nav_bar("2026-06-02", float(nav_before - distribution)),
        ],
    )
    repo.upsert_corporate_action(
        db,
        security_id=sec_id,
        action_type="dividend",
        ex_date=ex_date,
        dividend_amount=float(distribution),
    )
    db.commit()
    adjustments.adjust_all(db)
    db.commit()

    adjusted = db.fetchone(
        "SELECT close FROM mart.v_daily_price_adjusted "
        "WHERE security_id = %s AND trade_date = %s",
        (sec_id, dt.date(2026, 6, 1)),
    )
    expected = (nav_before * (nav_before - distribution) / nav_before).quantize(
        Decimal("0.000001")
    )
    assert adjusted["close"] == expected

    # On/after the ex-date the factor is 1.0: the NAV as struck.
    on_ex = db.fetchone(
        "SELECT close FROM mart.v_daily_price_adjusted "
        "WHERE security_id = %s AND trade_date = %s",
        (sec_id, ex_date),
    )
    assert on_ex["close"] == nav_before - distribution


# ---------------------------------------------------------------------------
# Isolation from the equity upkeep
# ---------------------------------------------------------------------------


def test_the_delisting_sweep_cannot_reach_a_fund(db):
    """MUTF is deliberately not one of SCREENER_EXCHANGES.

    A fund's ticker appearing in the equity delisted feed must not retire it: the
    feed has never heard of the fund, and a wrong delisting is one-way.
    """
    from fafnir.ingest import delisted

    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))
    sec_id = repo.resolve_security_id(db, FUND)

    class _DelistedFMP:
        bytes_downloaded = 0

        def delisted_companies(self, *, max_pages=5):
            return [
                {
                    "symbol": FUND,
                    "exchangeShortName": tracked.FUND_EXCHANGE,
                    "delistedDate": "2026-06-05",
                }
            ]

    marked, seen = delisted.load_delisted(db, _DelistedFMP())
    db.commit()

    assert (marked, seen) == (0, 0)
    row = db.fetchone(
        "SELECT is_actively_trading, delisted_date FROM core.security "
        "WHERE security_id = %s",
        (sec_id,),
    )
    assert row["is_actively_trading"] is True and row["delisted_date"] is None


def test_retiring_a_fund_keeps_its_history(db):
    """`track rm --closed` is an ordinary delisting: retained, not deleted."""
    _declare(db)
    tracked.load_tracked(db, _FakeFMP(profiles=_fund_profile()))
    _load_nav_history(db, [_nav_bar("2026-06-01", 400.0)])
    sec_id = repo.resolve_security_id(db, FUND)

    repo.untrack_symbol(db, symbol=FUND)
    assert (
        repo.mark_delisted(db, security_id=sec_id, delisted_date=dt.date(2026, 6, 30))
        is True
    )
    db.commit()

    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s", (sec_id,)
        )
        == 1
    )
    assert repo.resolve_security_id(db, FUND) == sec_id, "still addressable"
    assert db.fetchval(
        "SELECT delisted_date FROM core.security WHERE security_id = %s", (sec_id,)
    ) == dt.date(2026, 6, 30)


# ---------------------------------------------------------------------------
# Freshness: a NAV published in the evening is not a stale price
# ---------------------------------------------------------------------------


def _trading_days(db, count, *, through):
    """The last ``count`` open sessions on or before ``through``, ascending."""
    rows = db.fetchall(
        "SELECT trade_date FROM ref.trading_calendar "
        "WHERE exchange_code = 'NASDAQ' AND is_open AND trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT %s",
        (through, count),
    )
    return sorted(r["trade_date"] for r in rows)


def _price_on(db, sec_id, day, close=100):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sec_id,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 0,
            }
        ],
    )
    db.commit()


def test_a_fund_one_day_behind_the_market_is_not_stale(db):
    """NAV is struck at 4pm ET and posted that evening, after equity EOD.

    Without the allowance every fund is flagged every night, on a record_key (its
    own last_date) that changes daily -- so add_dq_flag_once cannot dedupe it and
    the queue grows without bound. That is the exact failure the once-per-occurrence
    rule exists to prevent, reintroduced by a price that is merely published later.
    """
    from fafnir.dq import checks

    # The calendar the test fixture seeds covers 2023-2024.
    days = _trading_days(db, 3, through=dt.date(2024, 12, 31))
    assert len(days) == 3, "the seeded trading calendar is needed for this test"
    older, yesterday, today = days

    equity = repo.upsert_security(
        db, primary_symbol="AAA", company_name="Test", asset_type="equity"
    )
    fund = repo.upsert_security(
        db, primary_symbol=FUND, company_name="Fund", asset_type="fund"
    )
    db.commit()
    _price_on(db, equity, today)  # the market's latest date
    _price_on(db, fund, yesterday)  # one session behind: normal for a NAV

    assert checks.check_freshness(db) == 0

    # Two sessions behind is past the allowance, and is a real problem.
    _price_on(db, fund, older)
    db.execute(
        "DELETE FROM core.daily_price WHERE security_id = %s AND trade_date = %s",
        (fund, yesterday),
    )
    db.commit()
    assert checks.check_freshness(db) == 1
    flagged = db.fetchval(
        "SELECT security_id FROM ops.data_quality_flag WHERE check_name = 'stale'"
    )
    assert flagged == fund


def test_an_equity_one_day_behind_is_still_stale(db):
    """The allowance is gated on asset_type, not granted to everything."""
    from fafnir.dq import checks

    days = _trading_days(db, 2, through=dt.date(2024, 12, 31))
    yesterday, today = days

    leader = repo.upsert_security(
        db, primary_symbol="AAA", company_name="Test", asset_type="equity"
    )
    laggard = repo.upsert_security(
        db, primary_symbol="BBB", company_name="Test", asset_type="equity"
    )
    db.commit()
    _price_on(db, leader, today)
    _price_on(db, laggard, yesterday)

    assert checks.check_freshness(db) == 1
