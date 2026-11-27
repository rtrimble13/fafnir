"""
Adjustment-factor computation.

Derives cumulative back-adjustment factors from corporate actions so that
``mart.v_daily_price_adjusted`` can produce split/dividend-adjusted OHLCV on read.

Precondition
------------
``core.daily_price`` must hold **genuinely unadjusted** prices. These factors are the
only adjustment in the system, so a vendor feed that has already been split-adjusted
gets adjusted twice: AAPL's 1990-01-02 close would enter as ~$0.35 instead of ~$39.20
and leave the view at ~$0.003. ``fafnir.ingest.daily_price`` therefore loads from FMP's
``historical-price-eod/non-split-adjusted`` endpoint, and dividend amounts are taken
from the as-declared ``dividend`` field (not the split-adjusted ``adjDividend``) so that
D and P below are quoted in the same share terms.

Conventions
-----------
Per-event factors (applied to all prices STRICTLY BEFORE the ex-date):
  * split num:den  -> price x (den/num),  volume x (num/den)
  * spin factor num:den (a spin-off stored as a split, ``split_kind =
    'spin_factor'``; spin-as-split, #282, #283)
                   -> price x (den/num),  volume x 1: no share count changed
  * cash dividend D, with P = raw close on the last trade day before ex-date:
        price x ((P - D) / P),            volume x 1
  * a cash dividend on a split's ex-date is stated per POST-split share, so P is
    first put on that basis: P x (den/num) of the date's splits (#286). A row the
    tape says was stated per pre-split share is flagged
    (``dividend_split_basis_suspect``) for restating, never re-based here. A spin
    factor is no split here either: the share the dividend is paid on is the one
    the prior close priced, so P is not re-based by it.

The cumulative price factor stored at ``effective_date = ex_date`` is the product
of per-event price factors for all events with ex_date >= effective_date. The view
selects, for a price at date t, the row with the smallest effective_date > t --
i.e. the product of every action that happened after t -- which is exactly the
back-adjustment for t. Prices on/after the latest ex-date get factor 1.0.

Range
-----
Because it is a *product* over a whole history, a cumulative factor spans far more
orders of magnitude than any single action: reverse splits drive the price factor
up and forward splits drive it down (volume moves the opposite way), and a
long-lived penny stock that has reverse-split five or six times passes 1e10. That
overflowed the original NUMERIC(20, 10) columns and killed the whole recompute
mid-run; migration 0013 stores both factors as unconstrained NUMERIC so no product
of positive ratios can overflow or round to zero. Factors far outside a plausible
band are still almost always bad vendor data, so they are flagged (see
:data:`EXTREME_FACTOR`) rather than dropped -- fafnir does not silently discard
corporate actions.
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Context, Decimal, localcontext
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("ingest.adjust")

# Significant digits carried through the factor arithmetic. Pinned rather than left
# to the ambient decimal context so the factors are reproducible everywhere -- the
# whole point of deriving them instead of storing an adjusted price. Ratios that
# terminate (2:1, 3:2, ...) stay exact; only the non-terminating ones (1:112) round,
# at a relative error of 1e-28.
PRECISION = 28

# A cumulative factor beyond this (or below its reciprocal) is possible in principle
# but in practice means a mis-scaled or duplicated vendor split: a $0.01 price
# back-adjusted by 1e12 is $1e10, which is not a price. Storage handles it since
# 0013; the DQ flag is so a human looks at the action history.
EXTREME_FACTOR = Decimal("1e12")

# Above this share of the universe, a failed recompute is not bad data -- it is the
# schema, the grants or a lock, and every security is failing for the same reason.
# `fafnir adjust` exits non-zero past it so a scheduled run cannot report success
# while writing nothing; below it, a few bad securities are flagged and stepped over.
SYSTEMIC_FAILURE_RATIO = 0.01

# The ratio alone is not a test on a small warehouse. 1% of anything under 100
# securities is less than one, so a SINGLE bad security satisfied it and `fafnir
# adjust` exited non-zero calling one failure systemic -- under `set -euo pipefail`
# in daily_update.sh and initial_backfill.sh that skips the mart refresh and the DQ
# pass entirely, which is the opposite of the stepped-over-and-carry-on behaviour
# the ratio exists to protect. Reachable on any `--limit`-built deployment, which
# is what both the install guide (§6) and backfill.md walk a new operator through.
# So the verdict needs an absolute floor as well: a handful of failures is bad data
# at any scale, and past ~2,000 securities the ratio is the binding condition again.
SYSTEMIC_FAILURE_FLOOR = 20

# ...and when it is systemic, the run should not grind through 21,000 securities to
# find that out. Failing this many in a row without a single success means nothing
# after them will succeed either, and continuing would write one committed
# `adjustment_failed` flag per security -- burying the genuine flags and inflating
# the open-flag count `fafnir status` reports, permanently.
EARLY_ABORT_FAILURES = 50

# Two splits of one ratio this close are one split recorded twice (G3). GLE's 1:10 sat
# on 2026-10-07 and 10-08, NXGL's 1:20 on 10-09 and 10-12: the bars moved once, and
# the adjusted series moved twice -- x100 and x400 before the earlier date. No SQL
# check saw either. Counted in open sessions on the security's venue.
DUPLICATE_SPLIT_SESSIONS = 10
DUPLICATE_SPLIT_CHECK = "split_duplicate_suspect"

# A dividend that shares its ex-date with a split is stated per POST-split share
# (#286). Its prior close is on the pre-split basis, so the factor values the
# dividend against that close times the date's split price factor. Neither vendor
# is consistent about it: of the 28 same-day pairs where the basis moves the factor
# by more than 10%, the ex-day open fitted the post-split amount on 6 of Sharadar's
# 9, and on about 4 of FMP's 18. Several FMP rows, and the OPNW operator repair of
# 2026-10, fitted the pre-split amount. Those must be restated, not guessed at here:
# a factor read off the tape would change whenever a bar is revised.
#
# So the tape is read only to flag. This check says the ex-day open fits a
# pre-split amount, or no dividend at all, better than the post-split amount the
# factor assumes.
SPLIT_BASIS_CHECK = "dividend_split_basis_suspect"
# The two bases must predict ex-day opens at least this far apart (as a share of
# the post-split prior close) before the open can tell them apart. A $0.05 dividend
# on a $40 stock that splits 2:1 differs by 0.1%, which is noise in any open.
SPLIT_BASIS_MIN_SEPARATION = Decimal("0.05")
# The ex-day open is read from the first bar on or after the ex-date, within this
# many calendar days. A first bar further out belongs to a halt, not to the ex-date.
SPLIT_BASIS_OPEN_WITHIN_DAYS = 10


def is_spin_factor(action: dict) -> bool:
    """A split row that is a spin-off's price factor, not a share split (#283)."""
    return action.get("split_kind") == repo.SPLIT_KIND_SPIN


def compute_for_security(db: Database, security_id: int) -> list[dict]:
    """Compute (and persist) adjustment factors for one security. Returns the rows."""
    actions = repo.corporate_actions_for(db, security_id)
    if not actions:
        repo.replace_adjustment_factors(db, security_id, [])
        return []
    _flag_duplicate_splits(db, security_id, actions)

    # Group per-event factors by ex_date (a split and dividend can share a date).
    # Exact Decimal math throughout -> the derived factor ties out to the penny
    # even across long action histories (money is exact, never float).
    price_by_date: dict = defaultdict(lambda: Decimal(1))
    vol_by_date: dict = defaultdict(lambda: Decimal(1))

    # Pinned precision (not the ambient context) so every environment derives the
    # same factors from the same actions.
    with localcontext(Context(prec=PRECISION)):
        # Splits first: a dividend on a split's ex-date is valued on the post-split
        # basis, so it needs that date's split price factor (SPLIT_BASIS_CHECK).
        split_price: dict = defaultdict(lambda: Decimal(1))
        for act in actions:
            if act["action_type"] == "split":
                ex_date = act["ex_date"]
                num = Decimal(act["split_numerator"])
                den = Decimal(act["split_denominator"])
                price_by_date[ex_date] *= den / num
                if is_spin_factor(act):
                    # The price falls by what was spun off; no share count changed.
                    continue
                vol_by_date[ex_date] *= num / den
                split_price[ex_date] *= den / num
        for act in actions:
            ex_date = act["ex_date"]
            if act["action_type"] == "dividend":
                amount = Decimal(act["dividend_amount"] or 0)
                if amount <= 0:
                    continue
                prior_close = repo.close_before(db, security_id, ex_date)
                if prior_close is None or Decimal(prior_close) <= 0:
                    # No price to value the dividend against -> skip (factor 1) and flag.
                    repo.add_dq_flag_once(
                        db,
                        check_name="dividend_no_prior_close",
                        severity="info",
                        security_id=security_id,
                        table_name="core.adjustment_factor",
                        record_key={"ex_date": str(ex_date)},
                    )
                    continue
                raw = Decimal(prior_close)
                # 1 with no split that day; den/num of the split when there is one.
                s = split_price[ex_date] if ex_date in split_price else Decimal(1)
                p = raw * s
                if s != 1:
                    _check_split_basis(db, security_id, ex_date, raw, s, amount)
                factor = (p - amount) / p
                if factor <= 0:
                    detail = {"prior_close": float(raw), "dividend": float(amount)}
                    if s != 1:
                        detail["split_price_factor"] = float(s)
                        detail["post_split_close"] = float(p)
                    repo.add_dq_flag_once(
                        db,
                        check_name="dividend_exceeds_price",
                        severity="warn",
                        security_id=security_id,
                        table_name="core.adjustment_factor",
                        record_key={"ex_date": str(ex_date)},
                        detail=detail,
                    )
                    continue
                price_by_date[ex_date] *= factor

        # Build cumulative factors by walking ex-dates from latest to earliest.
        ex_dates = sorted(price_by_date.keys())
        cum_price = Decimal(1)
        cum_vol = Decimal(1)
        cumulative: dict = {}
        for ex_date in reversed(ex_dates):
            cum_price *= price_by_date[ex_date]
            cum_vol *= vol_by_date[ex_date]
            cumulative[ex_date] = (cum_price, cum_vol)

    # Stored at full computed precision: rounding to a fixed number of DECIMAL
    # PLACES is what turned a deep forward-split history into a 0.0000000000 factor
    # (and every pre-split price into zero). Scale belongs to the column, and since
    # 0013 the column is unconstrained NUMERIC.
    factors = [
        {
            "effective_date": ex_date,
            "cumulative_price_factor": cumulative[ex_date][0],
            "cumulative_volume_factor": cumulative[ex_date][1],
        }
        for ex_date in ex_dates
    ]
    _flag_if_extreme(db, security_id, factors)
    repo.replace_adjustment_factors(db, security_id, factors)
    return factors


def split_basis_fit(
    prior_close: Decimal, split_price_factor: Decimal, dividend: Decimal, ex_open
) -> Optional[dict]:
    """Which share basis a dividend on a split's ex-date fits, read off the tape.

    ``prior_close`` is the raw close before the ex-date, on the pre-split basis.
    ``split_price_factor`` is that date's den/num, and ``ex_open`` is the first raw
    open on or after the ex-date. The ex-day open each reading predicts:

    * ``post_split``: the amount is per post-split share (what the factor assumes):
      ``P*s - D``;
    * ``pre_split``: the amount is per pre-split share: ``(P - D)*s``;
    * ``no_dividend``: the shares did not go ex at all: ``P*s``.

    Returns None when the open cannot tell the two bases apart
    (:data:`SPLIT_BASIS_MIN_SEPARATION`), or when there is no usable open. Otherwise
    returns the three predictions and ``fits``, the one nearest the open. A
    prediction at or below zero is impossible and never fits.
    """
    if ex_open is None or prior_close <= 0 or split_price_factor == 1:
        return None
    o = Decimal(ex_open)
    if o <= 0:
        return None
    base = prior_close * split_price_factor
    predicted = {
        "post_split": base - dividend,
        "pre_split": (prior_close - dividend) * split_price_factor,
        "no_dividend": base,
    }
    if (
        abs(predicted["post_split"] - predicted["pre_split"]) / base
        < SPLIT_BASIS_MIN_SEPARATION
    ):
        return None
    possible = {k: v for k, v in predicted.items() if v > 0}
    fits = min(possible, key=lambda k: abs(o - possible[k]))
    return {"fits": fits, "ex_open": o, "predicted_open": predicted}


def _check_split_basis(
    db: Database,
    security_id: int,
    ex_date,
    prior_close: Decimal,
    split_price_factor: Decimal,
    dividend: Decimal,
) -> None:
    """Flag a dividend on a split's ex-date whose ex-day open fits a pre-split
    amount, or no dividend, better than the post-split amount (SPLIT_BASIS_CHECK)."""
    ex_open = db.fetchval(
        "SELECT open FROM core.daily_price WHERE security_id = %s"
        " AND trade_date >= %s AND trade_date <= %s::date + %s"
        " ORDER BY trade_date LIMIT 1",
        (security_id, ex_date, ex_date, SPLIT_BASIS_OPEN_WITHIN_DAYS),
    )
    fit = split_basis_fit(prior_close, split_price_factor, dividend, ex_open)
    if fit is None or fit["fits"] == "post_split":
        return
    repo.add_dq_flag_once(
        db,
        check_name=SPLIT_BASIS_CHECK,
        severity="warn",
        security_id=security_id,
        table_name="core.adjustment_factor",
        record_key={"ex_date": str(ex_date)},
        detail={
            "fits": fit["fits"],
            "dividend": float(dividend),
            "prior_close": float(prior_close),
            "split_price_factor": float(split_price_factor),
            "ex_open": float(fit["ex_open"]),
            "predicted_open": {
                k: float(round(v, 6)) for k, v in fit["predicted_open"].items()
            },
        },
    )


def _split_ratio(action: dict) -> Optional[Decimal]:
    try:
        return Decimal(action["split_numerator"]) / Decimal(action["split_denominator"])
    except (TypeError, ArithmeticError):
        return None


def _same_ratio_pairs(actions: list[dict]) -> list[tuple[dict, dict]]:
    """Same-ratio split pairs within a calendar window wide enough to hold
    :data:`DUPLICATE_SPLIT_SESSIONS` sessions: the candidates, found without a
    query, because this runs for every security `fafnir adjust` recomputes."""
    splits = sorted(
        (a for a in actions if a["action_type"] == "split" and _split_ratio(a)),
        key=lambda a: a["ex_date"],
    )
    pairs = []
    for i, a in enumerate(splits):
        for b in splits[i + 1 :]:
            if (b["ex_date"] - a["ex_date"]).days > 3 * DUPLICATE_SPLIT_SESSIONS:
                break
            if _split_ratio(a) == _split_ratio(b):
                pairs.append((a, b))
    return pairs


def duplicate_split_pairs(
    db: Database, security_id: int, actions: list[dict], exchange_code: str
) -> list[tuple[dict, dict]]:
    """Pairs of splits of one ratio no more than :data:`DUPLICATE_SPLIT_SESSIONS`
    open sessions apart, earlier first."""
    return [
        (a, b)
        for a, b in _same_ratio_pairs(actions)
        if repo.sessions_apart(db, a["ex_date"], b["ex_date"], exchange_code)
        <= DUPLICATE_SPLIT_SESSIONS
    ]


def _split_moves(db: Database, security_id: int, action: dict) -> dict:
    """How the raw closes move across one split's ex-date, against its factor."""
    ex = action["ex_date"]
    before = repo.close_before(db, security_id, ex)
    after = db.fetchval(
        "SELECT close FROM core.daily_price WHERE security_id = %s AND trade_date >= %s"
        " ORDER BY trade_date LIMIT 1",
        (security_id, ex),
    )
    # A 1:10 reverse split multiplies the price by 10 on its ex-date: num/den inverted.
    factor = 1 / _split_ratio(action)
    move = (
        Decimal(after) / Decimal(before)
        if before is not None and after is not None and Decimal(before) > 0
        else None
    )
    return {
        "ex_date": str(ex),
        "split": f"{_num(action['split_numerator'])}:"
        f"{_num(action['split_denominator'])}",
        "close_before": str(before) if before is not None else None,
        "close_on_or_after": str(after) if after is not None else None,
        "close_ratio": float(round(move, 6)) if move is not None else None,
        "split_factor": float(round(factor, 6)),
        # Moves "by the split" when the close ratio is nearer the factor than 1:1,
        # on a log scale.
        "moves": (
            move is not None
            and move > 0
            and abs(math.log(float(move) / float(factor))) < abs(math.log(float(move)))
        ),
    }


def _num(v) -> str:
    return format(Decimal(v).normalize(), "f")


def _flag_duplicate_splits(db: Database, security_id: int, actions: list[dict]) -> None:
    """Flag two same-ratio splits close together (``split_duplicate_suspect``).

    The detail says which of them the raw closes move on: the close ratio across
    each ex-date against the split's factor. The other is the one to delete, after
    probing the vendor's split-adjusted feed (`source probe-prices` on the bar
    before each ex-date shows which one it applies). Never resolved by a run: the
    repair is `actions delete`, and with one of the pair gone the condition is too.
    """
    if not _same_ratio_pairs(actions):
        return
    exchange = (repo.security_price_profile(db, security_id) or {}).get(
        "exchange_code"
    ) or "NYSE"
    for a, b in duplicate_split_pairs(db, security_id, actions, exchange):
        rows = [_split_moves(db, security_id, x) for x in (a, b)]
        moving = [r["ex_date"] for r in rows if r["moves"]]
        logger.warning(
            "security %d: the same %s split on %s and %s -- one is a duplicate "
            "(the closes move on %s)",
            security_id,
            rows[0]["split"],
            a["ex_date"],
            b["ex_date"],
            ", ".join(moving) or "neither",
        )
        repo.add_dq_flag_once(
            db,
            check_name=DUPLICATE_SPLIT_CHECK,
            severity="warn",
            security_id=security_id,
            table_name="core.corporate_action",
            record_key={"ex_dates": [str(a["ex_date"]), str(b["ex_date"])]},
            detail={
                "splits": rows,
                "moves_on": moving,
                "sessions_apart": repo.sessions_apart(
                    db, a["ex_date"], b["ex_date"], exchange
                ),
            },
        )


def _flag_if_extreme(db: Database, security_id: int, factors: list[dict]) -> None:
    """Flag a factor set whose magnitude says "bad vendor data", without dropping it.

    A cumulative factor outside [1/EXTREME_FACTOR, EXTREME_FACTOR] is arithmetically
    fine to store since 0013, but it means either a genuinely absurd action history
    or -- far more often -- a mis-scaled, duplicated or inverted split from the feed.
    Either way the adjusted series for that security is not usable for research, so
    it gets an ``ops.data_quality_flag`` a human can work from. It is NOT skipped:
    dropping actions here would quietly replace a wrong series with a differently
    wrong one, and only the source data can settle which.
    """
    if not factors:
        return
    price = [f["cumulative_price_factor"] for f in factors]
    vol = [f["cumulative_volume_factor"] for f in factors]
    lo = 1 / EXTREME_FACTOR
    worst = max(price + vol)
    smallest = min(price + vol)
    if worst < EXTREME_FACTOR and smallest > lo:
        return
    logger.warning(
        "security %d: extreme cumulative adjustment factor (max %s, min %s) "
        "across %d ex-dates -- check its corporate actions",
        security_id,
        worst,
        smallest,
        len(factors),
    )
    repo.add_dq_flag_once(
        db,
        check_name="adjustment_factor_extreme",
        severity="warn",
        security_id=security_id,
        table_name="core.adjustment_factor",
        record_key={"effective_date": str(factors[0]["effective_date"])},
        detail={
            "max_factor": str(worst),
            "min_factor": str(smallest),
            "ex_dates": len(factors),
        },
    )


def adjust_all(
    db: Database,
    security_id: Optional[int] = None,
    security_ids: Optional[Iterable[int]] = None,
) -> dict:
    """Recompute factors for one security, a named set, or every security with actions.

    ``security_ids`` is the incremental path: a corporate-actions run knows exactly
    which securities it changed (`fafnir adjust --changed`), and recomputing those is
    a few hundred securities on a normal night instead of every security in the
    warehouse that has ever had an action. An empty set is a real answer -- nothing
    changed, so nothing needs recomputing -- and is not the same as ``None``, which
    means "recompute everything".

    Returns ``{"securities": recomputed, "failed": failed, "aborted": bool}``.

    One security's failure costs that security, not the run. Before this, the whole
    universe was recomputed inside a single transaction with no commit boundary, so
    the first security whose factors would not store (see the module docstring) both
    raised out of `fafnir adjust` -- ending the backfill at step 5 under `set -e` --
    and rolled back the factors of every security already done. A recompute is
    derived, idempotent, per-security work; it commits per security, and a security
    that still fails is flagged and stepped over so the other 21,000 get their
    factors.

    A stepped-over security keeps whatever factors its last successful run left --
    the replace is DELETE + INSERT in the transaction being rolled back, so nothing
    is destroyed. That is stale, not absent: its newest corporate actions are missing
    from the series until the flag is worked. On a first backfill it has none, and
    reads unadjusted.
    """
    if security_id is not None:
        compute_for_security(db, security_id)
        db.commit()
        return {"securities": 1, "failed": 0, "aborted": False}

    ids = (
        repo.securities_with_actions(db)
        if security_ids is None
        else sorted(security_ids)
    )
    done = 0
    failed = 0
    aborted = False
    for sid in ids:
        try:
            compute_for_security(db, sid)
            db.commit()
            done += 1
        except Exception as exc:  # one bad security must not end the run
            failed += 1
            # The transaction is aborted at this point; clear it before the flag.
            db.rollback()
            logger.exception("Adjustment factors failed for security %d: %s", sid, exc)
            try:
                repo.add_dq_flag_once(
                    db,
                    check_name="adjustment_failed",
                    severity="error",
                    security_id=sid,
                    table_name="core.adjustment_factor",
                    detail={"error": f"{type(exc).__name__}: {exc}"},
                )
                db.commit()
            except Exception:  # flagging is best-effort
                db.rollback()
                logger.warning("Could not flag the failure for security %d", sid)
            if done == 0 and failed >= EARLY_ABORT_FAILURES:
                aborted = True
                logger.error(
                    "Stopping: the first %d securities all failed without a single "
                    "success. That is the schema, the grants or a lock, not the data.",
                    failed,
                )
                break
    logger.info(
        "Recomputed adjustment factors for %d securities (%d failed%s)",
        done,
        failed,
        ", stopped early" if aborted else "",
    )
    return {"securities": done, "failed": failed, "aborted": aborted}
