"""
The FMP endpoints only ``duk`` reads, on the warehouse's client (ADR 0017).

``duk -S fmp``, ``duk yc`` and ``duk ls`` used to carry a second FMP client with
its own request code. These pin the five endpoints that moved onto
:class:`FMPClient` -- what each one asks the vendor and what it hands back -- so
the client is the one place an FMP endpoint is spelled.
"""

from __future__ import annotations

import pytest

from fafnir.sources.fmp import FMPClient


def _client(*payloads):
    """A client whose ``_call`` answers from a queue and records what was asked."""
    client = FMPClient.__new__(FMPClient)
    queue = list(payloads)
    calls: list[tuple[str, dict]] = []

    def _call(endpoint, params=None):
        calls.append((endpoint, dict(params or {})))
        return queue.pop(0), 200, 0

    client._call = _call
    return client, calls


class TestActivelyTradingList:
    def test_rows_as_sent(self):
        rows = [{"symbol": "AAPL", "name": "Apple Inc."}]
        client, calls = _client(rows)
        assert client.actively_trading_list() == rows
        assert calls == [("actively-trading-list", {})]

    def test_a_non_list_payload_is_empty(self):
        client, _ = _client({"unexpected": True})
        assert client.actively_trading_list() == []


class TestScreen:
    def test_filters_pass_through_under_the_vendors_names(self):
        client, calls = _client([{"symbol": "AAPL"}])
        rows = client.screen(
            {
                "sector": "Technology",
                "marketCapMoreThan": 1_000_000_000,
                "country": None,
            },
            limit=50,
        )
        assert rows == [{"symbol": "AAPL"}]
        endpoint, params = calls[0]
        assert endpoint == FMPClient.EP_SCREENER
        assert params == {
            "sector": "Technology",
            "marketCapMoreThan": 1_000_000_000,
            "limit": 50,
        }

    def test_booleans_are_spelled_as_the_vendor_wants(self):
        client, calls = _client([])
        client.screen({"isEtf": True, "isActivelyTrading": False})
        assert calls[0][1] == {"isEtf": "true", "isActivelyTrading": "false"}

    def test_one_request_whatever_the_size(self):
        # Unlike company_screener, which pages the whole universe.
        client, calls = _client([{"symbol": "A"}] * 5000)
        assert len(client.screen({})) == 5000
        assert len(calls) == 1


class TestEodDividendAdjusted:
    def test_asks_the_dividend_adjusted_endpoint_with_the_window(self):
        client, calls = _client([{"date": "2023-01-03", "adjClose": 154.0}])
        client.eod_dividend_adjusted("AAPL", "2023-01-01", "2023-01-31")
        assert calls == [
            (
                "historical-price-eod/dividend-adjusted",
                {"symbol": "AAPL", "from": "2023-01-01", "to": "2023-01-31"},
            )
        ]

    def test_one_bar_per_date_ascending(self):
        client, _ = _client(
            [
                {"date": "2023-01-03", "close": 2.0},
                {"date": "2023-01-02", "close": 1.0},
                {"date": "2023-01-03", "close": 3.0},  # a vendor-side repeat
            ]
        )
        bars = client.eod_dividend_adjusted("AAPL")
        assert [(b["date"], b["close"]) for b in bars] == [
            ("2023-01-02", 1.0),
            ("2023-01-03", 3.0),
        ]

    def test_historical_envelope_is_unwrapped(self):
        client, _ = _client({"symbol": "AAPL", "historical": [{"date": "2023-01-02"}]})
        assert client.eod_dividend_adjusted("AAPL") == [{"date": "2023-01-02"}]


class TestTreasuryRates:
    def test_window_and_rows(self):
        rows = [{"date": "2023-01-03", "month1": 4.35, "year10": 3.79}]
        client, calls = _client(rows)
        assert client.treasury_rates("2023-01-01", "2023-01-31") == rows
        assert calls == [("treasury-rates", {"from": "2023-01-01", "to": "2023-01-31"})]

    def test_no_window_sends_none(self):
        client, calls = _client([])
        client.treasury_rates()
        assert calls == [("treasury-rates", {})]


class TestSearchIdentifier:
    @pytest.mark.parametrize(
        "scheme,value,path,param",
        [
            ("cik", "51143", "search-cik", "cik"),
            ("isin", "US4592001014", "search-isin", "isin"),
            ("cusip", "459200101", "search-cusip", "cusip"),
        ],
    )
    def test_each_scheme_has_its_own_endpoint(self, scheme, value, path, param):
        row = {"symbol": "IBM"}
        client, calls = _client([row])
        assert client.search_identifier(scheme, value) == [row]
        assert calls == [(path, {param: value})]

    def test_a_single_object_answer_is_one_row(self):
        client, _ = _client({"symbol": "IBM"})
        assert client.search_identifier("cik", "51143") == [{"symbol": "IBM"}]

    def test_an_unknown_scheme_is_refused_before_any_request(self):
        client, calls = _client()
        with pytest.raises(ValueError):
            client.search_identifier("sedol", "0263494")
        with pytest.raises(ValueError):
            client.search_identifier("cik", "")
        assert calls == []
