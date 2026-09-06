"""Every SQL string in the tree survives psycopg's placeholder parser.

A literal ``%`` inside a query that carries parameters is not a runtime edge case,
it is a hard failure on the first execution: psycopg scans the whole string --
comments included -- for placeholders, and raises ``ProgrammingError: incomplete
placeholder`` before anything reaches the server. The escape is ``%%``.

This is a unit test on purpose. The statement that motivated it was
``upsert_security``, whose every caller is an integration test behind
``FAFNIR_TEST_DSN``; the whole write path was dead and the default suite was green.
A prose comment mentioning a percentage is exactly the kind of edit that looks
unreviewable, so the guard has to run where a database does not.

Best effort by design: SQL assembled at runtime is checked on its literal skeleton
(the fragments the author typed), which is where a hard-coded ``%`` can live
anyway.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# The cursor/Database methods that hand a string to psycopg for parsing.
DB_METHODS = frozenset(
    {"execute", "executemany", "fetchone", "fetchall", "fetchval", "copy"}
)

# psycopg's own rule, mirrored rather than imported: it reads `%`, then either
# `(name)` plus a format char or a single char, and accepts that char only if it is
# a format (s/b/t) or a second `%`. Anything else -- including a `%` at the very end
# -- is "incomplete placeholder". Mirrored because the parser lives in the private
# `psycopg._queries` while psycopg itself is an open `>=3.1` dependency: a guard
# this file exists to provide must not stop working on a routine version bump.
# test_the_local_rule_agrees_with_psycopg pins the two together.
_PLACEHOLDER = re.compile(r"%(?:\([^)]+\).|.|$)", re.S)
_VALID = ("s", "b", "t", "%")


def stray_percent(sql: str) -> str | None:
    """The first placeholder psycopg would reject, or None."""
    for match in _PLACEHOLDER.finditer(sql):
        if not match.group()[-1:] in _VALID:
            return match.group()
    return None


def _sql_fragments(node: ast.AST) -> str:
    """The literal text of a SQL argument: a plain string, or the typed parts of a
    concatenation or f-string with the interpolated values elided."""
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


_SITES = list(_call_sites())


def test_the_scan_reaches_the_repository():
    """A silent zero here would make every case below vacuous."""
    assert len(_SITES) > 50
    assert any(p.name == "repository.py" for p, _, _ in _SITES)


@pytest.mark.parametrize(
    "path, lineno, sql",
    _SITES,
    ids=[f"{p.relative_to(SRC)}:{n}" for p, n, _ in _SITES],
)
def test_placeholders_parse(path, lineno, sql):
    bad = stray_percent(sql)
    if bad is None:
        return
    offending = [ln.strip() for ln in sql.splitlines() if stray_percent(ln)]
    pytest.fail(
        f"{path}:{lineno} would raise on execution: incomplete placeholder "
        f"{bad!r}\n"
        f"  a literal percent sign must be doubled (`%%`), comments included\n"
        + "".join(f"  offending line: {ln}\n" for ln in offending)
    )


# A sample per branch of the rule: valid formats, both escapes, a named
# placeholder, and the three ways a bare `%` reaches the parser.
_SAMPLES = [
    "SELECT %s",
    "SELECT %(name)s",
    "SELECT %b, %t",
    "SELECT 100%% done, %s",
    "SELECT %s -- emptied 75% of the master",
    "SELECT %s -- 75%",
    "SELECT %s WHERE a %(x)y b",
]


@pytest.mark.parametrize("sql", _SAMPLES)
def test_the_local_rule_agrees_with_psycopg(sql):
    """Pin the mirrored rule to the real parser while it is still importable.

    If psycopg moves `_queries`, this is the case that goes red -- a prompt to
    re-check `stray_percent` against the new internals -- and never the tree-wide
    guard above, which does not import it.
    """
    queries = pytest.importorskip(
        "psycopg._queries",
        reason="psycopg._queries moved; re-check stray_percent against psycopg",
    )
    from psycopg.adapt import Transformer

    # psycopg checks the parameter count too, so the sample has to be given the
    # right number of them or every case "fails" for the wrong reason.
    positional = sql.replace("%%", "")
    params: object = {n: None for n in re.findall(r"%\(([^)]+)\)[sbt]", positional)}
    if not params:
        params = tuple([None] * len(re.findall(r"%[sbt]", positional)))

    try:
        queries.PostgresQuery(Transformer()).convert(sql, params)
        psycopg_rejects = False
    except Exception:
        psycopg_rejects = True

    assert (stray_percent(sql) is not None) is psycopg_rejects
