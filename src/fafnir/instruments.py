"""
What kind of instrument a ticker is: warrant, right, unit, or none of those.

ADR 0012 takes warrants, rights and units out of scope, and three different layers
have to agree on what one is. The security-master load uses it to skip them,
`fafnir security descope` to remove them, and the rename machinery to refuse a
rename that would turn one into another. So the classifier lives here, importing
nothing from fafnir, where the repository can use it without a cycle.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: Instrument kinds kept out of the universe by default (ADR 0012). None of them is
#: an equity: a warrant and a right are options on a listed share, and a unit is a
#: share stapled to warrants or rights until it separates. They are also where the
#: data-quality load came from -- 31% of the condition flags raised from 2026-09-08
#: to 2026-10-06 (82% of `stale`) fell on the 3.8% of active securities that are one
#: of these three, for 3.3% of the bars and ~0.03% of the dollar volume.
INSTRUMENT_KINDS = ("warrant", "right", "unit")

# Nasdaq's fifth-letter convention. W, R and U are reserved, so a five-letter ticker
# ending in one of them is that instrument whatever name the vendor serves it under
# ("SSACW SPACSphere Acquisition Corp." is a warrant).
_FIFTH_LETTER = re.compile(r"^[A-Z]{4}([WRU])$")
_LETTER_KIND = {"W": "warrant", "R": "right", "U": "unit"}
# The NYSE / NYSE American suffixes as FMP spells them: AAC-WT, TDW-WTA, ACII-UN.
# Preferreds (-PA, -PB ...) and share classes (BRK-B) never end in one of these.
_SUFFIX = re.compile(r"^[A-Z]{1,5}[-./](WS|WT[A-Z]?|W|RT|R|UN|U)$")
_SUFFIX_KIND = {
    "WS": "warrant",
    "WT": "warrant",
    "W": "warrant",
    "RT": "right",
    "R": "right",
    "UN": "unit",
    "U": "unit",
}
# Two shapes the ticker cannot settle alone, so the vendor's name must name the same
# kind the ticker points at:
#   * a three-letter base plus W/R/U -- a Nasdaq SPAC whose class A has three
#     letters (ZKPW, ZKPU); plenty of ordinary four-letter tickers end in W, R or U;
#   * a fifth letter Z, Nasdaq's "miscellaneous" letter, which issuers use for a
#     second series of warrants or rights (ODVWZ, AMPGZ).
_SHORT_BASE = re.compile(r"^[A-Z]{3}([WRU])$")
_SECOND_SERIES = re.compile(r"^[A-Z]{4}Z$")
_NAME_SAYS = {
    "warrant": re.compile(r"\b(warrants?|wts?)\b", re.IGNORECASE),
    "right": re.compile(r"\brights?\b", re.IGNORECASE),
    "unit": re.compile(r"\bunits?\b", re.IGNORECASE),
}
# The letters Nasdaq appends to an issue's base ticker for its warrants, rights,
# units and further series (ZKP -> ZKPW, ZKPR, ZKPU; ODVW -> ODVWZ).
_DESIGNATOR_LETTERS = frozenset("WRUZ")


def instrument_kind(
    symbol: Optional[str],
    company_name: Optional[str] = None,
    *,
    is_etf: bool = False,
    is_fund: bool = False,
) -> Optional[str]:
    """``"warrant"``, ``"right"`` or ``"unit"`` for one of those, else None.

    The ticker decides, because the vendor's name does not: FMP serves most SPAC
    warrants and units under the sponsor's plain name ("GRSVU Gores Holdings V,
    Inc."). The name is consulted only to confirm the two shapes the ticker cannot
    settle on its own, and then it has to agree with the ticker's letter -- which is
    what keeps GTER ("Globa Terra Acquisition Corporation Unit", a class A share
    whose ticker ends in R) and SNOW out.

    Funds and ETFs are never one of these, whatever their ticker.
    """
    if is_etf or is_fund:
        return None
    sym = (symbol or "").strip().upper()
    match = _FIFTH_LETTER.match(sym)
    if match:
        return _LETTER_KIND[match.group(1)]
    match = _SUFFIX.match(sym)
    if match:
        return _SUFFIX_KIND["WT" if match.group(1).startswith("WT") else match.group(1)]
    name = company_name or ""
    match = _SHORT_BASE.match(sym)
    if match:
        kind = _LETTER_KIND[match.group(1)]
        return kind if _NAME_SAYS[kind].search(name) else None
    if _SECOND_SERIES.match(sym):
        for kind in ("warrant", "right"):
            if _NAME_SAYS[kind].search(name):
                return kind
    return None


def out_of_scope_kind(
    symbol: Optional[str],
    company_name: Optional[str] = None,
    *,
    is_etf: bool = False,
    is_fund: bool = False,
    excluded: Iterable[str] = INSTRUMENT_KINDS,
) -> Optional[str]:
    """The excluded instrument kind this entry is, or None if it is in scope.

    A declared symbol (``fafnir track add``) is the operator overriding this rule
    for one ticker, and callers check that themselves: this function answers what
    the instrument is, not whether someone has asked to keep it.
    """
    kind = instrument_kind(symbol, company_name, is_etf=is_etf, is_fund=is_fund)
    return kind if kind is not None and kind in set(excluded) else None


def _described(kind: Optional[str]) -> str:
    return f"a {kind}" if kind else "not a warrant, right or unit"


def rename_changes_instrument(
    old_symbol: Optional[str],
    new_symbol: Optional[str],
    *,
    old_name: Optional[str] = None,
    new_name: Optional[str] = None,
    is_etf: bool = False,
    is_fund: bool = False,
) -> Optional[str]:
    """Why OLD -> NEW cannot be one instrument renamed, or None if it can be.

    A rename moves an instrument to a new ticker. It never turns a share into a
    warrant, right or unit, or one of those into a share or into each other. The
    vendor's rename feed nonetheless reports exactly that, shuffling one SPAC's
    tickers among themselves before launch (``ABCD -> ABCDU``, and back again).
    Carried out, such a row moves the class A share onto its unit's ticker. The
    security-master load then never refreshes it (ADR 0012), the price step feeds
    it the unit's bars, and the next `security descope` deletes it as a unit. So
    the rename machinery refuses a rename this returns a reason for.

    Two tests, because the kind alone cannot see every shuffle:

    * the kinds differ, each ticker classified with the name it is known by
      (``ABCD -> ABCDU``, ``AAC -> AAC-WT``, ``ABCDU -> ABCDW``);
    * one ticker is the other plus a single designator letter, which needs no
      name at all (``ZKP -> ZKPU``, where ``ZKPU`` alone is ambiguous).

    ``new_name`` defaults to ``old_name``: the feed reports one name for both
    sides. Funds and ETFs are never refused; the kinds do not apply to them.
    """
    if is_etf or is_fund:
        return None
    old = (old_symbol or "").strip().upper()
    new = (new_symbol or "").strip().upper()
    if not old or not new or old == new:
        return None
    old_kind = instrument_kind(old, old_name)
    new_kind = instrument_kind(new, new_name if new_name is not None else old_name)
    if old_kind != new_kind:
        return (
            f"{old} is {_described(old_kind)} and {new} is {_described(new_kind)}; "
            "a rename does not change what an instrument is"
        )
    for short, long in ((old, new), (new, old)):
        if (
            len(long) == len(short) + 1
            and long.startswith(short)
            and long[-1] in _DESIGNATOR_LETTERS
        ):
            return (
                f"{long} is {short} plus the designator letter {long[-1]}: one "
                "issue's tickers shuffled by the vendor, not a rename"
            )
    return None
