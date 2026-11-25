"""A dividend on a split's ex-date is stated per post-split share (#286).

AVZO (formerly Rallybio) went ex a 1:2 reverse split and a special cash
distribution on the same day, 2026-11-23. Sharadar filed the distribution as $27.80
per post-split share. The factor valued it against the raw prior close, 15.47,
which is on the pre-split basis. The factor came out negative, so the dividend was
skipped and the adjusted series fell 86% on a day a holder made about 3%.

The prior close now goes onto the post-split basis first: 15.47 x 2 = 30.94. The
dividend applies, and the adjusted ex-date move is +33% under the (P - D)/P
convention. That is not the holder's +3.4% (see the AVZO test). Rows
whose ex-day open fits a pre-split amount, or no dividend at all, are flagged
``dividend_split_basis_suspect`` to be restated by hand. The factor itself is never
read off the tape.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir.ingest.adjustments import SPLIT_BASIS_CHECK, split_basis_fit

D = Decimal


# ---------------------------------------------------------------------------
# The tape reading, pure
# ---------------------------------------------------------------------------


def test_avzo_fits_the_post_split_amount():
    # 15.47 pre-split, 1:2 (s = 2), $27.80 per new share, opened 4.85.
    fit = split_basis_fit(D("15.47"), D(2), D("27.80"), D("4.85"))
    assert fit["fits"] == "post_split"
    assert fit["predicted_open"]["post_split"] == D("3.14")
    assert fit["predicted_open"]["no_dividend"] == D("30.94")
    # (15.47 - 27.80) x 2 is impossible: never a fit.
    assert fit["predicted_open"]["pre_split"] < 0


def test_a_pre_split_amount_on_a_forward_split_is_read_as_one():
    # 40.00, 2:1 (s = 1/2), $10 per old share, opened 15.10: (40 - 10) / 2 = 15.
    fit = split_basis_fit(D(40), D("0.5"), D(10), D("15.10"))
    assert fit["fits"] == "pre_split"


def test_an_open_that_never_went_ex_is_read_as_no_dividend():
    # Opened at the split-adjusted prior close: the shares never went ex.
    fit = split_basis_fit(D("6.50"), D("0.5"), D(2), D("3.25"))
    assert fit["fits"] == "no_dividend"


def test_a_small_dividend_cannot_tell_the_bases_apart():
    # $0.05 on a $40 stock splitting 2:1: the two predictions are 0.1% apart.
    assert split_basis_fit(D(40), D("0.5"), D("0.05"), D("20.00")) is None


def test_no_split_or_no_open_is_no_reading():
    assert split_basis_fit(D(40), D(1), D(10), D(30)) is None
    assert split_basis_fit(D(40), D("0.5"), D(10), None) is None
    assert split_basis_fit(D(40), D("0.5"), D(10), D(0)) is None


# ---------------------------------------------------------------------------
# The factor and the flags, against Postgres
# ---------------------------------------------------------------------------


def _mk(db, symbol):
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


def _bar(db, sid, day, close, open_=None):
    o = close if open_ is None else open_
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": o,
                "high": max(o, close),
                "low": min(o, close),
                "close": close,
                "volume": 100_000,
            }
        ],
    )


def _split(db, sid, day, num, den):
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=day,
        split_numerator=num,
        split_denominator=den,
    )


def _dividend(db, sid, day, amount):
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=day,
        dividend_amount=amount,
    )


def _factor_at(db, sid, day):
    return db.fetchval(
        "SELECT cumulative_price_factor FROM core.adjustment_factor"
        " WHERE security_id = %s AND effective_date = %s",
        (sid, day),
    )


def _flags(db, sid, check):
    return db.fetchall(
        "SELECT record_key, detail FROM ops.data_quality_flag"
        " WHERE security_id = %s AND check_name = %s AND resolved_at IS NULL",
        (sid, check),
    )


FRI, MON = dt.date(2026, 11, 20), dt.date(2026, 11, 23)


@pytest.mark.integration
def test_avzo_values_the_dividend_on_the_post_split_basis(db):
    sid = _mk(db, "AVZO")
    _bar(db, sid, FRI, D("15.47"))
    _bar(db, sid, MON, D("4.19"), open_=D("4.85"))
    _split(db, sid, MON, 1, 2)
    _dividend(db, sid, MON, D("27.80"))

    adjustments.compute_for_security(db, sid)

    # x2 for the split, x (30.94 - 27.80) / 30.94 for the distribution.
    expected = D(2) * (D("30.94") - D("27.80")) / D("30.94")
    assert _factor_at(db, sid, MON) == pytest.approx(expected, rel=D("1e-20"))
    assert _flags(db, sid, "dividend_exceeds_price") == []
    assert _flags(db, sid, SPLIT_BASIS_CHECK) == []
    # The ex-date's adjusted return under (P - D)/P is C1 / (P - D) - 1 = 4.19 / 3.14
    # - 1 = +33%. It is not the holder's (4.19 + 27.80) / 30.94 - 1 = +3.4%: the two
    # agree only when the price falls by exactly the dividend.
    adj = {
        r["date"]: D(r["close"])
        for r in repo.read_price_history(db, "AVZO", None, None, adjusted=True)
    }
    assert adj[MON] / adj[FRI] - 1 == pytest.approx(
        D("4.19") / (D("30.94") - D("27.80")) - 1, rel=D("1e-6")
    )


@pytest.mark.integration
def test_a_pre_split_amount_is_flagged_not_rebased(db):
    """A 2:1 split with $10 per OLD share, as the tape shows (opens at 15.10):
    the factor still reads it as post-split, and the flag says to restate it."""
    sid = _mk(db, "PRES")
    _bar(db, sid, FRI, D(40))
    _bar(db, sid, MON, D(15), open_=D("15.10"))
    _split(db, sid, MON, 2, 1)
    _dividend(db, sid, MON, D(10))

    adjustments.compute_for_security(db, sid)
    adjustments.compute_for_security(db, sid)  # every night: flagged once

    assert _factor_at(db, sid, MON) == D("0.5") * (D(20) - D(10)) / D(20)
    flags = _flags(db, sid, SPLIT_BASIS_CHECK)
    assert len(flags) == 1
    assert flags[0]["record_key"] == {"ex_date": MON.isoformat()}
    detail = flags[0]["detail"]
    assert detail["fits"] == "pre_split"
    assert detail["predicted_open"] == {
        "post_split": 10.0,
        "pre_split": 15.0,
        "no_dividend": 20.0,
    }


@pytest.mark.integration
def test_a_post_split_amount_over_the_post_split_price_still_flags_exceeds(db):
    sid = _mk(db, "OVER")
    _bar(db, sid, FRI, D(10))
    _bar(db, sid, MON, D(19), open_=D(19))
    _split(db, sid, MON, 1, 2)
    _dividend(db, sid, MON, D(25))  # over 10 x 2 = 20

    adjustments.compute_for_security(db, sid)

    (flag,) = _flags(db, sid, "dividend_exceeds_price")
    assert flag["detail"] == {
        "prior_close": 10.0,
        "dividend": 25.0,
        "split_price_factor": 2.0,
        "post_split_close": 20.0,
    }
    assert _factor_at(db, sid, MON) == D(2)  # the split alone


@pytest.mark.integration
def test_a_dividend_on_its_own_date_is_unchanged(db):
    sid = _mk(db, "PLAIN")
    _bar(db, sid, FRI, D(40))
    _bar(db, sid, MON, D(39))
    _dividend(db, sid, MON, D(1))

    adjustments.compute_for_security(db, sid)

    assert _factor_at(db, sid, MON) == (D(40) - D(1)) / D(40)
    assert _flags(db, sid, SPLIT_BASIS_CHECK) == []
