"""The rows an upgrade test compares across a rollback, column by column.

The upgrade tests roll a database back below the migration they test, or migrate
it up from there, and check that the rows already present come through unchanged.
A digest of each whole row (``t::text``) cannot say that once a later migration adds
a column to the same table: on the far side of the rollback every row is a column
shorter, so the digests differ while no value has moved. 0045 was the first since
0031 to add one to ``core.security`` (``feed_frozen_on``, ``feed_frozen_note``).

So a snapshot keeps the text of every column of every row, and two snapshots are
compared on the columns both sides have. A column only one side has belongs to a
migration the rollback removes or the upgrade adds; every column both have must
hold the same values, NULL kept apart from the empty string.
"""

from __future__ import annotations

from typing import Iterable

from psycopg import sql


def snapshot(db, tables: Iterable[str]) -> dict[str, dict]:
    """``{table: {"columns": [...], "rows": [{column: text}, ...]}}`` for each
    ``schema.table``, every value as ``quote_nullable`` of its text."""
    out = {}
    for table in tables:
        schema, name = table.split(".")
        columns = [
            r["attname"]
            for r in db.fetchall(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
                " AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                (table,),
            )
        ]
        select = sql.SQL(", ").join(
            sql.SQL("quote_nullable(t.{c}::text) AS {c}").format(c=sql.Identifier(c))
            for c in columns
        )
        query = sql.SQL("SELECT {select} FROM {table} t").format(
            select=select, table=sql.Identifier(schema, name)
        )
        out[table] = {
            "columns": columns,
            "rows": db.fetchall(query.as_string(db.conn)),
        }
    return out


def on_shared_columns(a: dict, b: dict) -> tuple[dict, dict]:
    """Both snapshots, each table's rows sorted and cut to the columns both sides
    have: what a rollback or an upgrade must leave equal."""

    def cut(snap: dict, other: dict) -> dict:
        out = {}
        for table, s in snap.items():
            shared = sorted(set(s["columns"]) & set(other[table]["columns"]))
            out[table] = sorted(tuple(r[c] for c in shared) for r in s["rows"])
        return out

    return cut(a, b), cut(b, a)
