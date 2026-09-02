"""
Assembly and rendering for the per-company summary (`duk ls <company>`).

Pure functions over the dicts :func:`duk.datasource.db.company_summary` returns:
no SQL, no click, no I/O. That is what makes the whole report testable without a
database, which is how the rest of duk's compute modules are built --
``duk.indicators``, ``duk.return_utils`` and ``duk.stats`` all work the same way.

Prices are rendered through :mod:`duk.format_utils`, so a back-adjusted sub-penny
price keeps significant digits instead of printing ``0.00``, exactly as ``ph``
does. Every missing value renders as ``-``: a summary of a warehouse is largely a
report about what is and is not held, so "no data" has to be visible rather than
blank.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from duk.format_utils import DEFAULT_DECIMALS, round_price

MISSING = "-"

# Trailing windows reported, in calendar days. Calendar rather than trading days
# because the label is a calendar promise: "1Y" means a year ago, and the series
# is asof-ed to the closest bar at or before that date.
_WINDOWS = (("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365))

# Trading days per year, for annualising daily volatility. 252 matches
# duk.return_utils.annualized_return's default, so the two agree.
_TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Scalar formatting
# ---------------------------------------------------------------------------


def _is_num(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    return isinstance(value, (int, float)) and math.isfinite(value)


def to_float(value: Any) -> Optional[float]:
    """Decimal/int -> float for JSON and arithmetic; None for anything else.

    The warehouse returns money as exact ``Decimal`` and json.dumps cannot encode
    it. Converting once, here, keeps the rest of the module in plain floats.
    """
    return float(value) if _is_num(value) else None


def fmt_big(value: Any, *, decimals: int = 2) -> str:
    """A large count or amount as ``3.42T`` / ``54.2M``.

    Market caps and share volumes are the two numbers in this report nobody reads
    digit by digit; a magnitude suffix is what the eye actually wants.
    """
    number = to_float(value)
    if number is None:
        return MISSING
    sign = "-" if number < 0 else ""
    number = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if number >= limit:
            return f"{sign}{number / limit:,.{decimals}f}{suffix}"
    return f"{sign}{number:,.0f}"


def fmt_pct(value: Any, *, decimals: int = 1, signed: bool = False) -> str:
    """A fraction as a percentage. ``signed`` puts an explicit + on gains."""
    number = to_float(value)
    if number is None:
        return MISSING
    return f"{number * 100:{'+' if signed else ''}.{decimals}f}%"


def fmt_price(value: Any, decimals: int = DEFAULT_DECIMALS) -> str:
    """A price at the usual precision, never collapsing a real trade to 0.00."""
    number = to_float(value)
    if number is None:
        return MISSING
    rounded = round_price(number, decimals)
    text = f"{rounded:,.{decimals}f}"
    # round_price keeps significant digits for sub-penny values; formatting at
    # `decimals` would throw them away again, so render those as-is.
    return text if float(text.replace(",", "")) == rounded else f"{rounded:,}"


def fmt_date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return MISSING if value is None else str(value)


def fmt_int(value: Any) -> str:
    number = to_float(value)
    return MISSING if number is None else f"{int(number):,}"


def fmt_ratio(numerator: Any, denominator: Any) -> str:
    """A split as ``4-for-1``."""
    num, den = to_float(numerator), to_float(denominator)
    if num is None or den is None:
        return MISSING
    trim = lambda x: f"{x:g}"  # noqa: E731 -- one-liner, used twice below
    return f"{trim(num)}-for-{trim(den)}"


# ---------------------------------------------------------------------------
# Derived statistics
# ---------------------------------------------------------------------------


def _asof(series: pd.Series, when: dt.date) -> Optional[float]:
    """The last observation at or before `when`; None when the series starts later.

    Returning None rather than the first available price is deliberate: a security
    with eight months of history has no 1-year return, and inventing one from its
    IPO price would report a number that means something else entirely.
    """
    if series.empty:
        return None
    upto = series.loc[: pd.Timestamp(when)]
    return float(upto.iloc[-1]) if len(upto) else None


def trailing_returns(closes: pd.Series, asof: dt.date) -> dict[str, Optional[float]]:
    """Simple returns over each trailing window, plus year-to-date."""
    out: dict[str, Optional[float]] = {}
    latest = float(closes.iloc[-1]) if len(closes) else None
    windows = list(_WINDOWS) + [("YTD", None)]
    for label, days in windows:
        start_date = (
            dt.date(asof.year, 1, 1) - dt.timedelta(days=1)
            if days is None
            else asof - dt.timedelta(days=days)
        )
        base = _asof(closes, start_date)
        out[label] = (
            (latest / base) - 1.0
            if latest is not None and base not in (None, 0)
            else None
        )
    return out


def annualised_volatility(closes: pd.Series) -> Optional[float]:
    """Stdev of daily log returns, annualised. None below two returns."""
    if len(closes) < 3:
        return None
    import numpy as np

    rets = np.diff(np.log(closes.to_numpy(dtype=float)))
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2:
        return None
    return float(rets.std(ddof=1) * math.sqrt(_TRADING_DAYS))


def max_drawdown(closes: pd.Series) -> Optional[float]:
    """Largest peak-to-trough decline, as a negative fraction."""
    if closes.empty:
        return None
    values = closes.astype(float)
    drawdown = values / values.cummax() - 1.0
    worst = float(drawdown.min())
    return worst if math.isfinite(worst) else None


def build_summary(raw: dict) -> dict:
    """Turn the datasource's raw facts into the report's own shape.

    The output of this function IS the `--json` contract, so its keys are a public
    surface: `meta`, `prices`, `actions`, `fundamentals`, `dq`. Every value is
    JSON-encodable -- Decimals are floats and dates are ISO strings by the time a
    caller sees them.
    """
    profile = raw.get("profile") or {}
    coverage = raw.get("coverage") or {}
    actions = raw.get("actions") or {}
    last_bar = raw.get("last_bar") or {}
    closes = raw.get("adjusted_prices")
    closes = (
        closes["close"]
        if isinstance(closes, pd.DataFrame) and "close" in closes
        else pd.Series(dtype=float)
    )

    asof = last_bar.get("trade_date")
    window = (
        closes.loc[pd.Timestamp(asof) - pd.Timedelta(days=365) :]
        if len(closes) and asof
        else closes
    )

    meta = {
        "security_id": profile.get("security_id"),
        "symbol": profile.get("symbol"),
        "company_name": profile.get("company_name"),
        "asset_type": profile.get("asset_type"),
        "exchange_code": profile.get("exchange_code"),
        "exchange_name": profile.get("exchange_name"),
        "sector": profile.get("sector_name"),
        "industry": profile.get("industry_name"),
        "currency": profile.get("currency"),
        "country": profile.get("country"),
        "cik": profile.get("cik"),
        "isin": profile.get("isin"),
        "cusip": profile.get("cusip"),
        "is_actively_trading": profile.get("is_actively_trading"),
        "is_etf": profile.get("is_etf"),
        "is_fund": profile.get("is_fund"),
        "ipo_date": (
            fmt_date(profile.get("ipo_date")) if profile.get("ipo_date") else None
        ),
        "delisted_date": (
            fmt_date(profile.get("delisted_date"))
            if profile.get("delisted_date")
            else None
        ),
        "market_cap_usd": to_float(profile.get("market_cap_usd")),
        "beta": to_float(profile.get("beta")),
        "profile_updated_at": fmt_date(profile.get("updated_at")),
        "matched_former_symbol": profile.get("matched_former_symbol"),
        "matched_former_valid_to": (
            fmt_date(profile.get("matched_former_valid_to"))
            if profile.get("matched_former_valid_to")
            else None
        ),
    }

    prices = {
        "first_trade_date": (
            fmt_date(coverage.get("first_trade_date")) if coverage else None
        ),
        "last_trade_date": (
            fmt_date(coverage.get("last_trade_date")) if coverage else None
        ),
        "bar_count": coverage.get("bar_count"),
        "distinct_years": coverage.get("distinct_years"),
        "zero_volume_bars": coverage.get("zero_volume_bars"),
        "last_close": to_float(last_bar.get("close")),
        "last_volume": to_float(last_bar.get("volume")),
        "week52_low": to_float(window.min()) if len(window) else None,
        "week52_high": to_float(window.max()) if len(window) else None,
        "returns": trailing_returns(closes, asof) if len(closes) and asof else {},
        "annualised_volatility": annualised_volatility(window),
        "max_drawdown": max_drawdown(window),
    }

    ttm = to_float(actions.get("ttm_dividend_amount")) if actions else None
    last_close = prices["last_close"]
    corporate = {
        "split_count": actions.get("split_count") if actions else 0,
        "last_split_date": (
            fmt_date(actions.get("last_split_date")) if actions else None
        ),
        "last_split_numerator": (
            to_float(actions.get("last_split_numerator")) if actions else None
        ),
        "last_split_denominator": (
            to_float(actions.get("last_split_denominator")) if actions else None
        ),
        "dividend_count": actions.get("dividend_count") if actions else 0,
        "last_dividend_date": (
            fmt_date(actions.get("last_dividend_date")) if actions else None
        ),
        "last_dividend_amount": (
            to_float(actions.get("last_dividend_amount")) if actions else None
        ),
        "ttm_dividend_amount": ttm,
        # Trailing yield, not forward: the numerator is what was actually paid.
        "trailing_dividend_yield": (
            ttm / last_close if ttm is not None and last_close else None
        ),
        "adjustment_factor_rows": (
            actions.get("adjustment_factor_rows") if actions else 0
        ),
        "latest_factor_effective_date": (
            fmt_date(actions.get("latest_factor_effective_date")) if actions else None
        ),
    }

    fundamentals = raw.get("fundamentals")
    if fundamentals is not None:
        fundamentals = {
            k: (
                fmt_date(v)
                if isinstance(v, (dt.date, dt.datetime))
                else to_float(v) if _is_num(v) else v
            )
            for k, v in fundamentals.items()
        }
        fundamentals["ratios"] = fundamental_ratios(
            fundamentals, meta.get("market_cap_usd")
        )

    return {
        "meta": meta,
        "prices": prices,
        "actions": corporate,
        "fundamentals": fundamentals,
        "dq": _dq_groups(raw.get("dq_flags") or []),
    }


def fundamental_ratios(
    fundamentals: Optional[dict], market_cap: Optional[float]
) -> dict[str, Optional[float]]:
    """Valuation and profitability ratios, derived rather than stored.

    Vendor-computed ratios are deliberately not kept in the warehouse, so the
    number printed here is always arithmetic on the statement shown beside it --
    a reader can check it. Every ratio is computed only when its inputs are
    present and its denominator is non-zero; a missing input yields None, not a
    zero that would read as a real measurement.

    Defensive about column names on purpose: the fundamentals milestone has not
    landed, so this runs against a contract (doc/plans/duk-company-summary.md)
    rather than a table. An absent column simply drops its ratio.
    """
    if not fundamentals:
        return {}

    def num(key: str) -> Optional[float]:
        return to_float(fundamentals.get(key))

    def ratio(top: Optional[float], bottom: Optional[float]) -> Optional[float]:
        if top is None or bottom in (None, 0):
            return None
        return top / bottom

    revenue, net_income = num("revenue"), num("net_income")
    equity = num("total_equity")

    # A quarterly statement annualises x4 for the income-based multiples; without
    # it, P/E on a single quarter overstates by roughly four. Flagged in the
    # output, because an annualised quarter is not a filed TTM figure.
    periods = (
        4 if str(fundamentals.get("period", "")).lower().startswith("quarter") else 1
    )
    ttm_income = net_income * periods if net_income is not None else None
    ttm_revenue = revenue * periods if revenue is not None else None

    return {
        "price_earnings": ratio(market_cap, ttm_income),
        "price_sales": ratio(market_cap, ttm_revenue),
        "price_book": ratio(market_cap, equity),
        "net_margin": ratio(net_income, revenue),
        "return_on_equity": ratio(ttm_income, equity),
        "annualised": periods > 1,
    }


def _dq_groups(flags: Sequence[dict]) -> list[dict]:
    """Group open flags by (check, severity), keeping the offending keys.

    Grouped here rather than in SQL because mart.v_security_dq_open deliberately
    returns one row per flag -- naming the bar is what makes the section
    actionable, and the caller is the only place that knows how many keys fit.
    """
    grouped: dict[tuple, dict] = {}
    for flag in flags:
        key = (flag.get("check_name"), flag.get("severity"))
        entry = grouped.setdefault(
            key,
            {
                "check_name": key[0],
                "severity": key[1],
                "flags": 0,
                "first_detected": None,
                "last_detected": None,
                "keys": [],
            },
        )
        entry["flags"] += 1
        seen = flag.get("detected_at")
        if seen is not None:
            stamp = fmt_date(seen)
            if entry["first_detected"] is None or stamp < entry["first_detected"]:
                entry["first_detected"] = stamp
            if entry["last_detected"] is None or stamp > entry["last_detected"]:
                entry["last_detected"] = stamp
        record_key = flag.get("record_key") or {}
        if isinstance(record_key, dict):
            # The key is a date for every price check; fall back to the whole
            # object so an unfamiliar check still shows something identifying.
            label = (
                record_key.get("trade_date")
                or record_key.get("date")
                or record_key.get("ex_date")
                or record_key.get("last_date")
                or record_key.get("effective_date")
                or ", ".join(f"{k}={v}" for k, v in record_key.items())
            )
            if label:
                entry["keys"].append(str(label))
    return sorted(
        grouped.values(), key=lambda e: (e["check_name"] or "", e["severity"] or "")
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _terminal_width(default: int = 100) -> int:
    """Width to render into; falls back when there is no tty (pipes, cron)."""
    import shutil

    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except OSError:
        return default


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], right=()
) -> list[str]:
    """A plain column-aligned table, trimmed to the terminal.

    Written here rather than imported: `src/duk` does not depend on `fafnir`, and
    the equivalent helper in `fafnir/cli.py` is private to it.
    """
    if not rows:
        return []
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))
    ]
    limit = _terminal_width()

    def line(cells: Sequence[str]) -> str:
        out = [
            cells[i].rjust(widths[i]) if i in right else cells[i].ljust(widths[i])
            for i in range(len(cells))
        ]
        text = "  ".join(out).rstrip()
        return text if len(text) <= limit else text[: limit - 3] + "..."

    return [line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)]


def _pair(label: str, value: str, width: int = 13) -> str:
    return f"{label.ljust(width)}: {value}"


def _wrap(text: str, *, indent: str = "") -> list[str]:
    """A prose line folded to the terminal rather than run off it."""
    import textwrap

    return textwrap.wrap(
        text,
        width=min(_terminal_width(), 88),
        initial_indent=indent,
        subsequent_indent=indent + "      ",
    )


def render_text(summary: dict) -> str:
    """The sectioned report. Sections are always present, even when empty --
    "no section" and "nothing to report" are different claims about a warehouse."""
    meta, prices = summary["meta"], summary["prices"]
    actions, dq = summary["actions"], summary["dq"]
    lines: list[str] = []

    title = f"{meta.get('symbol') or MISSING}  {meta.get('company_name') or MISSING}"
    venue = " · ".join(
        p
        for p in (
            meta.get("exchange_code"),
            meta.get("country"),
            meta.get("currency"),
        )
        if p
    )
    width = min(_terminal_width(), 88)
    pad = max(1, width - len(title) - len(venue))
    lines.append(f"{title}{' ' * pad}{venue}")
    lines.append("-" * width)

    if meta.get("matched_former_symbol"):
        lines.append(
            f"Matched a former ticker: {meta['symbol']} traded as "
            f"{meta['matched_former_symbol']} until "
            f"{meta.get('matched_former_valid_to') or MISSING}."
        )
        lines.append("")

    lines.append(_pair("Sector", meta.get("sector") or MISSING))
    lines.append(_pair("Industry", meta.get("industry") or MISSING))
    lines.append(_pair("Market cap", fmt_big(meta.get("market_cap_usd"))))
    lines.append(
        _pair(
            "Beta", f"{meta['beta']:.2f}" if meta.get("beta") is not None else MISSING
        )
    )
    status = "actively trading" if meta.get("is_actively_trading") else "not trading"
    if meta.get("delisted_date"):
        status += f" (delisted {meta['delisted_date']})"
    for kind in ("etf", "fund"):
        if meta.get(f"is_{kind}"):
            status += f" · {kind}"
    lines.append(_pair("Status", status))
    lines.append(_pair("IPO", meta.get("ipo_date") or MISSING))
    ids = " · ".join(
        f"{label} {meta[key]}"
        for label, key in (("CIK", "cik"), ("ISIN", "isin"), ("CUSIP", "cusip"))
        if meta.get(key)
    )
    lines.append(_pair("Identifiers", ids or MISSING))
    lines.append(
        _pair("Profile as of", meta.get("profile_updated_at") or MISSING)
        + "  (market cap and beta refresh with the security master)"
    )
    lines.append("")

    lines.append("PRICE HISTORY (raw bars; statistics on the adjusted series)")
    if not prices.get("bar_count"):
        lines.append("  No price history loaded for this security.")
    else:
        lines.append(
            "  "
            + _pair(
                "Coverage",
                f"{prices['first_trade_date']} .. {prices['last_trade_date']}   "
                f"{fmt_int(prices['bar_count'])} bars over "
                f"{fmt_int(prices['distinct_years'])} years",
            )
        )
        lines.append(
            "  "
            + _pair(
                "Last close",
                f"{fmt_price(prices['last_close'])} on {prices['last_trade_date']}"
                f"       volume {fmt_big(prices['last_volume'], decimals=1)}",
            )
        )
        lines.append(
            "  "
            + _pair(
                "52w range",
                f"{fmt_price(prices['week52_low'])} .. "
                f"{fmt_price(prices['week52_high'])}  (adjusted)",
            )
        )
        returns = prices.get("returns") or {}
        if returns:
            rendered = "   ".join(
                f"{label} {fmt_pct(returns.get(label), signed=True)}"
                for label in ("1M", "3M", "6M", "YTD", "1Y")
                if label in returns
            )
            lines.append("  " + _pair("Returns", rendered))
        lines.append(
            "  "
            + _pair("Ann. vol", f"{fmt_pct(prices.get('annualised_volatility'))} (1y)")
            + f"          Max drawdown (1y): {fmt_pct(prices.get('max_drawdown'))}"
        )
        if prices.get("zero_volume_bars"):
            count = int(prices["zero_volume_bars"])
            lines.append(
                "  "
                + _pair(
                    "Gaps",
                    f"{count:,} {'bar' if count == 1 else 'bars'} with zero volume",
                )
            )
    lines.append("")

    lines.append("CORPORATE ACTIONS")
    if not (actions.get("split_count") or actions.get("dividend_count")):
        lines.append("  No splits or dividends recorded.")
    else:
        split_line = f"{fmt_int(actions['split_count'])}"
        if actions.get("last_split_date"):
            ratio = fmt_ratio(
                actions.get("last_split_numerator"),
                actions.get("last_split_denominator"),
            )
            split_line += f"    last {ratio} on {actions['last_split_date']}"
        lines.append("  " + _pair("Splits", split_line))

        dividend_line = f"{fmt_int(actions['dividend_count'])}"
        if actions.get("last_dividend_date"):
            dividend_line += (
                f"    last {fmt_price(actions['last_dividend_amount'], 6)}"
                f" ex {actions['last_dividend_date']}"
                f"   TTM {fmt_price(actions['ttm_dividend_amount'], 6)}"
            )
            yield_ = actions.get("trailing_dividend_yield")
            if yield_ is not None:
                dividend_line += f" ({fmt_pct(yield_, decimals=2)} yield)"
        lines.append("  " + _pair("Dividends", dividend_line))
    lines.append(
        "  "
        + _pair(
            "Adjustment",
            f"{fmt_int(actions.get('adjustment_factor_rows'))} factor rows"
            + (
                f", latest ex-boundary {actions['latest_factor_effective_date']}"
                if actions.get("latest_factor_effective_date")
                else ""
            ),
        )
    )
    lines.append("")

    lines.append("FUNDAMENTALS")
    if summary.get("fundamentals") is None:
        lines.append(
            "  Not loaded — the fundamentals milestone is planned (doc/extending.md)."
        )
    else:
        fundamentals = summary["fundamentals"]
        ratios = fundamentals.get("ratios") or {}
        for key, value in fundamentals.items():
            if key in ("security_id", "ratios"):
                continue
            lines.append("  " + _pair(str(key), _scalar(value), width=24))
        rendered = "   ".join(
            f"{label} {value:.1f}" if value is not None else f"{label} {MISSING}"
            for label, value in (
                ("P/E", ratios.get("price_earnings")),
                ("P/S", ratios.get("price_sales")),
                ("P/B", ratios.get("price_book")),
            )
        )
        if rendered:
            lines.append("  " + _pair("Valuation", rendered, width=24))
        lines.append(
            "  "
            + _pair(
                "Profitability",
                f"net margin {fmt_pct(ratios.get('net_margin'))}   "
                f"ROE {fmt_pct(ratios.get('return_on_equity'))}",
                width=24,
            )
        )
        if ratios.get("annualised"):
            lines += _wrap(
                "Multiples annualise the quarter x4; they are not a filed TTM "
                "figure.",
                indent="  ",
            )
    lines.append("")

    lines.append("DATA QUALITY (open flags)")
    if not dq:
        lines.append("  No open DQ flags.")
    else:
        rows = [
            [
                g["check_name"] or MISSING,
                g["severity"] or MISSING,
                str(g["flags"]),
                g["first_detected"] or MISSING,
                g["last_detected"] or MISSING,
                _join_keys(g["keys"]),
            ]
            for g in dq
        ]
        lines += [
            "  " + line
            for line in _table(
                ["CHECK", "SEV", "FLAGS", "FIRST SEEN", "LAST SEEN", "KEYS"],
                rows,
                right={2},
            )
        ]
        if any((g["check_name"] or "").startswith("price_") for g in dq):
            # The same caveat `fafnir dq list` carries. Without it the count reads
            # as a count of distinct problems, which for price_* it is not.
            lines += _wrap(
                "Note: price_* flags repeat per re-detection by design, so their "
                "count is not a count of distinct problems.",
                indent="  ",
            )
        lines.append(
            f"  Detail: fafnir dq list --detail --symbol {meta.get('symbol') or ''}".rstrip()
        )
    return "\n".join(lines)


def _join_keys(keys: Iterable[str], limit: int = 3) -> str:
    keys = list(keys)
    if not keys:
        return MISSING
    shown = ", ".join(keys[:limit])
    return shown if len(keys) <= limit else f"{shown}, +{len(keys) - limit} more"


def _scalar(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, (dt.date, dt.datetime)):
        return fmt_date(value)
    if _is_num(value):
        number = to_float(value)
        return fmt_big(number) if abs(number) >= 1e6 else f"{number:,.4g}"
    return str(value)


def render_flat(summary: dict) -> dict:
    """One flat row of scalars, for `--csv`.

    The DQ per-check breakdown does not survive flattening -- it is a table, not a
    field -- so it collapses to a count, and the text or `--json` output remains
    the way to see which checks fired.
    """
    meta, prices, actions = summary["meta"], summary["prices"], summary["actions"]
    returns = prices.get("returns") or {}
    flat = {
        "symbol": meta.get("symbol"),
        "company_name": meta.get("company_name"),
        "exchange_code": meta.get("exchange_code"),
        "sector": meta.get("sector"),
        "industry": meta.get("industry"),
        "currency": meta.get("currency"),
        "country": meta.get("country"),
        "market_cap_usd": meta.get("market_cap_usd"),
        "beta": meta.get("beta"),
        "is_actively_trading": meta.get("is_actively_trading"),
        "ipo_date": meta.get("ipo_date"),
        "delisted_date": meta.get("delisted_date"),
        "first_trade_date": prices.get("first_trade_date"),
        "last_trade_date": prices.get("last_trade_date"),
        "bar_count": prices.get("bar_count"),
        "last_close": prices.get("last_close"),
        "last_volume": prices.get("last_volume"),
        "week52_low": prices.get("week52_low"),
        "week52_high": prices.get("week52_high"),
        "annualised_volatility": prices.get("annualised_volatility"),
        "max_drawdown": prices.get("max_drawdown"),
        "split_count": actions.get("split_count"),
        "last_split_date": actions.get("last_split_date"),
        "dividend_count": actions.get("dividend_count"),
        "ttm_dividend_amount": actions.get("ttm_dividend_amount"),
        "trailing_dividend_yield": actions.get("trailing_dividend_yield"),
        "open_dq_flags": sum(g["flags"] for g in summary.get("dq") or []),
    }
    for label in ("1M", "3M", "6M", "YTD", "1Y"):
        flat[f"return_{label.lower()}"] = returns.get(label)
    return flat


def render_candidates(candidates: Sequence[dict]) -> str:
    """The did-you-mean table shown when a name matches several companies."""
    rows = [
        [
            c.get("symbol") or MISSING,
            c.get("company_name") or MISSING,
            c.get("exchange_code") or MISSING,
            "active" if c.get("is_actively_trading") else "delisted",
        ]
        for c in candidates
    ]
    return "\n".join(_table(["SYMBOL", "NAME", "EXCHANGE", "STATUS"], rows))
