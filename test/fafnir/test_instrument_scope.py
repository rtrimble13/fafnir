"""Unit tests for the instrument-scope rule (ADR 0012).

The rule decides which vendor entries are warrants, rights or units -- the kinds
this warehouse no longer carries. Every case below is a ticker/name pair from the
production security master on 2026-10-06, so a regression here is a real security
either leaking back into scope or, worse, a real company falling out of it.
"""

from __future__ import annotations

import click
import pytest

from fafnir import cli
from fafnir.config import FafnirConfig
from fafnir.instruments import (
    INSTRUMENT_KINDS,
    instrument_kind,
    out_of_scope_kind,
    rename_changes_instrument,
    select_descope_candidates,
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
    # The commands that read it say so in one line rather than a traceback.
    with pytest.raises(click.ClickException, match="warants"):
        cli._excluded_instruments(cfg)


# ---------------------------------------------------------------------------
# A rename never changes what an instrument is. These are the shapes of the
# vendor's ticker shuffles, not production pairs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old, new, name",
    [
        ("ABCD", "ABCDU", "Abcd Acquisition Corp."),  # share onto its unit
        ("ABCDU", "ABCD", "Abcd Acquisition Corp."),  # and back
        ("ABCD", "ABCDW", None),
        ("ABCD", "ABCDR", None),
        ("ABCDU", "ABCDW", None),  # unit onto its warrant
        ("AAC", "AAC-WT", "Ares Acquisition Corporation"),
        ("BIII-UN", "BIII", "Black Spade Acquisition III Co"),
        # A three-letter base: ZKPU alone needs its name, the shape does not.
        ("ZKP", "ZKPU", "Lafayette Digital Acquisition Corp. I"),
        ("ODVW", "ODVWZ", "Osisko Development Corp."),
    ],
)
def test_a_shuffle_within_one_issue_is_refused(old, new, name):
    assert rename_changes_instrument(old, new, old_name=name)


@pytest.mark.parametrize(
    "old, new, old_name, new_name",
    [
        ("FB", "META", "Facebook, Inc.", "Meta Platforms, Inc."),
        # A de-SPAC: warrants follow the share to the merged company's ticker.
        ("ABCDW", "NEWCW", "Abcd Acquisition Corp.", "NewCo Holdings, Inc."),
        ("AAC-WT", "NEWC-WT", "Ares Acquisition Corporation", "NewCo Holdings"),
        ("ZKPW", "NEWCW", "Lafayette Digital Acquisition Corp. I Warrant", None),
        # Four-letter tickers ending in W/R/U that are ordinary shares.
        ("CHRX", "CHRW", "C.H. Robinson Worldwide, Inc.", None),
        ("SNOW", "SNWF", "Snowflake Inc.", None),
        ("ABCD", "ABCD", "Abcd Acquisition Corp.", None),
        ("", "ABCDU", None, None),
    ],
)
def test_renames_that_keep_the_instrument_are_allowed(old, new, old_name, new_name):
    assert (
        rename_changes_instrument(old, new, old_name=old_name, new_name=new_name)
        is None
    )


def test_the_reason_names_both_sides():
    reason = rename_changes_instrument("ABCD", "ABCDU")
    assert reason.startswith("ABCD is not a warrant, right or unit and ABCDU is a unit")
    assert "ZKPU is ZKP plus the designator letter U" in rename_changes_instrument(
        "ZKP", "ZKPU"
    )


def test_funds_and_etfs_are_never_refused():
    assert rename_changes_instrument("ABCD", "ABCDU", is_etf=True) is None
    assert rename_changes_instrument("ABC", "ABCW", is_fund=True) is None


# ---------------------------------------------------------------------------
# Choosing what `security descope` removes, and what it keeps
# ---------------------------------------------------------------------------


def _sec(security_id, symbol, name="Abcd Acquisition Corp.", **extra):
    row = {
        "security_id": security_id,
        "primary_symbol": symbol,
        "company_name": name,
        "is_etf": False,
        "is_fund": False,
    }
    row.update(extra)
    return row


HELD = [
    _sec(1, "ABCD"),
    _sec(2, "ABCDW"),
    _sec(3, "ABCDU"),
    _sec(4, "GRP-UN", "Granite Real Estate Investment Trust"),
    _sec(5, "EFGHR"),
    _sec(6, "SNOW", "Snowflake Inc."),
]


def _symbols(rows):
    return sorted(r["primary_symbol"] for r in rows)


def test_every_excluded_kind_is_a_candidate_and_nothing_else():
    sel = select_descope_candidates(HELD, excluded=INSTRUMENT_KINDS)
    assert _symbols(sel.candidates) == ["ABCDU", "ABCDW", "EFGHR", "GRP-UN"]
    assert sel.kept_declared == sel.kept_by_request == sel.unmatched_keeps == []
    assert {r["primary_symbol"]: r["kind"] for r in sel.candidates}["GRP-UN"] == "unit"


def test_keep_takes_one_out_of_the_candidates_for_this_run():
    sel = select_descope_candidates(HELD, excluded=INSTRUMENT_KINDS, keep=["GRP-UN"])
    assert "GRP-UN" not in _symbols(sel.candidates)
    assert _symbols(sel.kept_by_request) == ["GRP-UN"]
    assert sel.unmatched_keeps == []


def test_keep_is_case_and_padding_insensitive():
    sel = select_descope_candidates(HELD, excluded=INSTRUMENT_KINDS, keep=[" grp-un "])
    assert _symbols(sel.kept_by_request) == ["GRP-UN"]


def test_a_declared_symbol_is_kept_separately_from_a_keep():
    sel = select_descope_candidates(
        HELD, excluded=INSTRUMENT_KINDS, declared=["ABCDU"], keep=["GRP-UN"]
    )
    assert _symbols(sel.kept_declared) == ["ABCDU"]
    assert _symbols(sel.kept_by_request) == ["GRP-UN"]
    assert _symbols(sel.candidates) == ["ABCDW", "EFGHR"]


def test_a_symbol_both_declared_and_kept_counts_as_matched():
    sel = select_descope_candidates(
        HELD, excluded=INSTRUMENT_KINDS, declared=["ABCDU"], keep=["ABCDU"]
    )
    assert sel.unmatched_keeps == []
    assert "ABCDU" not in _symbols(sel.candidates)


@pytest.mark.parametrize(
    "keep",
    [
        ["GRP-U"],  # a typo of a real candidate
        ["SNOW"],  # held, but not of an excluded kind
        ["ZZZZW"],  # not held at all
    ],
)
def test_a_keep_matching_no_candidate_is_reported_not_ignored(keep):
    sel = select_descope_candidates(HELD, excluded=INSTRUMENT_KINDS, keep=keep)
    assert sel.unmatched_keeps == [keep[0]]


def test_a_keep_outside_the_kinds_of_this_run_is_unmatched():
    sel = select_descope_candidates(HELD, excluded=("warrant",), keep=["GRP-UN"])
    assert _symbols(sel.candidates) == ["ABCDW"]
    assert sel.unmatched_keeps == ["GRP-UN"]


def test_the_cli_offers_keep():
    params = {p.name for p in cli.security_descope.params}
    assert "keep_symbols" in params
