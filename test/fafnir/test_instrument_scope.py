"""Unit tests for the instrument-scope rule (ADR 0012).

The rule decides which vendor entries are warrants, rights or units -- the kinds
this warehouse no longer carries. Every case below is a ticker/name pair from the
production security master on 2026-10-06, so a regression here is a real security
either leaking back into scope or, worse, a real company falling out of it.
"""

from __future__ import annotations

import pytest

from fafnir import cli
from fafnir.config import FafnirConfig
from fafnir.ingest.security_master import (
    INSTRUMENT_KINDS,
    instrument_kind,
    out_of_scope_kind,
)


@pytest.mark.parametrize(
    "symbol, name, kind",
    [
        # Nasdaq's fifth letter decides, whatever the vendor calls it.
        ("CCXIW", "Churchill Capital Corp XI Warrants", "warrant"),
        ("SSACW", "SPACSphere Acquisition Corp.", "warrant"),
        ("OPENW", "Opendoor Technologies Inc.", "warrant"),
        ("IRHOR", "Iron Horse Acquisitions Corp. II Rights", "right"),
        ("SIFYR", "Sify Technologies Limited Rights expiring", "right"),
        ("GRSVU", "Gores Holdings V, Inc.", "unit"),
        ("SPAQU", "Spartan Acquisition Corp. III", "unit"),
        # NYSE suffixes as FMP spells them.
        ("AAC-WT", "Ares Acquisition Corporation", "warrant"),
        ("TDW-WTA", "Tidewater Inc. Series A", "warrant"),
        ("FREY-WT", "FREYR Battery, Inc. WT", "warrant"),
        ("BIII-UN", "Black Spade Acquisition III Co", "unit"),
        ("ABC.WS", None, "warrant"),
        ("ABC-RT", None, "right"),
        # Ambiguous shapes the name confirms.
        ("ZKPW", "Lafayette Digital Acquisition Corp. I Warrant", "warrant"),
        ("ZKPU", "Lafayette Digital Acquisition Corp. I Units", "unit"),
        ("ODVWZ", "Osisko Development Corp. Warrant expiring", "warrant"),
        ("AMPGZ", "Amplitech Group, Inc. Series B Right", "right"),
    ],
)
def test_warrants_rights_and_units_are_recognised(symbol, name, kind):
    assert instrument_kind(symbol, name) == kind


@pytest.mark.parametrize(
    "symbol, name",
    [
        # Ordinary four-letter tickers ending in W, R or U.
        ("SNOW", "Snowflake Inc."),
        ("CHRW", "C.H. Robinson Worldwide, Inc."),
        # A class A share whose vendor name says "Unit" -- the letter disagrees.
        ("GTER", "Globa Terra Acquisition Corporation Unit"),
        # A fifth-letter Z with nothing in the name to say what it is.
        ("ABCDZ", "Abcd Holdings Inc."),
        # Share classes and preferreds.
        ("BRK-B", "Berkshire Hathaway Inc."),
        ("FITB-PA", "Fifth Third Bancorp"),
        ("NYCB-PU", "New York Community Capital Trust V BONUSES Units"),
        # Equity units under a ticker the rule does not reach.
        ("SWP", "Stanley Black & Decker 2017 Equity Units"),
        ("AAPL", "Apple Inc."),
        ("", None),
        (None, None),
    ],
)
def test_everything_else_is_left_alone(symbol, name):
    assert instrument_kind(symbol, name) is None


def test_funds_and_etfs_are_never_instruments_of_these_kinds():
    assert instrument_kind("ABCDW", "Abcd Fund", is_fund=True) is None
    assert instrument_kind("ABCDU", "Abcd ETF", is_etf=True) is None


def test_lower_case_and_padding_are_tolerated():
    assert instrument_kind("  ccxiw ", None) == "warrant"


def test_out_of_scope_honours_the_excluded_kinds():
    assert out_of_scope_kind("CCXIW", None) == "warrant"
    assert out_of_scope_kind("CCXIW", None, excluded=("right", "unit")) is None
    assert out_of_scope_kind("GRSVU", None, excluded=("unit",)) == "unit"
    assert out_of_scope_kind("CCXIW", None, excluded=()) is None
    assert out_of_scope_kind("AAPL", "Apple Inc.") is None


def test_the_cli_offers_exactly_the_kinds_the_rule_knows():
    assert tuple(cli._DESCOPE_KINDS) == INSTRUMENT_KINDS


def _config(tmp_path, body: str) -> FafnirConfig:
    path = tmp_path / "fafnirrc"
    path.write_text(body)
    return FafnirConfig(str(path))


def test_all_three_kinds_are_excluded_by_default(tmp_path):
    assert _config(tmp_path, "").excluded_instruments == INSTRUMENT_KINDS


def test_the_exclusion_can_be_narrowed_or_switched_off(tmp_path):
    cfg = _config(tmp_path, '[general]\nexclude_instruments = ["Unit", "unit"]\n')
    assert cfg.excluded_instruments == ("unit",)
    cfg = _config(tmp_path, "[general]\nexclude_instruments = []\n")
    assert cfg.excluded_instruments == ()
    cfg = _config(tmp_path, '[general]\nexclude_instruments = "right"\n')
    assert cfg.excluded_instruments == ("right",)


def test_a_misspelt_kind_is_an_error_not_a_silent_readmission(tmp_path):
    cfg = _config(tmp_path, '[general]\nexclude_instruments = ["warants"]\n')
    with pytest.raises(ValueError, match="warants"):
        cfg.excluded_instruments
