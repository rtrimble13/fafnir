"""
Display formatting for price series.

Prices are rendered at a fixed number of decimals (``##.00``) so a column reads
like money. The complication is the low end: duk serves back-adjusted history, and
a long split record drives old prices towards zero -- WMT's 1972 adjusted close is
about $0.0115, and a security that was quoted in sub-pennies before a large split
goes lower still. Rounding those to 2dp reports a real trade as ``0.00``.

So the rule here is: use the requested decimals, but never render a non-zero price
as zero. A value too small to survive the rounding falls back to a fixed number of
significant digits instead.

Formatting is a presentation concern and lives here rather than in the warehouse:
``mart.v_daily_price_adjusted`` is a shared contract read by fafnir and by SQL
clients, the db and live paths have to stay byte-identical, and duk casts prices to
float on the way in -- which would erase any scale the database applied anyway.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import pandas as pd

# Columns carrying a price. Volume is a share count (never formatted as money) and
# the date index is not numeric, so both are left alone.
PRICE_COLUMNS = ("open", "high", "low", "close", "vwap", "adj_close", "close_raw")

DEFAULT_DECIMALS = 2

# Significant digits kept when a price is too small for DEFAULT_DECIMALS. Four
# digits keeps a sub-penny quote legible (0.0003123) without printing float noise.
DEFAULT_FALLBACK_SIG = 4


def _is_number(value: Any) -> bool:
    """True for a real, finite number (rejects NaN/inf, bools and non-numerics)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _fallback_decimals(value: float, fallback_sig: int) -> int:
    """Decimals needed to show `fallback_sig` significant digits of `value`."""
    exponent = math.floor(math.log10(abs(value)))
    return fallback_sig - 1 - exponent


def round_price(
    value: Any,
    decimals: int = DEFAULT_DECIMALS,
    fallback_sig: int = DEFAULT_FALLBACK_SIG,
) -> Any:
    """Round a price, keeping significant digits rather than collapsing to zero.

    Returns the value unchanged when it is not a finite number, so NaN gaps and
    non-numeric cells pass through untouched.
    """
    if not _is_number(value):
        return value
    if value == 0 or round(value, decimals) != 0:
        return round(value, decimals)
    return round(value, _fallback_decimals(value, fallback_sig))


def format_price(
    value: Any,
    decimals: int = DEFAULT_DECIMALS,
    fallback_sig: int = DEFAULT_FALLBACK_SIG,
) -> str:
    """Render a price as fixed-decimal text, never flooring a non-zero to zero.

    Args:
        value: The price. Non-finite values (NaN gaps) render as an empty field.
        decimals: Decimal places for prices large enough to survive them.
        fallback_sig: Significant digits kept for prices that are not.

    Returns:
        The formatted string, with trailing zeros preserved (``250.00``).

    Examples:
        >>> format_price(250.0)
        '250.00'
        >>> format_price(0.2602372277)
        '0.26'
        >>> format_price(0.0003123)
        '0.0003123'
    """
    if not _is_number(value):
        return ""
    if value == 0 or round(value, decimals) != 0:
        return f"{value:.{decimals}f}"
    return f"{value:.{_fallback_decimals(value, fallback_sig)}f}"


def price_columns_in(
    df: pd.DataFrame, columns: Optional[Sequence[str]] = None
) -> list[str]:
    """The price-bearing columns present in `df` (case-insensitive)."""
    names = PRICE_COLUMNS if columns is None else columns
    wanted = {name.lower() for name in names}
    return [col for col in df.columns if str(col).lower() in wanted]


def format_price_columns(
    df: pd.DataFrame,
    decimals: int = DEFAULT_DECIMALS,
    fallback_sig: int = DEFAULT_FALLBACK_SIG,
    columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Copy of `df` with price columns rendered as fixed-decimal strings.

    For text output (CSV, console). Trailing zeros only survive as text, so the
    columns become object dtype -- callers must not compute on the result.
    """
    out = df.copy()
    for col in price_columns_in(out, columns):
        out[col] = out[col].map(
            lambda v: format_price(v, decimals=decimals, fallback_sig=fallback_sig)
        )
    return out


def round_price_columns(
    df: pd.DataFrame,
    decimals: int = DEFAULT_DECIMALS,
    fallback_sig: int = DEFAULT_FALLBACK_SIG,
    columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Copy of `df` with price columns rounded but still numeric.

    For JSON output, where a number must stay a number and trailing zeros carry no
    meaning. Applies the same no-flooring rule as :func:`format_price_columns`.
    """
    out = df.copy()
    for col in price_columns_in(out, columns):
        out[col] = out[col].map(
            lambda v: round_price(v, decimals=decimals, fallback_sig=fallback_sig)
        )
    return out
