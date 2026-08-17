"""A long price window must not be silently truncated to the endpoint's row cap.

Regression: `--from 1990-01-01` for AAPL and MSFT returned exactly 5000 bars
each, both starting 2006-09-28 -- the same date for two companies, which is a row
cap rather than history. The request succeeded, the run logged success, and 16
years were missing with nothing to indicate it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fafnir.sources.fmp import FMPClient

TODAY = date(2026, 8, 14)


def _client(cap=FMPClient.EOD_MAX_ROWS, first_listed=date(1980, 12, 12)):
    """FMP stand-in that truncates each response to the newest `cap` bars.

    Approximates trading days as weekdays (~260/yr against a real ~252), so the
    truncation boundary lands a little later than production's -- the shape of
    the failure is what matters, not the exact date.
    """
    client = FMPClient.__new__(FMPClient)
    calls: list[tuple] = []

    def _call(endpoint, params=None):
        params = params or {}
        start = (
            date.fromisoformat(params["from"]) if params.get("from") else first_listed
        )
        end = date.fromisoformat(params["to"]) if params.get("to") else TODAY
        calls.append((params.get("from"), params.get("to")))
        bars, cursor = [], max(start, first_listed)
        while cursor <= min(end, TODAY):
            if cursor.weekday() < 5:
                bars.append({"date": cursor.isoformat(), "close": 1.0})
            cursor += timedelta(days=1)
        return bars[-cap:], 200, 0  # the cap drops the OLDEST bars

    client._call = _call
    return client, calls


def test_a_1990_window_actually_starts_in_1990():
    client, calls = _client()
    bars = client.eod_raw("AAPL", from_date="1990-01-01", to_date="2026-08-14")

    assert bars[0]["date"] == "1990-01-01"
    assert bars[-1]["date"] == "2026-08-14"
    assert len(bars) > 9000
    assert len(calls) == 3  # 15-year slices across ~36.6 years


def test_one_unchunked_request_would_have_been_truncated():
    # The behaviour being worked around, pinned so a client change surfaces it.
    client, _ = _client()
    bars = client._eod_window("AAPL", "1990-01-01", "2026-08-14")
    assert len(bars) == FMPClient.EOD_MAX_ROWS
    assert bars[0]["date"] > "2006-01-01"


def test_no_individual_slice_reaches_the_cap():
    client, calls = _client()
    client.eod_raw("AAPL", from_date="1990-01-01", to_date="2026-08-14")
    for from_date, to_date in list(calls):
        window = client._eod_window("AAPL", from_date, to_date)
        assert len(window) < FMPClient.EOD_MAX_ROWS


def test_results_are_ascending_and_deduplicated():
    client, _ = _client()
    dates = [
        b["date"]
        for b in client.eod_raw("AAPL", from_date="1990-01-01", to_date="2026-08-14")
    ]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))


def test_incremental_window_still_costs_one_request():
    # The nightly path asks for a few days; it must not pay for chunking.
    client, calls = _client()
    client.eod_raw("AAPL", from_date="2026-08-01", to_date="2026-08-14")
    assert len(calls) == 1


def test_a_recent_listing_yields_only_its_real_history():
    client, _ = _client(first_listed=date(2020, 1, 2))
    bars = client.eod_raw("NEW", from_date="1990-01-01", to_date="2026-08-14")
    assert bars[0]["date"] == "2020-01-02"


def test_no_from_date_warns_at_the_cap(caplog):
    client, calls = _client()
    with caplog.at_level("WARNING"):
        bars = client.eod_raw("AAPL")
    assert len(calls) == 1
    assert len(bars) == FMPClient.EOD_MAX_ROWS
    assert "endpoint cap" in caplog.text


@pytest.mark.parametrize("chunk_days", [FMPClient.EOD_CHUNK_DAYS])
def test_chunk_size_leaves_headroom_under_the_cap(chunk_days):
    # ~252 trading days a year; the slice must stay clear of EOD_MAX_ROWS.
    expected_bars = chunk_days / 365.25 * 252
    assert expected_bars < FMPClient.EOD_MAX_ROWS * 0.8
