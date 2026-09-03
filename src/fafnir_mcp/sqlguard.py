"""
Statement validation for ``sql_read``.

ADR 0010 permits a free-form ``SELECT`` in the ops profile, and is explicit that
the permission is only the one argued for while five constraints all hold: ops
profile, on-host, a read-only role, a read-only transaction with a statement
allowlist, and a row cap. This module is the statement allowlist.

**What this catches and what it does not, deliberately.**

This is a lexer, not a parser, and pretending otherwise would be the dangerous
mistake. It enforces two structural properties that a lexer really can decide:

1. the statement begins with ``SELECT`` or ``WITH``;
2. it is exactly one statement.

It does *not* try to decide whether a ``SELECT`` writes. Several do --
``SELECT ... INTO newtable``, ``WITH x AS (INSERT ...) SELECT``, ``SELECT
nextval(...)`` -- and a regex that tried to spot them would either miss cases or
reject legitimate queries (``INTO`` is a valid column alias). Those are caught by
the layer that can actually decide: the ``READ ONLY`` transaction the server opens,
where PostgreSQL itself refuses them, on top of a role holding no write privilege
to begin with. Two independent mechanisms, neither relying on this file being
clever.

The reason to have this layer at all, given the transaction: a refused statement
should come back as a sentence explaining the rule, not as a Postgres error about
transaction modes. And "one statement" is genuinely worth enforcing here, because
whether a driver splits on semicolons is a property of the driver, not of the
warehouse.
"""

from __future__ import annotations

import re

from fafnir_mcp.errors import ToolError

#: The two keywords a read may begin with. ``WITH`` is allowed because a CTE is how
#: any interesting investigative query is written; a data-modifying CTE is refused
#: by the read-only transaction, not here (see the module docstring).
ALLOWED_LEADING = ("select", "with")

# A dollar-quote opener: $$ or $tag$.
_DOLLAR_OPEN = re.compile(r"\$(\w*)\$")


class MalformedSql(ToolError):
    """An unterminated string literal or comment.

    Refused rather than tolerated. A statement whose quoting does not close is
    malformed SQL that the server would reject anyway -- and it is also precisely
    the shape someone would reach for to confuse a lexer into disagreeing with the
    parser about where a statement ends.
    """


def strip_noise(sql: str) -> str:
    """Replace comments and quoted text with placeholders, leaving structure.

    A **single pass**, not a sequence of regex substitutions, and the difference is
    a real defect rather than a style preference. Substituting one token class at a
    time cannot be ordered correctly, because the classes overlap in both
    directions: strip line comments first and ``'--'`` as a string literal eats the
    rest of the line; strip string literals first and an apostrophe inside a
    comment opens a literal that swallows the query.

    The first of those was exploitable. ``SELECT '--'; DROP TABLE core.daily_price``
    stripped to ``SELECT '`` -- the comment pass having eaten the semicolon along
    with the second statement -- so the "exactly one statement" check saw one
    statement and accepted a batch. (Not exploitable end to end, since the
    read-only transaction still refused the DROP, but one of ADR 0010's five
    constraints was not holding.) A scanner that decides each token by what starts
    at the cursor cannot have that class of bug, in either direction.

    Handles, because PostgreSQL does: nestable block comments, ``''`` escapes,
    backslash escapes inside ``E'...'`` strings, ``""`` escapes in quoted
    identifiers, and ``$tag$`` dollar quoting.
    """
    text = str(sql)
    out: list[str] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        pair = text[i : i + 2]

        if pair == "--":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            out.append(" ")
            continue

        if pair == "/*":
            # Nestable, unlike SQL-standard comments: /* /* */ */ is one comment.
            depth, i = 1, i + 2
            while i < n and depth:
                if text[i : i + 2] == "/*":
                    depth, i = depth + 1, i + 2
                elif text[i : i + 2] == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise MalformedSql("sql has an unterminated /* block comment")
            out.append(" ")
            continue

        if ch == "'":
            # E'...' honours backslash escapes; a plain '...' does not (the default
            # standard_conforming_strings). Look back for the E prefix as its own
            # token, so `SELECTE'x'` is not mistaken for one.
            escaped = bool(re.search(r"(?:^|[^A-Za-z0-9_])[Ee]$", "".join(out)))
            i = _skip_quoted(text, i, "'", allow_backslash=escaped)
            out.append(" '' ")
            continue

        if ch == '"':
            i = _skip_quoted(text, i, '"', allow_backslash=False)
            out.append(' "" ')
            continue

        if ch == "$":
            opener = _DOLLAR_OPEN.match(text, i)
            if opener:
                close = text.find(opener.group(0), opener.end())
                if close < 0:
                    raise MalformedSql(
                        f"sql has an unterminated {opener.group(0)} dollar-quoted "
                        f"string"
                    )
                i = close + len(opener.group(0))
                out.append(" '' ")
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def _skip_quoted(text: str, start: int, quote: str, *, allow_backslash: bool) -> int:
    """Index just past a quoted run beginning at ``start``. Raises if unterminated."""
    i, n = start + 1, len(text)
    while i < n:
        ch = text[i]
        if allow_backslash and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if text[i + 1 : i + 2] == quote:  # doubled -> an escaped quote
                i += 2
                continue
            return i + 1
        i += 1
    kind = "string literal" if quote == "'" else "quoted identifier"
    raise MalformedSql(f"sql has an unterminated {kind}")


def validate_select(sql: str) -> str:
    """Return the statement if it is a single ``SELECT``/``WITH``; else raise.

    The returned text is the caller's original -- including comments, which are
    sometimes load-bearing (``/*+ ... */`` style hints, or an explanation the
    operator will read in ``pg_stat_activity``). Only the copy used for the checks
    is stripped.
    """
    if sql is None or not str(sql).strip():
        raise ToolError("sql must be a non-empty SELECT statement")

    original = str(sql).strip()
    stripped = strip_noise(original).strip()

    if not stripped:
        raise ToolError("sql contains no statement -- only comments or string literals")

    leading = re.match(r"[a-zA-Z]+", stripped.lstrip("( \t\r\n"))
    keyword = leading.group(0).lower() if leading else ""
    if keyword not in ALLOWED_LEADING:
        raise ToolError(
            f"sql_read runs read-only queries only, and this statement begins with "
            f"{keyword.upper() or 'an unrecognised token'!s}. Start with SELECT or "
            f"WITH. Changes to the warehouse go through the fafnir CLI "
            f"(`fafnir dq resolve`, `fafnir ingest`, `fafnir adjust`), never this "
            f"tool -- see ADR 0010."
        )

    # One statement. A trailing semicolon is fine; anything after it is not.
    body, _, tail = stripped.partition(";")
    if tail.strip():
        raise ToolError(
            "sql must be exactly one statement. Send them one at a time -- a "
            "semicolon-separated batch is refused because whether a driver splits "
            "on it is a property of the driver, not of the warehouse."
        )
    if not body.strip():  # pragma: no cover -- unreachable given the checks above
        raise ToolError("sql must be a non-empty SELECT statement")

    return original
