"""Live-mode identifier resolution: the FMP search call and its shaping.

`requests` is mocked throughout -- what is under test is which endpoint and
parameter each scheme uses, and that the vendor's rows are shaped into the same
candidate dicts the warehouse produces, so one did-you-mean table serves both
sources.
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from duk import identifiers as ids
from duk.datasource import live as ds_live
from duk.fmp_api import FMPAPIError, identifier_search_api

KEY = "test-key"
FMP_ROW = {
    "symbol": "IBM",
    "companyName": "International Business Machines Corporation",
    "cik": "0000051143",
    "exchangeShortName": "NYSE",
    "exchangeFullName": "New York Stock Exchange",
}


def _response(payload, status=200):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    response.status_code = status
    return response


class TestEndpointSelection:
    @pytest.mark.parametrize(
        "scheme,value,path,param",
        [
            ("cik", "51143", "search-cik", "cik"),
            ("isin", "US4592001014", "search-isin", "isin"),
            ("cusip", "459200101", "search-cusip", "cusip"),
        ],
    )
    @mock.patch("duk.fmp_api.requests.get")
    def test_each_scheme_calls_its_own_endpoint(
        self, mock_get, scheme, value, path, param
    ):
        mock_get.return_value = _response([FMP_ROW])
        assert identifier_search_api(scheme, value, KEY) == [FMP_ROW]
        url, kwargs = mock_get.call_args[0][0], mock_get.call_args[1]
        assert url.endswith(f"/stable/{path}")
        assert kwargs["params"][param] == value

    def test_an_unknown_scheme_is_a_programming_error_not_a_request(self):
        with pytest.raises(ValueError):
            identifier_search_api("sedol", "0263494", KEY)

    @mock.patch("duk.fmp_api.requests.get")
    def test_a_transport_failure_raises_without_leaking_the_key(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError(f"boom apikey={KEY}")
        with pytest.raises(FMPAPIError) as excinfo:
            identifier_search_api("cik", "51143", KEY)
        assert KEY not in str(excinfo.value)

    @mock.patch("duk.fmp_api.requests.get")
    def test_a_vendor_error_message_is_surfaced(self, mock_get):
        mock_get.return_value = _response({"Error Message": "Endpoint not available"})
        with pytest.raises(FMPAPIError) as excinfo:
            identifier_search_api("cik", "51143", KEY)
        assert "Endpoint not available" in str(excinfo.value)


class TestCandidateShaping:
    @mock.patch("duk.fmp_api.requests.get")
    def test_rows_are_shaped_like_the_warehouse_candidates(self, mock_get):
        mock_get.return_value = _response([FMP_ROW])
        found = ds_live.resolve_identifier(
            api_key=KEY, identifier=ids.parse("cik:51143")
        )
        assert found == [
            {
                "security_id": None,
                "symbol": "IBM",
                "company_name": "International Business Machines Corporation",
                "exchange_code": "NYSE",
                "exchange_name": "New York Stock Exchange",
                # The live search does not report listing status, and inventing
                # one would have the candidate table assert what no source said.
                "is_actively_trading": None,
                "delisted_date": None,
            }
        ]

    @mock.patch("duk.fmp_api.requests.get")
    def test_rows_without_a_ticker_are_dropped(self, mock_get):
        # A ticker is the whole point of live resolution: the price endpoint takes
        # nothing else, so a row without one is not a candidate.
        mock_get.return_value = _response([{"companyName": "Private Co"}, FMP_ROW])
        found = ds_live.resolve_identifier(
            api_key=KEY, identifier=ids.parse("cik:51143")
        )
        assert [c["symbol"] for c in found] == ["IBM"]

    @mock.patch("duk.fmp_api.requests.get")
    def test_no_match_is_an_empty_list_not_an_error(self, mock_get):
        mock_get.return_value = _response([])
        assert (
            ds_live.resolve_identifier(api_key=KEY, identifier=ids.parse("cik:1")) == []
        )
