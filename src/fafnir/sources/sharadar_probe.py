"""
`fafnir source probe-sharadar`: measure the Sharadar feed before anything loads it
(plan SA-0201, #49).

The repository does not adopt an endpoint on the strength of its documentation
(ADR 0007). The client (:mod:`fafnir.sources.sharadar`) and duk were built from
Sharadar's agent guide; the loaders still to come (#52, #58, #75) rest on facts no
document states -- how precise the prices are, which way a split's value points,
whether a dividend is as declared, how a reused ticker is spelled. This probe asks
the paid feed each of them, on a deterministic sample drawn across decades, and
gives each a verdict:

* **PASS** -- the fact is as the design assumes;
* **NOTE** -- a fact the design must accommodate, with the consequence stated (the
  loader converts, a check watches, a mitigation is recorded);
* **BLOCK** -- the fact contradicts the design; nothing loads until it has a
  written mitigation in the plan's decision log. The command exits non-zero.

The items (the numbering of #49):

1. API mechanics -- every documented or assumed request behaviour, asked directly.
2. Decimal precision of the price fields and volume.
3. Raw recovery -- ``closeunadj / close`` against the cumulative split product
   ACTIONS implies.
4. Split value direction (AAPL 2020-08-31, 4-for-1), and its consistency across
   the sample.
5. Dividend value -- as declared or split-adjusted (AAPL 2019-11-07, $0.77
   declared).
6. Ticker keys -- whether history is keyed by the current ticker (META, which was
   FB until 2022-06-09), and how a reused ticker of a dead issuer is spelled.
7. permaticker across tables.
8. Same-day values -- on the newest session, ``close == closeunadj``.
9. Update behaviour -- whether ``lastupdated`` moves on historical rows when a
   split or dividend restates them.
10. Row shapes -- null against zero.
11. The API as :mod:`fafnir.sources.sharadar` implements it (``duk -S sharadar``
    requests through the same client, ADR 0017).

Read-only: it writes nothing. Its report holds vendor rows (the worst cases, as
evidence), so it stays on the host; post only verdicts and aggregates (the repo and
its issues are public, ADR 0012 §8). Costs about ``2 x sample + 30`` requests.
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from fafnir.logging_config import get_logger
from fafnir.sources.base import SourceError
from fafnir.sources.sharadar import SharadarClient, rows_from_payload

logger = get_logger("source.probe_sharadar")

PASS, NOTE, BLOCK = "PASS", "NOTE", "BLOCK"

PRICE_FIELDS = ("open", "high", "low", "close", "closeadj", "closeunadj")

# Items 4-6 ask about events whose answers are public record.
SPLIT_CASE = ("AAPL", "2020-08-31", Decimal(4))  # 4-for-1
DIVIDEND_CASE = ("AAPL", "2019-11-07", Decimal("0.77"))  # as declared
RENAME_CASE = ("META", "FB", "2022-06-09")  # current, former, rename date

# Item 3's bands, relative to close x split product. A split ACTIONS lacks or
# double-counts moves the ratio by the split's own factor (>= 1.5x for anything
# but a 3-for-2 or its inverse), far outside "near"; rounding moves it by the
# 3-dp half-unit scaled by the product.
EXACT_REL = Decimal("1e-6")
NEAR_REL = Decimal("0.01")
# More than this share of split-affected bars outside "near" is systematic, not
# an issuer's bad row (#75's sharadar_split_ratio_mismatch watches those).
MISMATCH_BLOCK_SHARE = Decimal("0.01")

DECADES = (1990, 2000, 2010, 2020)
FUND_SHARE = 0.1


@dataclass
class Item:
    number: int
    name: str
    verdict: str
    level: str
    detail: str
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimals(value: Any) -> int:
    """Decimal places as the payload carries them (``18.19`` -> 2, ``4`` -> 0)."""
    d = _dec(value)
    if d is None:
        return 0
    exp = d.normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def splits_of(actions: Iterable[dict]) -> list[tuple[str, Decimal]]:
    """``(ex_date, value)`` for each split in an ACTIONS slice."""
    out = []
    for a in actions:
        if a.get("action") == "split":
            v = _dec(a.get("value"))
            if v:
                out.append((str(a["date"]), v))
    return out


def split_product_after(splits: list[tuple[str, Decimal]], day: str) -> Decimal:
    """The product of the splits that went ex strictly after ``day``: the factor a
    bar on ``day`` was divided by to make it split-adjusted. A bar on the ex-date
    already trades post-split."""
    prod = Decimal(1)
    for ex, value in splits:
        if ex > day:
            prod *= value
    return prod


def _body(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _rows(resp) -> Optional[list[dict]]:
    data = _body(resp)
    if resp.status_code != 200 or data is None:
        return None
    try:
        return rows_from_payload(data)
    except SourceError:
        return None


def _share(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _check(ok: bool, detail: str, *, blocks: bool = False) -> dict:
    return {"ok": bool(ok), "detail": detail, "blocks": blocks}


# ---------------------------------------------------------------------------
# Item 1 and 11: the API's mechanics, asked directly
# ---------------------------------------------------------------------------


def mechanics(client: SharadarClient) -> dict[str, dict]:
    """Each documented or assumed behaviour as one check. ``blocks`` marks the ones
    the client relies on: if one fails, a loader built on it is wrong."""
    c: dict[str, dict] = {}
    T = client.T_SEP

    tier = client.tier()
    c["paid_key"] = _check(tier == "paid", f"key served as {tier}", blocks=True)

    base = client.response(T, {"ticker": "AAPL", "limit": 3, "format": "json"})
    body = _body(base)
    rows = _rows(base) or []
    shape = sorted(body) if isinstance(body, dict) else type(body).__name__
    c["payload_shape"] = _check(
        isinstance(body, dict)
        and "data" in body
        and "count" in body
        and len(rows) == 3,
        f"HTTP {base.status_code}, keys {shape}, {len(rows)} rows",
        blocks=True,
    )
    dates = [r.get("date") for r in rows]
    c["default_order_newest_first"] = _check(
        dates == sorted(dates, reverse=True), f"first dates {dates}"
    )

    headers = {k.lower(): v for k, v in base.headers.items()}
    limits = {
        k: headers.get(f"x-ratelimit-{k}")
        for k in ("limit", "remaining", "reset", "weighted-limit", "cost")
    }
    c["rate_limit_headers"] = _check(
        limits["limit"] is not None, ", ".join(f"{k}={v}" for k, v in limits.items())
    )

    asc = _rows(
        client.response(
            T, {"ticker": "AAPL", "limit": 2, "sort": "date", "format": "json"}
        )
    ) or [{}]
    c["sort_date_ascending"] = _check(
        bool(asc[0].get("date")) and (not dates or asc[0]["date"] < dates[-1]),
        f"first date with sort=date: {asc[0].get('date')}",
    )

    off = _rows(
        client.response(
            T, {"ticker": "AAPL", "limit": 2, "offset": 1, "format": "json"}
        )
    ) or [{}]
    c["offset_honoured"] = _check(
        len(rows) > 1 and off[0].get("date") == rows[1].get("date"),
        f"offset=1 starts at {off[0].get('date')}, second row was "
        f"{rows[1].get('date') if len(rows) > 1 else None}",
        blocks=True,
    )

    fields = _rows(
        client.response(
            T,
            {
                "ticker": "AAPL",
                "limit": 1,
                "fields": "ticker,date,close",
                "format": "json",
            },
        )
    ) or [{}]
    c["fields_honoured"] = _check(
        sorted(fields[0]) == ["close", "date", "ticker"], f"columns {sorted(fields[0])}"
    )

    multi = (
        _rows(
            client.response(
                T, {"ticker": "AAPL,MSFT", "date": "2024-01-02", "format": "json"}
            )
        )
        or []
    )
    c["ticker_list"] = _check(
        {r.get("ticker") for r in multi} == {"AAPL", "MSFT"},
        f"ticker=AAPL,MSFT on 2024-01-02 -> {sorted({r.get('ticker') for r in multi})}",
    )

    window = (
        _rows(
            client.response(
                T,
                {
                    "ticker": "AAPL",
                    "from": "2020-08-27",
                    "to": "2020-09-02",
                    "format": "json",
                },
            )
        )
        or []
    )
    in_window = all("2020-08-27" <= r.get("date", "") <= "2020-09-02" for r in window)
    c["from_to_honoured"] = _check(
        bool(window) and in_window,
        f"{len(window)} rows, all in window: {in_window}",
        blocks=True,
    )

    sf1 = (
        _rows(
            client.response(
                client.T_SF1,
                {
                    "ticker": "AAPL",
                    "dimension": "ARQ",
                    "from": "2026-01-01",
                    "fields": "calendardate,date",
                    "format": "json",
                },
            )
        )
        or []
    )
    on_date = all(r.get("date", "") >= "2026-01-01" for r in sf1)
    on_cal = all(r.get("calendardate", "") >= "2026-01-01" for r in sf1)
    c["sf1_from_filters_date"] = _check(
        bool(sf1) and on_date,
        f"{len(sf1)} rows; all date >= from: {on_date}; all calendardate >= from: {on_cal}",
    )

    cap = client.response(T, {"ticker": "AAPL", "limit": 10_001, "format": "json"})
    capb = _body(cap)
    c["json_row_cap_10000"] = _check(
        cap.status_code != 200 and "limit" in str(capb).lower(),
        f"limit=10001 -> HTTP {cap.status_code} {str(capb)[:100]}",
        blocks=True,
    )

    future = _rows(
        client.response(
            T, {"lastupdated.gte": "2099-01-01", "limit": 1, "format": "json"}
        )
    )
    c["lastupdated_filter"] = _check(
        future == [],
        f"lastupdated.gte=2099-01-01 -> {None if future is None else len(future)} rows",
    )

    bogus = _rows(
        client.response(
            T,
            {
                "ticker": "AAPL",
                "limit": 1,
                "nosuchfilter.gte": "2099-01-01",
                "format": "json",
            },
        )
    )
    c["unknown_param_ignored"] = _check(
        bool(bogus),
        "an unknown parameter is "
        + (
            "ignored, rows returned: a misspelt filter fails silently"
            if bogus
            else "refused"
        ),
    )

    legacy = _rows(
        client.response(
            client.T_TICKERS, {"table": "SEP", "limit": 1, "format": "json"}
        )
    )
    modern = _rows(
        client.response(
            client.T_TICKERS, {"table": "stocks", "limit": 1, "format": "json"}
        )
    )
    c["tickers_table_filter_modern_names"] = _check(
        bool(modern),
        f"table=stocks -> {len(modern or [])} row(s); table=SEP -> {len(legacy or [])}",
    )

    by_name = _rows(
        client.response("stocks", {"ticker": "AAPL", "limit": 1, "format": "json"})
    ) or [{}]
    c["modern_and_legacy_paths"] = _check(
        bool(rows) and by_name[0].get("date") == rows[0].get("date"),
        f"/stocks and /SEP agree on the newest AAPL bar: "
        f"{bool(rows) and by_name[0].get('date') == rows[0].get('date')}",
    )

    bulk = client.response(client.T_TICKERS, {"years": "full"}, allow_redirects=False)
    loc = bulk.headers.get("Location") or ""
    parts = urlsplit(loc)
    c["bulk_redirect"] = _check(
        bulk.status_code in (301, 302, 303, 307, 308) and bool(parts.netloc),
        f"HTTP {bulk.status_code} -> {parts.netloc}{parts.path} (download host: allow it "
        "on the host's egress)",
        blocks=True,
    )
    return c


# ---------------------------------------------------------------------------
# The sample
# ---------------------------------------------------------------------------


def _decade(day: Optional[str]) -> Optional[int]:
    if not day:
        return None
    year = int(str(day)[:4])
    return max(DECADES[0], min(DECADES[-1], year - year % 10))


def draw_sample(listing: list[dict], size: int, seed: int) -> list[dict]:
    """``size`` securities, stratified by the decade of their first price and by
    listed / delisted, deterministically for ``seed``. Strata are filled round
    robin so a thin decade still appears."""
    strata: dict[tuple, list[dict]] = {}
    for row in sorted(listing, key=lambda r: str(r.get("permaticker"))):
        decade = _decade(row.get("firstpricedate"))
        if decade is None:
            continue
        strata.setdefault((decade, row.get("isdelisted") == "Y"), []).append(row)
    rng = random.Random(seed)
    for rows in strata.values():
        rng.shuffle(rows)
    out: list[dict] = []
    keys = sorted(strata)
    while len(out) < size and any(strata[k] for k in keys):
        for k in keys:
            if strata[k] and len(out) < size:
                out.append(strata[k].pop())
    return out


def _listing(client: SharadarClient, table: str) -> list[dict]:
    return client.query(
        client.T_TICKERS,
        {"table": table, "sort": "permaticker"},
        fields=[
            "table",
            "permaticker",
            "ticker",
            "isdelisted",
            "firstpricedate",
            "lastpricedate",
            "category",
            "relatedtickers",
        ],
    )


# ---------------------------------------------------------------------------
# Items 2-10, each a pure function of what was fetched
# ---------------------------------------------------------------------------


def precision(bars: dict[str, list[dict]], splits: dict[str, list]) -> Item:
    maxdp = {f: 0 for f in PRICE_FIELDS}
    vols = vol_int = vol_thousands = affected = 0
    for ticker, rows in bars.items():
        sp = splits.get(ticker, [])
        for r in rows:
            for f in PRICE_FIELDS:
                maxdp[f] = max(maxdp[f], decimals(r.get(f)))
            v = _dec(r.get("volume"))
            if v is not None:
                vols += 1
                vol_int += v == v.to_integral_value()
                vol_thousands += v % 1000 == 0
            if sp and split_product_after(sp, str(r["date"])) != 1:
                affected += 1
    least = min(maxdp.values())
    evidence = {
        "max_decimals": maxdp,
        "bars": sum(len(r) for r in bars.values()),
        "split_affected_bars": affected,
        "volume_integer_share": round(vol_int / vols, 4) if vols else None,
        "volume_multiple_of_1000_share": (
            round(vol_thousands / vols, 4) if vols else None
        ),
    }
    if least >= 4 or not affected:
        return Item(
            2, "precision", "precision_ok", PASS, f"at least {least} dp", evidence
        )
    return Item(
        2,
        "precision",
        f"precision_{least}dp",
        NOTE,
        f"prices carry at most {least} decimal places. The raw close is closeunadj, "
        "exact to that; a raw open/high/low recovered as field x closeunadj/close "
        f"carries up to 0.5e-{least} x the cumulative split factor of error, on "
        f"{affected} split-affected bars in the sample. Volume is split-adjusted "
        f"too, and whole on {_share(evidence['volume_integer_share'])} of bars: a raw "
        "volume recovered as volume x close/closeunadj needs rounding to store. "
        "Mitigation: #75.",
        evidence,
    )


def raw_recovery(bars: dict[str, list[dict]], splits: dict[str, list]) -> Item:
    bands = Counter()
    worst: Optional[dict] = None
    mismatched: Counter = Counter()
    vol_exact = vol_total = 0
    for ticker, rows in bars.items():
        sp = splits.get(ticker, [])
        if not sp:
            continue
        for r in rows:
            prod = split_product_after(sp, str(r["date"]))
            close, unadj = _dec(r.get("close")), _dec(r.get("closeunadj"))
            if prod == 1 or not close or not unadj:
                continue
            expect = close * prod
            rel = abs(unadj / expect - 1)
            resid = abs(unadj - expect)
            half = Decimal(5) * Decimal(10) ** -(decimals(r.get("close")) + 1)
            if rel <= EXACT_REL:
                bands["exact"] += 1
            elif resid <= half * prod + Decimal("0.0005"):
                bands["rounding"] += 1
            elif rel <= NEAR_REL:
                bands["near"] += 1
            else:
                bands["mismatch"] += 1
                mismatched[ticker] += 1
            if worst is None or rel > worst["rel"]:
                worst = {
                    "ticker": ticker,
                    "date": r["date"],
                    "close": str(close),
                    "closeunadj": str(unadj),
                    "split_product": str(prod),
                    "rel": rel,
                }
            v = _dec(r.get("volume"))
            if v is not None:
                vol_total += 1
                q = v / prod
                vol_exact += q == q.to_integral_value()
    total = sum(bands.values())
    evidence = {
        "bars": total,
        "bands": dict(bands),
        "mismatched_tickers": len(mismatched),
        "worst": {**worst, "rel": float(worst["rel"])} if worst else None,
        "raw_volume_integral_share": (
            round(vol_exact / vol_total, 4) if vol_total else None
        ),
    }
    if not total:
        return Item(
            3, "raw_recovery", "inconclusive", NOTE, "no split-affected bar", evidence
        )
    if bands["mismatch"]:
        share = Decimal(bands["mismatch"]) / total
        level = BLOCK if share > MISMATCH_BLOCK_SHARE else NOTE
        return Item(
            3,
            "raw_recovery",
            "ratio_mismatch",
            level,
            f"{bands['mismatch']} of {total} split-affected bars ({float(share):.2%}, "
            f"{len(mismatched)} tickers) are more than {NEAR_REL:%} from close x the "
            "split product ACTIONS implies: a split ACTIONS lacks or double-counts. "
            + (
                "Systematic: decide before loading."
                if level == BLOCK
                else "Isolated issuers: #75's sharadar_split_ratio_mismatch flags them."
            ),
            evidence,
        )
    verdict = (
        "ratio_matches_actions"
        if total == bands["exact"]
        else "ratio_matches_within_rounding"
    )
    return Item(
        3,
        "raw_recovery",
        verdict,
        PASS,
        f"all {total} split-affected bars agree with the ACTIONS split product "
        f"({bands['exact']} exactly, {bands['rounding']} within the rounding of the "
        f"stored decimals, {bands['near']} within {NEAR_REL:%})",
        evidence,
    )


def split_direction(
    case_actions: list[dict], bars: dict[str, list[dict]], splits: dict[str, list]
) -> Item:
    ticker, ex, ratio = SPLIT_CASE
    hit = [v for d, v in splits_of(case_actions) if d == ex]
    consistent = checked = 0
    for t, sp in splits.items():
        by_date = {str(r["date"]): r for r in bars.get(t, [])}
        days = sorted(by_date)
        for d, value in sp:
            before = [x for x in days if x < d]
            if d not in by_date or not before:
                continue
            a, b = by_date[before[-1]], by_date[d]
            ca, ua = _dec(a.get("close")), _dec(a.get("closeunadj"))
            cb, ub = _dec(b.get("close")), _dec(b.get("closeunadj"))
            if not (ca and ua and cb and ub):
                continue
            checked += 1
            # The unadjusted/adjusted ratio steps down by the split's own factor
            # across its ex-date.
            step = (ua / ca) / (ub / cb)
            consistent += abs(step / value - 1) <= NEAR_REL
    evidence = {
        "case": f"{ticker} {ex}",
        "case_value": str(hit[0]) if hit else None,
        "sample_splits_checked": checked,
        "sample_consistent": consistent,
    }
    if hit and hit[0] == ratio:
        level = PASS if consistent == checked else NOTE
        return Item(
            4,
            "split_direction",
            "split_value_new_per_old",
            level,
            f"{ticker}'s {ratio}-for-1 is value {hit[0]} (new shares per old; a 1-for-4 "
            f"is 0.25). Across the sample {consistent} of {checked} splits step the "
            "unadjusted/adjusted ratio by their value on the ex-date.",
            evidence,
        )
    if hit and hit[0] == 1 / ratio:
        return Item(
            4,
            "split_direction",
            "split_value_old_per_new",
            NOTE,
            f"{ticker}'s {ratio}-for-1 is value {hit[0]}: the loader inverts it",
            evidence,
        )
    return Item(
        4,
        "split_direction",
        "unknown",
        BLOCK,
        f"no {ticker} split on {ex} with value {ratio} or its inverse: {hit}",
        evidence,
    )


def dividend_value(case_actions: list[dict]) -> Item:
    ticker, ex, declared = DIVIDEND_CASE
    sp = splits_of(case_actions)
    hit = [
        _dec(a.get("value"))
        for a in case_actions
        if a.get("action") == "dividend" and str(a.get("date")) == ex
    ]
    prod = split_product_after(sp, ex)
    evidence = {
        "case": f"{ticker} {ex}",
        "declared": str(declared),
        "value": str(hit[0]) if hit else None,
        "split_product_after": str(prod),
    }
    if hit and hit[0] == declared:
        return Item(
            5,
            "dividend",
            "dividend_as_declared",
            PASS,
            "value is as declared",
            evidence,
        )
    if hit and prod != 1 and abs(hit[0] * prod - declared) <= Decimal("0.0005") * prod:
        return Item(
            5,
            "dividend",
            "dividend_split_adjusted",
            NOTE,
            f"{ticker}'s ${declared} on {ex} is {hit[0]} = {declared} / {prod}: dividends "
            "are split-adjusted. core.corporate_action holds the amount as declared, so "
            "the loader multiplies by the split product after the ex-date (the same "
            "ACTIONS splits), and a split ACTIONS lacks would mis-state the dividend.",
            evidence,
        )
    return Item(
        5,
        "dividend",
        "unknown",
        BLOCK,
        f"{ticker} {ex}: value {hit} matches neither",
        evidence,
    )


def ticker_keys(
    current_rows: list[dict], former_rows: list[dict], listing: list[dict]
) -> Item:
    current, former, renamed = RENAME_CASE
    earliest = min((str(r["date"]) for r in current_rows), default=None)
    dead = [r for r in listing if r.get("isdelisted") == "Y"]
    suffixed = [r for r in dead if re.search(r"[A-Z]\d+$", str(r.get("ticker", "")))]
    live = {r.get("ticker") for r in listing if r.get("isdelisted") != "Y"}
    reused = [r for r in suffixed if re.sub(r"\d+$", "", r["ticker"]) in live]
    evidence = {
        "case": f"{current} (was {former} until {renamed})",
        "earliest_bar_under_current": earliest,
        "rows_under_former": len(former_rows),
        "delisted": len(dead),
        "delisted_with_numeric_suffix": len(suffixed),
        "suffix_root_live_today": len(reused),
    }
    if earliest and earliest < renamed:
        return Item(
            6,
            "ticker_keys",
            "history_under_current_ticker",
            NOTE,
            f"{current}'s bars start {earliest}, before the {renamed} rename; {former} "
            f"returns {len(former_rows)} rows. A dead issuer keeps a numeric suffix "
            f"({len(suffixed)} of {len(dead)} delisted, {len(reused)} on a root a live "
            "security trades under). Loaders key on permaticker, never on the ticker of "
            "the day (#55).",
            evidence,
        )
    return Item(
        6,
        "ticker_keys",
        "history_under_ticker_of_the_day",
        BLOCK,
        f"{current}'s bars start {earliest}: the rename splits the history",
        evidence,
    )


def permaticker_across_tables(
    stocks: list[dict], fundamentals: list[dict], sep_columns: set, sf1_columns: set
) -> Item:
    by_ticker = {r.get("ticker"): r.get("permaticker") for r in stocks}
    both = [r for r in fundamentals if r.get("ticker") in by_ticker]
    same = sum(1 for r in both if by_ticker[r["ticker"]] == r.get("permaticker"))
    evidence = {
        "tickers_in_both": len(both),
        "same_permaticker": same,
        "sep_has_permaticker": "permaticker" in sep_columns,
        "sf1_has_permaticker": "permaticker" in sf1_columns,
    }
    if both and same == len(both):
        return Item(
            7,
            "permaticker",
            "permaticker_shared",
            NOTE,
            f"all {same} tickers listed for both stocks and fundamentals carry one "
            "permaticker. SEP and SF1 rows carry only the ticker, so they reach the "
            "permaticker through TICKERS on (ticker, table) -- as of the file's date.",
            evidence,
        )
    return Item(
        7,
        "permaticker",
        "permaticker_differs",
        BLOCK,
        f"{len(both) - same} of {len(both)} tickers carry different permatickers",
        evidence,
    )


def same_day(bars: dict[str, list[dict]]) -> Item:
    newest = max((str(r["date"]) for rows in bars.values() for r in rows), default=None)
    checked = agree = 0
    for rows in bars.values():
        for r in rows:
            if str(r["date"]) == newest:
                checked += 1
                agree += _dec(r.get("close")) == _dec(r.get("closeunadj"))
    evidence = {"date": newest, "bars": checked, "close_equals_closeunadj": agree}
    if checked and agree == checked:
        return Item(
            8,
            "same_day",
            "same_day_raw_confirmed",
            PASS,
            f"on {newest} all {checked} sampled bars have close == closeunadj",
            evidence,
        )
    return Item(
        8,
        "same_day",
        "same_day_differs",
        BLOCK if checked else NOTE,
        f"on {newest} {checked - agree} of {checked} bars have close != closeunadj",
        evidence,
    )


def update_behaviour(
    bars: dict[str, list[dict]], actions: dict[str, list[dict]]
) -> Item:
    restamped = older = tickers = 0
    for t, rows in bars.items():
        events = [
            str(a["date"])
            for a in actions.get(t, [])
            if a.get("action") in ("split", "dividend")
        ]
        days = [str(r["date"]) for r in rows]
        if not events or not days:
            continue
        last = max(e for e in events)
        before = [r for r in rows if str(r["date"]) < last]
        if not before or last > max(days):
            continue
        tickers += 1
        older += len(before)
        restamped += sum(1 for r in before if str(r.get("lastupdated") or "") >= last)
    evidence = {
        "tickers": tickers,
        "rows_before_last_event": older,
        "restamped": restamped,
    }
    if not older:
        return Item(
            9, "update_behaviour", "inconclusive", NOTE, "no event to judge", evidence
        )
    share = restamped / older
    if share >= 0.95:
        return Item(
            9,
            "update_behaviour",
            "history_restamped_on_adjustment",
            NOTE,
            f"{share:.1%} of rows before a security's latest split/dividend carry a "
            "lastupdated on or after it: every split and dividend re-stamps the whole "
            "history (closeadj moves). A lastupdated delta (#58) therefore re-reads "
            "whole histories; it reads raw fields and closeunadj, which do not move.",
            evidence,
        )
    return Item(
        9,
        "update_behaviour",
        "history_not_restamped",
        PASS,
        f"only {share:.1%} of older rows were re-stamped",
        evidence,
    )


def row_shapes(bars: dict[str, list[dict]], sf1: list[dict]) -> Item:
    def tally(rows: list[dict]) -> dict:
        nulls, zeros = Counter(), Counter()
        for r in rows:
            for k, v in r.items():
                if v is None or v == "":
                    nulls[k] += 1
                elif isinstance(v, (int, float)) and v == 0:
                    zeros[k] += 1
        return {"rows": len(rows), "nulls": dict(nulls), "zeros": dict(zeros)}

    sep = tally([r for rows in bars.values() for r in rows])
    fun = tally(sf1)
    return Item(
        10,
        "row_shapes",
        "recorded",
        PASS,
        f"price rows: {sum(sep['nulls'].values())} null and {sum(sep['zeros'].values())} "
        f"zero values; SF1: {len(fun['nulls'])} columns with nulls, {len(fun['zeros'])} "
        "with zeros. Nulls stay NULL on load; a zero is a value.",
        {"prices": sep, "sf1": fun},
    )


def api_shape(checks: dict[str, dict]) -> Item:
    failing = [k for k, v in checks.items() if not v["ok"]]
    blocking = [k for k in failing if checks[k]["blocks"]]
    evidence = {k: v for k, v in checks.items()}
    if blocking:
        return Item(
            11,
            "api_as_implemented",
            "differs",
            BLOCK,
            f"the client relies on: {', '.join(blocking)}",
            evidence,
        )
    return Item(
        11,
        "api_as_implemented",
        "as_implemented" if not failing else "as_implemented_with_notes",
        PASS if not failing else NOTE,
        "every behaviour the client relies on holds"
        + (f"; other differences: {', '.join(failing)}" if failing else ""),
        evidence,
    )


def mechanics_item(checks: dict[str, dict]) -> Item:
    notes = [
        k
        for k in ("unknown_param_ignored", "tickers_table_filter_modern_names")
        if k in checks
    ]
    return Item(
        1,
        "api_mechanics",
        "measured",
        NOTE,
        f"{sum(v['ok'] for v in checks.values())} of {len(checks)} as documented or "
        "assumed. Consequences: an unknown parameter is ignored, so every filter the "
        "loaders send is asserted in the response; TICKERS' table filter takes modern "
        "names (stocks, fundamentals, funds).",
        {"checks": {k: v["detail"] for k, v in checks.items()}, "notes": notes},
    )


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def probe_sharadar(
    client: SharadarClient, *, sample_size: int = 200, seed: int = 0
) -> dict[str, Any]:
    """Run every item; returns the report (JSON-serialisable)."""
    started = time.monotonic()
    checks = mechanics(client)
    if not checks["paid_key"]["ok"]:
        items = [api_shape(checks)]
        return _report(client, items, {}, started, sample_size, seed)

    stocks = _listing(client, "stocks")
    funds = _listing(client, "funds")
    fundamentals = _listing(client, "fundamentals")
    n_funds = max(1, int(sample_size * FUND_SHARE))
    sample = draw_sample(stocks, sample_size - n_funds, seed) + draw_sample(
        funds, n_funds, seed
    )

    bars: dict[str, list[dict]] = {}
    actions: dict[str, list[dict]] = {}
    for row in sample:
        t = row["ticker"]
        if t in bars:  # a fund and a stock may share a ticker; keep the first
            continue
        table = client.T_SFP if row.get("table") == "funds" else client.T_SEP
        try:
            bars[t] = client.query(table, {"ticker": t})
            actions[t] = client.query(client.T_ACTIONS, {"ticker": t})
        except SourceError as exc:
            logger.warning("probe: %s skipped: %s", t, exc)
    splits = {t: splits_of(a) for t, a in actions.items()}

    case_actions = client.query(client.T_ACTIONS, {"ticker": SPLIT_CASE[0]})
    case_bars = client.query(client.T_SEP, {"ticker": SPLIT_CASE[0]})
    bars_for_direction = {**bars, SPLIT_CASE[0]: case_bars}
    splits_for_direction = {**splits, SPLIT_CASE[0]: splits_of(case_actions)}
    current = client.query(client.T_SEP, {"ticker": RENAME_CASE[0]}, fields=["date"])
    former = client.query(client.T_SEP, {"ticker": RENAME_CASE[1]}, fields=["date"])
    sf1_tickers = [r["ticker"] for r in sample if r.get("table") != "funds"][:5]
    sf1 = (
        client.query(
            client.T_SF1, {"ticker": ",".join(sf1_tickers), "dimension": "ARQ"}
        )
        if sf1_tickers
        else []
    )
    sep_columns = {k for rows in bars.values() for r in rows[:1] for k in r}
    sf1_columns = {k for r in sf1[:1] for k in r}

    items = [
        mechanics_item(checks),
        precision(bars, splits),
        raw_recovery(bars, splits),
        split_direction(case_actions, bars_for_direction, splits_for_direction),
        dividend_value(case_actions),
        ticker_keys(current, former, stocks),
        permaticker_across_tables(stocks, fundamentals, sep_columns, sf1_columns),
        same_day(bars),
        update_behaviour(bars, actions),
        row_shapes(bars, sf1),
        api_shape(checks),
    ]
    composition = {
        "securities": len(bars),
        "funds": sum(
            1 for r in sample if r.get("table") == "funds" and r["ticker"] in bars
        ),
        "delisted": sum(
            1 for r in sample if r.get("isdelisted") == "Y" and r["ticker"] in bars
        ),
        "by_decade": dict(
            sorted(Counter(_decade(r.get("firstpricedate")) for r in sample).items())
        ),
        "stocks_listing_rows": len(stocks),
    }
    return _report(client, items, composition, started, sample_size, seed)


def _report(client, items, composition, started, sample_size, seed) -> dict[str, Any]:
    return {
        "probed_on": date.today().isoformat(),
        "base_url": client.base_url,
        "sample_size": sample_size,
        "seed": seed,
        "sample": composition,
        "requests": client.request_count,
        "bytes": client.bytes_downloaded,
        "elapsed_s": round(time.monotonic() - started, 1),
        "items": [asdict(i) for i in items],
    }


def blocking(report: dict[str, Any]) -> list[dict]:
    return [i for i in report["items"] if i["level"] == BLOCK]


def format_sharadar_report(report: dict[str, Any]) -> str:
    """Verdicts and aggregates for the terminal: what may be posted. The --json
    report adds the evidence rows, which stay on the host."""
    s = report["sample"]
    lines = [
        f"Sharadar feed probe ({report['probed_on']}): {report['base_url']}",
        f"  sample: {s.get('securities', 0)} securities ({s.get('funds', 0)} funds, "
        f"{s.get('delisted', 0)} delisted) by first-price decade {s.get('by_decade', {})}; "
        f"seed {report['seed']}",
        f"  cost: {report['requests']} requests, {report['bytes']:,} bytes, "
        f"{report['elapsed_s']}s",
        "",
    ]
    for i in report["items"]:
        lines.append(f"{i['number']:>2}. {i['name']}: {i['level']} {i['verdict']}")
        lines.append(f"    {i['detail']}")
        if i["number"] in (1, 11):
            for k, v in (i["evidence"].get("checks") or i["evidence"]).items():
                detail = v if isinstance(v, str) else v.get("detail")
                mark = "" if isinstance(v, str) else ("ok  " if v.get("ok") else "NO  ")
                lines.append(f"      {mark}{k}: {detail}")
    blocks = blocking(report)
    lines += [
        "",
        (
            f"BLOCKING: {', '.join(str(b['number']) + ' ' + b['verdict'] for b in blocks)}"
            if blocks
            else "No blocking verdict."
        ),
    ]
    return "\n".join(lines)
