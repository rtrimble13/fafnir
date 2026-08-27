"""The factor range a real action history needs (needs FAFNIR_TEST_DSN).

`initial_backfill.sh` died at step 5 on 2026-08-27 with

    psycopg.errors.NumericValueOutOfRange: numeric field overflow
    DETAIL: A field with precision 20, scale 10 must round to an absolute value
            less than 10^10.

because a cumulative factor is a PRODUCT over a whole action history, and
core.adjustment_factor typed both factors NUMERIC(20, 10) -- a window of only
[1e-10, 1e10). Deep reverse-split histories walk out of the top of it, deep
forward-split histories out of the bottom, and 21,106 symbols is more than enough
to contain both. Migration 0013 stores them as unconstrained NUMERIC.

These tests pin both ends of that range and the mart read across it.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import adjustments

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


def _mk_security(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name="Test",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _one_bar(db, sid, day, close, volume=1_000_000):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
            }
        ],
    )


def _splits(db, sid, ratios, first_day=dt.date(2023, 2, 1)):
    """One split per ratio (numerator, denominator), on consecutive ex-dates."""
    for i, (num, den) in enumerate(ratios):
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="split",
            ex_date=first_day + dt.timedelta(days=i),
            split_numerator=num,
            split_denominator=den,
        )


def _factors(db, sid):
    return db.fetchall(
        """
        SELECT effective_date, cumulative_price_factor, cumulative_volume_factor
        FROM core.adjustment_factor WHERE security_id = %s ORDER BY effective_date
        """,
        (sid,),
    )


def _flags(db, sid):
    return [
        r["check_name"]
        for r in db.fetchall(
            "SELECT check_name FROM ops.data_quality_flag WHERE security_id = %s",
            (sid,),
        )
    ]


def test_deep_reverse_split_history_stores_and_reads(db):
    """The failure that ended step 5: a price factor past 1e10.

    1:10 x 1:50 x 1:100 x 1:200 x 1:100 x 1:20 = 2e10. Nothing exotic -- that is an
    ordinary long-lived penny stock, and there are thousands of them in a 21k-symbol
    universe.
    """
    sid = _mk_security(db, "RVRS")
    _one_bar(db, sid, dt.date(2023, 1, 3), Decimal("0.05"))
    _splits(db, sid, [(1, 10), (1, 50), (1, 100), (1, 200), (1, 100), (1, 20)])

    adjustments.compute_for_security(db, sid)

    rows = _factors(db, sid)
    assert len(rows) == 6
    # The earliest boundary carries every action after it: 10*50*100*200*100*20.
    assert rows[0]["cumulative_price_factor"] == Decimal("2e10")
    assert rows[0]["cumulative_volume_factor"] == Decimal("0.5e-10")

    # And the bar before it reads back through the mart at the right level:
    # $0.05 pre-reverse-split is $1e9 in today's share terms.
    adj = repo.read_price_history(db, "RVRS", None, None, adjusted=True)
    assert Decimal(adj[0]["close"]) == Decimal("1e9")

    # A history this deep is implausible but not impossible; it is not flagged.
    assert "adjustment_factor_extreme" not in _flags(db, sid)


def test_deep_forward_split_history_keeps_a_nonzero_price_factor(db):
    """The same wall from the other side, and the more dangerous one.

    Six 100:1 splits put the volume factor at 1e12 (overflow again) and the price
    factor at 1e-12 -- which, rounded to 10 decimal places as the old column and the
    old mart both did, is 0.0000000000. That is not an error, it is a silently
    zeroed price series: the CHECK constraint was all that stood between a deep
    forward-split history and every pre-split close reading as 0.
    """
    sid = _mk_security(db, "FWD")
    _one_bar(db, sid, dt.date(2023, 1, 3), Decimal("2.50"))
    _splits(db, sid, [(100, 1)] * 6)

    adjustments.compute_for_security(db, sid)

    rows = _factors(db, sid)
    assert rows[0]["cumulative_price_factor"] == Decimal("1e-12")
    assert rows[0]["cumulative_volume_factor"] == Decimal("1e12")

    adj = repo.read_price_history(db, "FWD", None, None, adjusted=True)
    close = Decimal(adj[0]["close"])
    assert close > 0, "a non-zero price must never back-adjust to zero"
    assert close == Decimal("2.5e-12")


def test_an_absurd_factor_is_flagged_but_kept(db):
    """Past a plausible band it is vendor data, not a corporate action -- flag it.

    fafnir does not silently drop corporate actions: dropping one replaces a wrong
    series with a differently wrong one, and only the source can settle which. So
    the factors are stored as computed and a DQ flag points a human at the history.
    """
    sid = _mk_security(db, "JUNK")
    _one_bar(db, sid, dt.date(2023, 1, 3), Decimal("1.00"))
    _splits(db, sid, [(100, 1)] * 6)

    adjustments.compute_for_security(db, sid)

    assert "adjustment_factor_extreme" in _flags(db, sid)
    assert _factors(db, sid), "the factors are flagged, not discarded"


def test_adjust_all_steps_over_a_security_it_cannot_compute(db, monkeypatch):
    """One bad security costs that security, not the other 21,000.

    Step 5 was a single transaction with no commit boundary, so the first security
    that raised took every already-computed security's factors down with it and
    ended the backfill. Now the failure is flagged and the run continues.
    """
    good = _mk_security(db, "GOOD")
    bad = _mk_security(db, "BAD")
    for sid in (good, bad):
        _one_bar(db, sid, dt.date(2023, 1, 3), Decimal("10.00"))
        _splits(db, sid, [(2, 1)])

    real = adjustments.compute_for_security

    def flaky(database, security_id):
        if security_id == bad:
            raise RuntimeError("boom")
        return real(database, security_id)

    monkeypatch.setattr(adjustments, "compute_for_security", flaky)

    result = adjustments.adjust_all(db)

    assert result == {"securities": 1, "failed": 1}
    assert _factors(db, good), "the healthy security kept its factors"
    assert not _factors(db, bad)
    assert "adjustment_failed" in _flags(db, bad)
