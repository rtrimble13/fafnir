"""Database access layer for fafnir (psycopg 3)."""

from fafnir.db.connection import Database, connect

__all__ = ["Database", "connect"]
