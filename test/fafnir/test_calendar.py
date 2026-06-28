"""Unit tests for the US trading-calendar generator."""

from __future__ import annotations

import datetime as dt

from fafnir.db.seed import trading_days, us_market_holidays


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
