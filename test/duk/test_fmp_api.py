"""
Tests for FMP API module.
"""

import datetime as dt
from unittest import mock

import pytest
import requests

from duk.fmp_api import (
    FMPAPIError,
    actively_trading_list_api,
    adjusted_price_history_api,
    company_list_api,
    etf_symbol_list_api,
    industry_list_api,
    price_history_api,
    screener_api,
    sector_list_api,
    treasury_rates_api,
)


class TestPriceHistoryAPI:
    """Tests for price_history_api function."""

    def test_price_history_api_success(self):
        """Test successful API call with historical data."""
        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-03",
                    "open": 150.0,
                    "high": 155.0,
                    "low": 149.0,
                    "close": 154.0,
                    "volume": 1000000,
                },
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = price_history_api("AAPL", "test_api_key")

            assert len(result) == 2
            assert result[0]["date"] == "2023-01-03"
            assert result[0]["close"] == 154.0
            assert result[1]["date"] == "2023-01-02"

    def test_price_history_api_with_date_range(self):
        """Test API call with from and to dates."""
        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-06-01",
                    "open": 160.0,
                    "close": 162.0,
                }
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            from_date = dt.datetime.strptime("2023-06-01", "%Y-%m-%d")
            to_date = dt.datetime.strptime("2023-06-30", "%Y-%m-%d")

            result = price_history_api(
                "AAPL", "test_api_key", from_date=from_date, to_date=to_date
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["from"] == from_date.strftime("%Y-%m-%d")
            assert call_args[1]["params"]["to"] == to_date.strftime("%Y-%m-%d")
            assert len(result) == 1

    def test_price_history_api_empty_symbol(self):
        """Test that empty symbol raises ValueError."""
        with pytest.raises(ValueError, match="Symbol cannot be empty"):
            price_history_api("", "test_api_key")

    def test_price_history_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            price_history_api("AAPL", "")

    def test_price_history_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch price history"):
                price_history_api("AAPL", "test_api_key")

    def test_price_history_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch price history"):
                price_history_api("AAPL", "test_api_key")

    def test_price_history_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                price_history_api("AAPL", "test_api_key")

    def test_price_history_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                price_history_api("AAPL", "test_api_key")

    def test_price_history_api_list_response(self):
        """Test handling of API response that returns a list directly."""
        mock_response = [
            {"date": "2023-01-03", "close": 154.0},
            {"date": "2023-01-02", "close": 150.0},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = price_history_api("AAPL", "test_api_key")

            assert len(result) == 2
            assert result[0]["date"] == "2023-01-03"

    def test_price_history_api_empty_response(self):
        """Test handling of empty or unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = price_history_api("AAPL", "test_api_key")

            assert result == []

    def test_price_history_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch price history"):
                price_history_api("AAPL", "test_api_key")

    def test_price_history_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = {"symbol": "AAPL", "historical": []}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            price_history_api("AAPL", "test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "historical-price-eod/full" in call_args[0][0]
            assert "symbol=AAPL" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestAdjustedPriceHistoryAPI:
    """Tests for adjusted_price_history_api function."""

    def test_adjusted_price_history_api_success(self):
        """Test successful API call with dividend-adjusted historical data."""
        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-03",
                    "open": 150.0,
                    "high": 155.0,
                    "low": 149.0,
                    "close": 154.0,
                    "volume": 1000000,
                },
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = adjusted_price_history_api("AAPL", "test_api_key")

            assert len(result) == 2
            assert result[0]["date"] == "2023-01-03"
            assert result[0]["close"] == 154.0
            assert result[1]["date"] == "2023-01-02"

    def test_adjusted_price_history_api_with_date_range(self):
        """Test API call with from and to dates."""
        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-06-01",
                    "open": 160.0,
                    "close": 162.0,
                }
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            from_date = dt.datetime.strptime("2023-06-01", "%Y-%m-%d")
            to_date = dt.datetime.strptime("2023-06-30", "%Y-%m-%d")

            result = adjusted_price_history_api(
                "AAPL",
                "test_api_key",
                from_date=from_date,
                to_date=to_date,
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["from"] == from_date.strftime("%Y-%m-%d")
            assert call_args[1]["params"]["to"] == to_date.strftime("%Y-%m-%d")
            assert len(result) == 1

    def test_adjusted_price_history_api_empty_symbol(self):
        """Test that empty symbol raises ValueError."""
        with pytest.raises(ValueError, match="Symbol cannot be empty"):
            adjusted_price_history_api("", "test_api_key")

    def test_adjusted_price_history_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            adjusted_price_history_api("AAPL", "")

    def test_adjusted_price_history_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch dividend-adjusted price history"
            ):
                adjusted_price_history_api("AAPL", "test_api_key")

    def test_adjusted_price_history_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(
                FMPAPIError, match="Failed to fetch dividend-adjusted price history"
            ):
                adjusted_price_history_api("AAPL", "test_api_key")

    def test_adjusted_price_history_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                adjusted_price_history_api("AAPL", "test_api_key")

    def test_adjusted_price_history_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                adjusted_price_history_api("AAPL", "test_api_key")

    def test_adjusted_price_history_api_list_response(self):
        """Test handling of API response that returns a list directly."""
        mock_response = [
            {"date": "2023-01-03", "close": 154.0},
            {"date": "2023-01-02", "close": 150.0},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = adjusted_price_history_api("AAPL", "test_api_key")

            assert len(result) == 2
            assert result[0]["date"] == "2023-01-03"

    def test_adjusted_price_history_api_empty_response(self):
        """Test handling of empty or unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = adjusted_price_history_api("AAPL", "test_api_key")

            assert result == []

    def test_adjusted_price_history_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch dividend-adjusted price history"
            ):
                adjusted_price_history_api("AAPL", "test_api_key")

    def test_adjusted_price_history_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = {"symbol": "AAPL", "historical": []}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            adjusted_price_history_api("AAPL", "test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "dividend-adjusted" in call_args[0][0]
            assert "symbol=AAPL" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestTreasuryRatesAPI:
    """Tests for treasury_rates_api function."""

    def test_treasury_rates_api_success(self):
        """Test successful API call with treasury rates data."""
        mock_response = [
            {
                "date": "2023-01-03",
                "month1": 4.35,
                "month2": 4.42,
                "month3": 4.45,
                "year1": 4.68,
                "year5": 3.94,
                "year10": 3.79,
                "year30": 3.88,
            },
            {
                "date": "2023-01-02",
                "month1": 4.30,
                "month2": 4.40,
                "month3": 4.43,
                "year1": 4.65,
                "year5": 3.90,
                "year10": 3.75,
                "year30": 3.85,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = treasury_rates_api("test_api_key")

            assert len(result) == 2
            assert result[0]["date"] == "2023-01-03"
            assert result[0]["year10"] == 3.79
            assert result[1]["date"] == "2023-01-02"

    def test_treasury_rates_api_with_date_range(self):
        """Test API call with from and to dates."""
        from datetime import date

        mock_response = [
            {
                "date": "2023-06-01",
                "month1": 5.25,
                "year10": 3.70,
            }
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = treasury_rates_api(
                "test_api_key",
                start_date=date(2023, 6, 1),
                end_date=date(2023, 6, 30),
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["from"] == "2023-06-01"
            assert call_args[1]["params"]["to"] == "2023-06-30"
            assert len(result) == 1

    def test_treasury_rates_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            treasury_rates_api("")

    def test_treasury_rates_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch treasury rates"):
                treasury_rates_api("test_api_key")

    def test_treasury_rates_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch treasury rates"):
                treasury_rates_api("test_api_key")

    def test_treasury_rates_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                treasury_rates_api("test_api_key")

    def test_treasury_rates_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                treasury_rates_api("test_api_key")

    def test_treasury_rates_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = treasury_rates_api("test_api_key")

            assert result == []

    def test_treasury_rates_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = treasury_rates_api("test_api_key")

            assert result == []

    def test_treasury_rates_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch treasury rates"):
                treasury_rates_api("test_api_key")

    def test_treasury_rates_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            treasury_rates_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "treasury-rates" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestGetPriceHistory:
    """Tests for get_price_history function."""

    def test_get_price_history_basic(self):
        """Test basic usage without date range or limit."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
                {
                    "date": "2023-01-03",
                    "open": 150.0,
                    "high": 155.0,
                    "low": 149.0,
                    "close": 154.0,
                    "volume": 1000000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL")

            assert len(result) == 2
            assert result.index.name == "date"
            # Check that data is sorted ascending
            assert result.index[0].strftime("%Y-%m-%d") == "2023-01-02"
            assert result.index[1].strftime("%Y-%m-%d") == "2023-01-03"
            assert result.iloc[0]["close"] == 150.0
            assert result.iloc[1]["close"] == 154.0

    def test_get_price_history_with_date_range(self):
        """Test with start_date and end_date."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {"date": "2023-06-01", "close": 160.0},
                {"date": "2023-06-02", "close": 162.0},
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2023-06-01",
                end_date="2023-06-30",
            )

            assert len(result) == 2
            # Verify dates were passed to API
            call_args = mock_get.call_args
            assert "from" in call_args[1]["params"]
            assert "to" in call_args[1]["params"]

    def test_get_price_history_with_start_date_and_limit(self):
        """Test with start_date and limit - should keep first N records."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {"date": "2023-01-01", "close": 150.0},
                {"date": "2023-01-02", "close": 151.0},
                {"date": "2023-01-03", "close": 152.0},
                {"date": "2023-01-04", "close": 153.0},
                {"date": "2023-01-05", "close": 154.0},
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key", "AAPL", start_date="2023-01-01", limit=3
            )

            # Should keep first 3 records
            assert len(result) == 3
            assert result.index[0].strftime("%Y-%m-%d") == "2023-01-01"
            assert result.index[-1].strftime("%Y-%m-%d") == "2023-01-03"

    def test_get_price_history_with_end_date_and_limit(self):
        """Test with end_date and limit - should keep last N records."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {"date": "2023-01-01", "close": 150.0},
                {"date": "2023-01-02", "close": 151.0},
                {"date": "2023-01-03", "close": 152.0},
                {"date": "2023-01-04", "close": 153.0},
                {"date": "2023-01-05", "close": 154.0},
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key", "AAPL", end_date="2023-01-05", limit=3
            )

            # Should keep last 3 records
            assert len(result) == 3
            assert result.index[0].strftime("%Y-%m-%d") == "2023-01-03"
            assert result.index[-1].strftime("%Y-%m-%d") == "2023-01-05"

    def test_get_price_history_weekly_resampling(self):
        """Test resampling to weekly frequency."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 100.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 103.0,
                    "volume": 1000,
                },
                {
                    "date": "2023-01-03",
                    "open": 103.0,
                    "high": 107.0,
                    "low": 102.0,
                    "close": 106.0,
                    "volume": 1100,
                },
                {
                    "date": "2023-01-09",
                    "open": 106.0,
                    "high": 110.0,
                    "low": 105.0,
                    "close": 109.0,
                    "volume": 1200,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2023-01-01",
                end_date="2023-01-31",
                frequency="week",
            )

            # Should have resampled data
            assert len(result) >= 1
            assert "close" in result.columns

    def test_get_price_history_monthly_resampling(self):
        """Test resampling to monthly frequency."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 100.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 103.0,
                    "volume": 1000,
                },
                {
                    "date": "2023-01-15",
                    "open": 103.0,
                    "high": 107.0,
                    "low": 102.0,
                    "close": 106.0,
                    "volume": 1100,
                },
                {
                    "date": "2023-02-05",
                    "open": 106.0,
                    "high": 110.0,
                    "low": 105.0,
                    "close": 109.0,
                    "volume": 1200,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2023-01-01",
                end_date="2023-02-28",
                frequency="month",
            )

            # Should have resampled data with monthly frequency
            assert len(result) >= 1

    def test_get_price_history_empty_response(self):
        """Test handling of empty API response."""
        from duk.fmp_api import get_price_history

        mock_response = {"symbol": "AAPL", "historical": []}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL")

            assert len(result) == 0
            assert isinstance(result, __import__("pandas").DataFrame)

    def test_get_price_history_invalid_date_format(self):
        """Test handling of invalid date format."""
        from duk.fmp_api import get_price_history

        with pytest.raises(ValueError):
            get_price_history("test_api_key", "AAPL", start_date="invalid-date")

    def test_get_price_history_quarterly_resampling(self):
        """Test resampling to quarterly frequency."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {"date": "2023-01-02", "open": 100.0, "close": 103.0, "volume": 1000},
                {"date": "2023-02-15", "open": 103.0, "close": 106.0, "volume": 1100},
                {"date": "2023-04-05", "open": 106.0, "close": 109.0, "volume": 1200},
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2023-01-01",
                end_date="2023-06-30",
                frequency="quarter",
            )

            assert len(result) >= 1

    def test_get_price_history_annual_resampling(self):
        """Test resampling to annual frequency."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {"date": "2022-01-02", "open": 100.0, "close": 103.0, "volume": 1000},
                {"date": "2022-06-15", "open": 103.0, "close": 106.0, "volume": 1100},
                {"date": "2023-01-05", "open": 106.0, "close": 109.0, "volume": 1200},
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2022-01-01",
                end_date="2023-12-31",
                frequency="annual",
            )

            assert len(result) >= 1

    def test_get_price_history_with_fields_parameter(self):
        """Test filtering columns with fields parameter."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
                {
                    "date": "2023-01-03",
                    "open": 150.0,
                    "high": 155.0,
                    "low": 149.0,
                    "close": 154.0,
                    "volume": 1000000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Test with only close field
            result = get_price_history("test_api_key", "AAPL", fields=["close"])

            assert len(result) == 2
            assert list(result.columns) == ["close"]
            assert "open" not in result.columns
            assert "high" not in result.columns

    def test_get_price_history_with_multiple_fields(self):
        """Test filtering with multiple fields."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Test with OHLC fields only (no volume)
            result = get_price_history(
                "test_api_key",
                "AAPL",
                fields=["open", "high", "low", "close"],
            )

            assert len(result) == 1
            assert set(result.columns) == {"open", "high", "low", "close"}
            assert "volume" not in result.columns

    def test_get_price_history_with_invalid_fields(self):
        """Test that invalid fields are filtered out."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Invalid fields should be filtered out, keeping only valid ones
            result = get_price_history(
                "test_api_key",
                "AAPL",
                fields=["close", "invalid_field"],
            )

            # Should only have 'close' column (invalid_field filtered out)
            assert len(result) == 1
            assert list(result.columns) == ["close"]

    def test_get_price_history_with_all_valid_fields(self):
        """Test with all valid fields specified."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Test with all valid fields
            result = get_price_history(
                "test_api_key",
                "AAPL",
                fields=["open", "high", "low", "close", "volume"],
            )

            assert len(result) == 1
            assert set(result.columns) == {"open", "high", "low", "close", "volume"}

    def test_get_price_history_fields_none_returns_all(self):
        """Test that fields=None returns all columns."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Test with fields=None (default)
            result = get_price_history("test_api_key", "AAPL")

            assert len(result) == 1
            # Should have all columns
            assert "open" in result.columns
            assert "high" in result.columns
            assert "low" in result.columns
            assert "close" in result.columns
            assert "volume" in result.columns

    def test_get_price_history_fields_not_in_response(self):
        """Test that requesting fields not in response returns empty DataFrame."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "close": 150.0,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Request fields that don't exist in response
            result = get_price_history(
                "test_api_key", "AAPL", fields=["open", "high", "low"]
            )

            # Should return DataFrame with index but no columns
            assert len(result) == 1
            assert len(result.columns) == 0
            assert result.index.name == "date"

    def test_get_price_history_limit_applied_after_resampling(self):
        """Test that limit is applied after resampling."""
        from duk.fmp_api import get_price_history

        # Create 14 days of daily data (2 full weeks)
        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": f"2023-01-{i:02d}",
                    "open": 100.0 + i,
                    "high": 105.0 + i,
                    "low": 99.0 + i,
                    "close": 103.0 + i,
                    "volume": 1000 + i * 10,
                }
                for i in range(1, 15)
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            # Request weekly data with limit=2 and start_date (no end_date)
            # Should fetch data from start_date, resample to weekly, then limit to 2
            result = get_price_history(
                "test_api_key",
                "AAPL",
                start_date="2023-01-01",
                frequency="week",
                limit=2,
            )

            # Should have exactly 2 weekly records (limit applied after resampling)
            assert len(result) == 2
            # Verify it's resampled (should have aggregated data)
            assert "close" in result.columns
            assert "volume" in result.columns

    def test_get_price_history_with_adjusted_true(self):
        """Test that adjusted=True uses adjusted_price_history_api."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "adjOpen": 149.0,
                    "adjHigh": 151.0,
                    "adjLow": 148.0,
                    "adjClose": 150.0,
                    "adjVolume": 900000,
                },
                {
                    "date": "2023-01-03",
                    "adjOpen": 150.0,
                    "adjHigh": 155.0,
                    "adjLow": 149.0,
                    "adjClose": 154.0,
                    "adjVolume": 1000000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL", adjusted=True)

            # Verify the correct API endpoint was called (dividend-adjusted)
            call_args = mock_get.call_args
            assert "dividend-adjusted" in call_args[0][0]

            # Verify column names have "adj" prefix stripped
            assert len(result) == 2
            assert "open" in result.columns
            assert "high" in result.columns
            assert "low" in result.columns
            assert "close" in result.columns
            assert "volume" in result.columns
            # Verify no "adj" prefix remains
            assert "adjopen" not in result.columns
            assert "adjclose" not in result.columns

    def test_get_price_history_with_adjusted_false(self):
        """Test that adjusted=False uses regular price_history_api."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "open": 149.0,
                    "high": 151.0,
                    "low": 148.0,
                    "close": 150.0,
                    "volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL", adjusted=False)

            # Verify the correct API endpoint was called (regular price history)
            call_args = mock_get.call_args
            assert "historical-price-eod/full" in call_args[0][0]
            assert "dividend-adjusted" not in call_args[0][0]

            # Verify regular column names
            assert len(result) == 1
            assert "open" in result.columns
            assert "close" in result.columns

    def test_get_price_history_adjusted_strips_whitespace(self):
        """Test that adjusted=True strips 'adj' prefix and any whitespace."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "adj Open": 149.0,
                    "adj High": 151.0,
                    "adj Low": 148.0,
                    "adj Close": 150.0,
                    "adj Volume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL", adjusted=True)

            # Verify column names have "adj" prefix and whitespace stripped
            assert len(result) == 1
            # Check for properly cleaned column names (no "adj " prefix or extra spaces)
            assert "open" in result.columns
            assert "high" in result.columns
            assert "low" in result.columns
            assert "close" in result.columns
            assert "volume" in result.columns
            # Verify no whitespace or prefix remains
            for col in result.columns:
                assert not col.startswith("adj")
                assert col == col.strip()  # No leading or trailing whitespace

    def test_get_price_history_adjusted_only_strips_prefix(self):
        """Test that stripping 'adj' only removes prefix, not from other parts."""
        from duk.fmp_api import get_price_history

        mock_response = {
            "symbol": "AAPL",
            "historical": [
                {
                    "date": "2023-01-02",
                    "adjClose": 150.0,
                    "adjOpen": 149.0,
                    "adjHigh": 151.0,
                    "adjLow": 148.0,
                    "adjVolume": 900000,
                },
            ],
        }

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_price_history("test_api_key", "AAPL", adjusted=True)

            # Verify the prefix was stripped correctly using slicing
            assert len(result) == 1
            assert "close" in result.columns
            assert "open" in result.columns
            assert "high" in result.columns
            assert "low" in result.columns
            assert "volume" in result.columns
            # Verify values are correct after column rename
            assert result.iloc[0]["close"] == 150.0
            assert result.iloc[0]["open"] == 149.0


class TestGetYieldCurve:
    """Tests for get_yield_curve function."""

    def test_get_yield_curve_multiple_dates(self):
        """Test yield curve with multiple dates returns date-indexed DataFrame."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-01-03",
                "month1": 4.35,
                "month3": 4.45,
                "year1": 4.68,
                "year10": 3.79,
            },
            {
                "date": "2023-01-02",
                "month1": 4.30,
                "month3": 4.40,
                "year1": 4.65,
                "year10": 3.75,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-01-02",
                end_date="2023-01-03",
            )

            assert len(result) == 2
            assert result.index.name == "date"
            assert "month1" in result.columns
            assert "year10" in result.columns
            # Check date order is ascending
            assert result.index[0] < result.index[1]

    def test_get_yield_curve_single_date_par_rate(self):
        """Test yield curve with single date returns tenor-indexed DataFrame."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-06-01",
                "month1": 4.35,
                "month6": 4.50,
                "year1": 4.68,
                "year10": 3.79,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-06-01",
                end_date="2023-06-01",
            )

            # Single date should return tenor-indexed DataFrame
            assert result.index.name == "tenor"
            assert "years" in result.columns
            assert "date" in result.columns
            assert "par_rate" in result.columns
            assert "zero_rate" not in result.columns

            # Check tenor values
            assert "month1" in result.index
            assert "year10" in result.index

            # Check years values
            assert result.loc["month1", "years"] == 1 / 12
            assert result.loc["year10", "years"] == 10.0

            # Check par_rate values
            assert result.loc["month1", "par_rate"] == 4.35
            assert result.loc["year10", "par_rate"] == 3.79

    def test_get_yield_curve_single_date_zero_rate(self):
        """Test yield curve with single date and zero_rates=True."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-06-01",
                "month6": 4.50,
                "year1": 4.60,
                "year2": 4.20,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-06-01",
                end_date="2023-06-01",
                zero_rates=True,
            )

            # Single date with zero_rates should return tenor-indexed DataFrame
            assert result.index.name == "tenor"
            assert "zero_rate" in result.columns
            assert "par_rate" not in result.columns

    def test_get_yield_curve_with_limit(self):
        """Test yield curve with limit parameter."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-01-05", "year10": 3.80},
            {"date": "2023-01-04", "year10": 3.78},
            {"date": "2023-01-03", "year10": 3.76},
            {"date": "2023-01-02", "year10": 3.74},
            {"date": "2023-01-01", "year10": 3.72},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                end_date="2023-01-05",
                limit=3,
            )

            # Should keep last 3 records
            assert len(result) == 3

    def test_get_yield_curve_with_tenors_filter(self):
        """Test yield curve with tenors filter."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-06-01",
                "month1": 4.35,
                "month6": 4.50,
                "year1": 4.68,
                "year5": 4.00,
                "year10": 3.79,
                "year30": 3.88,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-06-01",
                end_date="2023-06-01",
                tenors=("year1", "year10"),
            )

            # Should only include year1 through year10
            assert "year1" in result.index
            assert "year5" in result.index
            assert "year10" in result.index
            # Should exclude month1, month6, year30
            assert "month1" not in result.index
            assert "month6" not in result.index
            assert "year30" not in result.index

    def test_get_yield_curve_with_interval(self):
        """Test yield curve with interval interpolation."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-06-01",
                "year1": 4.68,
                "year5": 4.00,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-06-01",
                end_date="2023-06-01",
                interval="annual",
            )

            # Should have interpolated points
            assert "year2" in result.index
            assert "year3" in result.index
            assert "year4" in result.index

    def test_get_yield_curve_empty_response(self):
        """Test handling of empty API response."""
        from duk.fmp_api import get_yield_curve

        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve("test_api_key")

            assert len(result) == 0
            assert isinstance(result, __import__("pandas").DataFrame)

    def test_get_yield_curve_invalid_date_format(self):
        """Test handling of invalid date format."""
        from duk.fmp_api import get_yield_curve

        with pytest.raises(ValueError):
            get_yield_curve("test_api_key", start_date="invalid-date")

    def test_get_yield_curve_invalid_tenors(self):
        """Test handling of invalid tenors parameter."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-06-01", "year1": 4.68},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(ValueError, match="Invalid tenor in filter"):
                get_yield_curve(
                    "test_api_key",
                    start_date="2023-06-01",
                    end_date="2023-06-01",
                    tenors=("invalid", "year10"),
                )

    def test_get_yield_curve_tenors_tuple_length(self):
        """Test that tenors must be a tuple of length 2."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-06-01", "year1": 4.68},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(ValueError, match="must be a tuple"):
                get_yield_curve(
                    "test_api_key",
                    start_date="2023-06-01",
                    end_date="2023-06-01",
                    tenors=("year1",),
                )

    def test_get_yield_curve_date_column_calculation(self):
        """Test that date column is correctly calculated for single date."""
        from datetime import date, timedelta

        from duk.fmp_api import get_yield_curve

        mock_response = [
            {
                "date": "2023-06-01",
                "year1": 4.68,
                "year5": 4.00,
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-06-01",
                end_date="2023-06-01",
            )

            record_date = date(2023, 6, 1)
            # year1 should be ~1 year in the future
            expected_date_year1 = record_date + timedelta(days=int(1 * 365))
            assert result.loc["year1", "date"] == expected_date_year1

            # year5 should be ~5 years in the future
            expected_date_year5 = record_date + timedelta(days=int(5 * 365))
            assert result.loc["year5", "date"] == expected_date_year5

    def test_get_yield_curve_multiple_dates_preserves_all_tenors(self):
        """Test that multiple dates result preserves all tenor columns."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-01-02", "month1": 4.30, "year1": 4.65, "year10": 3.75},
            {"date": "2023-01-03", "month1": 4.35, "year1": 4.68, "year10": 3.79},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-01-02",
                end_date="2023-01-03",
            )

            assert "month1" in result.columns
            assert "year1" in result.columns
            assert "year10" in result.columns
            assert len(result) == 2

    def test_get_yield_curve_with_start_date_and_limit(self):
        """Test yield curve with start_date and limit keeps first N records."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-01-01", "year10": 3.72},
            {"date": "2023-01-02", "year10": 3.74},
            {"date": "2023-01-03", "year10": 3.76},
            {"date": "2023-01-04", "year10": 3.78},
            {"date": "2023-01-05", "year10": 3.80},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-01-01",
                limit=3,
            )

            # Should keep first 3 records
            assert len(result) == 3

    def test_get_yield_curve_zero_rates_applies_bootstrap(self):
        """Test that zero_rates=True applies bootstrap transformation."""
        from duk.fmp_api import get_yield_curve

        mock_response = [
            {"date": "2023-01-02", "month6": 4.50, "year1": 4.60},
            {"date": "2023-01-03", "month6": 4.55, "year1": 4.65},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = get_yield_curve(
                "test_api_key",
                start_date="2023-01-02",
                end_date="2023-01-03",
                zero_rates=True,
            )

            # Should have multiple dates (2 records)
            assert len(result) == 2
            # Columns should be tenors
            assert "month6" in result.columns
            assert "year1" in result.columns


class TestCompanyListAPI:
    """Tests for company_list_api function."""

    def test_company_list_api_success(self):
        """Test successful API call with company list data."""
        mock_response = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 150.0,
                "exchange": "NASDAQ",
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft Corporation",
                "price": 300.0,
                "exchange": "NASDAQ",
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = company_list_api("test_api_key")

            assert len(result) == 2
            assert result[0]["symbol"] == "AAPL"
            assert result[1]["symbol"] == "MSFT"

    def test_company_list_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            company_list_api("")

    def test_company_list_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch company list"):
                company_list_api("test_api_key")

    def test_company_list_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch company list"):
                company_list_api("test_api_key")

    def test_company_list_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                company_list_api("test_api_key")

    def test_company_list_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                company_list_api("test_api_key")

    def test_company_list_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = company_list_api("test_api_key")

            assert result == []

    def test_company_list_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = company_list_api("test_api_key")

            assert result == []

    def test_company_list_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch company list"):
                company_list_api("test_api_key")

    def test_company_list_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            company_list_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "stock-list" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestEtfSymbolListAPI:
    """Tests for etf_symbol_list_api function."""

    def test_etf_symbol_list_api_success(self):
        """Test successful API call with ETF list data."""
        mock_response = [
            {
                "symbol": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "price": 450.0,
                "exchange": "NYSE Arca",
            },
            {
                "symbol": "QQQ",
                "name": "Invesco QQQ Trust",
                "price": 380.0,
                "exchange": "NASDAQ",
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = etf_symbol_list_api("test_api_key")

            assert len(result) == 2
            assert result[0]["symbol"] == "SPY"
            assert result[1]["symbol"] == "QQQ"

    def test_etf_symbol_list_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            etf_symbol_list_api("")

    def test_etf_symbol_list_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch ETF symbol list"):
                etf_symbol_list_api("test_api_key")

    def test_etf_symbol_list_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch ETF symbol list"):
                etf_symbol_list_api("test_api_key")

    def test_etf_symbol_list_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                etf_symbol_list_api("test_api_key")

    def test_etf_symbol_list_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                etf_symbol_list_api("test_api_key")

    def test_etf_symbol_list_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = etf_symbol_list_api("test_api_key")

            assert result == []

    def test_etf_symbol_list_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = etf_symbol_list_api("test_api_key")

            assert result == []

    def test_etf_symbol_list_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch ETF symbol list"):
                etf_symbol_list_api("test_api_key")

    def test_etf_symbol_list_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            etf_symbol_list_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "etf-list" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestSectorListAPI:
    """Tests for sector_list_api function."""

    def test_sector_list_api_success(self):
        """Test successful API call with sector list data."""
        mock_response = [
            {"sector": "Technology"},
            {"sector": "Healthcare"},
            {"sector": "Financial Services"},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = sector_list_api("test_api_key")

            assert len(result) == 3
            assert result[0]["sector"] == "Technology"
            assert result[1]["sector"] == "Healthcare"

    def test_sector_list_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            sector_list_api("")

    def test_sector_list_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch sector list"):
                sector_list_api("test_api_key")

    def test_sector_list_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch sector list"):
                sector_list_api("test_api_key")

    def test_sector_list_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                sector_list_api("test_api_key")

    def test_sector_list_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                sector_list_api("test_api_key")

    def test_sector_list_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = sector_list_api("test_api_key")

            assert result == []

    def test_sector_list_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = sector_list_api("test_api_key")

            assert result == []

    def test_sector_list_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch sector list"):
                sector_list_api("test_api_key")

    def test_sector_list_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            sector_list_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "available-sectors" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestIndustryListAPI:
    """Tests for industry_list_api function."""

    def test_industry_list_api_success(self):
        """Test successful API call with industry list data."""
        mock_response = [
            {"industry": "Software - Application"},
            {"industry": "Semiconductors"},
            {"industry": "Biotechnology"},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = industry_list_api("test_api_key")

            assert len(result) == 3
            assert result[0]["industry"] == "Software - Application"
            assert result[1]["industry"] == "Semiconductors"

    def test_industry_list_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            industry_list_api("")

    def test_industry_list_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(FMPAPIError, match="Failed to fetch industry list"):
                industry_list_api("test_api_key")

    def test_industry_list_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(FMPAPIError, match="Failed to fetch industry list"):
                industry_list_api("test_api_key")

    def test_industry_list_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                industry_list_api("test_api_key")

    def test_industry_list_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                industry_list_api("test_api_key")

    def test_industry_list_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = industry_list_api("test_api_key")

            assert result == []

    def test_industry_list_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = industry_list_api("test_api_key")

            assert result == []

    def test_industry_list_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(FMPAPIError, match="Failed to fetch industry list"):
                industry_list_api("test_api_key")

    def test_industry_list_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            industry_list_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "available-industries" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestActivelyTradingListAPI:
    """Tests for actively_trading_list_api function."""

    def test_actively_trading_list_api_success(self):
        """Test successful API call with actively trading list data."""
        mock_response = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 150.0,
                "exchange": "NASDAQ",
            },
            {
                "symbol": "TSLA",
                "name": "Tesla, Inc.",
                "price": 250.0,
                "exchange": "NASDAQ",
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = actively_trading_list_api("test_api_key")

            assert len(result) == 2
            assert result[0]["symbol"] == "AAPL"
            assert result[1]["symbol"] == "TSLA"

    def test_actively_trading_list_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            actively_trading_list_api("")

    def test_actively_trading_list_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch actively trading list"
            ):
                actively_trading_list_api("test_api_key")

    def test_actively_trading_list_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(
                FMPAPIError, match="Failed to fetch actively trading list"
            ):
                actively_trading_list_api("test_api_key")

    def test_actively_trading_list_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                actively_trading_list_api("test_api_key")

    def test_actively_trading_list_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                actively_trading_list_api("test_api_key")

    def test_actively_trading_list_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = actively_trading_list_api("test_api_key")

            assert result == []

    def test_actively_trading_list_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = actively_trading_list_api("test_api_key")

            assert result == []

    def test_actively_trading_list_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch actively trading list"
            ):
                actively_trading_list_api("test_api_key")

    def test_actively_trading_list_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            actively_trading_list_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "actively-trading-list" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30


class TestScreenerAPI:
    """Tests for screener_api function."""

    def test_screener_api_success(self):
        """Test successful API call with screener data."""
        mock_response = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 150.0,
                "exchange": "NASDAQ",
                "marketCap": 2500000000000,
                "sector": "Technology",
                "industry": "Consumer Electronics",
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft Corporation",
                "price": 300.0,
                "exchange": "NASDAQ",
                "marketCap": 2300000000000,
                "sector": "Technology",
                "industry": "Software - Infrastructure",
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key")

            assert len(result) == 2
            assert result[0]["symbol"] == "AAPL"
            assert result[1]["symbol"] == "MSFT"

    def test_screener_api_with_sector_filter(self):
        """Test API call with sector filter."""
        mock_response = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "sector": "Technology",
            }
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", sector="Technology")

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["sector"] == "Technology"
            assert len(result) == 1

    def test_screener_api_with_market_cap_filters(self):
        """Test API call with market cap filters."""
        mock_response = [
            {"symbol": "AAPL", "marketCap": 2500000000000},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api(
                "test_api_key",
                marketCapMoreThan=1000000000,
                marketCapLowerThan=5000000000000,
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["marketCapMoreThan"] == 1000000000
            assert call_args[1]["params"]["marketCapLowerThan"] == 5000000000000
            assert len(result) == 1

    def test_screener_api_with_price_filters(self):
        """Test API call with price filters."""
        mock_response = [
            {"symbol": "AAPL", "price": 150.0},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", priceMoreThan=10, priceLowerThan=200)

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["priceMoreThan"] == 10
            assert call_args[1]["params"]["priceLowerThan"] == 200
            assert len(result) == 1

    def test_screener_api_with_beta_filters(self):
        """Test API call with beta filters."""
        mock_response = [
            {"symbol": "AAPL", "beta": 1.2},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", betaMoreThan=0.5, betaLowerThan=1.5)

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["betaMoreThan"] == 0.5
            assert call_args[1]["params"]["betaLowerThan"] == 1.5
            assert len(result) == 1

    def test_screener_api_with_dividend_filters(self):
        """Test API call with dividend filters."""
        mock_response = [
            {"symbol": "AAPL", "dividend": 0.92},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api(
                "test_api_key", dividendMoreThan=0.5, dividendLowerThan=2
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["dividendMoreThan"] == 0.5
            assert call_args[1]["params"]["dividendLowerThan"] == 2
            assert len(result) == 1

    def test_screener_api_with_volume_filters(self):
        """Test API call with volume filters."""
        mock_response = [
            {"symbol": "AAPL", "volume": 50000000},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api(
                "test_api_key", volumeMoreThan=1000, volumeLowerThan=1000000000
            )

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["volumeMoreThan"] == 1000
            assert call_args[1]["params"]["volumeLowerThan"] == 1000000000
            assert len(result) == 1

    def test_screener_api_with_exchange_filter(self):
        """Test API call with exchange filter."""
        mock_response = [
            {"symbol": "AAPL", "exchange": "NASDAQ"},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", exchange="NASDAQ")

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["exchange"] == "NASDAQ"
            assert len(result) == 1

    def test_screener_api_with_country_filter(self):
        """Test API call with country filter."""
        mock_response = [
            {"symbol": "AAPL", "country": "US"},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", country="US")

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["country"] == "US"
            assert len(result) == 1

    def test_screener_api_with_industry_filter(self):
        """Test API call with industry filter."""
        mock_response = [
            {"symbol": "AAPL", "industry": "Consumer Electronics"},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", industry="Consumer Electronics")

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["industry"] == "Consumer Electronics"
            assert len(result) == 1

    def test_screener_api_with_boolean_filters(self):
        """Test API call with boolean filters."""
        mock_response = [
            {"symbol": "AAPL", "isEtf": False, "isFund": False},
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api(
                "test_api_key",
                isEtf=False,
                isFund=False,
                isActivelyTrading=True,
            )

            # Verify the correct parameters were passed (as lowercase strings)
            call_args = mock_get.call_args
            assert call_args[1]["params"]["isEtf"] == "false"
            assert call_args[1]["params"]["isFund"] == "false"
            assert call_args[1]["params"]["isActivelyTrading"] == "true"
            assert len(result) == 1

    def test_screener_api_with_limit(self):
        """Test API call with limit parameter."""
        mock_response = [{"symbol": f"SYM{i}", "price": 100.0 + i} for i in range(100)]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key", limit=100)

            # Verify the correct parameters were passed
            call_args = mock_get.call_args
            assert call_args[1]["params"]["limit"] == 100
            assert len(result) == 100

    def test_screener_api_with_all_filters(self):
        """Test API call with multiple filters combined."""
        mock_response = [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "price": 150.0,
                "marketCap": 2500000000000,
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "beta": 1.2,
                "dividend": 0.92,
                "volume": 50000000,
                "exchange": "NASDAQ",
                "country": "US",
            },
        ]

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api(
                "test_api_key",
                marketCapMoreThan=1000000000,
                marketCapLowerThan=5000000000000,
                sector="Technology",
                industry="Consumer Electronics",
                betaMoreThan=0.5,
                betaLowerThan=1.5,
                priceMoreThan=10,
                priceLowerThan=200,
                dividendMoreThan=0.5,
                dividendLowerThan=2,
                volumeMoreThan=1000,
                volumeLowerThan=1000000000,
                exchange="NASDAQ",
                country="US",
                isEtf=False,
                isFund=False,
                isActivelyTrading=True,
                limit=100,
            )

            # Verify all parameters were passed
            call_args = mock_get.call_args
            params = call_args[1]["params"]
            assert params["marketCapMoreThan"] == 1000000000
            assert params["marketCapLowerThan"] == 5000000000000
            assert params["sector"] == "Technology"
            assert params["industry"] == "Consumer Electronics"
            assert params["betaMoreThan"] == 0.5
            assert params["betaLowerThan"] == 1.5
            assert params["priceMoreThan"] == 10
            assert params["priceLowerThan"] == 200
            assert params["dividendMoreThan"] == 0.5
            assert params["dividendLowerThan"] == 2
            assert params["volumeMoreThan"] == 1000
            assert params["volumeLowerThan"] == 1000000000
            assert params["exchange"] == "NASDAQ"
            assert params["country"] == "US"
            assert params["isEtf"] == "false"
            assert params["isFund"] == "false"
            assert params["isActivelyTrading"] == "true"
            assert params["limit"] == 100
            assert len(result) == 1

    def test_screener_api_empty_api_key(self):
        """Test that empty API key raises ValueError."""
        with pytest.raises(ValueError, match="API key cannot be empty"):
            screener_api("")

    def test_screener_api_network_error(self):
        """Test handling of network errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Network error")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch stock screener results"
            ):
                screener_api("test_api_key")

    def test_screener_api_http_error(self):
        """Test handling of HTTP errors."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = (
                requests.exceptions.HTTPError("404 Not Found")
            )

            with pytest.raises(
                FMPAPIError, match="Failed to fetch stock screener results"
            ):
                screener_api("test_api_key")

    def test_screener_api_invalid_json(self):
        """Test handling of invalid JSON response."""
        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

            with pytest.raises(FMPAPIError, match="Failed to parse JSON response"):
                screener_api("test_api_key")

    def test_screener_api_error_message_in_response(self):
        """Test handling of error message in API response."""
        mock_response = {"Error Message": "Invalid API key"}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            with pytest.raises(FMPAPIError, match="FMP API error"):
                screener_api("test_api_key")

    def test_screener_api_empty_response(self):
        """Test handling of empty list response."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key")

            assert result == []

    def test_screener_api_unexpected_response(self):
        """Test handling of unexpected response format."""
        mock_response = {}

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            result = screener_api("test_api_key")

            assert result == []

    def test_screener_api_timeout(self):
        """Test handling of request timeout."""
        with mock.patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timeout")

            with pytest.raises(
                FMPAPIError, match="Failed to fetch stock screener results"
            ):
                screener_api("test_api_key")

    def test_screener_api_url_construction(self):
        """Test that the correct URL and parameters are used."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            screener_api("test_api_key")

            # Verify the correct URL was called
            call_args = mock_get.call_args
            assert "company-screener" in call_args[0][0]
            assert call_args[1]["params"]["apikey"] == "test_api_key"
            assert call_args[1]["timeout"] == 30

    def test_screener_api_none_parameters_not_included(self):
        """Test that None parameters are not included in the request."""
        mock_response = []

        with mock.patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = mock.Mock()

            screener_api("test_api_key", sector=None, industry=None, limit=None)

            # Verify that None parameters are not in the request params
            call_args = mock_get.call_args
            params = call_args[1]["params"]
            assert "sector" not in params
            assert "industry" not in params
            assert "limit" not in params
            # Only apikey should be present
            assert "apikey" in params
