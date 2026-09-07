"""The `cik:` / `isin:` / `cusip:` argument grammar, and the SQL it implies.

Three separate things are guarded here, and they break for different reasons:

  * The parser itself -- what counts as a scheme, what normalises to what, and
    which malformed values are refused rather than passed on as a ticker.
  * The rule that an UNPREFIXED argument still means exactly what it always meant.
    This feature is an added branch on `ph` and `ls`, and the whole point is that
    nobody's existing command changes meaning.
  * That the identifier SQL and migration 0023's expression indexes still spell
    the normalisation the same way. An index on `ltrim(btrim(cik),'0')` does not
    serve a query written `ltrim(cik,'0')`, and nothing else in the test suite
    would notice: the lookup stays correct, it just quietly stops using the index.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from duk import identifiers as ids
from duk.datasource import db as ds_db

REPO = Path(__file__).resolve().parents[2]


class TestSchemes:
    def test_a_bare_argument_is_a_ticker(self):
        ident = ids.parse("ibm")
        assert (ident.scheme, ident.value, ident.explicit) == ("ticker", "IBM", False)

    def test_known_schemes_are_recognised_case_insensitively(self):
        for text in ("cik:51143", "CIK:51143", "Cik:51143"):
            assert ids.parse(text).scheme == ids.CIK

    def test_an_unknown_prefix_is_not_a_scheme(self):
        # `ls "Baker: A History"` is a name search, not a typo'd identifier. Only
        # the known schemes claim the colon.
        ident = ids.parse("Baker: A History")
        assert ident.is_ticker
        assert ident.raw == "Baker: A History"

    def test_ticker_and_symbol_are_explicit_escape_hatches(self):
        for text in ("ticker:aapl", "symbol:aapl"):
            ident = ids.parse(text)
            assert (ident.scheme, ident.value, ident.explicit) == (
                "ticker",
                "AAPL",
                True,
            )


class TestNormalisation:
    def test_cik_zero_padding_is_presentation_not_identity(self):
        assert ids.parse("cik:0000051143").value == ids.parse("cik:51143").value

    def test_isin_is_uppercased_and_ungrouped(self):
        assert ids.parse("isin: us4592 0010 14").value == "US4592001014"

    def test_cusip_keeps_both_lengths(self):
        assert ids.parse("cusip:459200101").value == "459200101"
        assert ids.parse("cusip:45920010").value == "45920010"
        assert ids.parse("cusip:459200-10-1").value == "459200101"

    def test_cusip_from_isin_extracts_the_embedded_issue(self):
        assert ids.cusip_from_isin("US4592001014") == "459200101"
        assert ids.cusip_from_isin("CA0679011084") == "067901108"
        # Only US/CA ISINs embed a CUSIP; a German one embeds a WKN-derived NSIN.
        assert ids.cusip_from_isin("DE0005190003") is None


class TestMalformedValuesAreRefused:
    # Refused, not passed through as a ticker: a transposed CIK that comes back
    # "no data found" reads as "the warehouse does not have this security", which
    # is a different and much more alarming claim than "that is not a CIK".
    @pytest.mark.parametrize(
        "text",
        [
            "cik:abc",
            "cik:51143x",
            "cik:",
            "isin:US459",
            "isin:4592001014AB",
            "cusip:1234567",
            "cusip:4592001010A",
            "cusip:",
        ],
    )
    def test_refused(self, text):
        with pytest.raises(ids.IdentifierError):
            ids.parse(text)

    def test_the_message_names_the_scheme_and_shows_a_good_value(self):
        with pytest.raises(ids.IdentifierError) as excinfo:
            ids.parse("cik:five")
        message = str(excinfo.value)
        assert "cik:five" in message and "cik:51143" in message


class TestIdentifierSqlAndIndexesAgree:
    """Migration 0023 indexes an EXPRESSION, so the two must be spelled alike."""

    MIGRATION = (
        REPO / "sql/migrations/0023_security_identifier_indexes.up.sql"
    ).read_text()

    @pytest.mark.parametrize(
        "expression",
        ["ltrim(btrim(cik), '0')", "upper(btrim(isin))", "upper(btrim(cusip))"],
    )
    def test_the_expression_appears_in_both(self, expression):
        assert expression in ds_db._CIK_RESOLVE_SQL + ds_db._ISIN_RESOLVE_SQL + (
            ds_db._CUSIP_RESOLVE_SQL
        )
        # The migration writes it inside a CREATE INDEX ... ((expr)) wrapper, so
        # compare on whitespace-insensitive text.
        squashed = re.sub(r"\s+", " ", self.MIGRATION)
        assert expression in squashed

    def test_identifier_reads_stay_on_the_mart_seam(self):
        # The same rule test_mart_read_seam.py enforces for the module as a whole,
        # asserted directly on the new statements so a `core.security` shortcut in
        # one of them fails here with the reason attached.
        for sql in (
            ds_db._CIK_RESOLVE_SQL,
            ds_db._ISIN_RESOLVE_SQL,
            ds_db._CUSIP_RESOLVE_SQL,
            ds_db._CUSIP8_RESOLVE_SQL,
            ds_db._CUSIP_VIA_ISIN_SQL,
        ):
            assert "mart.v_security_profile" in sql
            assert "core." not in sql and "ops." not in sql
