"""Every SQL string in the tree survives psycopg's placeholder parser.

A literal ``%`` inside a query that carries parameters is not a runtime edge case,
it is a hard failure on the first execution: psycopg parses the whole string --
comments included -- looking for placeholders, and raises ``ProgrammingError:
incomplete placeholder`` before anything reaches the server. The escape is ``%%``.

This is a unit test on purpose. The statement that motivated it was
``upsert_security``, whose every caller is an integration test behind
``FAFNIR_TEST_DSN``; the whole write path was dead and the default suite was green.
A prose comment mentioning a percentage is exactly the kind of edit that looks
unreviewable, so the check has to run where a database does not.

Best effort by design: SQL assembled at runtime is checked on its literal skeleton
(the fragments the author typed), which is where a hard-coded ``%`` can live
anyway.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

SRC = Path(__file__).resolve().parents[2] / "src"

# The cursor/Database methods that hand a string to psycopg for parsing.
DB_METHODS = frozenset(
    {"execute", "executemany", "fetchone", "fetchall", "fetchval", "copy"}
)

_NAMED = re.compile(r"%\(([^)]+)\)s")


def _sql_fragments(node: ast.AST) -> str:
    """The literal text of a SQL argument: a plain string, or the typed parts of a
    concatenation/f-string with the interpolated values elided."""
    return " ".join(
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _call_sites():
    """(path, lineno, sql) for every db call that also passes parameters."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        # Module-level `SQL = """..."""` constants, so a query defined once and
        # executed elsewhere is still seen.
        consts = {
            t.id: n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
            for t in n.targets
            if isinstance(t, ast.Name) and isinstance(n.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in DB_METHODS:
                continue
            if len(node.args) < 2 and not any(k.arg == "params" for k in node.keywords):
                continue  # no parameters: psycopg never scans for placeholders
            arg = node.args[0]
            sql = (
                consts.get(arg.id, "")
                if isinstance(arg, ast.Name)
                else _sql_fragments(arg)
            )
            if "%" in sql:
                yield path, node.lineno, sql


def _params(sql: str):
    """A stand-in argument set of the right shape for `sql`."""
    names = _NAMED.findall(sql)
    if names:
        return {n: None for n in names}
    return tuple([None] * sql.replace("%%", "").count("%s"))


_SITES = list(_call_sites())


@pytest.mark.parametrize(
    "path, lineno, sql",
    _SITES,
    ids=[f"{p.relative_to(SRC)}:{n}" for p, n, _ in _SITES],
)
def test_placeholders_parse(path, lineno, sql):
    try:
        PostgresQuery(Transformer()).convert(sql, _params(sql))
    except Exception as exc:  # pragma: no cover - the message is the point
        offending = [
            ln.strip()
            for ln in sql.splitlines()
            if "%" in ln.replace("%%", "").replace("%s", "")
        ]
        pytest.fail(
            f"{path}:{lineno} would raise on execution: {exc}\n"
            f"  a literal percent sign must be doubled (`%%`), comments included\n"
            + "".join(f"  offending line: {ln}\n" for ln in offending)
        )
