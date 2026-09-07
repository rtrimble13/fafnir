"""Security identifiers a user may type in place of a ticker.

``duk ph cik:51143`` and ``duk ls cusip:459200101`` both mean IBM. This module is
the parser and normaliser for that syntax and nothing else: it does no I/O, knows
no SQL and imports no click, so the grammar can be asserted without a database and
both data sources share one definition of what the user typed.

The syntax is ``scheme:value``, and the prefix is **required**. An identifier is
not guessable from its shape -- ``51143`` is a plausible CIK and a plausible
nothing-at-all, and a nine-digit CUSIP and a nine-character ticker are the same
string to a regex. Requiring the prefix means an unprefixed argument is still
exactly what it has always been (a ticker, or for ``ls`` a company name), so this
feature cannot change the meaning of a command anyone is already running.

For the same reason an unrecognised prefix is NOT an error: ``ls "Baker: A
History"`` is a name search, not a typo'd scheme. Only the four known schemes
below claim the colon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TICKER = "ticker"
CIK = "cik"
ISIN = "isin"
CUSIP = "cusip"

# `symbol:` is an alias for `ticker:`, because the CLI argument is called SYMBOL
# and the warehouse column is `symbol`. Both exist mainly as an escape hatch: a
# ticker that ever collides with a scheme name can still be spelled explicitly.
_ALIASES = {
    "ticker": TICKER,
    "symbol": TICKER,
    "cik": CIK,
    "isin": ISIN,
    "cusip": CUSIP,
}

LABELS = {TICKER: "ticker", CIK: "CIK", ISIN: "ISIN", CUSIP: "CUSIP"}

# ISIN: 2-letter ISO country code, 9 alphanumeric NSIN, 1 check digit.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# CUSIP: 9 alphanumeric characters, the last of which is a check digit. Eight are
# accepted too -- the check digit is routinely dropped in spreadsheets and internal
# systems, and an 8-character CUSIP identifies the issue just as uniquely.
_CUSIP_RE = re.compile(r"^[A-Z0-9]{8,9}$")
_DIGITS_RE = re.compile(r"^[0-9]+$")

# Separators people paste along with the value: ISINs and CUSIPs are often written
# in groups ("US4592 0010 14", "459200-10-1").
_SEPARATORS = re.compile(r"[\s\-.]")


class IdentifierError(ValueError):
    """The scheme was recognised but the value is not a well-formed identifier."""


@dataclass(frozen=True)
class Identifier:
    """A parsed ``scheme:value`` argument.

    ``raw`` is what the user typed (stripped), kept so error messages quote the
    argument back rather than a normalised form the user would not recognise.
    ``explicit`` says whether a scheme prefix was actually present: an explicit
    ``ticker:AAPL`` means "this is a ticker", so a caller that would otherwise fall
    back to a company-name search must not.
    """

    scheme: str
    value: str
    raw: str
    explicit: bool = False

    @property
    def is_ticker(self) -> bool:
        return self.scheme == TICKER

    @property
    def label(self) -> str:
        return LABELS[self.scheme]

    def describe(self) -> str:
        """How the identifier is named in user-facing prose."""
        if self.is_ticker:
            return f"ticker {self.value}"
        return f"{self.label} {self.value}"


def parse(text: str) -> Identifier:
    """Parse ``scheme:value``; anything else is a ticker (or, for ``ls``, a name).

    Raises :class:`IdentifierError` when a known scheme carries a value that cannot
    be an identifier of that kind -- a typo is worth reporting, because the
    alternative is a confident "no security found" for a security that is there.
    """
    raw = (text or "").strip()
    scheme_part, sep, value_part = raw.partition(":")
    scheme = _ALIASES.get(scheme_part.strip().lower()) if sep else None
    if scheme is None:
        return Identifier(TICKER, raw.upper(), raw, explicit=False)

    value = value_part.strip()
    if not value:
        raise IdentifierError(
            f"'{raw}' has no value after '{scheme_part.strip().lower()}:'"
        )
    return Identifier(scheme, _normalise(scheme, value, raw), raw, explicit=True)


def _normalise(scheme: str, value: str, raw: str) -> str:
    if scheme == TICKER:
        return value.upper()
    compact = _SEPARATORS.sub("", value).upper()
    if scheme == CIK:
        if not _DIGITS_RE.match(compact):
            raise IdentifierError(
                f"'{raw}' is not a CIK: expected digits, e.g. cik:51143"
            )
        # Zero-padding is presentation, not identity: the SEC writes 0000051143 and
        # people type 51143. Both normalise to the same key, and the warehouse side
        # strips the padding off the stored value the same way.
        return compact.lstrip("0") or "0"
    if scheme == ISIN:
        if not _ISIN_RE.match(compact):
            raise IdentifierError(
                f"'{raw}' is not an ISIN: expected 12 characters, "
                "2 letters then 9 alphanumerics then a check digit, "
                "e.g. isin:US4592001014"
            )
        return compact
    if scheme == CUSIP:
        if not _CUSIP_RE.match(compact):
            raise IdentifierError(
                f"'{raw}' is not a CUSIP: expected 8 or 9 alphanumeric characters, "
                "e.g. cusip:459200101"
            )
        return compact
    raise IdentifierError(f"Unknown identifier scheme '{scheme}'")  # pragma: no cover


def cusip_from_isin(isin: str) -> str | None:
    """The CUSIP embedded in a North American ISIN, or None.

    A US or Canadian ISIN is literally ``country + CUSIP + check digit``, which is
    what lets a CUSIP lookup still find a security whose ``cusip`` column the
    vendor left null but whose ``isin`` it filled in.
    """
    if len(isin) == 12 and isin[:2] in ("US", "CA"):
        return isin[2:11]
    return None
