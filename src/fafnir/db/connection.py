"""
Thin connection / query helpers over psycopg 3.

Every query is parameterized -- string-concatenated SQL is never used. The
:class:`Database` wrapper exposes a small surface (execute, fetch, executemany,
copy-based bulk upsert) so loaders stay declarative and consistent.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from fafnir.logging_config import get_logger

logger = get_logger("db")


def connect(dsn: str, autocommit: bool = False) -> psycopg.Connection:
    """Open a psycopg connection. Caller owns the lifecycle."""
    conn = psycopg.connect(dsn, autocommit=autocommit)
    return conn


class Database:
    """A small wrapper around a psycopg connection with parameterized helpers."""

    def __init__(self, dsn: str, autocommit: bool = False):
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None
        self._autocommit = autocommit

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "Database":
        self._conn = connect(self.dsn, autocommit=self._autocommit)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            if exc_type is None and not self._autocommit:
                self._conn.commit()
            elif not self._autocommit:
                self._conn.rollback()
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise RuntimeError("Database used outside of a context manager")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Explicit transaction block; commits on success, rolls back on error."""
        with self.conn.transaction():
            yield self.conn

    # -- queries ------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute a statement; return affected row count."""
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self.conn.cursor() as cur:
            cur.executemany(sql, list(rows))
            return cur.rowcount

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> Optional[dict]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetchval(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    def execute_script(self, sql_text: str) -> None:
        """Execute a multi-statement SQL script (migrations/seeds)."""
        with self.conn.cursor() as cur:
            cur.execute(sql_text)
