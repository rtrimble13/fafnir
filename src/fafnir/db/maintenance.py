"""
Routine maintenance helpers: keep price partitions and the trading calendar
extended to a rolling horizon, and refresh the screening materialized view.
"""

from __future__ import annotations

import datetime as dt

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("maintenance")

# Default number of years the rolling horizon stays ahead of the current year.
HORIZON_EXTRA_YEARS = 2


def current_horizon_year(extra_years: int = HORIZON_EXTRA_YEARS) -> int:
    """Target horizon = this calendar year + ``extra_years``."""
    return dt.date.today().year + extra_years


def _table_exists(db: Database, name: str) -> bool:
    return bool(
        db.fetchval(
            """
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'core' AND c.relname = %s
            """,
            (name,),
        )
    )


def ensure_year_partition(db: Database, year: int) -> bool:
    """Create core.daily_price_y<year> if absent. Returns True if created.

    Robust against the DEFAULT partition: Postgres refuses to attach a new range
    partition while rows that belong in that range sit in the default partition.
    So if a default partition exists, we detach it, create the year partition,
    relocate any stray rows for that year out of the (now standalone) default
    table into the new partition, then reattach the default. This keeps the
    catch-all safety net while making partition creation always succeed.

    `year` is an int from a controlled range loop, so the f-string DDL is safe.
    """
    name = f"daily_price_y{year}"
    if _table_exists(db, name):
        return False

    lo, hi = f"{year}-01-01", f"{year + 1}-01-01"
    has_default = _table_exists(db, "daily_price_default")

    if has_default:
        db.execute(
            "ALTER TABLE core.daily_price DETACH PARTITION core.daily_price_default"
        )
    try:
        db.execute(
            f"CREATE TABLE core.{name} PARTITION OF core.daily_price "
            f"FOR VALUES FROM ('{lo}') TO ('{hi}')"
        )
        if has_default:
            # Move any rows for this year out of the detached default into the
            # new partition so the default can be reattached without conflict.
            moved = db.execute(
                f"WITH moved AS ("
                f"  DELETE FROM core.daily_price_default "
                f"  WHERE trade_date >= %s AND trade_date < %s RETURNING *"
                f") INSERT INTO core.{name} SELECT * FROM moved",
                (lo, hi),
            )
            if moved:
                logger.info("Relocated %d rows from default into core.%s", moved, name)
    finally:
        if has_default:
            db.execute(
                "ALTER TABLE core.daily_price "
                "ATTACH PARTITION core.daily_price_default DEFAULT"
            )
    logger.info("Created partition core.%s", name)
    return True


def ensure_partitions(db: Database, start_year: int, end_year: int) -> int:
    created = 0
    for year in range(start_year, end_year + 1):
        if ensure_year_partition(db, year):
            created += 1
    return created


def _max_calendar_year(db: Database) -> int | None:
    val = db.fetchval(
        "SELECT max(extract(year FROM trade_date))::int FROM ref.trading_calendar"
    )
    return int(val) if val is not None else None


def ensure_horizon(
    db: Database, *, through_year: int, floor_year: int
) -> tuple[int, int]:
    """Extend partitions and the trading calendar out to ``through_year``.

    Idempotent and cheap to run nightly: existing yearly partitions are skipped,
    and only the missing tail of the calendar is generated. ``floor_year`` is the
    earliest year to guarantee a partition for (the backfill start). Returns
    ``(partitions_created, calendar_rows_added)``.
    """
    created = ensure_partitions(db, floor_year, through_year)

    # Extend only the calendar tail (years not already present).
    from fafnir.db.seed import seed_calendar

    max_cal = _max_calendar_year(db)
    cal_start = (max_cal + 1) if max_cal is not None else floor_year
    cal_rows = 0
    if cal_start <= through_year:
        cal_rows = seed_calendar(db, cal_start, through_year)
    logger.info(
        "Horizon ensured through %d: %d partition(s), %d calendar rows",
        through_year,
        created,
        cal_rows,
    )
    return created, cal_rows


def refresh_marts(db: Database, concurrently: bool = True) -> None:
    """Refresh derived materialized views (security_latest)."""
    mode = "CONCURRENTLY" if concurrently else ""
    try:
        db.execute(f"REFRESH MATERIALIZED VIEW {mode} mart.security_latest")
    except Exception:
        # CONCURRENTLY requires a prior non-concurrent populate; fall back.
        db.execute("REFRESH MATERIALIZED VIEW mart.security_latest")
    logger.info("Refreshed mart.security_latest")
