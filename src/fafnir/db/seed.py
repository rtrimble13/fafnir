"""
Seeding: apply static SQL seed files, then generate the US trading calendar.

The calendar is computed (weekdays minus NYSE holidays with observance rules)
rather than shipped as a static table so it extends to any year range via config.
Good Friday is derived from Easter (Anonymous Gregorian algorithm). Juneteenth is
included from 2022 onward, matching the NYSE schedule.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable

from fafnir.db.connection import Database
from fafnir.db.migrate import find_sql_dir
from fafnir.logging_config import get_logger

logger = get_logger("seed")


def _easter(year: int) -> dt.date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """nth weekday (Mon=0) of a month; n negative counts from the end."""
    if n > 0:
        d = dt.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + dt.timedelta(days=offset + 7 * (n - 1))
    # last weekday of month
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    d = nxt - dt.timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - dt.timedelta(days=offset)


def _observed(d: dt.date) -> dt.date:
    """NYSE observance: Saturday holiday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:  # Saturday
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + dt.timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[dt.date]:
    holidays = {
        _observed(dt.date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday (3rd Mon Feb)
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day (last Mon May)
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving (4th Thu Nov)
        _observed(dt.date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(dt.date(year, 6, 19)))  # Juneteenth
    return holidays


def trading_days(start_year: int, end_year: int) -> Iterable[dt.date]:
    for year in range(start_year, end_year + 1):
        holidays = us_market_holidays(year)
        d = dt.date(year, 1, 1)
        end = dt.date(year, 12, 31)
        while d <= end:
            if d.weekday() < 5 and d not in holidays:
                yield d
            d += dt.timedelta(days=1)


def seed_calendar(db: Database, start_year: int, end_year: int) -> int:
    """Populate ref.trading_calendar (open days) for all seeded exchanges."""
    exchanges = [
        r["exchange_code"]
        for r in db.fetchall(
            "SELECT exchange_code FROM ref.exchange WHERE country = 'US'"
        )
    ]
    if not exchanges:
        exchanges = ["NASDAQ", "NYSE", "AMEX"]
    count = 0
    days = list(trading_days(start_year, end_year))
    for ex in exchanges:
        params = [(ex, d) for d in days]
        count += db.executemany(
            """
            INSERT INTO ref.trading_calendar (exchange_code, trade_date, is_open)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (exchange_code, trade_date) DO NOTHING
            """,
            params,
        )
    logger.info(
        "Seeded trading calendar %d-%d for %d exchanges (%d open days each)",
        start_year,
        end_year,
        len(exchanges),
        len(days),
    )
    return count


def apply_sql_seeds(db: Database, sql_dir: Path | None = None) -> list[str]:
    sql_dir = sql_dir or find_sql_dir()
    seed_dir = sql_dir / "seeds"
    applied: list[str] = []
    if not seed_dir.is_dir():
        return applied
    for path in sorted(seed_dir.glob("*.sql")):
        logger.info("Applying seed %s", path.name)
        db.execute_script(path.read_text())
        applied.append(path.name)
    return applied


def seed(
    db: Database, start_year: int, end_year: int, sql_dir: Path | None = None
) -> dict:
    applied = apply_sql_seeds(db, sql_dir)
    cal = seed_calendar(db, start_year, end_year)
    return {"sql_seeds": applied, "calendar_rows": cal}
