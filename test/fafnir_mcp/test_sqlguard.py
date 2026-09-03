"""
Unit tests for the ``sql_read`` statement allowlist.

ADR 0010 permits a free-form ``SELECT`` in the ops profile only while five
constraints hold, and names this module as one of them. What it must get right is
narrow but exact: accept the reads an investigation is written in, refuse anything
that is not one statement beginning with SELECT/WITH, and -- the part that is easy
to get wrong -- decide both of those from the statement's *structure*, so that a
semicolon or the word DELETE inside a string literal changes nothing.

It deliberately does NOT try to decide whether a SELECT writes. ``SELECT ... INTO``
and a data-modifying CTE both do, and both are caught by the read-only transaction
instead -- asserted in the integration tests, where a real server can refuse them.
"""

from __future__ import annotations

import pytest

from fafnir_mcp.errors import ToolError
from fafnir_mcp.sqlguard import strip_noise, validate_select

ACCEPTED = [
    "SELECT 1",
    "select * from ops.data_quality_flag",
    "  \n\t SELECT 1 \n ",
    "SELECT 1;",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "with recursive t as (select 1) select * from t",
    "(SELECT 1)",
    "/* a leading comment */ SELECT 1",
    "SELECT 1 -- a trailing comment",
    "SELECT 1 /* an inner\n comment */ + 1",
    # The structural cases: a semicolon and a keyword inside quoted text are data.
    "SELECT ';' AS semicolon_in_a_string",
    "SELECT 'DELETE FROM core.daily_price' AS looks_scary",
    "SELECT 'it''s an escaped quote'",
    'SELECT 1 AS "a; quoted identifier"',
    "SELECT $$dollar quoted ; with a semicolon$$",
    "SELECT $tag$tagged ; dollar quoting$tag$",
    "SELECT E'\\'' AS backslash_escaped_quote",
    "SELECT 1 /* /* postgres nests */ these */ + 1",
    # A realistic triage query -- the thing the tool exists for.
    """
    SELECT f.check_name, count(*) AS flags
      FROM ops.data_quality_flag f
     WHERE f.resolved_at IS NULL AND f.check_name LIKE 'price_%'
     GROUP BY 1 ORDER BY 2 DESC
    """,
]

REFUSED = [
    # Not a read.
    "INSERT INTO core.security (primary_symbol) VALUES ('X')",
    "UPDATE ops.data_quality_flag SET resolved_at = now()",
    "DELETE FROM core.daily_price",
    "TRUNCATE core.daily_price",
    "DROP TABLE core.daily_price",
    "ALTER TABLE core.security ADD COLUMN x int",
    "CREATE TABLE mart.sneaky AS SELECT 1",
    "GRANT SELECT ON ops.data_quality_flag TO public",
    "COPY core.security FROM '/etc/passwd'",
    "DO $$ BEGIN PERFORM 1; END $$",
    "CALL some_procedure()",
    "VACUUM core.daily_price",
    "SET default_transaction_read_only = off",
    "BEGIN",
    "EXPLAIN ANALYZE DELETE FROM core.daily_price",
    # Not one statement. The second is what makes this matter.
    "SELECT 1; DROP TABLE core.daily_price",
    "select 1; delete from core.daily_price",
    "SELECT 1;SELECT 2",
    # Nothing to run.
    "",
    "   ",
    "-- only a comment",
    "/* only a block comment */",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_reads_are_accepted(sql):
    assert validate_select(sql) == sql.strip()


@pytest.mark.parametrize("sql", REFUSED)
def test_everything_else_is_refused(sql):
    with pytest.raises(ToolError):
        validate_select(sql)


def test_none_is_refused():
    with pytest.raises(ToolError):
        validate_select(None)


def test_the_original_statement_is_returned_not_the_stripped_copy():
    """Comments survive to the server.

    They are sometimes load-bearing -- an operator reading `pg_stat_activity`
    during a slow query wants the agent's own explanation of what it was asking.
    Only the copy used for the checks is stripped.
    """
    sql = "SELECT 1 -- why the agent ran this"
    assert validate_select(sql) == sql


def test_refusal_says_where_changes_go():
    """A refused write should point at the CLI, not just say no.

    The message is the entire payload an agent gets back, so it is the only place
    the ADR 0010 rule can be taught at the moment it is being broken.
    """
    with pytest.raises(ToolError) as exc:
        validate_select("DELETE FROM core.daily_price")
    assert "fafnir" in str(exc.value)


# Statement-boundary evasions: each hides a semicolon inside a token class, so a
# lexer that strips token classes in the wrong order sees one statement where there
# are two. The first of these was a real defect -- sequential regex passes stripped
# the line comment first, eating the semicolon AND the second statement, and the
# batch was accepted. See strip_noise's docstring.
BOUNDARY_EVASIONS = [
    "SELECT '--'; DROP TABLE core.daily_price",
    "SELECT '/*'; DROP TABLE core.daily_price",
    "SELECT E'\\''; DROP TABLE core.daily_price",
    "SELECT $$x$$; DROP TABLE core.daily_price",
    'SELECT 1 AS "a"; DROP TABLE core.daily_price',
    "SELECT 1 /* /* nested */ */ ; DROP TABLE core.daily_price",
    "SELECT 'a' /* ; */ ; DROP TABLE core.daily_price",
]


@pytest.mark.parametrize("sql", BOUNDARY_EVASIONS)
def test_a_semicolon_hidden_in_a_token_still_ends_a_statement(sql):
    """The guarantee ADR 0010 names: exactly one statement, decided structurally."""
    with pytest.raises(ToolError):
        validate_select(sql)


# Unterminated quoting is malformed SQL the server would reject anyway, and is also
# the shape one would reach for to make a lexer and a parser disagree about where a
# statement ends. Refused rather than tolerated.
MALFORMED = [
    "SELECT 'unterminated",
    "SELECT /* unterminated",
    'SELECT "unterminated',
    "SELECT $$unterminated",
    "SELECT $tag$unterminated",
]


@pytest.mark.parametrize("sql", MALFORMED)
def test_unterminated_quoting_is_refused(sql):
    with pytest.raises(ToolError):
        validate_select(sql)


class TestStripNoise:
    """The lexer's one job, tested directly.

    Ordering is the subtle part: strip line comments before string literals and
    ``'--'`` as a literal eats the rest of the line; strip them after and a comment
    containing an apostrophe swallows the query. Both orderings are wrong in one
    direction, so both directions are asserted.
    """

    def test_a_double_dash_inside_a_string_is_not_a_comment(self):
        assert "1" in strip_noise("SELECT '--' AS dashes, 1")

    def test_an_apostrophe_inside_a_comment_does_not_open_a_string(self):
        # If the apostrophe opened a literal, the following semicolon would be
        # swallowed and a two-statement batch would slip through as one.
        with pytest.raises(ToolError):
            validate_select("SELECT 1 -- it's fine\n; DROP TABLE core.security")

    def test_dollar_quoting_hides_its_whole_body(self):
        assert ";" not in strip_noise("SELECT $$ ; $$")

    def test_block_comments_are_removed(self):
        assert "DROP" not in strip_noise("SELECT /* DROP TABLE t */ 1").upper()
