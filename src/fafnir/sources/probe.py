"""
Live verification of FMP's price payloads.

Two questions this answers against a real API key, neither of which can be settled
from documentation:

1. **What are the OHLC field names?** The unadjusted endpoint labels them
   ``adjOpen``/``adjHigh``/``adjLow``/``adjClose`` rather than ``open``/``close``.
   The loader accepts both, but if FMP ever ships a third spelling every bar would
   quarantine, so the actual keys are worth seeing.

2. **Is the feed really unadjusted?** This is the one that matters, and field names
   do not answer it -- a payload can be named ``close`` and still be split-adjusted,
   which is exactly the bug in doc/adr/0004-unadjusted-price-feed.md. The test is
   arithmetic: pick a date far enough back to sit behind a known split, then check

       unadjusted_close  ==  split_adjusted_close x cumulative_split_ratio

   For AAPL on 1990-01-02 that is ~$39.20 == ~$0.35 x 112. If the two endpoints
   agree instead of differing by the split ratio, the "unadjusted" feed is not
   unadjusted and ingestion must stop.

Costs 3 requests (two price windows + splits) and writes nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fafnir.ingest.daily_price import _OHLC_ALIASES, _validate_bar
from fafnir.logging_config import get_logger

logger = get_logger("source.probe")

# Far enough back to sit behind several splits for a long-lived symbol, and a real
# trading day. Callers can override both.
DEFAULT_SYMBOL = "AAPL"
DEFAULT_DATE = date(1990, 1, 2)

# How close the two feeds must agree, relatively, to call the ratio a match.
TOLERANCE = Decimal("0.02")


def _close_of(bar: dict) -> Optional[Decimal]:
    """The bar's close under whichever spelling it uses."""
    for key in _OHLC_ALIASES["close"]:
        value = bar.get(key)
        if value not in (None, ""):
            try:
                return Decimal(str(value))
            except (ArithmeticError, ValueError):
                return None
    return None


def _bar_on_or_after(bars: list[dict], target: date) -> Optional[dict]:
    """First bar dated on/after ``target`` -- the date itself may be a holiday."""
    dated = []
    for bar in bars:
        try:
            when = datetime.strptime(str(bar.get("date"))[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if when >= target:
            dated.append((when, bar))
    return min(dated)[1] if dated else None


def cumulative_split_ratio(splits: list[dict], after: date) -> Decimal:
    """Product of num/den for every split with an ex-date strictly after ``after``.

    This is how much a pre-split price shrinks when restated in today's shares:
    AAPL's 2:1, 2:1, 7:1 and 4:1 since 1990 multiply to 112.
    """
    ratio = Decimal(1)
    for rec in splits:
        try:
            ex_date = datetime.strptime(str(rec.get("date"))[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if ex_date <= after:
            continue
        num = rec.get("numerator", rec.get("splitTo"))
        den = rec.get("denominator", rec.get("splitFrom"))
        try:
            num_d, den_d = Decimal(str(num)), Decimal(str(den))
        except (ArithmeticError, ValueError, TypeError):
            continue
        if num_d > 0 and den_d > 0:
            ratio *= num_d / den_d
    return ratio


def probe_prices(
    fmp,
    symbol: str = DEFAULT_SYMBOL,
    on_date: date = DEFAULT_DATE,
    window_days: int = 10,
) -> dict[str, Any]:
    """Compare the unadjusted and split-adjusted feeds for one symbol/date.

    Returns a report dict; ``verdict`` is one of:
      ``unadjusted_confirmed``  -- the feeds differ by exactly the split ratio.
      ``feeds_agree``           -- both returned the same price despite a split;
                                   the unadjusted endpoint is NOT unadjusted.
      ``ratio_mismatch``        -- they differ, but not by the split ratio.
      ``inconclusive``          -- no splits after this date (try an older one), or
                                   a feed returned nothing.
    """
    to_date = (on_date + timedelta(days=window_days)).isoformat()
    from_date = on_date.isoformat()

    raw_bars = fmp.eod_raw(symbol, from_date=from_date, to_date=to_date)
    adj_bars = fmp.eod_split_adjusted(symbol, from_date=from_date, to_date=to_date)
    splits = fmp.splits(symbol)

    raw_bar = _bar_on_or_after(raw_bars, on_date)
    adj_bar = _bar_on_or_after(adj_bars, on_date)

    report: dict[str, Any] = {
        "symbol": symbol,
        "date": on_date,
        "unadjusted_fields": sorted(raw_bar) if raw_bar else [],
        "split_adjusted_fields": sorted(adj_bar) if adj_bar else [],
        "ohlc_spelling": None,
        "loader_accepts": False,
        "quarantine_reason": None,
        "unadjusted_close": _close_of(raw_bar) if raw_bar else None,
        "split_adjusted_close": _close_of(adj_bar) if adj_bar else None,
        "split_ratio": cumulative_split_ratio(splits, on_date),
        "implied_ratio": None,
        "verdict": "inconclusive",
        "detail": "",
    }

    if raw_bar:
        # Which spelling did the unadjusted payload actually use, and would the
        # ingestion boundary accept the bar as-is?
        plain = [f for f in _OHLC_ALIASES if f in raw_bar]
        prefixed = [f for f, keys in _OHLC_ALIASES.items() if keys[1] in raw_bar]
        if len(plain) == 4:
            report["ohlc_spelling"] = "open/high/low/close"
        elif len(prefixed) == 4:
            report["ohlc_spelling"] = "adjOpen/adjHigh/adjLow/adjClose"
        elif plain or prefixed:
            report["ohlc_spelling"] = "mixed"
        else:
            report["ohlc_spelling"] = "unrecognized"
        row, reason = _validate_bar(raw_bar)
        report["loader_accepts"] = row is not None
        report["quarantine_reason"] = reason

    raw_close = report["unadjusted_close"]
    adj_close = report["split_adjusted_close"]
    ratio = report["split_ratio"]

    if raw_close is None or adj_close is None or adj_close <= 0:
        report["detail"] = (
            "One of the feeds returned no usable bar near this date. Pick a date "
            "inside the symbol's trading history, or try another symbol."
        )
        return report

    implied = raw_close / adj_close
    report["implied_ratio"] = implied

    if ratio == 1:
        report["detail"] = (
            f"No splits recorded after {on_date} for {symbol}, so both feeds should "
            "agree and this date cannot distinguish them. Re-run with an earlier "
            "--date, or a symbol that has split."
        )
        report["verdict"] = "inconclusive"
        return report

    if abs(implied - ratio) / ratio <= TOLERANCE:
        report["verdict"] = "unadjusted_confirmed"
        report["detail"] = (
            f"The unadjusted close is {implied:.4f}x the split-adjusted close, "
            f"matching the {ratio}:1 cumulative split since {on_date}. The feed "
            "behind core.daily_price is genuinely raw."
        )
    elif abs(implied - 1) <= TOLERANCE:
        report["verdict"] = "feeds_agree"
        report["detail"] = (
            f"Both feeds returned the same close despite a {ratio}:1 split since "
            f"{on_date}. The 'unadjusted' endpoint is returning split-adjusted "
            "prices -- do NOT ingest; fafnir would adjust every split twice."
        )
    else:
        report["verdict"] = "ratio_mismatch"
        report["detail"] = (
            f"The feeds differ by {implied:.4f}x but the recorded splits imply "
            f"{ratio}x. Either the splits payload is incomplete or one feed changed "
            "meaning. Investigate before backfilling."
        )
    return report


def format_report(report: dict[str, Any]) -> str:
    """Render a probe report for the terminal."""
    ok = {"unadjusted_confirmed": "PASS", "inconclusive": "INCONCLUSIVE"}
    status = ok.get(report["verdict"], "FAIL")
    lines = [
        f"FMP price-feed probe: {report['symbol']} @ {report['date']}",
        "",
        "Field names",
        f"  unadjusted     : {', '.join(report['unadjusted_fields']) or '(no bar)'}",
        f"  split-adjusted : {', '.join(report['split_adjusted_fields']) or '(no bar)'}",
        f"  OHLC spelling  : {report['ohlc_spelling']}",
        f"  loader accepts : {report['loader_accepts']}"
        + (
            f"  (would quarantine: {report['quarantine_reason']})"
            if report["quarantine_reason"]
            else ""
        ),
        "",
        "Adjustment cross-check",
        f"  unadjusted close     : {report['unadjusted_close']}",
        f"  split-adjusted close : {report['split_adjusted_close']}",
        f"  cumulative split     : {report['split_ratio']}",
        "  implied ratio        : "
        + (
            f"{report['implied_ratio']:.4f}"
            if report["implied_ratio"] is not None
            else "n/a"
        ),
        "",
        f"  {status}: {report['verdict']}",
        f"  {report['detail']}",
    ]
    return "\n".join(lines)
