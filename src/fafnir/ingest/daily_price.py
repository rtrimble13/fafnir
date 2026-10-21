"""
Daily OHLCV loader.

For each symbol: resolve security_id, compute the incremental window from the
watermark (minus an overlap to catch late corrections), fetch raw bars, land the
raw payload, set aside bars dated on days the venue did not trade, validate each
remaining bar at the boundary, quarantine bad bars (never drop), upsert good bars
idempotently, and advance the watermark.

NON-SESSION DATES. FMP returns bars for weekends and holidays for some symbols:
money-market funds strike a NAV seven days a week, and a ticker shared with another
instrument can carry that instrument's weekend prints (PI's ~$0.09 Saturday bars
between $150 closes). None of them is a session of the venue the security is
listed on, so they are not bars of *this* security and are not stored -- and they
are not quarantined either, because a quarantine is a claim that a real session's
bar was bad. The raw payload still lands in full. See :func:`_drop_non_session`.

NAV-PRICED SECURITIES. An open-end mutual fund has one price a day and no volume:
FMP returns a bar carrying a close and either no open/high/low or synthetic ones.
Either shape is a correct bar for a fund and a defect for an equity, so the
allowance is gated on the security (``asset_type`` or ``is_fund``) rather than on
what the payload happens to omit -- see :func:`_is_nav_priced` and
:func:`_validate_bar`. Everything downstream is unchanged: the expanded row
satisfies every constraint on core.daily_price as written, and a distribution
back-adjusts through core.adjustment_factor exactly as a cash dividend does. See
doc/adr/0006-curated-fund-universe.md.

RESTATEMENTS. FMP's first print of a bar is usually right; what it serves on a later
read of the same date often is not. Between 2026-09-16 and 10-14 the nightly re-read
of the last five sessions replaced first prints with another fund's prices (OUSL
~11 -> ~58, QCJL ~26 -> 0.07), with prices at the scale of a split not yet gone ex
(GCDT x6, ZBAO x50) and with a phantom split's scale (SKYD x2) -- and stored every
one. So a nightly load never overwrites a stored bar by more than
``[prices] restatement_max_ratio`` (close) or ``restatement_max_volume_ratio``
(volume), either way: it keeps the stored bar and flags
``price_restatement_conflict`` (:func:`restatement_detail`). An explicit operator
re-fetch (``ingest prices --from``) overwrites as before, and reports every bar it
restated beyond those ratios. A split-scaled bar with no stored copy to protect is
quarantined instead (:func:`split_scale_detail`).

A volume difference where both sides are at or below a round lot
(``[prices] restatement_min_volume``, 100) is not a restatement: FMP rounds an
odd-lot day up to 100 shares when it re-reads it (WSGE 9 -> 100, CCRP 3 -> 100;
outside sources show the first print's count). The re-read's prices are judged as
usual, and the stored volume is kept (:func:`odd_lot_rounding`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, SourceError, payload_hash

logger = get_logger("ingest.price")

ENDPOINT = "historical-price-eod/non-split-adjusted"

# The endpoint this loader used before the unadjusted-feed fix. Its bars were
# split-adjusted, so anything ingested under it is wrong for core.daily_price and
# its watermarks must not be reused. Kept only so the changeover can be detected.
LEGACY_SPLIT_ADJUSTED_ENDPOINT = "historical-price-eod/full"

# A bar quarantined this many times stops holding the watermark (it stays flagged
# for review, but ingestion is allowed to advance past it).
MAX_QUARANTINE_HOLDS = 5

# The calendar used when a security's own venue has none. Funds carry pseudo-venues
# that ref.trading_calendar does not seed; the US equity sessions are the right
# answer for every venue this project loads.
CALENDAR_FALLBACK_EXCHANGE = "NASDAQ"

# Asset types priced at a single daily NAV rather than traded through a session.
# For these, a bar with a close and no open/high/low is the whole truth about the
# day, not a payload with three fields missing.
NAV_ASSET_TYPES = frozenset({"fund"})

# Asset types that trade through an exchange session even when the vendor also
# marks them as funds: an ETF has real OHLCV, so ``is_fund`` must not turn its bars
# into NAV strikes. See _is_nav_priced.
SESSION_TRADED_ASSET_TYPES = frozenset({"etf"})

# A stored bar whose OHLC the money column's scale flattened to one value. It shares
# the `price_` prefix because that is the family an operator globs for
# (`fafnir dq list --check 'price_*'`), but it is NOT a quarantine: the bar was
# written. repository.count_price_quarantines excludes it by name for that reason --
# counting it would let a corrupted-but-stored bar buy down the watermark budget of a
# rejected one. See repository.NON_QUARANTINE_PRICE_CHECKS, which must list it.
#
# NOT to be confused with the NAV shape above, which also stores
# open = high = low = close. That one is correct and must never be flagged: a fund
# strikes one price a day, and there was no range to lose. _scale_collapse_detail
# tells them apart by reading the SOURCE bar -- a NAV payload has no open/high/low
# to compare, so it cannot show the range this check requires evidence of -- and,
# for a NAV strike whose payload does carry open/high/low, by being told so: those
# fields are synthetic and are not read (see _is_nav_strike).
SCALE_COLLAPSE_CHECK = "price_scale_collapse"

# What core.daily_price can actually store (sql/migrations/0005_daily_price.up.sql):
# money is NUMERIC(20, 6), volume is BIGINT. Postgres rounds a value to the column's
# scale on insert and only *then* evaluates ck_daily_price_positive, so a positive
# but sub-resolution price -- a delisted sub-penny shell quoted at 1e-7 -- is stored
# as 0.000000 and fails the constraint, aborting the whole batch. Asking `float(v) > 0`
# is therefore the wrong question: a bar has to survive the column, not Python. Every
# numeric field below is coerced to what the column would hold before it is judged.
_MONEY_SCALE = Decimal("0.000001")  # NUMERIC(20, 6) -> 6 decimal places
_MONEY_MAX = Decimal(10) ** 14  # NUMERIC(20, 6) -> 14 integer digits
_VOLUME_MAX = 2**63 - 1  # BIGINT

# A nightly re-read that would move a stored bar further than the guard allows. Not a
# quarantine: the stored bar stays, so it is written once per open condition
# (add_dq_flag_once), and repository.NON_QUARANTINE_PRICE_CHECKS lists it so it does
# not spend a rejected bar's watermark budget. See RESTATEMENTS above.
RESTATEMENT_CHECK = "price_restatement_conflict"

# A new bar at a split-adjusted scale, before a split on file. A quarantine: the bar
# is not stored, and it holds the watermark like any other (MAX_QUARANTINE_HOLDS).
SPLIT_SCALE_CHECK = "price_split_scale_mismatch"

# Calendar days either side of a conflicting bar searched for a split on file, which
# is the usual reason a vendor restates a bar (GCDT, ZBAO, SKYD).
RESTATEMENT_SPLIT_DAYS = 30

# An explicit re-fetch records at most this many restated bars in its run's params;
# a full re-backfill across a feed change would otherwise write every bar there.
RESTATED_PARAMS_MAX = 500

# The split-scale guard (split_scale_detail): a split on file this many calendar
# days after a new bar, a close within this share of the split's price factor of the
# last stored close, a volume within this share of the expected one, and a split at
# least this far from 1:1 (a 5:4 split's factor is inside an ordinary day's range).
SPLIT_SCALE_DAYS = 30
SPLIT_SCALE_PRICE_TOLERANCE = 0.10
SPLIT_SCALE_VOLUME_TOLERANCE = 0.25
SPLIT_SCALE_MIN_FACTOR = 1.5
SPLIT_SCALE_MEDIAN_SESSIONS = 20


@dataclass(frozen=True)
class RestatementGuard:
    """How far a nightly re-read may move a stored bar, either way. Volumes at or
    below ``min_volume`` on both sides are a round lot's rounding, never a
    restatement (:func:`odd_lot_rounding`)."""

    max_ratio: float = 1.25
    max_volume_ratio: float = 3.0
    min_volume: int = 100


# FMP's unadjusted (and dividend-adjusted) endpoints prefix the OHLC field names
# with "adj"; the `full` endpoint does not. The prefix is a naming convention on
# those payloads, not a claim that the values carry an extra adjustment.
_OHLC_ALIASES = {
    "open": ("open", "adjOpen"),
    "high": ("high", "adjHigh"),
    "low": ("low", "adjLow"),
    "close": ("close", "adjClose"),
}

# Volume gets back-adjusted the opposite way to price -- a 4:1 split multiplies
# pre-split share counts by 4 -- so a volume that arrived already split-adjusted
# would be inflated by the split ratio SQUARED, not collapsed toward zero. Where a
# payload offers `unadjustedVolume` it is by definition the raw count, so prefer it;
# core.daily_price is defined as raw. See doc/adr/0004-unadjusted-price-feed.md.
_VOLUME_ALIASES = ("unadjustedVolume", "volume")


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal(value) -> Optional[Decimal]:
    """Parse one feed value as an exact Decimal, or None if it is not a finite number.

    Goes through ``str`` so a float's binary noise is not carried into the warehouse:
    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827, ``Decimal("0.1")`` is 0.1.
    NaN and the infinities parse but are not numbers a price column can hold, so they
    are rejected here rather than blowing up further down.
    """
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return parsed if parsed.is_finite() else None


def _as_money(value) -> Optional[Decimal]:
    """Quantize a price to NUMERIC(20, 6), or None if the column cannot hold it.

    One None covers every way a field fails to carry a usable price -- junk, zero or
    negative, sub-resolution, or wider than the column -- because the caller that
    needs the precise quarantine reason gets it from :func:`_reject_reason`.
    """
    parsed = _decimal(value)
    if parsed is None:
        return None
    try:
        rounded = parsed.quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None  # more digits than the column could hold anyway
    if rounded <= 0 or rounded >= _MONEY_MAX:
        return None
    return rounded


def _reject_reason(value) -> str:
    """Say why a present-but-unusable price was rejected, for the DQ review queue.

    The distinction is what an operator acts on: a zero means the feed sent nothing
    tradeable, while a sub-resolution price means the feed sent a real quote this
    warehouse cannot represent -- different data, different remedy.
    """
    parsed = _decimal(value)
    if parsed is None:
        return "missing_or_nonnumeric_ohlc"
    if parsed <= 0:
        return "non_positive_price"
    if parsed >= _MONEY_MAX:
        return "price_out_of_range"
    return "subresolution_price"


def _as_volume(value) -> Optional[int]:
    """Parse a volume as an int, truncating toward zero; None if it is not a number."""
    parsed = _decimal(value)
    return None if parsed is None else int(parsed)


def _as_vwap(value) -> Optional[Decimal]:
    """Quantize vwap to NUMERIC(20, 6), or None when absent, junk or too wide.

    vwap is nullable and carries no CHECK, so only precision can break the insert.
    An unusable one is dropped rather than failing an otherwise good bar -- unlike
    OHLC, a missing vwap costs nothing.
    """
    parsed = _decimal(value)
    if parsed is None:
        return None
    try:
        rounded = parsed.quantize(_MONEY_SCALE, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None
    return None if abs(rounded) >= _MONEY_MAX else rounded


def _source_price(bar: dict, field: str) -> Optional[Decimal]:
    """The raw Decimal :func:`_ohlc` would take for this field, before quantizing.

    Mirrors _ohlc's alias rule exactly -- first spelling carrying a *storable* price
    -- so the two never disagree about which of ``open`` / ``adjOpen`` the bar was
    read from. What differs is only that this returns the source value rather than
    the value rounded to the column, which is the whole point: the difference between
    them is the information the column threw away.
    """
    for key in _OHLC_ALIASES[field]:
        value = bar.get(key)
        if value in (None, ""):
            continue
        if _as_money(value) is not None:
            return _decimal(value)
    return None


def _scale_collapse_detail(
    bar: dict, row: dict, *, nav_strike: bool = False
) -> Optional[dict]:
    """Evidence that quantizing flattened a bar that had a real range, else None.

    The loud failure -- a price below the column's resolution -- is quarantined by
    ``_reject_reason`` and never stored. This is the quiet one either side of it: a
    bar whose OHLC all round to the *same* storable value. ROUND_HALF_UP at 6 places
    puts the rejection cliff at 5e-7, so a security quoted between roughly 5e-7 and
    1.5e-6 passes every check and lands in core.daily_price with
    ``open = high = low = close`` -- a real intraday range silently replaced by a
    zero-range session. Nothing downstream can tell that from a genuine no-trade day,
    and close-to-close returns computed over a run of them are not wrong so much as
    fictional.

    A bar whose *source* high and low are already equal is a real flat session and is
    not flagged: the test is that the range existed and the column lost it, not that
    the stored bar is flat. Nor is a NAV strike (``nav_strike``): its open/high/low
    are synthetic and were never read, so they are no evidence of a range.
    """
    if nav_strike:
        return None
    if len({row["open"], row["high"], row["low"], row["close"]}) != 1:
        return None
    source = [_source_price(bar, f) for f in ("open", "high", "low", "close")]
    if any(v is None for v in source):
        return None
    high, low = max(source), min(source)
    if high == low:
        return None
    # Rendered fixed-point, not with str(). Decimal.__str__ switches to scientific
    # notation below 1e-6 -- which is precisely the range this check reports on -- so
    # str() would write "8.01E-7" for the source high next to a "scale" of "0.000001",
    # and the operator reading the flag would be comparing two numbers written in
    # different notations to judge whether the range mattered. format(x, "f") keeps
    # every field in one notation and leaves the value exact.
    return {
        "source_high": format(high, "f"),
        "source_low": format(low, "f"),
        "source_range": format(high - low, "f"),
        "stored": format(row["close"], "f"),
        "scale": format(_MONEY_SCALE, "f"),
    }


def _ohlc(bar: dict, field: str) -> tuple[Optional[Decimal], Optional[str]]:
    """Read one OHLC field, accepting either FMP spelling (``open``/``adjOpen``).

    Returns ``(price, None)`` for the first spelling carrying a *storable* price --
    one core.daily_price will hold as a positive value -- so a payload with
    ``"open": 0`` (or ``"open": 1e-7``) next to a valid ``"adjOpen": 39.0`` is read
    from the field that has the price, rather than being quarantined on the strength
    of the one that does not. Preferring the unprefixed name is only a tie-break
    between two storable values, not a reason to discard a good one.

    When nothing is storable it returns ``(None, reason)`` for the first value that
    was *present*, so the caller reports the precise reason instead of collapsing
    every failure into one. Neither spelling present is its own reason.
    """
    present = None
    for key in _OHLC_ALIASES[field]:
        value = bar.get(key)
        if value in (None, ""):
            continue
        if present is None:
            present = value
        price = _as_money(value)
        if price is not None:
            return price, None
    if present is None:
        return None, "missing_or_nonnumeric_ohlc"
    return None, _reject_reason(present)


def _is_nav_priced(asset_type: Optional[str], is_fund: Optional[bool]) -> bool:
    """Whether a security is priced at a daily NAV rather than traded in a session.

    ``asset_type`` alone is not enough: the security master stores every fund as
    ``asset_type = 'equity'`` with ``is_fund = true`` (5,097 of them in production,
    and not one row carries 'fund'), so gating on :data:`NAV_ASSET_TYPES` alone
    left the NAV allowance applying to nothing.
    """
    if asset_type in NAV_ASSET_TYPES:
        return True
    return bool(is_fund) and asset_type not in SESSION_TRADED_ASSET_TYPES


def _is_nav_strike(bar: dict, nav_only: bool) -> bool:
    """Whether this bar is a NAV strike: a NAV-priced security's bar with no volume.

    Volume is the tell, not the security alone, so a traded bar on a security the
    vendor also calls a fund -- a closed-end fund with real volume -- is still read
    as a session. An absent volume counts as zero, as it does in validation.
    """
    if not nav_only:
        return False
    raw = next((bar.get(k) for k in _VOLUME_ALIASES if bar.get(k) not in (None, "")), 0)
    return _as_volume(raw) == 0


def _validate_bar(
    bar: dict, *, nav_only: bool = False
) -> tuple[Optional[dict], Optional[str]]:
    """Type and sanity-check a single bar. Returns (clean_row, reason_if_bad).

    Prices come back as Decimal, not float: core.daily_price is exact NUMERIC, and
    handing psycopg a float would reintroduce a coercion after validation -- exactly
    where the sub-resolution bug lived. What is checked here is what gets stored.

    ``nav_only`` marks a security priced at a daily NAV (see :func:`_is_nav_priced`).
    Its zero-volume bars are NAV strikes (:func:`_is_nav_strike`) and are built from
    the close alone: ``open = high = low = close``, which is what a single daily
    strike means. Vendor open/high/low on a strike are ignored even when present,
    because they are not prices anyone traded at: TDEAX's 1990 backfill had 3,773 of
    4,712 bars quarantined as cross_field_violation because FMP filled open/high/low
    with a distribution-adjusted NAV and close with the raw one (2006-01-03:
    o = h = l = 8.35, c = 12.91), and reading them threw away the fund's real
    history. The close itself is never synthesized and still faces every check: a
    zero, sub-resolution or out-of-range close is quarantined on any asset type.

    A NAV-priced security's bar WITH volume is read as a session, with one
    allowance: an absent open/high/low stands in as the close.
    """
    trade_date = _parse_date(bar.get("date"))
    if trade_date is None:
        return None, "unparseable_date"
    if _is_nav_strike(bar, nav_only):
        c, reason = _ohlc(bar, "close")
        if reason:
            return None, reason
        o = h = lo = c
    else:
        # Close first, so it is available to stand in for the others under nav_only.
        # It is also the field whose absence is fatal on any asset type: without a
        # close there is no price, and nothing to stand in WITH.
        prices: dict[str, Decimal] = {}
        for field in ("close", "open", "high", "low"):
            price, reason = _ohlc(bar, field)
            if reason:
                # Only an ABSENT field is stood in for. A field that is present but
                # unusable -- zero, negative, sub-resolution, out of range -- is bad
                # data on a traded bar whatever the security, and laundering it into
                # copies of the close would hide what the quarantine exists to show.
                if not (
                    nav_only
                    and field != "close"
                    and reason == "missing_or_nonnumeric_ohlc"
                ):
                    return None, reason
                price = prices["close"]
            prices[field] = price
        o, h, lo, c = prices["open"], prices["high"], prices["low"], prices["close"]

    raw_volume = 0
    for key in _VOLUME_ALIASES:
        value = bar.get(key)
        if value not in (None, ""):
            raw_volume = value
            break
    vol = _as_volume(raw_volume)
    if vol is None:
        return None, "nonnumeric_volume"
    if h < lo or h < o or h < c or lo > o or lo > c:
        return None, "cross_field_violation"
    if vol < 0:
        return None, "negative_volume"
    if vol > _VOLUME_MAX:
        return None, "volume_out_of_range"
    return {
        "trade_date": trade_date,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": vol,
        "vwap": _as_vwap(bar.get("vwap")),
    }, None


def _session_calendar(
    db: Database, exchange_code: Optional[str], bars: list[dict]
) -> Optional[tuple[frozenset, date, date]]:
    """The venue calendar covering these bars, or None when there is none to judge by.

    Read once per symbol, bounded by the bars' own dates, so a nightly run reads a
    handful of rows and a backfill one span. Falls back to
    :data:`CALENDAR_FALLBACK_EXCHANGE` when the security's venue has no calendar.
    """
    dates = [d for d in (_parse_date(b.get("date")) for b in bars) if d is not None]
    if not dates:
        return None
    start, end = min(dates), max(dates)
    for code in dict.fromkeys(
        c for c in (exchange_code, CALENDAR_FALLBACK_EXCHANGE) if c
    ):
        calendar = repo.open_sessions(db, code, start, end)
        if calendar is not None:
            return calendar
    return None


def _drop_non_session(
    bars: list[dict], calendar: Optional[tuple[frozenset, date, date]]
) -> tuple[list[dict], int]:
    """Split off bars dated on days the venue was closed. Returns (kept, dropped).

    A date counts as closed only when the calendar covers it and has no open session
    for it: the seed writes open days only, so inside the calendar's span a missing
    row is a weekend, a holiday or a closure, while outside it (past the
    ensure-horizon range) nothing is known and the bar is kept. An unparseable date
    is kept too -- :func:`_validate_bar` quarantines it with the precise reason.
    """
    if calendar is None:
        return list(bars), 0
    open_dates, first, last = calendar
    kept: list[dict] = []
    dropped = 0
    for bar in bars:
        d = _parse_date(bar.get("date"))
        if d is not None and first <= d <= last and d not in open_dates:
            dropped += 1
            continue
        kept.append(bar)
    return kept, dropped


def non_session_dates(
    db: Database, exchange_code: Optional[str], dates: Iterable[date]
) -> set[date]:
    """Which of these dates the loader would set aside as non-session for a venue.

    The same calendar and the same rule as :func:`_drop_non_session`, for dates
    already stored -- so `fafnir prices delete --non-session` and the set-aside on
    arrival cannot disagree about what a session is.
    """
    bars = [{"date": d.isoformat()} for d in dates]
    kept, _ = _drop_non_session(bars, _session_calendar(db, exchange_code, bars))
    kept_dates = {b["date"] for b in kept}
    return {_parse_date(b["date"]) for b in bars if b["date"] not in kept_dates}


def _drop_suppressed(bars: list[dict], suppressed: frozenset) -> tuple[list[dict], int]:
    """Split off bars on dates an operator deleted or corrected. Returns (kept, dropped)."""
    if not suppressed:
        return list(bars), 0
    kept = [b for b in bars if _parse_date(b.get("date")) not in suppressed]
    return kept, len(bars) - len(kept)


def _fold(new, old) -> Optional[float]:
    """``max(new/old, old/new)`` for two positive values; None when either is not
    (a zero volume is no ratio, so it restates nothing)."""
    try:
        n, o = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if n <= 0 or o <= 0:
        return None
    return max(n / o, o / n)


def _bar_json(row: dict) -> dict:
    return {
        f: (str(row[f]) if isinstance(row.get(f), Decimal) else row.get(f))
        for f in ("open", "high", "low", "close", "volume")
    }


def odd_lot_rounding(stored: dict, row: dict, guard: RestatementGuard) -> bool:
    """Whether the two volumes differ beyond the guard only as a round lot's
    rounding: both at or below ``guard.min_volume``.

    FMP's re-read of an odd-lot-only day reports 100 shares, where its first print
    had the count (2026-10-16 nightly: 87 of 103 conflicts, stored 3 to 33, re-read
    exactly 100, the same OHLC; 179 more on 10-20 and 10-21). The first print is the
    one outside sources agree with, so the stored volume is kept.
    """
    volume = _fold(row["volume"], stored["volume"])
    if volume is None or volume <= guard.max_volume_ratio:
        return False
    return max(float(row["volume"]), float(stored["volume"])) <= guard.min_volume


def restatement_detail(
    stored: dict, row: dict, guard: RestatementGuard
) -> Optional[dict]:
    """Evidence that ``row`` restates the ``stored`` bar beyond ``guard``, else None.

    Either side counts: a close more than ``max_ratio`` away (OUSL ~11 -> ~58, QCJL
    26 -> 0.07), or a volume more than ``max_volume_ratio`` away (ZBAO's re-fetch,
    x50 at the same prices).
    """
    close = _fold(row["close"], stored["close"])
    volume = _fold(row["volume"], stored["volume"])
    over = []
    if close is not None and close > guard.max_ratio:
        over.append("close")
    if (
        volume is not None
        and volume > guard.max_volume_ratio
        and not odd_lot_rounding(stored, row, guard)
    ):
        over.append("volume")
    if not over:
        return None
    return {
        "over": over,
        "close_ratio": round(float(row["close"]) / float(stored["close"]), 6),
        "volume_ratio": (
            round(float(row["volume"]) / float(stored["volume"]), 6)
            if volume is not None
            else None
        ),
        "stored": {
            **_bar_json(stored),
            "ingestion_run_id": stored.get("ingestion_run_id"),
        },
        "incoming": _bar_json(row),
    }


def split_scale_detail(
    row: dict,
    prev_close,
    median_volume: Optional[float],
    split: dict,
) -> Optional[dict]:
    """Evidence that a new bar sits at a split-adjusted scale, else None (G2).

    A bar dated before a split on file, whose close is the last stored close times
    the split's price factor (``den/num``: x50 for a 1:50, x0.5 for a 2:1), within
    :data:`SPLIT_SCALE_PRICE_TOLERANCE`. Its volume must look like the vendor's
    adjusted bar too, within :data:`SPLIT_SCALE_VOLUME_TOLERANCE` of the 20-session
    median times either the inverse factor (an adjustment applied consistently) or
    one (the price alone adjusted -- the shape FMP actually served: GCDT's x6 and
    ZBAO's x50 bars carried their first prints' volumes, within 8%). A real move of
    exactly a split's factor on ordinary volume, in the weeks before that split,
    is what this cannot tell apart, so splits nearer 1:1 than
    :data:`SPLIT_SCALE_MIN_FACTOR` are not judged: a 5:4 split's factor is inside an
    ordinary day's range.
    """
    num, den = split.get("split_numerator"), split.get("split_denominator")
    try:
        factor = float(den) / float(num)
        moved = float(row["close"]) / float(prev_close)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if factor <= 0 or max(factor, 1 / factor) < SPLIT_SCALE_MIN_FACTOR:
        return None
    if abs(moved / factor - 1) > SPLIT_SCALE_PRICE_TOLERANCE:
        return None
    if not median_volume or not row.get("volume"):
        return None
    vol = float(row["volume"]) / float(median_volume)
    shapes = {"adjusted": 1 / factor, "unchanged": 1.0}
    shape = next(
        (
            name
            for name, expected in shapes.items()
            if abs(vol / expected - 1) <= SPLIT_SCALE_VOLUME_TOLERANCE
        ),
        None,
    )
    if shape is None:
        return None
    return {
        "reason": "split_scale_mismatch",
        "close": str(row["close"]),
        "prev_close": str(prev_close),
        "close_ratio": round(moved, 6),
        "split_ex_date": str(split["ex_date"]),
        "split": split.get("split")
        or f"{split['split_numerator']}:{split['split_denominator']}",
        "price_factor": round(factor, 6),
        "volume": row.get("volume"),
        "median_volume": median_volume,
        "volume_shape": shape,
    }


def _window_start(
    start_date: Optional[date],
    watermark: Optional[date],
    overlap_days: int,
    backfill_start: Optional[date],
) -> Optional[date]:
    """The first date to ask the vendor for.

    An explicit window wins; a symbol with a watermark resumes from it, less the
    overlap that catches late corrections; a symbol with neither is being loaded for
    the first time and starts at ``backfill_start``. Asking with no start at all is
    not "everything": FMP applies its own default window of about five years, which
    is how GBF, DDI and MRT -- loaded after the initial backfill, without ``--from``
    -- came to hold histories starting 2021-08-30..09-02 while their dividends went
    back decades. None only when the caller gave no ``backfill_start`` either.
    """
    if start_date is not None:
        return start_date
    if watermark is not None:
        return watermark - timedelta(days=overlap_days)
    return backfill_start


def _tally_skipped_foreign(run, n: int) -> None:
    """Add to the run's ``params.skipped_foreign`` (ADR 0015)."""
    params = getattr(run, "params", None)
    if isinstance(params, dict):
        params["skipped_foreign"] = params.get("skipped_foreign", 0) + n


def load_symbol_prices(
    db: Database,
    fmp: FMPClient,
    symbol: str,
    *,
    run: RunLog,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    overlap_days: int = 5,
    stats: Optional[dict] = None,
    backfill_start: Optional[date] = None,
    guard: Optional[RestatementGuard] = None,
    restated: Optional[list] = None,
) -> int:
    """Load one symbol's prices within the (incremental) window.

    Returns rows upserted. ``stats``, when given, accumulates outcomes that only mean
    something in aggregate -- see :func:`load_prices`, which uses them to tell a
    genuinely empty load apart from a successful one. ``backfill_start`` is where a
    symbol with no watermark starts (see :func:`_window_start`).

    ``guard`` bounds what the load may restate (RESTATEMENTS, in the module
    docstring). A load with no ``start_date`` is the nightly incremental one: a bar
    that would restate a stored one beyond the guard is not written, and is flagged.
    With a ``start_date`` it is an explicit re-fetch, which overwrites, appends what
    it restated to ``restated`` and resolves the conflicts on the dates it covered.
    """

    def _tally(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    guard = guard or RestatementGuard()
    explicit = start_date is not None
    sec_id = repo.resolve_security_id(db, symbol)
    if sec_id is None:
        logger.warning("Unknown symbol %s; skipping (load securities first)", symbol)
        _tally("unknown")
        return 0
    if not repo.is_fed_by(db, sec_id, repo.FMP_SOURCE):
        # Not a security FMP feeds (ADR 0015 §2): one an operator split off another
        # (`fafnir security split-history`), or one another vendor minted. Either
        # way FMP serves its ticker for a different issuer, so whatever comes back
        # under it is not this security's.
        logger.warning(
            "%s resolves to security %s, which FMP does not feed; not loading prices "
            "into it",
            symbol,
            sec_id,
        )
        _tally("not_fmp_fed")
        return 0

    # Read the security once per symbol, not once per bar: what shape of payload
    # counts as a valid bar, and which calendar says a date was a session, are
    # properties of the security, and a fund's whole history goes through this loop.
    profile = repo.security_price_profile(db, sec_id) or {}
    nav_only = _is_nav_priced(profile.get("asset_type"), profile.get("is_fund"))

    if start_date is None:
        wm = repo.get_watermark(db, "fmp", ENDPOINT, sec_id)
        start_date = _window_start(None, wm, overlap_days, backfill_start)

    bars = fmp.eod_raw(
        symbol,
        from_date=start_date.isoformat() if start_date else None,
        to_date=end_date.isoformat() if end_date else None,
    )

    repo.land_payload(
        db,
        endpoint=ENDPOINT,
        params={
            "symbol": symbol,
            "from": start_date.isoformat() if start_date else None,
            "to": end_date.isoformat() if end_date else None,
        },
        symbol=symbol,
        http_status=200,
        payload=bars,
        payload_hash=payload_hash(bars),
        nbytes=0,
        ingestion_run_id=run.run_id,
    )

    if stats is not None:
        stats["bars"] = stats.get("bars", 0) + len(bars)
    if not bars:
        # Routine on an incremental run (no new session yet); a red flag on a
        # backfill. Only the caller can tell which, so record and move on.
        _tally("empty")
        logger.debug("No bars returned for %s (from=%s)", symbol, start_date)

    bars, off_session = _drop_non_session(
        bars, _session_calendar(db, profile.get("exchange_code"), bars)
    )
    if off_session:
        if stats is not None:
            stats["non_session"] = stats.get("non_session", 0) + off_session
        logger.debug(
            "%s: set aside %d bar(s) dated on non-session days", symbol, off_session
        )

    # A bar on a date an operator edited (ops.operator_override) is set aside the
    # same way. For a deleted bar, the vendor that sent it still serves it, so
    # without this the overlap window re-inserts it on the next run. For a re-dated or
    # re-scaled one (`prices shift|rescale`), the vendor's copy of that date is the
    # wrong version the operator replaced. Like a non-session bar it is not a
    # quarantine, and it does not hold the watermark.
    bars, removed = _drop_suppressed(bars, repo.suppressed_price_dates(db, sec_id))
    if removed:
        if stats is not None:
            stats["suppressed"] = stats.get("suppressed", 0) + removed
        logger.info(
            "%s: set aside %d bar(s) on dates an operator deleted or corrected "
            "(fafnir override list)",
            symbol,
            removed,
        )

    clean: list[dict] = []
    quarantined_dates: list[date] = []
    for bar in bars:
        row, reason = _validate_bar(bar, nav_only=nav_only)
        if reason:
            run.rows_quarantined += 1
            # Deliberately add_dq_flag, not add_dq_flag_once: here each detection
            # IS the signal. count_price_quarantines counts these rows to decide
            # when a persistently-bad bar has held the watermark long enough
            # (MAX_QUARANTINE_HOLDS, below), so deduplicating them would freeze
            # that counter at 1 and hold the watermark behind the bar forever.
            repo.add_dq_flag(
                db,
                check_name=f"price_{reason}",
                severity="warn",
                security_id=sec_id,
                table_name="core.daily_price",
                record_key={"symbol": symbol, "date": str(bar.get("date"))},
                detail={"reason": reason},
                ingestion_run_id=run.run_id,
            )
            # Remember the (parseable) date so the watermark won't skip past it.
            qd = _parse_date(bar.get("date"))
            if qd is not None:
                quarantined_dates.append(qd)
            continue
        row["security_id"] = sec_id
        clean.append(row)

        # The bar stores, so it is not a quarantine -- but it may store wrong. See
        # _scale_collapse_detail: a real range flattened to one value by the money
        # column's scale passes every check above and is indistinguishable
        # downstream from a genuine no-trade session.
        #
        # add_dq_flag_once, unlike the quarantine flag above: this condition is a
        # property of a stored bar, not of an attempt to store one, so re-reading
        # the same bar on the next overlap must not add a second row. Nothing counts
        # its repeats the way count_price_quarantines counts the quarantine flags'
        # -- which is exactly why that function now excludes this check by name.
        collapse = _scale_collapse_detail(
            bar, row, nav_strike=_is_nav_strike(bar, nav_only)
        )
        if collapse is not None:
            repo.add_dq_flag_once(
                db,
                check_name=SCALE_COLLAPSE_CHECK,
                severity="warn",
                security_id=sec_id,
                table_name="core.daily_price",
                record_key={"symbol": symbol, "date": str(bar.get("date"))},
                detail=collapse,
                ingestion_run_id=run.run_id,
            )

    stored = repo.stored_bars_on(db, sec_id, [r["trade_date"] for r in clean])
    # A date another party holds is not FMP's to restate (ADR 0015): the upsert
    # skips and counts it, so the guard neither flags nor resolves anything there.
    # Whether FMP's read disagrees with that row is the reconcile's question.
    theirs = {d for d, old in stored.items() if old["source"] != repo.FMP_SOURCE}
    foreign = [r for r in clean if r["trade_date"] in theirs]
    if foreign:
        clean = [r for r in clean if r["trade_date"] not in theirs]
    clean, held, scaled = _guard_bars(
        db,
        sec_id,
        symbol,
        clean,
        stored,
        explicit=explicit,
        guard=guard,
        run=run,
        restated=restated,
    )
    for qd in scaled:
        quarantined_dates.append(qd)
    if stats is not None and held:
        stats["restatement_conflicts"] = stats.get("restatement_conflicts", 0) + len(
            held
        )

    written = repo.upsert_daily_prices(db, clean + foreign, ingestion_run_id=run.run_id)
    if written.skipped_foreign:
        # Keys another party holds (ADR 0015): left as they are, and counted on the
        # run rather than as quarantines, which keep meaning "the feed sent a bad
        # row". Today every vendor row is FMP's, so this stays at zero until a
        # second vendor loads.
        _tally_skipped_foreign(run, written.skipped_foreign)
        if stats is not None:
            stats["skipped_foreign"] = (
                stats.get("skipped_foreign", 0) + written.skipped_foreign
            )

    # Advance the watermark only up to the latest *contiguous* clean date: never
    # past the earliest quarantined bar, so the overlap re-fetches that date next
    # run and a later upstream correction can still land (no permanent gap).
    #
    # Bounded: a date that has already been quarantined MAX_QUARANTINE_HOLDS times
    # stops holding the line (it stays flagged for review, but the watermark is
    # allowed past it) so a permanently-bad bar can't stall ingestion forever and
    # grow the re-pull window without bound.
    holding = [
        qd
        for qd in quarantined_dates
        if repo.count_price_quarantines(db, sec_id, qd.isoformat())
        < MAX_QUARANTINE_HOLDS
    ]
    # A date whose re-read was held still has its stored bar: it is not a hole, and
    # the watermark may pass it.
    clean_dates = [r["trade_date"] for r in clean] + held
    if holding:
        cutoff = min(holding)
        safe_dates = [d for d in clean_dates if d < cutoff]
    else:
        safe_dates = clean_dates
    if safe_dates:
        repo.set_watermark(db, "fmp", ENDPOINT, max(safe_dates), sec_id)
    elif quarantined_dates and not holding:
        # Nothing in the window survived validation, and every bad bar has spent its
        # MAX_QUARANTINE_HOLDS budget. Without this branch the watermark is never
        # written at all: `safe_dates` is derived from `clean_dates`, so a symbol
        # with no storable bar anywhere in its history takes the `else` above, gets
        # an empty list, and skips the write -- leaving get_watermark returning None,
        # which sends the next run back to `from_date=None` and re-pulls the entire
        # history. The budget above cannot save it, because exhausting the budget
        # only widens an already-empty set.
        #
        # That is not hypothetical: a security quoted below the 1e-6 the money
        # column can hold has EVERY bar rejected, so it was re-fetched and re-flagged
        # in full every night, spending the bandwidth and writing one
        # price_subresolution_price flag per bar per run without bound.
        #
        # Advancing to the last bar seen is the same trade the budget already makes
        # -- the bars stay flagged for review, ingestion just stops re-litigating
        # them. `overlap_days` still re-reads the recent tail, so a genuine upstream
        # correction inside that window is picked up.
        logger.warning(
            "%s: no storable bars in the window and every quarantined date has "
            "spent its budget; advancing the watermark to %s so the history is not "
            "re-pulled nightly. The bars stay flagged for review.",
            symbol,
            max(quarantined_dates),
        )
        repo.set_watermark(db, "fmp", ENDPOINT, max(quarantined_dates), sec_id)

    run.rows_inserted += written
    return written


def _guard_bars(
    db: Database,
    sec_id: int,
    symbol: str,
    clean: list[dict],
    stored: dict,
    *,
    explicit: bool,
    guard: RestatementGuard,
    run: RunLog,
    restated: Optional[list],
) -> tuple[list[dict], list[date], list[date]]:
    """Hold back what a load must not write. Returns ``(to_write, held_dates,
    quarantined_dates)``.

    * A bar with a stored copy that it restates beyond ``guard``: on a nightly load
      the stored bar stays and ``price_restatement_conflict`` is flagged (the date is
      held, not quarantined); on an explicit re-fetch it is written, reported in
      ``restated`` and the run's params, and the date's open conflict is resolved.
    * A bar written over a stored copy whose volume differs only as a round lot's
      rounding (:func:`odd_lot_rounding`): written with the stored volume, on either
      load, and counted ``restatement_odd_lot_kept`` in the run's params.
    * A bar with no stored copy at a split-adjusted scale: quarantined
      (``price_split_scale_mismatch``), on any load.
    """
    to_write: list[dict] = []
    held: list[date] = []
    scaled: list[date] = []
    conflicts: list[tuple[dict, dict, dict]] = []
    for row in clean:
        old = stored.get(row["trade_date"])
        if old is None:
            to_write.append(row)
            continue
        detail = restatement_detail(old, row, guard)
        if detail is not None and not explicit:
            conflicts.append((row, old, detail))
            held.append(row["trade_date"])
            continue
        if odd_lot_rounding(old, row, guard):
            # The vendor's round lot is not a revision of the first print's count.
            row = {**row, "volume": old["volume"]}
            kept = run.params.get("restatement_odd_lot_kept", 0)
            run.params["restatement_odd_lot_kept"] = kept + 1
        if detail is not None:
            conflicts.append((row, old, detail))
        to_write.append(row)

    if conflicts:
        splits = repo.splits_near(
            db,
            sec_id,
            min(r["trade_date"] for r, _, _ in conflicts)
            - timedelta(days=RESTATEMENT_SPLIT_DAYS),
            max(r["trade_date"] for r, _, _ in conflicts)
            + timedelta(days=RESTATEMENT_SPLIT_DAYS),
        )
    for row, old, detail in conflicts:
        d = row["trade_date"]
        near = [
            {"ex_date": str(s["ex_date"]), "split": s["split"]}
            for s in splits
            if abs((s["ex_date"] - d).days) <= RESTATEMENT_SPLIT_DAYS
        ]
        if not explicit:
            logger.warning(
                "%s %s: the vendor restates the stored bar (close %s -> %s, volume "
                "%s -> %s); kept the stored one and flagged %s",
                symbol,
                d,
                old["close"],
                row["close"],
                old["volume"],
                row["volume"],
                RESTATEMENT_CHECK,
            )
            repo.add_dq_flag_once(
                db,
                check_name=RESTATEMENT_CHECK,
                severity="warn",
                security_id=sec_id,
                table_name="core.daily_price",
                record_key={"symbol": symbol, "date": d.isoformat()},
                detail={**detail, "splits_near": near},
                ingestion_run_id=run.run_id,
            )
            continue
        entry = {
            "symbol": symbol,
            "security_id": sec_id,
            "date": d.isoformat(),
            "over": detail["over"],
            "close": [str(old["close"]), str(row["close"])],
            "volume": [old["volume"], row["volume"]],
        }
        if restated is not None:
            restated.append(entry)
        logged = run.params.setdefault("restated", [])
        if len(logged) < RESTATED_PARAMS_MAX:
            logged.append(entry)
        run.params["restated_count"] = run.params.get("restated_count", 0) + 1

    if explicit:
        written = {r["trade_date"]: r for r in to_write}
        for flag in repo.open_restatement_conflicts(
            db, sec_id, RESTATEMENT_CHECK, list(written)
        ):
            d = date.fromisoformat(flag["d"])
            old, new = stored.get(d) or {}, written[d]
            repo.resolve_dq_flags(
                db,
                repo.DqFilter(flag_ids=(int(flag["dq_flag_id"]),)),
                note=(
                    f"operator re-fetch, run {run.run_id}: close "
                    f"{old.get('close')} -> {new['close']}, volume "
                    f"{old.get('volume')} -> {new['volume']}"
                ),
                resolved_by="ingest prices",
            )

    fresh = [r for r in to_write if r["trade_date"] not in stored]
    if not fresh:
        return to_write, held, scaled
    splits_after = repo.splits_near(
        db,
        sec_id,
        min(r["trade_date"] for r in fresh) + timedelta(days=1),
        max(r["trade_date"] for r in fresh) + timedelta(days=SPLIT_SCALE_DAYS),
    )
    if not splits_after:
        return to_write, held, scaled
    out: list[dict] = []
    for row in to_write:
        d = row["trade_date"]
        upcoming = [
            s
            for s in splits_after
            if d < s["ex_date"] <= d + timedelta(days=SPLIT_SCALE_DAYS)
        ]
        if d in stored or not upcoming:
            out.append(row)
            continue
        prev = repo.close_before(db, sec_id, d)
        median = repo.median_volume_before(
            db, sec_id, d, bars=SPLIT_SCALE_MEDIAN_SESSIONS
        )
        detail = next(
            (
                found
                for s in upcoming
                if prev is not None
                and (found := split_scale_detail(row, prev, median, s)) is not None
            ),
            None,
        )
        if detail is None:
            out.append(row)
            continue
        logger.warning(
            "%s %s: close %s is the last close %s at the scale of the %s split of "
            "%s; quarantined as %s",
            symbol,
            d,
            row["close"],
            prev,
            detail["split"],
            detail["split_ex_date"],
            SPLIT_SCALE_CHECK,
        )
        run.rows_quarantined += 1
        # A quarantine, so add_dq_flag: its repeats are the watermark's budget.
        repo.add_dq_flag(
            db,
            check_name=SPLIT_SCALE_CHECK,
            severity="warn",
            security_id=sec_id,
            table_name="core.daily_price",
            record_key={"symbol": symbol, "date": d.isoformat()},
            detail=detail,
            ingestion_run_id=run.run_id,
        )
        scaled.append(d)
    return out, held, scaled


def _guard_split_adjusted_changeover(db: Database, start_date: Optional[date]) -> None:
    """Refuse to run incrementally on a warehouse still holding split-adjusted prices.

    Switching to the unadjusted endpoint also changes the watermark key, so every
    symbol looks brand new. On an incremental run that means ``from_date=None``, and
    a bare request is capped at 5000 bars -- the warehouse would quietly refill with
    ~20 years of history and keep the old split-adjusted rows for everything older.
    A backfill with an explicit ``--from`` is the only safe path across the boundary,
    so make the operator take it.
    """
    if start_date is not None:
        return  # explicit window: this IS the re-backfill.
    if repo.count_watermarks(db, "fmp", ENDPOINT):
        return  # already loading from the unadjusted feed.
    legacy = repo.count_watermarks(db, "fmp", LEGACY_SPLIT_ADJUSTED_ENDPOINT)
    if not legacy:
        return  # a fresh warehouse: nothing to migrate.
    raise SourceError(
        f"{legacy} symbol(s) were last loaded from "
        f"{LEGACY_SPLIT_ADJUSTED_ENDPOINT}, whose bars are split-adjusted. "
        f"core.daily_price must hold raw prices, so those rows have to be replaced, "
        "not appended to. Re-backfill explicitly (see doc/backfill.md, "
        "'Switching to the unadjusted price feed'):\n"
        "  fafnir ingest prices --include-inactive --from <backfill-start>\n"
        "then `fafnir adjust` and `fafnir db refresh-marts`. An incremental run "
        "cannot cross this boundary safely and has been refused."
    )


def load_prices(
    db: Database,
    fmp: FMPClient,
    symbols: Iterable[str],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    overlap_days: int = 5,
    backfill_start: Optional[date] = None,
    guard: Optional[RestatementGuard] = None,
    restated: Optional[list] = None,
) -> int:
    """Load prices for many symbols.

    ``backfill_start`` is where a symbol loaded for the first time (no watermark, no
    explicit window) starts; the CLI passes the configured calendar_start_year.
    ``guard`` and ``restated``: see :func:`load_symbol_prices`.

    Raises rather than returning a quiet zero when the load cannot have worked.
    A run that reports success having written nothing is the most expensive kind
    of failure here: ``initial_backfill.sh`` would march on to the adjustment and
    mart steps, and the warehouse would look finished while holding no prices.
    """
    symbols = list(symbols)
    if not symbols:
        raise ValueError(
            "No symbols to load. Populate the security master first "
            "(fafnir ingest securities), or pass --symbols."
        )
    _guard_split_adjusted_changeover(db, start_date)

    with RunLog(
        db,
        source="fmp",
        endpoint=ENDPOINT,
        params={"symbols": len(symbols)},
        window_from=start_date,
        window_to=end_date,
    ) as run:
        total = 0
        stats: dict[str, int] = {}
        for symbol in symbols:
            total += load_symbol_prices(
                db,
                fmp,
                symbol,
                run=run,
                start_date=start_date,
                end_date=end_date,
                overlap_days=overlap_days,
                stats=stats,
                backfill_start=backfill_start,
                guard=guard,
                restated=restated,
            )
            # One symbol -- its landing payload, bars, DQ flags and watermark --
            # is the unit of work. Committing here is what makes the backfill
            # resumable in practice: an interruption costs this symbol, and the
            # watermarks already written let a re-run skip what is done.
            db.commit()
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded

        unknown = stats.get("unknown", 0)
        empty = stats.get("empty", 0)
        bars = stats.get("bars", 0)
        if unknown == len(symbols):
            raise ValueError(
                f"None of the {len(symbols)} requested symbols exist in the "
                "security master -- run `fafnir ingest securities` first"
            )
        if unknown:
            logger.warning(
                "%d of %d symbols are not in the security master", unknown, len(symbols)
            )
        # An explicit window means we asked for history that must exist. With no
        # window this is the incremental path, where "nothing new" is the normal
        # answer outside trading hours and must not fail the nightly run.
        if start_date is not None and bars == 0:
            raise SourceError(
                f"FMP returned no bars for any of {len(symbols)} symbols since "
                f"{start_date} -- treating a backfill that loaded nothing as a failure"
            )
        if empty:
            logger.info(
                "%d of %d symbols returned no bars in the requested window",
                empty,
                len(symbols),
            )
        conflicts = stats.get("restatement_conflicts", 0)
        if conflicts:
            logger.warning(
                "Kept %d stored bar(s) the vendor restated beyond the guard; flagged "
                "as %s (fafnir dq list --check %s)",
                conflicts,
                RESTATEMENT_CHECK,
                RESTATEMENT_CHECK,
            )
        non_session = stats.get("non_session", 0)
        if non_session:
            logger.info(
                "Set aside %d bars dated on non-session days (weekends, holidays)",
                non_session,
            )
        logger.info("Loaded %d price rows across %d symbols", total, len(symbols))
        return total
