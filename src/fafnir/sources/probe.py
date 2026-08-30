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

3. **Is the VOLUME unadjusted?** Asked separately, because volume back-adjusts the
   opposite way to price -- a split multiplies pre-split share counts rather than
   dividing them -- so an already-adjusted volume is inflated by the split ratio
   squared instead of collapsing toward zero. There is no vanish-to-zero tell and no
   DQ check covers volume, which makes it the quieter of the two failures.
   ``classify_volume`` documents the one case the two feeds cannot settle on their
   own.

Costs 3 requests (two price windows + splits) and writes nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fafnir.ingest.daily_price import _OHLC_ALIASES, _VOLUME_ALIASES, _validate_bar
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


def _decimal(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _volume_of(bar: dict) -> tuple[Optional[Decimal], Optional[str]]:
    """The bar's volume and which key it came from."""
    for key in _VOLUME_ALIASES:
        value = bar.get(key)
        if value not in (None, ""):
            try:
                return Decimal(str(value)), key
            except (ArithmeticError, ValueError):
                return None, key
    return None, None


def classify_volume(raw_bar: dict, adj_bar: dict, ratio: Decimal) -> tuple[str, str]:
    """Decide whether the unadjusted feed's VOLUME is also unadjusted.

    Volume back-adjusts the opposite way to price -- pre-split share counts are
    multiplied by the split ratio, not divided -- so an already-adjusted volume is
    inflated by ratio**2 rather than collapsed. There is no vanish-to-zero tell, and
    no DQ check covers volume, so this is worth reading carefully.

    The honest limitation: comparing the two feeds cannot always settle it. "FMP
    adjusts volume on neither endpoint" and "FMP adjusts volume on both" produce an
    identical signature (equal volumes on both feeds). The tiebreaker is
    ``unadjustedVolume`` -- where the payload carries it, it is by definition raw.
    When it is absent and the volumes match, this says so instead of guessing.
    """
    # Compare each feed's HEADLINE `volume`; `unadjustedVolume` is the reference
    # against which that headline is judged, so it must not stand in for it here
    # (unlike in the loader, which deliberately prefers it).
    raw_vol = _decimal(raw_bar.get("volume"))
    adj_vol = _decimal(adj_bar.get("volume"))
    unadj = _decimal(raw_bar.get("unadjustedVolume", adj_bar.get("unadjustedVolume")))

    # Gate on the value PARSING, not on the key existing: a present-but-non-numeric
    # `unadjustedVolume` is what the loader would quarantine as `nonnumeric_volume`,
    # so treating its mere presence as proof of rawness green-lights a backfill in
    # which every bar fails validation.
    raw_unadj = _decimal(raw_bar.get("unadjustedVolume"))
    if raw_unadj is not None and raw_unadj >= 0:
        return (
            "volume_raw_confirmed",
            "The unadjusted feed carries an explicit `unadjustedVolume`, which the "
            "loader prefers over `volume`. That is raw by definition.",
        )
    if raw_bar.get("unadjustedVolume") not in (None, ""):
        return (
            "volume_unusable",
            f"`unadjustedVolume` is present but not a usable number "
            f"({raw_bar['unadjustedVolume']!r}). The loader prefers that field, so "
            "every bar would quarantine as `nonnumeric_volume`.",
        )

    if raw_vol is None or adj_vol is None or raw_vol <= 0 or adj_vol <= 0:
        return "inconclusive", "One of the feeds returned no usable volume."

    implied = adj_vol / raw_vol
    if ratio != 1 and abs(implied - ratio) / ratio <= TOLERANCE:
        return (
            "volume_raw_confirmed",
            f"The split-adjusted feed reports {implied:.4f}x the unadjusted feed's "
            f"volume, matching the {ratio}:1 split. Volume is restated in today's "
            "shares there and raw here -- which also means the retired `.../full` "
            "feed was inflating stored volume.",
        )

    if abs(implied - 1) <= TOLERANCE:
        if unadj is not None and unadj > 0:
            if abs(raw_vol - unadj) / unadj <= TOLERANCE:
                return (
                    "volume_raw_confirmed",
                    "Both feeds report the same volume, and it equals the "
                    "payload's `unadjustedVolume` -- FMP does not split-adjust "
                    "volume at all. Raw either way.",
                )
            return (
                "volume_adjusted",
                f"Both feeds report {raw_vol}, but `unadjustedVolume` is "
                f"{unadj}. The volume being ingested is split-adjusted; "
                "fafnir would inflate it again by the split ratio.",
            )
        return (
            "volume_ambiguous",
            "Both feeds report the same volume and neither carries "
            "`unadjustedVolume`, so this cannot distinguish 'FMP never adjusts "
            "volume' (fine) from 'FMP adjusts it on both endpoints' (would be "
            "double-counted). Check one date against an outside source -- an "
            "exchange or another vendor -- before trusting deep-history volume.",
        )

    return (
        "volume_ratio_mismatch",
        f"The feeds' volumes differ by {implied:.4f}x, which matches neither 1 nor "
        f"the {ratio}:1 split. Investigate before relying on volume.",
    )


def _bar_date(bar: dict) -> Optional[date]:
    try:
        return datetime.strptime(str(bar.get("date"))[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _bar_on_or_after(bars: list[dict], target: date) -> Optional[dict]:
    """First bar dated on/after ``target`` -- the date itself may be a holiday.

    Keyed on the date alone. Comparing whole ``(date, bar)`` tuples would fall
    through to comparing the dicts when two bars share a date and differ in
    content, which raises TypeError -- and duplicate dates are a real possibility
    here, since ``eod_split_adjusted`` does not dedup the way ``eod_raw`` does.
    """
    dated = [(when, bar) for bar in bars if (when := _bar_date(bar)) and when >= target]
    return min(dated, key=lambda pair: pair[0])[1] if dated else None


def _bar_on_date(bars: list[dict], target: date) -> Optional[dict]:
    """The bar for exactly ``target``, or None."""
    for bar in bars:
        if _bar_date(bar) == target:
            return bar
    return None


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

    # Both bars MUST be the same trading day. Selecting each feed's nearest bar
    # independently silently compares different days whenever the feeds' coverage
    # differs, and charges the intervening price move to the adjustment ratio -- a
    # 6% one-day move is enough to turn a correct 4:1 feed into a `ratio_mismatch`.
    # So anchor on the unadjusted feed, then demand that exact date from the other.
    raw_bar = _bar_on_or_after(raw_bars, on_date)
    compared_date = _bar_date(raw_bar) if raw_bar else None
    adj_bar = _bar_on_date(adj_bars, compared_date) if compared_date else None

    report: dict[str, Any] = {
        "symbol": symbol,
        "date": on_date,
        "compared_date": compared_date,
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
        # Headline `volume` from each feed -- the pair the verdict compares. The
        # `unadjustedVolume` reference is reported separately so the two are never
        # confused, and volume_key names the field the loader will actually ingest.
        "unadjusted_volume": _decimal(raw_bar.get("volume")) if raw_bar else None,
        "split_adjusted_volume": _decimal(adj_bar.get("volume")) if adj_bar else None,
        "unadjusted_volume_field": (
            _decimal(raw_bar.get("unadjustedVolume")) if raw_bar else None
        ),
        "volume_key": _volume_of(raw_bar)[1] if raw_bar else None,
        "volume_verdict": "inconclusive",
        "volume_detail": "",
    }

    if raw_bar and adj_bar:
        report["volume_verdict"], report["volume_detail"] = classify_volume(
            raw_bar, adj_bar, report["split_ratio"]
        )

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
        if raw_bar is not None and adj_bar is None:
            report["detail"] = (
                f"The unadjusted feed has a bar for {compared_date} but the "
                "split-adjusted feed does not, so there is no like-for-like pair to "
                "compare. Comparing adjacent days instead would charge the price "
                "move between them to the split ratio, so this reports nothing "
                "rather than a wrong answer. Try a nearby --date."
            )
        else:
            report["detail"] = (
                "One of the feeds returned no usable bar near this date. Pick a "
                "date inside the symbol's trading history, or try another symbol."
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
    vol_ok = {
        "volume_raw_confirmed": "PASS",
        "inconclusive": "INCONCLUSIVE",
        "volume_ambiguous": "UNDECIDED",
    }
    vol_status = vol_ok.get(report["volume_verdict"], "FAIL")
    lines = [
        f"FMP price-feed probe: {report['symbol']} @ {report['date']}",
        f"  bar compared: {report.get('compared_date') or '(none found)'}"
        + (
            ""
            if report.get("compared_date") in (None, report["date"])
            else "  (nearest trading day on/after the requested date)"
        ),
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
        "",
        "Volume cross-check",
        f"  unadjusted feed `volume`     : {report['unadjusted_volume']}",
        f"  split-adjusted feed `volume` : {report['split_adjusted_volume']}",
        "  `unadjustedVolume`           : "
        + (
            str(report["unadjusted_volume_field"])
            if report["unadjusted_volume_field"] is not None
            else "(not present)"
        ),
        f"  loader would ingest          : `{report['volume_key'] or 'n/a'}`",
        f"  {vol_status}: {report['volume_verdict']}",
        f"  {report['volume_detail']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fund NAV probe (ADR 0006)
# ---------------------------------------------------------------------------
#
# ADR 0001 and ADR 0004 rest on one precondition: the feed behind core.daily_price
# is genuinely unadjusted, because fafnir's own factors are the only adjustment in
# the system. `probe_prices` verifies that for equities using splits. Funds rarely
# split, so the same question has to be asked of DISTRIBUTIONS instead:
#
#   * A raw NAV series DROPS by the distributed amount on the ex-date. fafnir's
#     dividend factor then back-adjusts it, exactly as for a cash dividend, and the
#     adjusted series is a total-return series.
#   * An already-reinvested NAV series does NOT drop. Loading distributions as
#     corporate actions on top of it would adjust every one of them twice -- ADR
#     0004's failure reproduced on a new asset class, and this time with no split
#     ratio to make it obvious.
#
# The test is the same arithmetic as the equity probe, read the other way round:
#
#     nav_before - nav_after  ~=  distribution   (raw)
#     nav_before - nav_after  ~=  0              (already adjusted)
#
# Costs 3 requests (prices, dividends, splits) and writes nothing.

# How much of the distribution must show up in the NAV drop to call the series raw.
# Wide on purpose: the drop is the distribution PLUS that day's market move, and a
# fund can move a percent of NAV on its own. The two hypotheses being separated are
# "the whole distribution" and "none of it", so the band only has to exclude the
# midpoint -- a narrow band would report ratio_mismatch on ordinary market noise.
NAV_DROP_MIN_SHARE = Decimal("0.5")
NAV_DROP_MAX_SHARE = Decimal("1.5")

# How close to zero the drop must be to call the series already-reinvested. This is
# deliberately NOT "anything below NAV_DROP_MIN_SHARE": a NAV that fell by 40% of the
# distribution is neither hypothesis, and reporting it as already-adjusted would name
# the wrong cause and print the wrong remedy. Anything between the two bands is a
# mismatch -- a question, not an answer.
NAV_NO_DROP_MAX_SHARE = Decimal("0.15")

# A distribution smaller than this share of NAV cannot be told from a normal day's
# move, whichever hypothesis is true.
NAV_MIN_MATERIAL_SHARE = Decimal("0.005")  # 0.5% of NAV

# Named rather than inlined so the guidance in a failing report stays correct if
# the price endpoint ever moves.
NAV_ENDPOINT = "historical-price-eod/non-split-adjusted"


def _largest_distribution(
    dividends: list[dict],
) -> tuple[Optional[date], Optional[Decimal]]:
    """The biggest cash distribution on record, with its ex-date.

    Biggest, not most recent: the probe needs a distribution large enough that the
    NAV drop it causes stands clear of that day's market move, and for a fund that
    is almost always a December capital-gain distribution rather than a monthly
    income one.
    """
    best_date: Optional[date] = None
    best_amount: Optional[Decimal] = None
    for rec in dividends:
        when = _bar_date(rec)
        amount = _decimal(rec.get("dividend"))
        if amount is None:
            amount = _decimal(rec.get("adjDividend"))
        if when is None or amount is None or amount <= 0:
            continue
        if best_amount is None or amount > best_amount:
            best_date, best_amount = when, amount
    return best_date, best_amount


def probe_fund_nav(fmp, symbol: str, window_days: int = 10) -> dict[str, Any]:
    """Decide whether a fund's NAV series is raw or already distribution-adjusted.

    Returns a report dict; ``verdict`` is one of:
      ``nav_raw_confirmed``   -- NAV drops by about the distribution. Load
                                 distributions as corporate actions; the design in
                                 ADR 0006 is correct as written.
      ``nav_already_adjusted``-- NAV does not drop across the ex-date. Do NOT load
                                 distributions for funds; fafnir would adjust each
                                 of them twice.
      ``ratio_mismatch``      -- NAV moved, but not by the distribution. The feed or
                                 the distribution record disagrees with the other.
      ``no_price_history``    -- the unadjusted endpoint serves no bars for this
                                 symbol at all, which is itself the answer to
                                 whether a fund can be ingested this way.
      ``inconclusive``        -- no distributions on record, none material enough to
                                 separate the hypotheses, or a missing bar either
                                 side of the ex-date.
    """
    dividends = fmp.dividends(symbol)
    splits = fmp.splits(symbol)
    ex_date, amount = _largest_distribution(dividends)

    report: dict[str, Any] = {
        "symbol": symbol,
        "ex_date": ex_date,
        "distribution": amount,
        "distributions_on_record": len(dividends),
        "splits_on_record": len(splits),
        "nav_before": None,
        "nav_before_date": None,
        "nav_after": None,
        "nav_after_date": None,
        "nav_drop": None,
        "drop_share": None,
        "bars_returned": 0,
        "ohlc_spelling": None,
        "loader_accepts_as_fund": False,
        "loader_accepts_as_equity": False,
        "quarantine_reason": None,
        "verdict": "inconclusive",
        "detail": "",
    }

    if ex_date is None or amount is None:
        # Still worth pulling bars: "does this endpoint serve funds at all" is
        # question 1 of the three, and it is answerable without a distribution.
        bars = fmp.eod_raw(
            symbol,
            from_date=(date.today() - timedelta(days=window_days * 3)).isoformat(),
            to_date=date.today().isoformat(),
        )
        _describe_nav_bars(report, bars)
        if not bars:
            report["verdict"] = "no_price_history"
            report["detail"] = (
                f"{NAV_ENDPOINT} returned no bars for {symbol}. A fund "
                "cannot be ingested from this endpoint -- find one that serves NAV "
                "before declaring funds in ref.tracked_symbol."
            )
        else:
            report["detail"] = (
                f"No cash distributions on record for {symbol}, so nothing "
                "distinguishes a raw NAV series from an already-reinvested one. "
                "Probe a fund that pays a December capital gain."
            )
        return report

    bars = fmp.eod_raw(
        symbol,
        from_date=(ex_date - timedelta(days=window_days)).isoformat(),
        to_date=(ex_date + timedelta(days=window_days)).isoformat(),
    )
    _describe_nav_bars(report, bars)

    if not bars:
        report["verdict"] = "no_price_history"
        report["detail"] = (
            f"{NAV_ENDPOINT} returned no bars for {symbol} around {ex_date}. Either "
            "it does not serve this fund, or the window misses its history -- try a "
            "wider --window before concluding the endpoint cannot be used."
        )
        return report

    # The last bar strictly before the ex-date, and the bar on/after it: the drop
    # this measures is the one the adjustment factor would divide into.
    before_pairs = [
        (when, bar) for bar in bars if (when := _bar_date(bar)) and when < ex_date
    ]
    after_bar = _bar_on_or_after(bars, ex_date)
    before_bar = (
        max(before_pairs, key=lambda pair: pair[0])[1] if before_pairs else None
    )

    if before_bar is None or after_bar is None:
        report["detail"] = (
            f"No bar on one side of {ex_date}, so there is no drop to measure. "
            "Widen --window or pick another distribution."
        )
        return report

    nav_before = _close_of(before_bar)
    nav_after = _close_of(after_bar)
    report["nav_before"], report["nav_before_date"] = nav_before, _bar_date(before_bar)
    report["nav_after"], report["nav_after_date"] = nav_after, _bar_date(after_bar)

    if nav_before is None or nav_after is None or nav_before <= 0:
        report["detail"] = "A bar either side of the ex-date carried no usable NAV."
        return report

    if amount / nav_before < NAV_MIN_MATERIAL_SHARE:
        report["detail"] = (
            f"The largest distribution on record ({amount}) is only "
            f"{amount / nav_before:.4%} of NAV -- smaller than a normal day's move, "
            "so it cannot separate a raw series from an adjusted one. Probe a fund "
            "with a larger capital-gain distribution."
        )
        report["verdict"] = "inconclusive"
        return report

    drop = nav_before - nav_after
    share = drop / amount
    report["nav_drop"] = drop
    report["drop_share"] = share

    if NAV_DROP_MIN_SHARE <= share <= NAV_DROP_MAX_SHARE:
        report["verdict"] = "nav_raw_confirmed"
        report["detail"] = (
            f"NAV fell {drop} across the {ex_date} ex-date against a distribution "
            f"of {amount} ({share:.2f}x). The series is raw, so fafnir's dividend "
            "factors are the only adjustment applied and the adjusted series is a "
            "total-return series."
        )
    elif abs(share) <= NAV_NO_DROP_MAX_SHARE:
        report["verdict"] = "nav_already_adjusted"
        report["detail"] = (
            f"NAV moved {drop} across the {ex_date} ex-date against a distribution "
            f"of {amount} -- it did not drop. The feed has already reinvested "
            "distributions. Do NOT load fund distributions into "
            "core.corporate_action: fafnir would adjust every one of them twice."
        )
    else:
        report["verdict"] = "ratio_mismatch"
        report["detail"] = (
            f"NAV moved {drop} against a distribution of {amount} ({share:.2f}x) -- "
            "too little to be the whole distribution and too much to be none of it, "
            "so this says neither. Either the distribution record is incomplete or "
            "the NAV series means something other than a raw strike. Investigate "
            "before declaring funds."
        )
    return report


def _describe_nav_bars(report: dict[str, Any], bars: list[dict]) -> None:
    """Record what the payload looks like and whether the loader would take it.

    Both answers matter and they are different questions: a NAV payload carrying
    only a close is accepted for a fund and quarantined for an equity, which is
    exactly the asset-type gate in ``daily_price._validate_bar``. Showing both says
    whether the gate is doing the work, or whether the payload never needed it.
    """
    report["bars_returned"] = len(bars)
    if not bars:
        return
    sample = bars[-1]
    plain = [f for f in _OHLC_ALIASES if f in sample]
    prefixed = [f for f, keys in _OHLC_ALIASES.items() if keys[1] in sample]
    if len(plain) == 4:
        report["ohlc_spelling"] = "open/high/low/close"
    elif len(prefixed) == 4:
        report["ohlc_spelling"] = "adjOpen/adjHigh/adjLow/adjClose"
    elif plain or prefixed:
        report["ohlc_spelling"] = "close only" if set(plain) <= {"close"} else "mixed"
    else:
        report["ohlc_spelling"] = "unrecognized"
    as_fund, _ = _validate_bar(sample, nav_only=True)
    as_equity, reason = _validate_bar(sample)
    report["loader_accepts_as_fund"] = as_fund is not None
    report["loader_accepts_as_equity"] = as_equity is not None
    report["quarantine_reason"] = reason


def format_fund_report(report: dict[str, Any]) -> str:
    """Render a fund NAV probe report for the terminal."""
    ok = {"nav_raw_confirmed": "PASS", "inconclusive": "INCONCLUSIVE"}
    status = ok.get(report["verdict"], "FAIL")
    return "\n".join(
        [
            f"FMP fund NAV probe: {report['symbol']}",
            "",
            "Coverage",
            f"  bars returned        : {report['bars_returned']}",
            f"  OHLC spelling        : {report['ohlc_spelling'] or '(no bar)'}",
            f"  loader accepts (fund): {report['loader_accepts_as_fund']}",
            f"  ... as an equity     : {report['loader_accepts_as_equity']}"
            + (
                f"  (would quarantine: {report['quarantine_reason']})"
                if report["quarantine_reason"]
                else ""
            ),
            f"  distributions        : {report['distributions_on_record']}",
            f"  splits               : {report['splits_on_record']}",
            "",
            "Distribution cross-check",
            f"  ex-date              : {report['ex_date'] or '(none on record)'}",
            f"  distribution         : {report['distribution']}",
            f"  NAV before ({report['nav_before_date'] or 'n/a'}) : "
            f"{report['nav_before']}",
            f"  NAV after  ({report['nav_after_date'] or 'n/a'}) : "
            f"{report['nav_after']}",
            f"  NAV drop             : {report['nav_drop']}",
            "  drop / distribution  : "
            + (
                f"{report['drop_share']:.2f}x"
                if report["drop_share"] is not None
                else "n/a"
            ),
            "",
            f"  {status}: {report['verdict']}",
            f"  {report['detail']}",
        ]
    )


# ---------------------------------------------------------------------------
# Corporate-actions coverage probe (ADR 0007)
# ---------------------------------------------------------------------------
#
# The market-wide calendar sweep rests on one assumption: that
# `splits-calendar` / `dividends-calendar` carry the same events, with the same
# values, that the per-symbol `splits` / `dividends` endpoints do. If they do not,
# the failure is silent -- a missing dividend is not an error, it is an adjusted
# price series that is quietly wrong, and nothing in the warehouse would say so.
#
# fafnir does not adopt an endpoint on the strength of its documentation (see
# probe_prices and probe_fund_nav above). This is the same gate for the same
# reason, and it writes nothing.


def _action_key(rec: dict, kind: str) -> Optional[tuple]:
    """Identify one action for comparison: (kind, ex_date, the values that matter).

    Values are compared as Decimals, not floats or strings, so that 2 and 2.000000
    are the same split and 0.24 and 0.2400 the same dividend -- a spurious mismatch
    would fail the probe for a formatting difference and stall the rollout.
    """
    when = _bar_date(rec)
    if when is None:
        return None
    if kind == "split":
        num = _decimal(rec.get("numerator", rec.get("splitTo")))
        den = _decimal(rec.get("denominator", rec.get("splitFrom")))
        if num is None or den is None or not num or not den:
            return None
        return ("split", when, num / den)
    amount = _decimal(rec.get("dividend"))
    if amount is None:
        amount = _decimal(rec.get("adjDividend"))
    if amount is None:
        return None
    return ("dividend", when, amount)


def _in_window(keys: set, start: date, end: date) -> set:
    return {k for k in keys if start <= k[1] <= end}


# Below this many calendar rows, "the calendar starts late" is not evidence of
# anything. These feeds are market-wide: any real window containing ex-dates comes
# back with hundreds to thousands of rows across thousands of symbols, so a short
# coverage window is a property of the RESPONSE. A handful of rows is a feed that
# returned almost nothing, and why it did is a question this heuristic cannot answer
# -- so it declines to guess rather than blaming a cut-off.
MIN_ROWS_TO_JUDGE_TRUNCATION = 100


def _truncation_cutoff(report: dict[str, Any]) -> Optional[date]:
    """The date a cut-off response starts at, or None if this is not one.

    A truncated response and a genuine coverage gap look identical per symbol -- the
    event is on the per-symbol feed and not on the calendar either way. What tells
    them apart is whether the misses line up by DATE or by SECURITY: a row cap drops
    the oldest rows for every symbol at once, so every missing event sits before the
    earliest date the calendar returned at all, while a real gap leaves misses
    scattered among events the calendar did carry from the same period.

    Three conditions, and all of them are load-bearing:

    * every miss predates the earliest row returned -- otherwise the calendar plainly
      did reach that period and simply lacks these securities;
    * the calendar's earliest row is later than the window start -- otherwise nothing
      was cut off at all;
    * the response was big enough for its span to mean something
      (:data:`MIN_ROWS_TO_JUDGE_TRUNCATION`).

    Without the third, a calendar holding one event trivially satisfies the first,
    and every genuine single-symbol coverage gap would be reported as a client bug.
    """
    earliest = report.get("calendar_earliest")
    missing = report.get("missing_keys") or []
    if earliest is None or not missing:
        return None
    if report.get("calendar_events", 0) < MIN_ROWS_TO_JUDGE_TRUNCATION:
        return None
    if earliest <= report["window_from"]:
        return None  # the calendar reached the start of the window; nothing was cut
    return earliest if all(key[1] < earliest for key in missing) else None


def probe_actions(fmp, symbols: list[str], days: int = 90) -> dict[str, Any]:
    """Check the market-wide action calendars against the per-symbol endpoints.

    Pulls each symbol both ways over the same window and diffs the events. Returns a
    report dict; ``verdict`` is one of:

      ``calendar_complete``   -- every per-symbol event in the window appears on the
                                calendar with equal values. Adopt the sweep.
      ``calendar_incomplete`` -- the calendar omits events the per-symbol feed has.
                                **Stop.** Keep the per-symbol path for the affected
                                asset type; the sweep would silently lose them.
      ``calendar_truncated``  -- every missing event predates the earliest row the
                                calendar returned at all, i.e. the response was cut
                                off rather than the feed being short on those
                                symbols. A client bug, not a vendor gap; fix the
                                paging and re-probe.
      ``field_mismatch``      -- the same events, but different values or a payload
                                whose fields the transform does not read. Fix the
                                transform, then re-probe.
      ``no_events``           -- nothing went ex in the window, so the two feeds
                                cannot be told apart. Widen --days or pick a payer.

    Costs ``2 + 2N`` requests and writes nothing. Note that the calendar is fetched
    to ``today`` even though the loader stores nothing past the ex-date: the probe is
    asking what the feed contains, not what fafnir would keep.
    """
    end = date.today()
    start = end - timedelta(days=days)

    calendar: dict[str, set] = {}
    # The earliest ex-date any calendar row carried. If the response was truncated,
    # this lands well after `start` and every event before it is missing for every
    # symbol at once -- which is what tells a client-side cut-off apart from a feed
    # that genuinely does not carry some securities.
    calendar_earliest: Optional[date] = None
    for kind, rows in (
        ("split", fmp.splits_calendar(start, end)),
        ("dividend", fmp.dividends_calendar(start, end)),
    ):
        for rec in rows:
            sym = str(rec.get("symbol") or "").strip().upper()
            key = _action_key(rec, kind)
            if sym and key is not None:
                calendar.setdefault(sym, set()).add(key)
                if calendar_earliest is None or key[1] < calendar_earliest:
                    calendar_earliest = key[1]

    report: dict[str, Any] = {
        "symbols": symbols,
        "window_from": start,
        "window_to": end,
        "calendar_symbols": len(calendar),
        "calendar_events": sum(len(v) for v in calendar.values()),
        "calendar_earliest": calendar_earliest,
        "per_symbol": [],
        "missing_keys": [],
        "missing_total": 0,
        "extra_total": 0,
        "matched_total": 0,
        "verdict": "no_events",
        "detail": "",
    }

    for symbol in symbols:
        symbol = symbol.strip().upper()
        direct: set = set()
        for kind, rows in (
            ("split", fmp.splits(symbol)),
            ("dividend", fmp.dividends(symbol)),
        ):
            for rec in rows:
                key = _action_key(rec, kind)
                if key is not None:
                    direct.add(key)
        direct = _in_window(direct, start, end)
        from_calendar = _in_window(calendar.get(symbol, set()), start, end)

        # Missing is the finding that stops the rollout: an event the per-symbol feed
        # knows about and the calendar does not is an event the sweep would never
        # load. Extra is the reverse and is informational -- the calendar carrying
        # more than the per-symbol endpoint costs nothing.
        missing = sorted(direct - from_calendar)
        extra = sorted(from_calendar - direct)
        report["per_symbol"].append(
            {
                "symbol": symbol,
                "per_symbol_events": len(direct),
                "calendar_events": len(from_calendar),
                "matched": len(direct & from_calendar),
                "missing_from_calendar": [f"{k[0]} {k[1]} {k[2]}" for k in missing],
                "extra_on_calendar": [f"{k[0]} {k[1]} {k[2]}" for k in extra],
            }
        )
        report["missing_keys"].extend(missing)
        report["missing_total"] += len(missing)
        report["extra_total"] += len(extra)
        report["matched_total"] += len(direct & from_calendar)

    total_direct = sum(r["per_symbol_events"] for r in report["per_symbol"])
    if total_direct == 0 and report["calendar_events"] == 0:
        report["detail"] = (
            f"No splits or dividends on either feed for {', '.join(symbols)} in the "
            f"last {days} days, so the two cannot be compared. Widen --days, or "
            "probe a reliable dividend payer (KO, JNJ, SPY)."
        )
        return report

    truncated_before = _truncation_cutoff(report)
    if truncated_before is not None:
        report["verdict"] = "calendar_truncated"
        report["detail"] = (
            f"The calendar returned nothing before {truncated_before}, though "
            f"{report['window_from']} was requested, and every missing event falls "
            "in that gap. That is a cut-off response, not a feed that lacks these "
            "securities -- the endpoint caps rows per response and drops the oldest "
            "to fit. Fix the paging in FMPClient._actions_calendar and re-probe; do "
            "not conclude anything about coverage from this run."
        )
    elif report["missing_total"]:
        report["verdict"] = "calendar_incomplete"
        report["detail"] = (
            f"{report['missing_total']} event(s) are on the per-symbol feed and not "
            "on the calendar. The sweep would lose them silently, and every adjusted "
            "price before the missing ex-date would be wrong. Keep "
            '`actions_mode = "symbol"` for the affected asset type.'
        )
    elif total_direct and report["matched_total"] < total_direct:
        report["verdict"] = "field_mismatch"
        report["detail"] = (
            "The calendar carries the same ex-dates with different values. Reconcile "
            "the field names in fafnir.ingest.corporate_actions before adopting."
        )
    elif report["matched_total"]:
        report["verdict"] = "calendar_complete"
        report["detail"] = (
            f"All {report['matched_total']} event(s) matched. Set "
            '`actions_mode = "auto"` in [general]. Funds stay on the per-symbol '
            "path regardless -- they have no listing venue, so this probe cannot "
            "speak for them unless you probed one."
        )
    else:
        report["detail"] = (
            "The calendar returned events but the per-symbol feed had none in the "
            "window, so there is nothing to verify against. Widen --days."
        )
    return report


def format_actions_report(report: dict[str, Any]) -> str:
    """Render a corporate-actions coverage probe for the terminal."""
    ok = {"calendar_complete": "PASS", "no_events": "INCONCLUSIVE"}
    status = ok.get(report["verdict"], "FAIL")
    earliest = report.get("calendar_earliest")
    covered = f"{earliest} .. {report['window_to']}" if earliest else "(nothing)"
    lines = [
        f"FMP corporate-actions coverage probe: "
        f"{report['window_from']} .. {report['window_to']}",
        f"  calendar feeds: {report['calendar_events']} events across "
        f"{report['calendar_symbols']} symbols",
        f"  calendar covered: {covered}"
        + ("" if not earliest or earliest <= report["window_from"] else "   <-- SHORT"),
        "",
    ]
    for row in report["per_symbol"]:
        lines.append(
            f"  {row['symbol']:<8} per-symbol {row['per_symbol_events']:>3}"
            f"   calendar {row['calendar_events']:>3}"
            f"   matched {row['matched']:>3}"
        )
        for missed in row["missing_from_calendar"]:
            lines.append(f"      MISSING from calendar: {missed}")
        for extra in row["extra_on_calendar"]:
            lines.append(f"      only on calendar     : {extra}")
    lines += [
        "",
        f"  {status}: {report['verdict']}",
        f"  {report['detail']}",
    ]
    return "\n".join(lines)
