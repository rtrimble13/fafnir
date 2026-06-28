"""
Adjustment-factor computation.

Derives cumulative back-adjustment factors from corporate actions so that
``mart.v_daily_price_adjusted`` can produce split/dividend-adjusted OHLCV on read.

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
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.logging_config import get_logger

logger = get_logger("ingest.adjust")


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
            prior_close = repo.close_on_or_before(db, security_id, ex_date)
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

    factors = [
        {
            "effective_date": ex_date,
            "cumulative_price_factor": round(cumulative[ex_date][0], 10),
            "cumulative_volume_factor": round(cumulative[ex_date][1], 10),
        }
        for ex_date in ex_dates
    ]
    repo.replace_adjustment_factors(db, security_id, factors)
    return factors


def adjust_all(db: Database, security_id: Optional[int] = None) -> int:
    """Recompute factors for one security or every security with actions.

    Returns the number of securities processed."""
    if security_id is not None:
        compute_for_security(db, security_id)
        return 1
    ids = repo.securities_with_actions(db)
    for sid in ids:
        compute_for_security(db, sid)
    logger.info("Recomputed adjustment factors for %d securities", len(ids))
    return len(ids)
