"""Console tables: what duk prints by default when a person is reading.

``--csv`` and ``--json`` are for programs and stay byte-for-byte what they were. A
terminal gets this instead: aligned columns in a border, numbers right-aligned with
thousands separators, prices at the requested decimals (never floored to 0.00, the
rule :mod:`duk.format_utils` states), a missing value shown as a dash rather than
left blank, and a footer that says how many rows there are.

**When.** :func:`resolve_format` decides once per command. An explicit ``--table``,
``--csv`` or ``--json`` wins; then ``DUK_FORMAT`` or ``[general].console_format``;
then *auto*, which is a table when stdout is a terminal and CSV otherwise. So a
pipe, a redirect, ``$(duk ...)``, an agent's shell and every script that already
reads duk's CSV get exactly what they got before, without having to say so.

**Never a different number.** A table is a rendering of the same rows the CSV
carries. Text may be shortened with an ellipsis to fit the terminal; a number never
is -- a truncated number is a wrong number, so a table too wide for its numbers
overflows instead.

No dependency: pure functions over plain rows, so the layout is asserted in tests
without a terminal and duk installs nothing it did not before.
"""

from __future__ import annotations

import datetime as dt
import math
import numbers
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from duk.format_utils import DEFAULT_DECIMALS, DEFAULT_FALLBACK_SIG

FORMATS = ("auto", "table", "csv", "json")

#: Columns that carry a price per share (format_utils' list, and the screener's).
PRICE_COLUMNS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "vwap",
        "adj_close",
        "close_raw",
        "price",
        "last_close",
        "week52_low",
        "week52_high",
    }
)
#: Money amounts too large for cents to matter in a table: whole units, commas.
WHOLE_MONEY_COLUMNS = frozenset({"marketcap", "market_cap", "market_cap_usd"})
#: Cash amounts per share that are routinely quoted past the cent (a dividend of
#: 0.2425). Shown with at least two and at most six places, never rounded below
#: what the value holds.
AMOUNT_COLUMNS = frozenset({"dividend_amount", "dividend", "lastdiv", "amount"})
#: Whole-number columns that are counts, so they take thousands separators.
COUNT_COLUMNS = frozenset(
    {
        "volume",
        "last_volume",
        "security_count",
        "bar_count",
        "count",
        "n_observations",
    }
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _is_identifier_like(name: str) -> bool:
    """An integer that names something rather than counting it: no separators."""
    lowered = name.lower()
    return (
        lowered.endswith("_id")
        or lowered == "id"
        or lowered.endswith("year")
        or lowered.endswith("_no")
        or lowered in {"cik", "fiscal_year", "fiscal_quarter", "split_numerator"}
        or lowered in {"split_denominator", "page"}
    )


@dataclass(frozen=True)
class Style:
    """The characters a table is drawn with."""

    top: tuple[str, str, str, str]  # left, fill, join, right
    mid: tuple[str, str, str, str]
    bottom: tuple[str, str, str, str]
    vertical: str
    missing: str
    ellipsis: str


UNICODE = Style(
    top=("╭", "─", "┬", "╮"),
    mid=("├", "─", "┼", "┤"),
    bottom=("╰", "─", "┴", "╯"),
    vertical="│",
    missing="—",
    ellipsis="…",
)
ASCII = Style(
    top=("+", "-", "+", "+"),
    mid=("+", "-", "+", "+"),
    bottom=("+", "-", "+", "+"),
    vertical="|",
    missing="-",
    ellipsis="~",
)


def style_for_stream(stream: Any = None) -> Style:
    """Box-drawing characters where the stream can encode them, ASCII where not.

    A Windows code page (cp1252, cp437) cannot encode ``╭``, and a table that
    raises UnicodeEncodeError halfway down is worse than a plainer one.
    """
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "╭─┬╮│…—".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ASCII
    return UNICODE


# ---------------------------------------------------------------------------
# Choosing the format
# ---------------------------------------------------------------------------


class FormatError(ValueError):
    """More than one output format was asked for, or an unknown one configured."""


def requested_format(
    *,
    table: bool = False,
    csv: bool = False,
    json: bool = False,
    configured: Optional[str] = None,
) -> Optional[str]:
    """The format asked for -- by flag, ``DUK_FORMAT`` or ``configured``
    (``[general].console_format``), in that order -- or None for *auto*."""
    asked = [
        name for name, on in (("table", table), ("csv", csv), ("json", json)) if on
    ]
    if len(asked) > 1:
        raise FormatError("Only one of --table, --csv or --json can be specified")
    if asked:
        return asked[0]
    for origin, value in (
        ("DUK_FORMAT", os.environ.get("DUK_FORMAT", "")),
        ("[general].console_format", configured or ""),
    ):
        value = value.strip().lower()
        if not value:
            continue
        if value not in FORMATS:
            raise FormatError(
                f"{origin} is {value!r}; expected one of {', '.join(FORMATS)}"
            )
        return None if value == "auto" else value
    return None


def is_terminal(stream: Any = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def resolve_format(
    *,
    table: bool = False,
    csv: bool = False,
    json: bool = False,
    configured: Optional[str] = None,
    stream: Any = None,
) -> str:
    """``table``, ``csv`` or ``json`` for this invocation.

    Flags beat ``DUK_FORMAT``, which beats ``configured`` (``[general].
    console_format``); *auto* -- the default -- is a table on a terminal and CSV
    anywhere else, which keeps every pipe and script reading what it always read.
    """
    asked = requested_format(table=table, csv=csv, json=json, configured=configured)
    if asked is not None:
        return asked
    return "table" if is_terminal(stream) else "csv"


def terminal_width(stream: Any = None) -> Optional[int]:
    """The terminal's width, or None when stdout is not one (do not fit to it)."""
    if not is_terminal(stream):
        return None
    return shutil.get_terminal_size((100, 24)).columns


def color_enabled() -> bool:
    """Bold headers and dim borders, unless NO_COLOR asks otherwise
    (https://no-color.org). click strips the codes from a non-terminal anyway."""
    return not os.environ.get("NO_COLOR")


# ---------------------------------------------------------------------------
# Cell formatting
# ---------------------------------------------------------------------------


def humanize(name: str) -> str:
    """``market_cap_usd`` -> ``MARKET CAP USD``; ``companyName`` -> ``COMPANY NAME``."""
    return _CAMEL.sub(" ", str(name)).replace("_", " ").upper()


def _is_missing(value: Any) -> bool:
    """None, NaN, NaT or a NaN Decimal: the cell holds no value."""
    if value is None:
        return True
    if isinstance(value, Decimal):
        return value.is_nan()
    if isinstance(value, (str, bytes)):
        return False
    try:
        import pandas as pd

        result = pd.isna(value)
    except (TypeError, ValueError, ImportError):  # pragma: no cover
        return False
    return bool(result) if getattr(result, "ndim", 0) == 0 else False


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool) or type(value).__name__ == "bool_"


def _is_number(value: Any) -> bool:
    if _is_bool(value):
        return False
    return isinstance(value, (numbers.Real, Decimal))


def _as_date_text(value: Any) -> Optional[str]:
    """``YYYY-MM-DD`` for a date, or the full timestamp when it has a time of day."""
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0) and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        return value
    try:
        import pandas as pd

        if isinstance(value, pd.Timestamp):
            return _as_date_text(value.to_pydatetime())
    except ImportError:  # pragma: no cover
        pass
    return None


def _fixed(value: Any, decimals: int) -> str:
    """``value`` at ``decimals`` places with thousands separators, exactly for a
    Decimal (no float on the way), and never ``-0.00``."""
    if isinstance(value, Decimal):
        quantum = Decimal(1).scaleb(-decimals)
        text = f"{value.quantize(quantum):,f}"
    else:
        text = f"{float(value):,.{decimals}f}"
    return text[1:] if text.startswith("-") and set(text) <= set("-0.,") else text


def format_price(value: Any, decimals: int = DEFAULT_DECIMALS) -> str:
    """A price at ``decimals`` places with separators, keeping significant digits
    when the value is too small to survive them (format_utils' rule)."""
    number = float(value)
    if number == 0 or round(number, decimals) != 0:
        return _fixed(value, decimals)
    exponent = math.floor(math.log10(abs(number)))
    return _fixed(value, DEFAULT_FALLBACK_SIG - 1 - exponent)


def format_amount(value: Any, min_decimals: int = 2, max_decimals: int = 6) -> str:
    """A per-share cash amount: two places at least, the value's own digits up to
    six, so a 0.2425 dividend is not shown as 0.24."""
    if isinstance(value, Decimal):
        exponent = -value.normalize().as_tuple().exponent
    else:
        text = f"{float(value):.{max_decimals}f}".rstrip("0")
        exponent = len(text.partition(".")[2])
    return _fixed(value, max(min_decimals, min(max_decimals, int(exponent))))


def format_exact(value: Any) -> str:
    """A stored decimal with separators and every digit it holds (``ph --exact``)."""
    if isinstance(value, Decimal):
        return f"{value:,f}"
    if isinstance(value, numbers.Integral):
        return f"{int(value):,}"
    text = str(value)
    if re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", text):
        whole, _, frac = text.partition(".")
        sign = "-" if whole.startswith("-") else ""
        grouped = f"{int(whole.lstrip('-')):,}"
        return f"{sign}{grouped}.{frac}" if frac else f"{sign}{grouped}"
    return text


@dataclass
class Column:
    """How one column is rendered. ``kind`` is inferred unless given."""

    key: str
    header: Optional[str] = None
    kind: Optional[str] = (
        None  # text|date|price|money|amount|count|id|number|bool|exact
    )
    decimals: Optional[int] = None

    @property
    def title(self) -> str:
        return self.header if self.header is not None else humanize(self.key)

    @property
    def numeric(self) -> bool:
        return self.kind in {"price", "money", "amount", "count", "id", "number"} or (
            self.kind == "exact"
        )


def infer_kind(name: str, values: Iterable[Any]) -> str:
    """The rendering a column gets from its name, else from what it holds."""
    lowered = str(name).lower()
    present = [v for v in values if not _is_missing(v)]
    sample = present[0] if present else None
    if lowered == "date" or lowered.endswith("_date") or lowered.endswith("_at"):
        return "date" if sample is None or _as_date_text(sample) else "text"
    if sample is not None and _as_date_text(sample) is not None:
        return "date"
    if sample is None:
        return "text"
    if all(_is_bool(v) for v in present):
        return "bool"
    if not all(_is_number(v) for v in present):
        return "text"
    if lowered in PRICE_COLUMNS:
        return "price"
    if lowered in WHOLE_MONEY_COLUMNS:
        return "money"
    if lowered in AMOUNT_COLUMNS:
        return "amount"
    if _is_identifier_like(lowered):
        return "id"
    if lowered in COUNT_COLUMNS or all(
        isinstance(v, numbers.Integral) for v in present
    ):
        return "count"
    return "number"


def format_cell(value: Any, column: Column, style: Style) -> str:
    """One value as table text. A missing value is ``style.missing``, never blank:
    "no value" and "zero" must not look alike."""
    if _is_missing(value):
        return style.missing
    kind = column.kind or "text"
    if kind == "date":
        return _as_date_text(value) or str(value)
    if kind == "bool" or _is_bool(value):
        return "yes" if bool(value) else "no"
    if not _is_number(value):
        return str(value)
    if kind == "exact":
        return format_exact(value)
    if kind == "price":
        return format_price(
            value, DEFAULT_DECIMALS if column.decimals is None else column.decimals
        )
    if kind == "money":
        return _fixed(value, 0 if column.decimals is None else column.decimals)
    if kind == "amount":
        return format_amount(value)
    if kind == "count":
        return f"{int(value):,}"
    if kind == "id":
        return str(int(value)) if float(value).is_integer() else str(value)
    decimals = 4 if column.decimals is None else column.decimals
    return _fixed(value, decimals)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def display_width(text: str) -> int:
    """Columns ``text`` occupies: wide East Asian characters take two."""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
        if not unicodedata.combining(ch)
    )


def _truncate(text: str, width: int, ellipsis: str) -> str:
    if display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch) > width - 1:
            break
        out += ch
    return out + ellipsis


def _pad(text: str, width: int, right: bool) -> str:
    gap = " " * max(0, width - display_width(text))
    return gap + text if right else text + gap


@dataclass
class Table:
    """Rows to draw, and how. Build with :func:`table_from_rows`."""

    columns: list[Column]
    rows: list[list[str]]
    title: Optional[str] = None
    footer: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def table_from_rows(
    rows: Sequence[Mapping[str, Any]],
    columns: Optional[Sequence[Column | str]] = None,
    *,
    decimals: Optional[int] = None,
    price_decimals: Optional[int] = None,
    exact: bool = False,
    title: Optional[str] = None,
    footer: Optional[str] = None,
    style: Optional[Style] = None,
) -> Table:
    """Format ``rows`` (mappings) into a :class:`Table`.

    ``decimals`` sets the places of ordinary numbers, ``price_decimals`` those of
    price columns; ``exact`` prints every number with every digit it carries.
    """
    style = style or style_for_stream()
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    specs: list[Column] = []
    for spec in columns if columns is not None else keys:
        column = Column(spec) if isinstance(spec, str) else Column(**vars(spec))
        values = [row.get(column.key) for row in rows]
        if column.kind is None:
            column.kind = infer_kind(column.key, values)
            if exact and column.kind not in {"date", "text", "bool", "id"}:
                column.kind = "exact"
        if column.decimals is None:
            if column.kind == "price" and price_decimals is not None:
                column.decimals = price_decimals
            elif column.kind == "number" and decimals is not None:
                column.decimals = decimals
        specs.append(column)
    body = [[format_cell(row.get(c.key), c, style) for c in specs] for row in rows]
    return Table(columns=specs, rows=body, title=title, footer=footer)


def table_from_frame(frame: Any, **kwargs: Any) -> Table:
    """A DataFrame (index dropped) as a :class:`Table`."""
    records = frame.to_dict(orient="records")
    return table_from_rows(
        records, kwargs.pop("columns", None) or list(frame.columns), **kwargs
    )


def render(
    table: Table,
    *,
    style: Optional[Style] = None,
    max_width: Optional[int] = None,
    color: bool = False,
) -> str:
    """Draw ``table``. ``max_width`` shortens text columns to fit (numbers never);
    ``color`` makes the header bold and the borders dim."""
    style = style or style_for_stream()
    headers = [c.title for c in table.columns]
    widths = [
        max([display_width(h), *(display_width(r[i]) for r in table.rows)])
        for i, h in enumerate(headers)
    ]
    if max_width:
        _fit(widths, table.columns, max_width)

    def styled(text: str, **how: Any) -> str:
        if not color:
            return text
        import click

        return click.style(text, **how)

    def rule(parts: tuple[str, str, str, str]) -> str:
        left, fill, join, right = parts
        return styled(
            left + join.join(fill * (w + 2) for w in widths) + right, dim=True
        )

    bar = styled(style.vertical, dim=True)

    def line(cells: Sequence[str], header: bool = False) -> str:
        out = []
        for i, cell in enumerate(cells):
            text = _truncate(cell, widths[i], style.ellipsis)
            right = table.columns[i].numeric and not header
            if header and table.columns[i].numeric:
                right = True
            padded = _pad(text, widths[i], right)
            out.append(" " + (styled(padded, bold=True) if header else padded) + " ")
        return bar + bar.join(out) + bar

    lines: list[str] = []
    if table.title:
        lines.append(styled(table.title, bold=True))
    lines.append(rule(style.top))
    lines.append(line(headers, header=True))
    lines.append(rule(style.mid))
    lines.extend(line(row) for row in table.rows)
    lines.append(rule(style.bottom))
    if table.footer:
        lines.append(styled(table.footer, dim=True))
    lines.extend(table.notes)
    return "\n".join(lines)


def _fit(widths: list[int], columns: Sequence[Column], max_width: int) -> None:
    """Shrink the widest text columns, in place, until the table fits or every
    text column is down to its floor. Numeric and date columns keep their width."""
    floor = 8

    def total() -> int:
        return sum(widths) + 3 * len(widths) + 1

    shrinkable = [i for i, c in enumerate(columns) if c.kind in ("text", None)]
    while total() > max_width:
        candidates = [i for i in shrinkable if widths[i] > floor]
        if not candidates:
            return
        widest = max(candidates, key=lambda i: widths[i])
        widths[widest] -= 1


def render_key_values(
    pairs: Sequence[tuple[str, Any]],
    *,
    title: Optional[str] = None,
    footer: Optional[str] = None,
    decimals: Optional[int] = None,
    style: Optional[Style] = None,
    max_width: Optional[int] = None,
    color: bool = False,
) -> str:
    """A two-column FIELD / VALUE table (``fa``, one record seen one field a line).

    Each value is formatted by its own field's rule, since the rows are unlike."""
    style = style or style_for_stream()
    rows = []
    for key, value in pairs:
        column = Column(key, kind=infer_kind(key, [value]), decimals=None)
        if column.kind == "number" and decimals is not None:
            column.decimals = decimals
        rows.append([str(key), format_cell(value, column, style)])
    table = Table(
        columns=[Column("field", kind="text"), Column("value", kind="number")],
        rows=rows,
        title=title,
        footer=footer,
    )
    return render(table, style=style, max_width=max_width, color=color)


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    """``1 bar`` / ``2,046 bars``."""
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count:,} {word}"
