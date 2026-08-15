"""
Ingestion run-log: open an ``ops.ingestion_run`` row at the start of a load and
close it with final counts and status. Every load gets a lineage record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("runlog")


@dataclass
class RunLog:
    db: Database
    source: str
    endpoint: str
    params: dict = field(default_factory=dict)
    window_from: Optional[date] = None
    window_to: Optional[date] = None
    run_id: Optional[int] = None
    rows_inserted: int = 0
    rows_quarantined: int = 0
    symbols_requested: int = 0
    bytes_downloaded: int = 0

    def __enter__(self) -> "RunLog":
        row = self.db.fetchone(
            """
            INSERT INTO ops.ingestion_run
                (source, endpoint, params, window_from, window_to, status, started_at)
            VALUES (%s,%s,%s,%s,%s,'started', now())
            RETURNING ingestion_run_id
            """,
            (
                self.source,
                self.endpoint,
                json.dumps(self.params, default=str),
                self.window_from,
                self.window_to,
            ),
        )
        self.run_id = int(row["ingestion_run_id"])
        # Commit the open row immediately: until it is durable no other session
        # can see the run, so nothing can monitor a backfill in flight, and a
        # crash would erase the evidence that it ever started.
        self.db.commit()
        logger.info(
            "ingestion_run %s started: %s/%s", self.run_id, self.source, self.endpoint
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        status = "success"
        error_message = None
        if exc_type is not None:
            status = "failed"
            error_message = f"{exc_type.__name__}: {exc}"
            # The failing statement may have aborted the transaction, which would
            # reject the UPDATE below with InFailedSqlTransaction. Discard only
            # the uncommitted tail -- everything committed at a unit boundary
            # stays -- so the run can still be marked failed.
            self.db.rollback()
        elif self.rows_quarantined > 0:
            status = "partial"
        self.db.execute(
            """
            UPDATE ops.ingestion_run SET
                rows_inserted = %s, rows_quarantined = %s, symbols_requested = %s,
                bytes_downloaded = %s, status = %s, error_message = %s, finished_at = now()
            WHERE ingestion_run_id = %s
            """,
            (
                self.rows_inserted,
                self.rows_quarantined,
                self.symbols_requested,
                self.bytes_downloaded,
                status,
                error_message,
                self.run_id,
            ),
        )
        # Make the outcome durable here rather than leaving it to the caller: on
        # the failure path Database.__exit__ rolls back, which would otherwise
        # discard the very record that says the run failed.
        self.db.commit()
        level = logger.error if status == "failed" else logger.info
        level(
            "ingestion_run %s %s: inserted=%d quarantined=%d bytes=%d",
            self.run_id,
            status,
            self.rows_inserted,
            self.rows_quarantined,
            self.bytes_downloaded,
        )
        return False  # never suppress exceptions
