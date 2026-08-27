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
  * cash dividend D, with P = raw close on the last trade day before ex-date:
        price x ((P - D) / P),            volume x 1

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

from collections import defaultdict
from decimal import Context, Decimal, localcontext
from typing import Optional

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

# ...and when it is systemic, the run should not grind through 21,000 securities to
# find that out. Failing this many in a row without a single success means nothing
# after them will succeed either, and continuing would write one committed
# `adjustment_failed` flag per security -- burying the genuine flags and inflating
# the open-flag count `fafnir status` reports, permanently.
EARLY_ABORT_FAILURES = 50


def compute_for_security(db: Database, security_id: int) -> list[dict]:
    """Compute (and persist) adjustment factors for one security. Returns the rows."""
    actions = repo.corporate_actions_for(db, security_id)
    if not actions:
        repo.replace_adjustment_factors(db, security_id, [])
        return []

    # Group per-event factors by ex_date (a split and dividend can share a date).
    # Exact Decimal math throughout -> the derived factor ties out to the penny
    # even across long action histories (money is exact, never float).
    price_by_date: dict = defaultdict(lambda: Decimal(1))
    vol_by_date: dict = defaultdict(lambda: Decimal(1))

    # Pinned precision (not the ambient context) so every environment derives the
    # same factors from the same actions.
    with localcontext(Context(prec=PRECISION)):
        for act in actions:
            ex_date = act["ex_date"]
            if act["action_type"] == "split":
                num = Decimal(act["split_numerator"])
                den = Decimal(act["split_denominator"])
                price_by_date[ex_date] *= den / num
                vol_by_date[ex_date] *= num / den
            elif act["action_type"] == "dividend":
                amount = Decimal(act["dividend_amount"] or 0)
                if amount <= 0:
                    continue
                prior_close = repo.close_before(db, security_id, ex_date)
                if prior_close is None or Decimal(prior_close) <= 0:
                    # No price to value the dividend against -> skip (factor 1) and flag.
                    repo.add_dq_flag(
                        db,
                        check_name="dividend_no_prior_close",
                        severity="info",
                        security_id=security_id,
                        table_name="core.adjustment_factor",
                        record_key={"ex_date": str(ex_date)},
                    )
                    continue
                p = Decimal(prior_close)
                factor = (p - amount) / p
                if factor <= 0:
                    repo.add_dq_flag(
                        db,
                        check_name="dividend_exceeds_price",
                        severity="warn",
                        security_id=security_id,
                        table_name="core.adjustment_factor",
                        record_key={"ex_date": str(ex_date)},
                        detail={"prior_close": float(p), "dividend": float(amount)},
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
    repo.add_dq_flag(
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


def adjust_all(db: Database, security_id: Optional[int] = None) -> dict:
    """Recompute factors for one security or every security with actions.

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

    ids = repo.securities_with_actions(db)
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
                repo.add_dq_flag(
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
