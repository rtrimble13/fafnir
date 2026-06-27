"""
Routine maintenance helpers: create future yearly price partitions and refresh
the screening materialized view.
"""

from __future__ import annotations

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("maintenance")


def ensure_year_partition(db: Database, year: int) -> bool:
    """Create core.daily_price_y<year> if absent. Returns True if created."""
    name = f"daily_price_y{year}"
    exists = db.fetchval(
        """
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'core' AND c.relname = %s
        """,
        (name,),
    )
    if exists:
        return False
    db.execute(f"""
        CREATE TABLE core.{name} PARTITION OF core.daily_price
        FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
        """)
    logger.info("Created partition core.%s", name)
    return True


def ensure_partitions(db: Database, start_year: int, end_year: int) -> int:
    created = 0
    for year in range(start_year, end_year + 1):
        if ensure_year_partition(db, year):
            created += 1
    return created


def refresh_marts(db: Database, concurrently: bool = True) -> None:
    """Refresh derived materialized views (security_latest)."""
    mode = "CONCURRENTLY" if concurrently else ""
    try:
        db.execute(f"REFRESH MATERIALIZED VIEW {mode} mart.security_latest")
    except Exception:
        # CONCURRENTLY requires a prior non-concurrent populate; fall back.
        db.execute("REFRESH MATERIALIZED VIEW mart.security_latest")
    logger.info("Refreshed mart.security_latest")
