"""
Integration tests for the ingestion core (needs FAFNIR_TEST_DSN):
idempotency, adjustment correctness, point-in-time stability, and DQ checks.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import adjustments
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.daily_price import load_symbol_prices
from fafnir.ingest.runlog import RunLog

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")


class _FakeFMP:
    """Minimal FMP stand-in returning canned bars (no network)."""

    bytes_downloaded = 0

    def __init__(self, bars):
        self._bars = bars

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return self._bars


def _mk_security(db, symbol="AAA"):
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


def _prices(sid, rows):
    return [{"security_id": sid, **r} for r in rows]


def test_price_upsert_is_idempotent(db):
    sid = _mk_security(db)
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 101,
                "high": 103,
                "low": 100,
                "close": 102,
                "volume": 1100,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_daily_prices(db, rows)  # second load: same window
    n = db.fetchval(
        "SELECT count(*) FROM core.daily_price WHERE security_id=%s", (sid,)
    )
    assert n == 2  # no duplication


def test_split_adjustment_is_correct_and_pit_stable(db):
    sid = _mk_security(db, "BBB")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 2),
                "open": 50,
                "high": 51,
                "low": 49,
                "close": 50,
                "volume": 2000,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=2,
        split_denominator=1,
    )
    adjustments.compute_for_security(db, sid)

    adj = repo.read_price_history(db, "BBB", None, None, adjusted=True)
    by_date = {r["date"]: r for r in adj}
    # Pre-split closes halved; ex-date and later unchanged.
    assert float(by_date[dt.date(2023, 5, 31)]["close"]) == pytest.approx(50.0)
    assert float(by_date[dt.date(2023, 6, 1)]["close"]) == pytest.approx(50.0)
    assert float(by_date[dt.date(2023, 6, 2)]["close"]) == pytest.approx(50.0)
    # Volume scaled into post-split share terms pre-split.
    assert int(by_date[dt.date(2023, 5, 31)]["volume"]) == 2000

    raw = repo.read_price_history(db, "BBB", None, None, adjusted=False)
    raw_by_date = {r["date"]: r for r in raw}
    assert float(raw_by_date[dt.date(2023, 5, 31)]["close"]) == pytest.approx(100.0)


def test_dividend_adjustment(db):
    sid = _mk_security(db, "CCC")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 5, 31),
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1000,
            },
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 99,
                "high": 99,
                "low": 99,
                "close": 99,
                "volume": 1000,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2023, 6, 1),
        dividend_amount=1.0,
    )
    adjustments.compute_for_security(db, sid)
    adj = {
        r["date"]: r
        for r in repo.read_price_history(db, "CCC", None, None, adjusted=True)
    }
    # Prior close 100, dividend 1 -> factor (100-1)/100 = 0.99 applied to 5/31.
    assert float(adj[dt.date(2023, 5, 31)]["close"]) == pytest.approx(99.0)
    assert float(adj[dt.date(2023, 6, 1)]["close"]) == pytest.approx(99.0)


def test_gap_check_flags_missing_day(db):
    sid = _mk_security(db, "DDD")
    # 2023-06-01 (Thu) and 2023-06-05 (Mon) present; 2023-06-02 (Fri, open) missing.
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
            {
                "trade_date": dt.date(2023, 6, 5),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    checks.check_gaps(db, exchange_code="NASDAQ")
    flags = db.fetchall(
        "SELECT record_key FROM ops.data_quality_flag WHERE check_name='gap' AND security_id=%s",
        (sid,),
    )
    flagged_dates = {f["record_key"]["trade_date"] for f in flags}
    assert "2023-06-02" in flagged_dates


def test_outlier_check_flags_unexplained_jump(db):
    sid = _mk_security(db, "EEE")
    rows = _prices(
        sid,
        [
            {
                "trade_date": dt.date(2023, 6, 1),
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1,
            },
            {
                "trade_date": dt.date(2023, 6, 2),
                "open": 100,
                "high": 200,
                "low": 100,
                "close": 200,
                "volume": 1,
            },
        ],
    )
    repo.upsert_daily_prices(db, rows)
    n = checks.check_outliers(db, threshold=0.5)
    assert n >= 1


def test_ensure_year_partition_relocates_default_rows(db):
    # A row in a year with no dedicated partition lands in the DEFAULT partition;
    # creating that year's partition must succeed and relocate the stray row
    # (regression test for the attach-conflict bug).
    # Repeatable: the partition table persists across runs (TRUNCATE won't drop it).
    db.execute("DROP TABLE IF EXISTS core.daily_price_y2099")
    sid = _mk_security(db, "FFF")
    # 2099 has no dedicated partition -> goes to daily_price_default.
    far = dt.date(2099, 3, 15)
    repo.upsert_daily_prices(
        db,
        _prices(
            sid,
            [
                {
                    "trade_date": far,
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "volume": 1,
                }
            ],
        ),
    )
    in_default = db.fetchval(
        "SELECT count(*) FROM core.daily_price_default WHERE security_id=%s", (sid,)
    )
    assert in_default == 1

    # Creating the 2099 partition must not raise and must relocate the row.
    created = maintenance.ensure_year_partition(db, 2099)
    assert created is True
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price_default WHERE security_id=%s",
            (sid,),
        )
        == 0
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price_y2099 WHERE security_id=%s", (sid,)
        )
        == 1
    )
    # Row is still visible through the parent partitioned table.
    assert (
        db.fetchval(
            "SELECT close FROM core.daily_price WHERE security_id=%s AND trade_date=%s",
            (sid, far),
        )
        == 10
    )


def test_watermark_not_advanced_past_quarantined_bar(db):
    # 6/1 clean, 6/2 bad (high<low) -> quarantined, 6/5 clean. The watermark must
    # stay at 6/1 so the overlap re-fetches 6/2 next run (no permanent gap).
    sid = _mk_security(db, "GGG")
    bars = [
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-02",
            "open": 10,
            "high": 8,
            "low": 9,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-05",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
    ]
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(
            db,
            _FakeFMP(bars),
            "GGG",
            run=run,
            start_date=dt.date(2023, 6, 1),
            end_date=dt.date(2023, 6, 5),
        )
    # Clean bars were still written (6/1 and 6/5); 6/2 quarantined.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id=%s", (sid,)
        )
        == 2
    )
    # Watermark held at the last contiguous clean date before the quarantine.
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 1)


def test_resolvers_agree_on_ambiguous_symbol(db):
    # Two securities share primary_symbol with no xref row -> exercise the fallback.
    sid_fmp = repo.upsert_security(
        db, primary_symbol="DUP", company_name="A", source="fmp"
    )
    repo.upsert_security(db, primary_symbol="DUP", company_name="B", source="other")

    from_repo = repo.resolve_security_id(db, "DUP")

    import psycopg
    from psycopg.rows import dict_row

    from duk.datasource.db import _resolve_security_id

    with psycopg.connect(DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        from_duk = _resolve_security_id(cur, "DUP")

    # Both paths resolve identically, and to the fmp-source row (deterministic).
    assert from_repo == from_duk == sid_fmp


def test_watermark_releases_after_quarantine_budget(db):
    # A persistently-bad bar holds the watermark for MAX_QUARANTINE_HOLDS runs,
    # then ingestion is allowed past it (no unbounded stall).
    from fafnir.ingest.daily_price import MAX_QUARANTINE_HOLDS

    sid = _mk_security(db, "HHH")
    bars = [
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-02",
            "open": 10,
            "high": 8,
            "low": 9,
            "close": 10,
            "volume": 1,
        },
        {
            "date": "2023-06-05",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        },
    ]
    for i in range(MAX_QUARANTINE_HOLDS):
        with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
            load_symbol_prices(
                db,
                _FakeFMP(bars),
                "HHH",
                run=run,
                start_date=dt.date(2023, 6, 1),
                end_date=dt.date(2023, 6, 5),
            )
        if i < MAX_QUARANTINE_HOLDS - 1:
            # Still within budget -> held below the bad 6/2 bar.
            assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(
                2023, 6, 1
            )
    # Budget exhausted -> watermark advances past the bad bar to the latest clean.
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 5)


class _RecordingFMP(_FakeFMP):
    """_FakeFMP that remembers the window each call asked for.

    The window is the whole point of the test below: a symbol whose watermark is
    never written asks for `from_date=None` -- its entire history -- on every single
    run, and that is the difference between one flag per bar and one flag per bar
    per night forever.
    """

    def __init__(self, bars):
        super().__init__(bars)
        self.windows: list = []

    def eod_raw(self, symbol, from_date=None, to_date=None):
        self.windows.append(from_date)
        return super().eod_raw(symbol, from_date, to_date)


def _subresolution_bars(*days):
    """Bars quoted below the 1e-6 core.daily_price can hold -- every one rejected."""
    return [
        {
            "date": d,
            "open": "0.0000001",
            "high": "0.0000001",
            "low": "0.0000001",
            "close": "0.0000001",
            "volume": 1,
        }
        for d in days
    ]


def test_a_history_with_no_storable_bar_stops_being_re_pulled(db):
    # The production shape behind 3,783 price_subresolution_price flags over 8
    # securities in three nights. Every bar is below the money column's resolution,
    # so `clean` is empty -- and `safe_dates` is derived from `clean`, so the
    # watermark was never written at all. get_watermark then returns None, the next
    # run asks for the full history again, and every bar is re-flagged. The
    # quarantine budget cannot break the loop, because exhausting it only widens an
    # already-empty set.
    from fafnir.ingest.daily_price import MAX_QUARANTINE_HOLDS

    sid = _mk_security(db, "SUBP")
    fmp = _RecordingFMP(_subresolution_bars("2023-06-01", "2023-06-02", "2023-06-05"))

    for _ in range(MAX_QUARANTINE_HOLDS):
        with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
            load_symbol_prices(db, fmp, "SUBP", run=run)

    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 5)
    # ...and the next run asks for a window instead of the whole history again.
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(db, fmp, "SUBP", run=run)
    assert fmp.windows[-1] is not None
    assert fmp.windows[:MAX_QUARANTINE_HOLDS] == [None] * MAX_QUARANTINE_HOLDS


def test_an_unstorable_history_still_holds_the_line_while_in_budget(db):
    # The release is bounded, not immediate: the bars are re-read while there is any
    # chance the feed corrects them, which is the same trade the budget already makes
    # for a single bad bar among good ones.
    from fafnir.ingest.daily_price import MAX_QUARANTINE_HOLDS

    sid = _mk_security(db, "SUBP")
    fmp = _RecordingFMP(_subresolution_bars("2023-06-01", "2023-06-02"))
    for _ in range(MAX_QUARANTINE_HOLDS - 1):
        with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
            load_symbol_prices(db, fmp, "SUBP", run=run)
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) is None


def test_the_unstorable_bars_stay_flagged_after_the_watermark_moves(db):
    # Advancing the watermark is about ingestion, not about the verdict: the bars
    # were never stored and the operator still has to decide what to do about the
    # security. Losing the flags here would turn a bounded loop into a silent drop.
    from fafnir.ingest.daily_price import MAX_QUARANTINE_HOLDS

    sid = _mk_security(db, "SUBP")
    fmp = _RecordingFMP(_subresolution_bars("2023-06-01", "2023-06-02"))
    for _ in range(MAX_QUARANTINE_HOLDS):
        with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
            load_symbol_prices(db, fmp, "SUBP", run=run)

    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 2)
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s "
            "AND check_name = 'price_subresolution_price' AND resolved_at IS NULL",
            (sid,),
        )
        > 0
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s", (sid,)
        )
        == 0
    ), "an unstorable bar must never reach core.daily_price"


def test_a_clean_bar_still_beats_a_quarantined_one_for_the_watermark(db):
    # Regression guard on the new branch: it must only fire when there is nothing
    # storable at all. With one good bar the ordinary rule still applies -- the
    # watermark sits at the last clean date, not at the later bad one.
    sid = _mk_security(db, "MIXED")
    bars = [
        {
            "date": "2023-06-01",
            "open": 10,
            "high": 10,
            "low": 10,
            "close": 10,
            "volume": 1,
        }
    ] + _subresolution_bars("2023-06-02")
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(db, _FakeFMP(bars), "MIXED", run=run)
    assert repo.get_watermark(db, "fmp", PRICE_ENDPOINT, sid) == dt.date(2023, 6, 1)


def _flattened_bars(*days):
    """Bars quoted just ABOVE the 5e-7 rejection cliff.

    Real HIND numbers. Every field rounds to 0.000001, so the bar is stored with a
    zero range and passes every existing check -- the condition price_scale_collapse
    exists to make visible.
    """
    return [
        {
            "date": d,
            "open": "0.000000788",
            "high": "0.000000801",
            "low": "0.000000732",
            "close": "0.000000733",
            "volume": 1,
        }
        for d in days
    ]


def test_a_flattened_bar_is_stored_and_flagged(db):
    sid = _mk_security(db, "HIND")
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(db, _FakeFMP(_flattened_bars("2023-06-01")), "HIND", run=run)

    # Stored, not quarantined -- that is what makes it quiet.
    stored = db.fetchone(
        "SELECT open, high, low, close FROM core.daily_price WHERE security_id = %s",
        (sid,),
    )
    assert stored is not None
    assert len({stored["open"], stored["high"], stored["low"], stored["close"]}) == 1

    flag = db.fetchone(
        "SELECT severity, detail FROM ops.data_quality_flag WHERE security_id = %s "
        "AND check_name = 'price_scale_collapse'",
        (sid,),
    )
    assert flag is not None
    assert flag["severity"] == "warn"
    assert flag["detail"]["source_high"] == "0.000000801"


def test_the_collapse_flag_is_written_once_not_once_per_overlap(db):
    # Unlike the quarantine flags, nothing counts this one's repeats, and the daily
    # overlap re-reads the same bars every run. add_dq_flag_once is what keeps one
    # corrupted bar to one row instead of one row per night.
    sid = _mk_security(db, "HIND")
    bars = _flattened_bars("2023-06-01", "2023-06-02")
    for _ in range(3):
        with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
            load_symbol_prices(
                db,
                _FakeFMP(bars),
                "HIND",
                run=run,
                start_date=dt.date(2023, 6, 1),
                end_date=dt.date(2023, 6, 2),
            )
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s "
            "AND check_name = 'price_scale_collapse'",
            (sid,),
        )
        == 2
    )


def test_a_collapse_flag_does_not_spend_a_quarantine_budget(db):
    # price_scale_collapse shares the `price_` prefix an operator globs for, but it
    # describes a bar that WAS stored. Counting it as a quarantine would let a
    # corrupted-but-stored bar buy down the watermark budget of a rejected one on the
    # same date, releasing ingestion past a bar nobody had looked at.
    sid = _mk_security(db, "HIND")
    repo.add_dq_flag(
        db,
        check_name="price_scale_collapse",
        severity="warn",
        security_id=sid,
        record_key={"symbol": "HIND", "date": "2023-06-01"},
    )
    assert repo.count_price_quarantines(db, sid, "2023-06-01") == 0

    repo.add_dq_flag(
        db,
        check_name="price_subresolution_price",
        severity="warn",
        security_id=sid,
        record_key={"symbol": "HIND", "date": "2023-06-01"},
    )
    assert repo.count_price_quarantines(db, sid, "2023-06-01") == 1


def test_the_collapse_flag_is_reachable_by_the_price_glob(db):
    # `fafnir dq list --check 'price_*'` is the documented way an operator finds this
    # family, and sharing the prefix is the reason the exclusion above had to be by
    # name rather than by pattern.
    _mk_security(db, "HIND")
    with RunLog(db, source="fmp", endpoint=PRICE_ENDPOINT, params={}) as run:
        load_symbol_prices(db, _FakeFMP(_flattened_bars("2023-06-01")), "HIND", run=run)
    rows = repo.list_dq_flags(db, repo.DqFilter(checks=("price_*",)), limit=10)
    assert any(r["check_name"] == "price_scale_collapse" for r in rows)


# -- the split-adjusted feed regression ---------------------------------------
#
# fafnir's factors are the ONLY adjustment applied to core.daily_price, so the feed
# behind it has to be genuinely unadjusted. Loading FMP's `historical-price-eod/full`
# (already split-adjusted) instead put AAPL's 1990-01-02 close in at ~$0.35 rather
# than its true ~$39.20, and the adjustment routine then divided by the 112:1
# cumulative split a second time, landing at ~$0.003. These pin both halves: the
# routine reproduces the real split-adjusted series from raw input, and the loader
# asks for the unadjusted endpoint.

# AAPL splits with an ex-date after 1990-01-02: 2:1, 2:1, 7:1, 4:1.
_AAPL_SPLITS = [
    (dt.date(2000, 6, 21), 2),
    (dt.date(2005, 2, 28), 2),
    (dt.date(2014, 6, 9), 7),
    (dt.date(2020, 8, 31), 4),
]
_AAPL_CUM_SPLIT = 2 * 2 * 7 * 4  # 112


def test_raw_1990_close_survives_a_112_to_1_cumulative_split(db):
    """A deep-history bar must adjust to raw/112, not raw/112/112."""
    sid = _mk_security(db, "AAPL")
    repo.upsert_daily_prices(
        db,
        _prices(
            sid,
            [
                # The close as it actually traded in 1990, pre all four splits.
                {
                    "trade_date": dt.date(1990, 1, 2),
                    "open": 39.20,
                    "high": 39.20,
                    "low": 39.20,
                    "close": 39.20,
                    "volume": 1_000_000,
                },
                {
                    "trade_date": dt.date(2026, 8, 14),
                    "open": 231,
                    "high": 231,
                    "low": 231,
                    "close": 231,
                    "volume": 50_000_000,
                },
            ],
        ),
    )
    for ex_date, num in _AAPL_SPLITS:
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="split",
            ex_date=ex_date,
            split_numerator=num,
            split_denominator=1,
        )
    adjustments.compute_for_security(db, sid)

    rows = db.fetchall(
        "SELECT trade_date, close, close_raw, volume, price_factor "
        "FROM mart.v_daily_price_adjusted WHERE security_id=%s ORDER BY trade_date",
        (sid,),
    )
    old, recent = rows

    # ~0.35, i.e. 39.20/112 -- the published split-adjusted 1990 close.
    expected = Decimal("39.20") / _AAPL_CUM_SPLIT
    assert abs(Decimal(old["close"]) - expected) < Decimal("0.000001")
    # The distinguishing assertion: a second application would land near 0.003.
    assert Decimal(old["close"]) > Decimal("0.3")
    assert Decimal(old["close_raw"]) == Decimal("39.200000")
    # Volume back-adjusts the other way, into today's share terms.
    assert int(old["volume"]) == 1_000_000 * _AAPL_CUM_SPLIT
    # Nothing happens after the last ex-date, so a recent bar is untouched.
    assert Decimal(recent["price_factor"]) == 1
    assert Decimal(recent["close"]) == Decimal("231")


def test_price_loader_reads_the_unadjusted_endpoint():
    """The loader's endpoint is also its watermark key, so pin it explicitly."""
    from fafnir.sources.fmp import FMPClient

    assert PRICE_ENDPOINT == "historical-price-eod/non-split-adjusted"
    assert FMPClient.EP_EOD_RAW == PRICE_ENDPOINT
