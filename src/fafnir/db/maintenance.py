"""
Routine maintenance helpers: keep price partitions and the trading calendar
extended to a rolling horizon, and refresh the screening materialized view.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

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


#: The materialized views ``fafnir db refresh-marts`` refreshes, in this order.
#: A build that adds one appends it here. The factor panel (0037, #100) reads the
#: statements and prices, not security_latest, so the order is only the log's.
MATVIEWS: tuple[str, ...] = ("mart.security_latest", "mart.fundamental_panel")

#: The SQLSTATEs a CONCURRENTLY refresh raises for a reason a plain refresh
#: answers: the view holds no data yet (0A000, feature_not_supported) or it has no
#: unique index (55000, object_not_in_prerequisite_state). Anything else is a real
#: failure.
CONCURRENT_UNSUPPORTED = frozenset({"0A000", "55000"})


class MartRefreshError(RuntimeError):
    """One or more materialized views failed to refresh. The others refreshed."""

    def __init__(self, failures: list[tuple[str, str]]):
        self.failures = failures
        super().__init__("; ".join(f"{view}: {error}" for view, error in failures))


def _is_populated(db: Database, view: str) -> bool:
    schema, _, name = view.partition(".")
    populated = db.fetchval(
        "SELECT ispopulated FROM pg_matviews WHERE schemaname = %s AND matviewname = %s",
        (schema, name),
    )
    if populated is None:
        raise LookupError(f"no materialized view {view}")
    return bool(populated)


def _refresh(db: Database, view: str, concurrently: bool) -> str:
    """Refresh one view in its own transaction; the mode it took.

    CONCURRENTLY (readers keep the old rows meanwhile) only where the view holds
    data. If PostgreSQL still refuses it for one of CONCURRENT_UNSUPPORTED's
    reasons, the attempt is rolled back to a savepoint and the view refreshed
    plainly. Any other error propagates: before #96 a bare ``except`` turned every
    failure into a second, plain attempt, so a broken view reported success, and
    outside autocommit the second attempt met an aborted transaction.
    """
    import psycopg

    with db.conn.transaction():
        if concurrently and _is_populated(db, view):
            try:
                with db.conn.transaction():
                    db.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                return "concurrently"
            except psycopg.Error as exc:
                if exc.sqlstate not in CONCURRENT_UNSUPPORTED:
                    raise
                logger.warning(
                    "%s: CONCURRENTLY refused (%s); refreshing plainly",
                    view,
                    exc.sqlstate,
                )
        db.execute(f"REFRESH MATERIALIZED VIEW {view}")
        return "plain"


def refresh_marts(
    db: Database, concurrently: bool = True, views: Optional[Sequence[str]] = None
) -> list[tuple[str, str]]:
    """Refresh the derived materialized views, :data:`MATVIEWS` unless ``views``
    names others, in order: ``(view, mode)`` for each.

    Each view is refreshed and committed on its own, so one that fails neither
    undoes nor stops the rest. If any failed, :class:`MartRefreshError` names them
    all once the others are done.
    """
    done: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    for view in MATVIEWS if views is None else views:
        try:
            mode = _refresh(db, view, concurrently)
        except Exception as exc:  # noqa: BLE001 -- reported below, never swallowed
            logger.error("Refreshing %s failed: %s", view, exc)
            failures.append((view, f"{type(exc).__name__}: {exc}"))
            continue
        db.commit()
        logger.info("Refreshed %s (%s)", view, mode)
        done.append((view, mode))
    if failures:
        raise MartRefreshError(failures)
    return done
