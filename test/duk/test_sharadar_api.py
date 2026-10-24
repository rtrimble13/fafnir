"""Unit tests for ``duk -S sharadar`` (HTTP mocked).

duk makes no request of its own: every call goes through the warehouse's
``SharadarClient`` (ADR 0017). These tests pin what duk *does* with a payload --
the raw and adjusted recovery arithmetic, the two payload shapes, the SEP→SFP
fallback -- and that the request is the client's (key in the header, never the
URL; the client's redaction on failure), not what the vendor sends.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest
import requests

from duk.sharadar_api import (
    DEFAULT_BASE_URL,
    SharadarAPIError,
    frame_from_rows,
    get_price_history,
    price_table_api,
)
from fafnir.sources import base as base_mod
from fafnir.sources.base import SourceError

# A deliberately low-entropy fixture: the tests only need a value that must not
# appear in a log or a message, and a realistic-looking key trips secret scanners.
SECRET = "test-sharadar-key-not-a-secret"

# Two bars of a stock that later split 2:1: the split-adjusted fields are half
# the as-traded values, closeunadj is as traded, closeadj carries a dividend too.
ROWS = [
    {
        "ticker": "ACME",
        "date": "2020-01-02",
        "open": 50.0,
        "high": 52.0,
        "low": 49.0,
        "close": 51.0,
        "volume": 2000,
        "closeadj": 45.9,
        "closeunadj": 102.0,
        "lastupdated": "2020-08-31",
    },
    {
        "ticker": "ACME",
        "date": "2020-01-03",
        "open": 51.0,
        "high": 53.0,
        "low": 50.5,
        "close": 52.0,
        "volume": 1000,
        "closeadj": 46.8,
        "closeunadj": 104.0,
        "lastupdated": "2020-08-31",
    },
]


class TestFrameFromRows:
    def test_raw_recovers_as_traded_values_with_the_vendor_identities(self):
        df = frame_from_rows(ROWS, adjusted=False)
        first = df.iloc[0]
        # close == closeunadj; open/high/low scaled by the same ratio (2.0)
        assert first["close"] == pytest.approx(102.0)
        assert first["open"] == pytest.approx(100.0)
        assert first["high"] == pytest.approx(104.0)
        assert first["low"] == pytest.approx(98.0)
        # VolumeUnadj = Volume × Close / CloseUnadj: 2000 × 51 / 102 = 1000
        assert int(first["volume"]) == 1000

    def test_adjusted_scales_every_price_by_closeadj_over_close(self):
        df = frame_from_rows(ROWS, adjusted=True)
        first = df.iloc[0]
        assert first["close"] == pytest.approx(45.9)
        assert first["open"] == pytest.approx(50.0 * 45.9 / 51.0)
        # volume stays as delivered (split-adjusted), like FMP's adjusted feed
        assert int(first["volume"]) == 2000

    def test_frame_is_date_indexed_ascending(self):
        df = frame_from_rows(list(reversed(ROWS)), adjusted=False)
        assert df.index.name == "date"
        assert list(df.index.strftime("%Y-%m-%d")) == ["2020-01-02", "2020-01-03"]

    def test_missing_unadjusted_column_is_an_error_not_a_silent_adjusted_series(self):
        rows = [{k: v for k, v in r.items() if k != "closeunadj"} for r in ROWS]
        with pytest.raises(ValueError):
            frame_from_rows(rows, adjusted=False)


def _response(payload, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = {}
    resp.content = b"[]"
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# The client's HTTP seam. ``requests.Session.get`` is what ``BaseSource._get``
# calls, so patching it covers every request duk can make and nothing else.
GET = "requests.Session.get"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: None)


class TestPriceTableAPI:
    @mock.patch(GET)
    def test_request_is_the_warehouse_clients(self, mock_get):
        mock_get.return_value = _response(ROWS)
        rows = price_table_api(
            "SEP",
            "acme",
            SECRET,
            from_date=date(2020, 1, 1),
            to_date=date(2020, 1, 31),
        )
        assert rows == ROWS
        url = mock_get.call_args.args[0]
        params = mock_get.call_args.kwargs["params"]
        headers = mock_get.call_args.kwargs["headers"]
        assert url == f"{DEFAULT_BASE_URL}/SEP"
        assert params["ticker"] == "ACME"
        # Sharadar's documented spelling (agent guide, 2026-10-19); the Nasdaq
        # Data Link era's `date.gte` / `date.lte` would be ignored.
        assert params["from"] == "2020-01-01"
        assert params["to"] == "2020-01-31"
        assert params["format"] == "json"
        # The key travels in the header the client uses, never in the URL, so an
        # exception that renders the URL cannot carry it (ADR 0017).
        assert headers["x-api-key"] == SECRET
        assert "api_key" not in params and SECRET not in str(params)

    @mock.patch(GET)
    def test_base_url_override(self, mock_get):
        mock_get.return_value = _response([])
        price_table_api("SFP", "SPY", SECRET, base_url="https://example.test/v9/")
        assert mock_get.call_args.args[0] == "https://example.test/v9/SFP"

    @mock.patch(GET)
    def test_datatable_payload_shape_is_accepted(self, mock_get):
        payload = {
            "datatable": {
                "columns": [{"name": k} for k in ROWS[0]],
                "data": [list(r.values()) for r in ROWS],
            }
        }
        mock_get.return_value = _response(payload)
        assert price_table_api("SEP", "ACME", SECRET) == ROWS

    @mock.patch(GET)
    def test_transport_failure_neither_raises_nor_logs_the_key(self, mock_get, caplog):
        mock_get.side_effect = requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: /v1.0/data/SEP?api_key={SECRET}"
        )
        with pytest.raises(SharadarAPIError) as excinfo:
            price_table_api("SEP", "ACME", SECRET)
        assert SECRET not in str(excinfo.value)
        assert SECRET not in caplog.text
        assert excinfo.value.__cause__ is None
        # duk's error is the warehouse client's, so one handler serves both.
        assert isinstance(excinfo.value, SourceError)

    @mock.patch(GET)
    def test_an_error_body_that_echoes_the_key_is_redacted(self, mock_get):
        mock_get.return_value = _response(
            {"Error Message": f"key {SECRET} is not valid"}
        )
        with pytest.raises(SharadarAPIError) as excinfo:
            price_table_api("SEP", "ACME", SECRET)
        assert SECRET not in str(excinfo.value)

    def test_unknown_table_refused(self):
        with pytest.raises(ValueError):
            price_table_api("SF1", "ACME", SECRET)

    def test_a_missing_key_is_refused_before_any_request(self):
        with mock.patch(GET) as mock_get:
            with pytest.raises(ValueError):
                price_table_api("SEP", "ACME", "")
            assert mock_get.call_count == 0


class TestGetPriceHistory:
    @mock.patch(GET)
    def test_falls_back_to_sfp_when_sep_is_empty(self, mock_get):
        mock_get.side_effect = [_response([]), _response(ROWS)]
        df = get_price_history(SECRET, "ACME", fields=["close"])
        assert list(df.columns) == ["close"]
        assert len(df) == 2
        tables = [c.args[0].rsplit("/", 1)[-1] for c in mock_get.call_args_list]
        assert tables == ["SEP", "SFP"]

    @mock.patch(GET)
    def test_one_client_serves_both_tables(self, mock_get):
        # The fallback reuses the client, so one session and one byte count
        # cover the whole read -- what a loader would see.
        import duk.sharadar_api as mod

        mock_get.side_effect = [_response([]), _response(ROWS)]
        with mock.patch.object(mod, "make_client", wraps=mod.make_client) as made:
            get_price_history(SECRET, "ACME")
        assert made.call_count == 1

    @mock.patch(GET)
    def test_empty_everywhere_is_an_empty_frame(self, mock_get):
        mock_get.return_value = _response([])
        assert get_price_history(SECRET, "NOPE").empty

    @mock.patch(GET)
    def test_limit_keeps_the_tail_like_the_other_sources(self, mock_get):
        mock_get.return_value = _response(ROWS)
        df = get_price_history(SECRET, "ACME", limit=1)
        assert len(df) == 1
        assert df.index[0].strftime("%Y-%m-%d") == "2020-01-03"
