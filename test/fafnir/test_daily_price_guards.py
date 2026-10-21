"""A price load that quietly writes nothing must fail, not report success.

The failure these guard against: `initial_backfill.sh` ran `ingest prices`
against an empty security master, every symbol was skipped, the run exited 0,
and the script marched on to adjustment and marts -- leaving a warehouse that
looked finished and held no prices.
"""

from __future__ import annotations

from datetime import date

import pytest

import fafnir.ingest.daily_price as dp
from fafnir.ingest.daily_price import load_prices
from fafnir.sources.fmp import SourceError


class _FMP:
    bytes_downloaded = 0


class _DB:
    """load_prices commits at each symbol boundary; count them.

    ``watermarks`` maps endpoint -> row count, which is all the split-adjusted
    changeover guard reads. The default is a fresh warehouse (no watermarks at all).
    """

    def __init__(self, watermarks: dict | None = None):
        self.commits = 0
        self._watermarks = watermarks or {}

    def commit(self) -> None:
        self.commits += 1

    def fetchval(self, sql, params=None):
        return self._watermarks.get(params[1], 0)


class _RunLogStub:
    def __init__(self, *a, **kw):
        self.run = type("R", (), {"run_id": 1, "rows_quarantined": 0})()

    def __enter__(self):
        return self.run

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def outcomes(monkeypatch):
    """Drive load_prices by scripting each symbol's per-symbol outcome."""
    monkeypatch.setattr(dp, "RunLog", _RunLogStub)

    def install(mapping):
        def fake(db, fmp, symbol, *, run, stats=None, **kw):
            outcome = mapping[symbol]
            if outcome == "unknown":
                stats["unknown"] = stats.get("unknown", 0) + 1
                return 0
            if outcome == "empty":
                stats["empty"] = stats.get("empty", 0) + 1
                stats.setdefault("bars", 0)
                return 0
            fetched, written = outcome
            stats["bars"] = stats.get("bars", 0) + fetched
            return written

        monkeypatch.setattr(dp, "load_symbol_prices", fake)

    return install


def test_empty_symbol_list_raises(outcomes):
    with pytest.raises(ValueError, match="No symbols to load"):
        load_prices(_DB(), _FMP(), [])


def test_every_symbol_unknown_raises(outcomes):
    outcomes({"AAPL": "unknown", "MSFT": "unknown"})
    with pytest.raises(ValueError, match="security master"):
        load_prices(_DB(), _FMP(), ["AAPL", "MSFT"], start_date=date(1990, 1, 1))


def test_backfill_that_fetched_no_bars_raises(outcomes):
    outcomes({"AAPL": "empty", "MSFT": "empty"})
    with pytest.raises(SourceError, match="no bars for any"):
        load_prices(_DB(), _FMP(), ["AAPL", "MSFT"], start_date=date(1990, 1, 1))


def test_incremental_run_with_nothing_new_is_not_an_error(outcomes):
    # The nightly job on a weekend: no window, no new bars, must stay quiet.
    outcomes({"AAPL": "empty", "MSFT": "empty"})
    assert load_prices(_DB(), _FMP(), ["AAPL", "MSFT"]) == 0


def test_partial_success_does_not_raise(outcomes):
    outcomes({"AAPL": (9000, 9000), "GONE": "unknown", "MSFT": "empty"})
    got = load_prices(
        _DB(), _FMP(), ["AAPL", "GONE", "MSFT"], start_date=date(1990, 1, 1)
    )
    assert got == 9000


def test_bars_arrived_but_all_quarantined_does_not_raise(outcomes):
    # Quarantine is loud by construction (ops.data_quality_flag + a run status of
    # 'partial'), so it is not the silent failure these guards exist for.
    outcomes({"AAPL": (500, 0)})
    assert load_prices(_DB(), _FMP(), ["AAPL"], start_date=date(1990, 1, 1)) == 0


# -- the split-adjusted -> unadjusted feed changeover -------------------------
#
# The endpoint switch also changes the watermark key, so every symbol looks new.
# Left alone, the next incremental run would fetch each symbol with no from_date,
# hit the 5000-bar cap, and refill the warehouse with ~20 years of history while
# every older row stayed split-adjusted -- a silently wrong price series.

_LEGACY = {dp.LEGACY_SPLIT_ADJUSTED_ENDPOINT: 8000}


def test_incremental_run_refuses_to_cross_the_changeover(outcomes):
    outcomes({"AAPL": "empty"})
    with pytest.raises(SourceError, match="split-adjusted"):
        load_prices(_DB(_LEGACY), _FMP(), ["AAPL"])


def test_explicit_backfill_is_allowed_across_the_changeover(outcomes):
    # An explicit window IS the re-backfill, so it must not be blocked.
    outcomes({"AAPL": (500, 500)})
    got = load_prices(_DB(_LEGACY), _FMP(), ["AAPL"], start_date=date(1990, 1, 1))
    assert got == 500


def test_incremental_run_is_allowed_once_the_new_feed_has_watermarks(outcomes):
    outcomes({"AAPL": "empty"})
    marks = dict(_LEGACY, **{dp.ENDPOINT: 8000})
    assert load_prices(_DB(marks), _FMP(), ["AAPL"]) == 0


def test_fresh_warehouse_is_not_blocked(outcomes):
    outcomes({"AAPL": "empty"})
    assert load_prices(_DB(), _FMP(), ["AAPL"]) == 0


# ---------------------------------------------------------------------------
# G7: a round lot's rounding is not a restatement
# ---------------------------------------------------------------------------


def _bar(close, volume):
    return {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def test_an_odd_lot_read_back_as_a_round_lot_is_no_restatement():
    # WSGE 2026-10-16: 9 shares on the first print, exactly 100 on the re-read.
    guard = dp.RestatementGuard()
    stored, incoming = _bar("10.25", 9), _bar("10.25", 100)
    assert dp.odd_lot_rounding(stored, incoming, guard)
    assert dp.restatement_detail(stored, incoming, guard) is None
    # Either way round, it is the rounding.
    assert dp.restatement_detail(incoming, stored, guard) is None


def test_a_volume_revision_above_a_round_lot_is_still_one():
    guard = dp.RestatementGuard()
    for stored, incoming in (
        (2_123_830, 6_888_543),  # RCT
        (109, 21),  # MAYS: the larger side is above the floor
        (11, 331),  # 2026-10-20: not a round lot
    ):
        old, new = _bar("5", stored), _bar("5", incoming)
        assert not dp.odd_lot_rounding(old, new, guard)
        assert dp.restatement_detail(old, new, guard)["over"] == ["volume"]


def test_a_close_conflict_at_odd_lot_volume_is_still_flagged():
    detail = dp.restatement_detail(
        _bar("11.39", 9), _bar("67.03", 100), dp.RestatementGuard()
    )
    assert detail["over"] == ["close"]


def test_the_round_lot_is_a_setting():
    stored, incoming = _bar("10.25", 9), _bar("10.25", 100)
    off = dp.RestatementGuard(min_volume=0)
    assert not dp.odd_lot_rounding(stored, incoming, off)
    assert dp.restatement_detail(stored, incoming, off)["over"] == ["volume"]
    # Within the volume ratio there is nothing to round.
    assert not dp.odd_lot_rounding(_bar("1", 40), _bar("1", 100), dp.RestatementGuard())
