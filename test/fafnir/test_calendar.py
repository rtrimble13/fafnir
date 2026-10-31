"""Unit tests for the US trading-calendar generator."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from fafnir.db.seed import (
    AD_HOC_CLOSURES,
    MLK_DAY_FIRST_CLOSED,
    trading_days,
    us_market_holidays,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"


def _migration_dates(name: str) -> set[dt.date]:
    sql = (MIGRATIONS / name).read_text()
    return {
        dt.date.fromisoformat(m) for m in re.findall(r"DATE '(\d{4}-\d{2}-\d{2})'", sql)
    }


def test_known_holidays_2023():
    h = us_market_holidays(2023)
    assert dt.date(2023, 1, 2) in h  # New Year's (Jan 1 Sun -> observed Mon)
    assert dt.date(2023, 4, 7) in h  # Good Friday
    assert dt.date(2023, 5, 29) in h  # Memorial Day (last Mon May)
    assert dt.date(2023, 6, 19) in h  # Juneteenth
    assert dt.date(2023, 7, 4) in h  # Independence Day
    assert dt.date(2023, 11, 23) in h  # Thanksgiving (4th Thu)
    assert dt.date(2023, 12, 25) in h  # Christmas


def test_juneteenth_only_from_2022():
    assert dt.date(2021, 6, 18) not in us_market_holidays(2021)
    assert dt.date(2022, 6, 20) in us_market_holidays(2022)  # Jun 19 Sun -> Mon


def test_trading_days_exclude_weekends_and_holidays():
    days = set(trading_days(2023, 2023))
    assert dt.date(2023, 7, 4) not in days  # holiday
    assert dt.date(2023, 7, 1) not in days  # Saturday
    assert dt.date(2023, 7, 3) in days  # Monday, open
    assert dt.date(2023, 7, 5) in days  # Wednesday, open
    # 2023 had 250 NYSE trading days.
    assert len(days) == 250


# ---------------------------------------------------------------------------
# Ad-hoc closures (0022)
# ---------------------------------------------------------------------------


def test_ad_hoc_closures_are_not_trading_days():
    """The generator knows recurring rules; these eleven days follow none of them.

    Every one is a weekday that is not a listed holiday, so without the exclusion
    the generator emits it as an open session, check_gaps finds no bar for any
    security on it, and the whole universe is flagged for a day nothing was wrong.
    """
    for closure in AD_HOC_CLOSURES:
        assert closure.weekday() < 5, f"{closure} is a weekend; it needs no exclusion"
        assert closure not in us_market_holidays(closure.year), (
            f"{closure} is already a recurring holiday -- it does not belong in "
            "AD_HOC_CLOSURES"
        )
        assert closure not in set(trading_days(closure.year, closure.year))


def test_september_2001_closure_spans_four_sessions():
    """The longest US market closure since 1933, and the shape is easy to get wrong.

    The attacks were Tuesday the 11th; trading resumed Monday the 17th. The 15th
    and 16th were a weekend and were never sessions to begin with.
    """
    days = set(trading_days(2001, 2001))
    for d in (11, 12, 13, 14):
        assert dt.date(2001, 9, d) not in days
    assert dt.date(2001, 9, 10) in days
    assert dt.date(2001, 9, 17) in days


def test_ad_hoc_closures_match_the_migration():
    """0022 corrects deployed rows; AD_HOC_CLOSURES governs generated ones.

    A migration is frozen and must not change meaning when the constant is next
    edited, so the list is deliberately written twice. This is what stops the two
    from drifting apart -- add a closure to one and this fails until it is in both.
    """
    assert _migration_dates("0022_ad_hoc_market_closures.up.sql") == set(
        AD_HOC_CLOSURES
    )


# ---------------------------------------------------------------------------
# MLK Day before 1998 (0041)
# ---------------------------------------------------------------------------


def _mlk(year: int) -> dt.date:
    third_monday = 15 + (0 - dt.date(year, 1, 15).weekday()) % 7
    return dt.date(year, 1, third_monday)


def test_mlk_day_is_a_session_until_nyse_first_closed_for_it():
    """NYSE traded through MLK Day until 1998: the warehouse holds 1,247-2,511 bars
    on each of 1990-1997's, and 2 on 1998-01-19."""
    assert MLK_DAY_FIRST_CLOSED == 1998
    for year in range(1990, 1998):
        assert _mlk(year) not in us_market_holidays(year)
        assert _mlk(year) in set(trading_days(year, year))
    for year in (1998, 1999, 2026):
        assert _mlk(year) in us_market_holidays(year)
        assert _mlk(year) not in set(trading_days(year, year))
    assert _mlk(1997) == dt.date(1997, 1, 20)
    assert _mlk(1998) == dt.date(1998, 1, 19)


def test_mlk_sessions_match_the_migration():
    """0041 adds the sessions to calendars already generated; the seed's rule
    governs new ones. As with 0022, the dates are written out in the migration, so
    this keeps the two in step."""
    seeded = {
        _mlk(year)
        for year in range(1990, MLK_DAY_FIRST_CLOSED)
        if _mlk(year) in set(trading_days(year, year))
    }
    assert _migration_dates("0041_mlk_day_before_1998.up.sql") == seeded
    assert _migration_dates("0041_mlk_day_before_1998.down.sql") == seeded
