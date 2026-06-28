"""Tests for rolling-horizon maintenance (partitions + calendar)."""

from __future__ import annotations

import datetime as dt

import pytest

from fafnir.db import maintenance


def test_current_horizon_year():
    this_year = dt.date.today().year
    assert maintenance.current_horizon_year(0) == this_year
    assert maintenance.current_horizon_year(2) == this_year + 2


@pytest.mark.integration
def test_ensure_horizon_extends_partitions_and_calendar(db):
    # Far-future years with no partitions yet; repeatable via DROP.
    for y in (2033, 2034):
        db.execute(f"DROP TABLE IF EXISTS core.daily_price_y{y}")

    created, _ = maintenance.ensure_horizon(db, through_year=2034, floor_year=2033)
    assert created == 2
    for y in (2033, 2034):
        assert maintenance._table_exists(db, f"daily_price_y{y}")

    # The trading calendar was extended through 2034 (~250 trading days).
    n2034 = db.fetchval(
        "SELECT count(*) FROM ref.trading_calendar "
        "WHERE extract(year FROM trade_date) = 2034"
    )
    assert n2034 > 200

    # Idempotent: a second run creates nothing new.
    created2, cal2 = maintenance.ensure_horizon(db, through_year=2034, floor_year=2033)
    assert created2 == 0
    assert cal2 == 0

    for y in (2033, 2034):
        db.execute(f"DROP TABLE IF EXISTS core.daily_price_y{y}")
