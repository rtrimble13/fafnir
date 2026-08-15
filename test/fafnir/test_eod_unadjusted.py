"""core.daily_price must hold AS-TRADED prices, not split-adjusted ones.

Regression: the loader used historical-price-eod/full, which despite its name is
split-adjusted -- AAPL closed at 499.24 on 2020-08-28 (4:1 split the next
session) and /full reports 124.81. core.adjustment_factor then applied the split
again, so pre-2014 AAPL would have been divided by 28 twice.
"""

from __future__ import annotations

import pytest

from fafnir.ingest.daily_price import ENDPOINT, _validate_bar
from fafnir.sources.fmp import FMPClient

# The 2020-08-28 AAPL bar exactly as /non-split-adjusted returns it: as-traded
# values under adj*-prefixed names.
AAPL_PRE_SPLIT = {
    "date": "2020-08-28",
    "adjOpen": 504.05,
    "adjHigh": 505.77,
    "adjLow": 498.31,
    "adjClose": 499.24,
    "volume": 187630000,
    "symbol": "AAPL",
}
SPLIT_ADJUSTED_CLOSE = 124.81  # what /full would have given


def _client(payload):
    client = FMPClient.__new__(FMPClient)
    client._call = lambda endpoint, params=None: (payload, 200, 0)
    return client


def test_the_endpoint_is_the_unadjusted_one():
    assert FMPClient.EP_EOD_FULL == "historical-price-eod/non-split-adjusted"


def test_loader_endpoint_matches_the_client():
    # They key watermarks and lineage; a mismatch silently orphans both.
    assert ENDPOINT == FMPClient.EP_EOD_FULL


def test_as_traded_prices_reach_the_canonical_fields():
    bar = _client([AAPL_PRE_SPLIT])._eod_window("AAPL", "2020-08-28", "2020-08-28")[0]
    assert bar["open"] == 504.05
    assert bar["high"] == 505.77
    assert bar["low"] == 498.31
    assert bar["close"] == 499.24


def test_original_keys_are_preserved_for_lineage():
    bar = _client([AAPL_PRE_SPLIT])._eod_window("AAPL", "2020-08-28", "2020-08-28")[0]
    assert bar["adjClose"] == 499.24


def test_the_normalized_bar_validates_and_keeps_the_as_traded_close():
    bar = _client([AAPL_PRE_SPLIT])._eod_window("AAPL", "2020-08-28", "2020-08-28")[0]
    row, reason = _validate_bar(bar)

    assert reason is None
    assert row["close"] == 499.24
    assert row["close"] != SPLIT_ADJUSTED_CLOSE
    assert str(row["trade_date"]) == "2020-08-28"
    # This endpoint carries no vwap; absence must not fail the bar.
    assert row["vwap"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2020-08-28", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {
            "date": "2020-08-28",
            "adjOpen": 1.0,
            "adjHigh": 2.0,
            "adjLow": 0.5,
            "adjClose": 1.5,
        },
    ],
)
def test_both_field_spellings_normalize_the_same(payload):
    got = FMPClient._normalize_bar(payload)
    assert (got["open"], got["high"], got["low"], got["close"]) == (1.0, 2.0, 0.5, 1.5)


def test_canonical_names_win_when_both_are_present():
    both = dict(AAPL_PRE_SPLIT, close=1.5)
    assert FMPClient._normalize_bar(both)["close"] == 1.5
