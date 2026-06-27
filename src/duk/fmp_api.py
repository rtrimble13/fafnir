"""
FMP (Financial Modeling Prep) API integration.

This module provides functions for interacting with the FMP API
to retrieve financial and market data.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from duk.date_utils import get_api_date_range

logger = logging.getLogger(__name__)


class FMPAPIError(Exception):
    """Exception raised for FMP API errors."""

    pass


def price_history_api(
    symbol: str,
    api_key: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Request security price history from FMP API.

    Retrieves end-of-day (EOD) historical price data for a given security
    symbol from the Financial Modeling Prep API.

    Args:
        symbol: The ticker symbol for the security (e.g., "AAPL", "MSFT")
        api_key: FMP API key for authentication
        from_date: Optional start date for historical data (format: YYYY-MM-DD)
        to_date: Optional end date for historical data (format: YYYY-MM-DD)

    Returns:
        List of dictionaries containing historical price data. Each dictionary
        contains fields like date, open, high, low, close, volume, etc.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> history = price_history_api("AAPL", "your_api_key")
        >>> history = price_history_api("AAPL", "your_api_key",
        ...                            from_date="2023-01-01",
        ...                            to_date="2023-12-31")
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/historical-price-eod/full?symbol={symbol}"

    # Build query parameters
    params = {"apikey": api_key}
    if from_date:
        params["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        params["to"] = to_date.strftime("%Y-%m-%d")

    logger.debug(f"Requesting price history for {symbol} from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch price history for {symbol}: {e}")
        raise FMPAPIError(f"Failed to fetch price history for {symbol}: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract historical data from response
    if isinstance(data, dict) and "historical" in data:
        historical_data = data["historical"]
        logger.info(f"Retrieved {len(historical_data)} records for {symbol}")
        return historical_data
    elif isinstance(data, list):
        # Some endpoints return a list directly
        logger.info(f"Retrieved {len(data)} records for {symbol}")
        return data
    else:
        logger.warning(f"Unexpected response format for {symbol}")
        return []


def adjusted_price_history_api(
    symbol: str,
    api_key: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Request dividend-adjusted security price history from FMP API.

    Retrieves end-of-day (EOD) dividend-adjusted historical price data for a
    given security symbol from the Financial Modeling Prep API.

    Args:
        symbol: The ticker symbol for the security (e.g., "AAPL", "MSFT")
        api_key: FMP API key for authentication
        from_date: Optional start date for historical data (format: YYYY-MM-DD)
        to_date: Optional end date for historical data (format: YYYY-MM-DD)

    Returns:
        List of dictionaries containing dividend-adjusted historical price data.
        Each dictionary contains fields like date, open, high, low, close, volume, etc.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> history = adjusted_price_history_api("AAPL", "your_api_key")
        >>> history = adjusted_price_history_api("AAPL", "your_api_key",
        ...                                      from_date="2023-01-01",
        ...                                      to_date="2023-12-31")
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/historical-price-eod/dividend-adjusted?symbol={symbol}"

    # Build query parameters
    params = {"apikey": api_key}
    if from_date:
        params["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        params["to"] = to_date.strftime("%Y-%m-%d")

    logger.debug(
        f"Requesting dividend-adjusted price history for {symbol} from FMP API"
    )

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to fetch dividend-adjusted price history for {symbol}: {e}"
        )
        raise FMPAPIError(
            f"Failed to fetch dividend-adjusted price history for {symbol}: {e}"
        ) from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract historical data from response
    if isinstance(data, dict) and "historical" in data:
        historical_data = data["historical"]
        logger.info(
            f"Retrieved {len(historical_data)} dividend-adjusted records for {symbol}"
        )
        return historical_data
    elif isinstance(data, list):
        # Some endpoints return a list directly
        logger.info(f"Retrieved {len(data)} dividend-adjusted records for {symbol}")
        return data
    else:
        logger.warning(f"Unexpected response format for {symbol}")
        return []


def treasury_rates_api(
    api_key: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Request treasury rates from FMP API.

    Retrieves historical US Treasury rates data from the Financial Modeling
    Prep API.

    Args:
        api_key: FMP API key for authentication
        start_date: Optional start date for historical data
        end_date: Optional end date for historical data

    Returns:
        List of dictionaries containing treasury rates data. Each dictionary
        contains fields like date and various treasury rate maturities.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> from datetime import date
        >>> rates = treasury_rates_api("your_api_key")
        >>> rates = treasury_rates_api("your_api_key",
        ...                            start_date=date(2023, 1, 1),
        ...                            end_date=date(2023, 12, 31))
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/treasury-rates"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}
    if start_date:
        params["from"] = start_date.strftime("%Y-%m-%d")
    if end_date:
        params["to"] = end_date.strftime("%Y-%m-%d")

    logger.debug("Requesting treasury rates from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch treasury rates: {e}")
        raise FMPAPIError(f"Failed to fetch treasury rates: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} treasury rate records")
        return data
    else:
        logger.warning("Unexpected response format for treasury rates")
        return []


def company_list_api(api_key: str) -> List[Dict[str, Any]]:
    """
    Request list of all company stocks from FMP API.

    Retrieves a list of all available company stock symbols and their
    information from the Financial Modeling Prep API.

    Args:
        api_key: FMP API key for authentication

    Returns:
        List of dictionaries containing company stock information. Each
        dictionary typically contains fields like symbol, name, price,
        exchange, and other company details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> companies = company_list_api("your_api_key")
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/stock-list"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    logger.debug("Requesting company list from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch company list: {e}")
        raise FMPAPIError(f"Failed to fetch company list: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} company records")
        return data
    else:
        logger.warning("Unexpected response format for company list")
        return []


def etf_symbol_list_api(api_key: str) -> List[Dict[str, Any]]:
    """
    Request list of all ETF symbols from FMP API.

    Retrieves a list of all available ETF (Exchange Traded Fund) symbols
    and their information from the Financial Modeling Prep API.

    Args:
        api_key: FMP API key for authentication

    Returns:
        List of dictionaries containing ETF information. Each dictionary
        typically contains fields like symbol, name, price, exchange, and
        other ETF details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> etfs = etf_symbol_list_api("your_api_key")
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/etf-list"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    logger.debug("Requesting ETF symbol list from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ETF symbol list: {e}")
        raise FMPAPIError(f"Failed to fetch ETF symbol list: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} ETF records")
        return data
    else:
        logger.warning("Unexpected response format for ETF symbol list")
        return []


def sector_list_api(api_key: str) -> List[Dict[str, Any]]:
    """
    Request list of all sectors from FMP API.

    Retrieves a list of all available market sectors from the Financial
    Modeling Prep API.

    Args:
        api_key: FMP API key for authentication

    Returns:
        List of dictionaries containing sector information. Each dictionary
        typically contains the sector name and related details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> sectors = sector_list_api("your_api_key")
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/available-sectors"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    logger.debug("Requesting sector list from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch sector list: {e}")
        raise FMPAPIError(f"Failed to fetch sector list: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} sector records")
        return data
    else:
        logger.warning("Unexpected response format for sector list")
        return []


def industry_list_api(api_key: str) -> List[Dict[str, Any]]:
    """
    Request list of all industries from FMP API.

    Retrieves a list of all available industries from the Financial
    Modeling Prep API.

    Args:
        api_key: FMP API key for authentication

    Returns:
        List of dictionaries containing industry information. Each dictionary
        typically contains the industry name and related details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> industries = industry_list_api("your_api_key")
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/available-industries"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    logger.debug("Requesting industry list from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch industry list: {e}")
        raise FMPAPIError(f"Failed to fetch industry list: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} industry records")
        return data
    else:
        logger.warning("Unexpected response format for industry list")
        return []


def actively_trading_list_api(api_key: str) -> List[Dict[str, Any]]:
    """
    Request list of actively trading securities from FMP API.

    Retrieves a list of all actively trading securities from the Financial
    Modeling Prep API.

    Args:
        api_key: FMP API key for authentication

    Returns:
        List of dictionaries containing actively trading security information.
        Each dictionary typically contains fields like symbol, name, price,
        exchange, and other details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> active = actively_trading_list_api("your_api_key")
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/actively-trading-list"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    logger.debug("Requesting actively trading list from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch actively trading list: {e}")
        raise FMPAPIError(f"Failed to fetch actively trading list: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} actively trading records")
        return data
    else:
        logger.warning("Unexpected response format for actively trading list")
        return []


def screener_api(
    api_key: str,
    marketCapMoreThan: Optional[float] = None,
    marketCapLowerThan: Optional[float] = None,
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    betaMoreThan: Optional[float] = None,
    betaLowerThan: Optional[float] = None,
    priceMoreThan: Optional[float] = None,
    priceLowerThan: Optional[float] = None,
    dividendMoreThan: Optional[float] = None,
    dividendLowerThan: Optional[float] = None,
    volumeMoreThan: Optional[int] = None,
    volumeLowerThan: Optional[int] = None,
    exchange: Optional[str] = None,
    country: Optional[str] = None,
    isEtf: Optional[bool] = None,
    isFund: Optional[bool] = None,
    isActivelyTrading: Optional[bool] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Request stock screener results from FMP API.

    Retrieves a list of stocks that match the specified screening criteria
    from the Financial Modeling Prep API.

    Args:
        api_key: FMP API key for authentication
        marketCapMoreThan: Optional minimum market capitalization
        marketCapLowerThan: Optional maximum market capitalization
        sector: Optional sector filter (e.g., "Technology")
        industry: Optional industry filter (e.g., "Consumer Electronics")
        betaMoreThan: Optional minimum beta value
        betaLowerThan: Optional maximum beta value
        priceMoreThan: Optional minimum stock price
        priceLowerThan: Optional maximum stock price
        dividendMoreThan: Optional minimum dividend
        dividendLowerThan: Optional maximum dividend
        volumeMoreThan: Optional minimum trading volume
        volumeLowerThan: Optional maximum trading volume
        exchange: Optional exchange filter (e.g., "NASDAQ")
        country: Optional country filter (e.g., "US")
        isEtf: Optional filter for ETFs (True/False)
        isFund: Optional filter for funds (True/False)
        isActivelyTrading: Optional filter for actively trading securities (True/False)
        limit: Optional maximum number of results to return

    Returns:
        List of dictionaries containing stock screening results. Each dictionary
        contains fields like symbol, name, price, market cap, sector, industry,
        and other company details.

    Raises:
        FMPAPIError: If the API request fails or returns an error
        ValueError: If required parameters are invalid

    Example:
        >>> # Screen for technology stocks with market cap > $1B
        >>> results = screener_api(
        ...     "your_api_key",
        ...     sector="Technology",
        ...     marketCapMoreThan=1000000000,
        ...     limit=100
        ... )
        >>> # Screen for stocks on NASDAQ with price between $10 and $200
        >>> results = screener_api(
        ...     "your_api_key",
        ...     exchange="NASDAQ",
        ...     priceMoreThan=10,
        ...     priceLowerThan=200,
        ...     isActivelyTrading=True
        ... )
    """
    if not api_key:
        raise ValueError("API key cannot be empty")

    # Construct the base URL
    base_url = "https://financialmodelingprep.com/stable"
    endpoint = f"{base_url}/company-screener"

    # Build query parameters
    params: Dict[str, Any] = {"apikey": api_key}

    # Add optional parameters if provided
    if marketCapMoreThan is not None:
        params["marketCapMoreThan"] = marketCapMoreThan
    if marketCapLowerThan is not None:
        params["marketCapLowerThan"] = marketCapLowerThan
    if sector is not None:
        params["sector"] = sector
    if industry is not None:
        params["industry"] = industry
    if betaMoreThan is not None:
        params["betaMoreThan"] = betaMoreThan
    if betaLowerThan is not None:
        params["betaLowerThan"] = betaLowerThan
    if priceMoreThan is not None:
        params["priceMoreThan"] = priceMoreThan
    if priceLowerThan is not None:
        params["priceLowerThan"] = priceLowerThan
    if dividendMoreThan is not None:
        params["dividendMoreThan"] = dividendMoreThan
    if dividendLowerThan is not None:
        params["dividendLowerThan"] = dividendLowerThan
    if volumeMoreThan is not None:
        params["volumeMoreThan"] = volumeMoreThan
    if volumeLowerThan is not None:
        params["volumeLowerThan"] = volumeLowerThan
    if exchange is not None:
        params["exchange"] = exchange
    if country is not None:
        params["country"] = country
    if isEtf is not None:
        params["isEtf"] = str(isEtf).lower()
    if isFund is not None:
        params["isFund"] = str(isFund).lower()
    if isActivelyTrading is not None:
        params["isActivelyTrading"] = str(isActivelyTrading).lower()
    if limit is not None:
        params["limit"] = limit

    logger.debug("Requesting stock screener results from FMP API")

    try:
        response = requests.get(endpoint, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch stock screener results: {e}")
        raise FMPAPIError(f"Failed to fetch stock screener results: {e}") from e

    try:
        data = response.json()
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        raise FMPAPIError(f"Failed to parse JSON response: {e}") from e

    # Check if the response contains an error message
    if isinstance(data, dict) and "Error Message" in data:
        error_msg = data["Error Message"]
        logger.error(f"FMP API error: {error_msg}")
        raise FMPAPIError(f"FMP API error: {error_msg}")

    # Extract data from response
    if isinstance(data, list):
        logger.info(f"Retrieved {len(data)} stock screener records")
        return data
    else:
        logger.warning("Unexpected response format for stock screener")
        return []


def get_price_history(
    api_key: str,
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[List[str]] = None,
    adjusted: bool = False,
) -> pd.DataFrame:
    """
    Get historical price data as a pandas DataFrame.

    This function combines the api_date_range and price_history_api functions
    to retrieve and process historical price data for a security. It supports
    date range calculation, data resampling, and limiting the number of records.

    Args:
        api_key: FMP API key for authentication
        symbol: The ticker symbol for the security (e.g., "AAPL", "MSFT")
        start_date: Optional start date string (format: YYYY-MM-DD)
        end_date: Optional end date string (format: YYYY-MM-DD)
        frequency: Frequency of data points. Valid values are:
            'day' (daily), 'week' (weekly), 'month' (monthly),
            'quarter' (quarterly), 'semi-annual' (semi-annually),
            'annual' (annually). Default is 'day'.
        limit: Optional number of records to return. When combined with:
            - start_date (no end_date): returns first `limit` records
            - end_date (no start_date): returns last `limit` records
        fields: Optional list of columns to return. Valid fields are:
            'open', 'high', 'low', 'close', 'volume'. Default is all fields.
        adjusted: If True, retrieve dividend-adjusted price history. Default is False.

    Returns:
        pandas DataFrame with historical price data, indexed on Date (ascending).
        Columns include the specified fields (or all fields if not specified).

    Raises:
        ValueError: If parameters are invalid or if invalid fields are specified
        FMPAPIError: If the API request fails

    Examples:
        >>> # Get all available daily data for AAPL
        >>> df = get_price_history("your_api_key", "AAPL")

        >>> # Get daily data for a specific date range
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        start_date="2023-01-01",
        ...                        end_date="2023-12-31")

        >>> # Get first 10 daily records starting from a date
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        start_date="2023-01-01", limit=10)

        >>> # Get last 10 daily records before a date
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        end_date="2023-12-31", limit=10)

        >>> # Get weekly data for the last 30 weeks
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        frequency="week", limit=30)

        >>> # Get monthly data for a date range
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        start_date="2023-01-01",
        ...                        end_date="2023-12-31",
        ...                        frequency="month")

        >>> # Get only close prices
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        start_date="2023-01-01",
        ...                        end_date="2023-12-31",
        ...                        fields=["close"])

        >>> # Get OHLC data without volume
        >>> df = get_price_history("your_api_key", "AAPL",
        ...                        start_date="2023-01-01",
        ...                        end_date="2023-12-31",
        ...                        fields=["open", "high", "low", "close"])
    """
    # Validate fields parameter
    valid_fields = ["open", "high", "low", "close", "volume"]
    if fields is not None:
        fields = [f for f in fields if f in valid_fields]
    else:
        fields = valid_fields  # Default to all fields

    # Convert string dates to date objects
    start_date_obj = None
    end_date_obj = None
    if start_date:
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Calculate date range using api_date_range
    calculated_start, calculated_end = get_api_date_range(
        start_date=start_date_obj,
        end_date=end_date_obj,
        limit=limit,
        frequency=frequency,
    )

    # Convert dates back to strings for the API call
    #    from_date = calculated_start.strftime("%Y-%m-%d") if calculated_start else None
    #    to_date = calculated_end.strftime("%Y-%m-%d") if calculated_end else None

    logger.info(
        f"Fetching price history for {symbol} from "
        f"{calculated_start} to {calculated_end}"
    )

    # Fetch data from API - use adjusted or regular price history
    if adjusted:
        data = adjusted_price_history_api(
            symbol=symbol,
            api_key=api_key,
            from_date=calculated_start,
            to_date=calculated_end,
        )
    else:
        data = price_history_api(
            symbol=symbol,
            api_key=api_key,
            from_date=calculated_start,
            to_date=calculated_end,
        )

    # Convert to DataFrame
    if not data:
        logger.warning(f"No data returned for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(data)

    # Convert df columns to lower case for consistency
    df.columns = [col.lower() for col in df.columns]

    # Strip "adj" prefix from column names if adjusted data is used
    if adjusted:
        df.columns = [col.removeprefix("adj").strip() for col in df.columns]

    # Keep only the relevant columns
    expected_columns = ["date"] + fields
    df = df.loc[:, [col for col in expected_columns if col in df.columns]]

    # Ensure date column exists and convert to datetime
    if "date" not in df.columns:
        logger.error("No 'date' column in response data")
        raise ValueError("Response data missing 'date' column")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")  # Sort ascending by date

    # Set date as index
    df = df.set_index("date")

    # Resample if frequency is not 'day'
    if frequency != "day":
        logger.info(f"Resampling data to {frequency} frequency")
        # Map frequency to pandas resample offset
        freq_map = {
            "week": "W",
            "month": "ME",
            "quarter": "QE",
            "semi-annual": "6ME",
            "annual": "YE",
        }

        if frequency in freq_map:
            # Resample: use last value for each period (typical for OHLC data)
            # For open: use first, for high: use max, for low: use min,
            # for close/volume: use last
            agg_dict = {}
            if "open" in df.columns:
                agg_dict["open"] = "first"
            if "high" in df.columns:
                agg_dict["high"] = "max"
            if "low" in df.columns:
                agg_dict["low"] = "min"
            if "close" in df.columns:
                agg_dict["close"] = "last"
            if "volume" in df.columns:
                agg_dict["volume"] = "sum"

            # Add any other columns with 'last' aggregation
            for col in df.columns:
                if col not in agg_dict:
                    agg_dict[col] = "last"

            df = df.resample(freq_map[frequency]).agg(agg_dict).dropna()
            logger.debug(
                f"Resampled to {frequency} frequency, {len(df)} records remaining"
            )

    # Apply limit if specified (after resampling)
    if limit is not None and limit > 0:
        # Case: limit with start_date and no end_date - keep first `limit` records
        if start_date_obj is not None and end_date_obj is None:
            df = df.head(limit)
            logger.debug(f"Keeping first {limit} records")
        # Case: limit with end_date and no start_date - keep last `limit` records
        else:
            df = df.tail(limit)
            logger.debug(f"Keeping last {limit} records")

    logger.info(f"Returning {len(df)} records for {symbol}")
    return df


def get_yield_curve(
    api_key: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
    zero_rates: bool = False,
    tenors: Optional[tuple] = None,
    interval: Optional[str] = None,
) -> pd.DataFrame:
    """
    Get treasury yield curve data as a pandas DataFrame.

    This function retrieves treasury rates from the FMP API and processes them
    to return a yield curve. It supports date range filtering, zero rate
    bootstrapping, tenor filtering, and interpolation.

    Args:
        api_key: FMP API key for authentication
        start_date: Optional start date string (format: YYYY-MM-DD)
        end_date: Optional end date string (format: YYYY-MM-DD)
        limit: Optional number of records to return. When combined with:
            - start_date (no end_date): returns first `limit` records
            - end_date (no start_date): returns last `limit` records
        zero_rates: If True, transform par rates to zero rates using
            bootstrapping. Default is False.
        tenors: Optional tuple of (start_tenor, end_tenor) to filter the
            yield curve. For example, ('month1', 'year10') returns only
            tenors between 1 month and 10 years inclusive.
        interval: Optional interpolation interval. Valid values are:
            'day', 'week', 'month', 'quarter', 'semi-annual', 'annual'.
            If provided, the yield curve is interpolated to this interval.

    Returns:
        pandas DataFrame with yield curve data.

        If multiple dates are returned:
            - DataFrame is indexed on date (ascending order)
            - Columns are the tenor names (e.g., 'month1', 'year1', 'year10')

        If only one date is returned:
            - DataFrame is indexed on tenor names
            - Contains columns:
                - 'years': Tenor value in years (float)
                - 'date': Estimated date (record date + years)
                - 'par_rate' or 'zero_rate': Rate values (depending on
                  zero_rates parameter)

    Raises:
        ValueError: If parameters are invalid
        FMPAPIError: If the API request fails

    Examples:
        >>> # Get yield curve for a specific date
        >>> df = get_yield_curve("your_api_key",
        ...                      start_date="2023-06-01",
        ...                      end_date="2023-06-01")

        >>> # Get zero rate curve for a specific date
        >>> df = get_yield_curve("your_api_key",
        ...                      start_date="2023-06-01",
        ...                      end_date="2023-06-01",
        ...                      zero_rates=True)

        >>> # Get last 30 days of yield curves
        >>> df = get_yield_curve("your_api_key", limit=30)

        >>> # Get yield curve with quarterly interpolation
        >>> df = get_yield_curve("your_api_key",
        ...                      start_date="2023-06-01",
        ...                      end_date="2023-06-01",
        ...                      interval="quarter")

        >>> # Get only 1-year to 10-year tenors
        >>> df = get_yield_curve("your_api_key",
        ...                      start_date="2023-06-01",
        ...                      end_date="2023-06-01",
        ...                      tenors=("year1", "year10"))
    """
    from datetime import timedelta

    from duk.rates_utils import (
        MONTHS_PER_YEAR,
        _tenor_to_months,
        bootstrap_zero_rates,
        interpolate_rates,
        treasury_rates2df,
    )

    # Convert string dates to date objects
    start_date_obj = None
    end_date_obj = None
    if start_date:
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Calculate date range using get_api_date_range (mimics get_price_history)
    calculated_start, calculated_end = get_api_date_range(
        start_date=start_date_obj,
        end_date=end_date_obj,
        limit=limit,
        frequency="day",
    )

    logger.info(f"Fetching yield curve from {calculated_start} to {calculated_end}")

    # Fetch data from API
    data = treasury_rates_api(
        api_key=api_key,
        start_date=calculated_start,
        end_date=calculated_end,
    )

    # Convert to DataFrame using treasury_rates2df
    if not data:
        logger.warning("No data returned for yield curve")
        return pd.DataFrame()

    df = treasury_rates2df(data)

    if df.empty:
        return df

    # Sort by date index ascending
    df = df.sort_index()

    # Apply limit if specified (after sorting)
    if limit is not None and limit > 0:
        # Case: limit with start_date and no end_date - keep first `limit` records
        if start_date_obj is not None and end_date_obj is None:
            df = df.head(limit)
            logger.debug(f"Keeping first {limit} records")
        # Case: limit with end_date and no start_date - keep last `limit` records
        else:
            df = df.tail(limit)
            logger.debug(f"Keeping last {limit} records")

    # Apply zero rate transformation if requested
    if zero_rates:
        logger.debug("Bootstrapping zero rates")
        # Interpolate to semi-annual before bootstrapping as per requirements
        df = interpolate_rates(df, interval="semi-annual")
        df = bootstrap_zero_rates(df)

    # Apply interpolation if interval is specified (after zero rate bootstrapping)
    if interval is not None:
        logger.debug(f"Interpolating rates with interval: {interval}")
        df = interpolate_rates(df, interval=interval)

    # Apply tenor filter if specified
    if tenors is not None:
        if len(tenors) != 2:
            raise ValueError("tenors must be a tuple of (start_tenor, end_tenor)")

        start_tenor, end_tenor = tenors
        try:
            start_months = _tenor_to_months(start_tenor)
            end_months = _tenor_to_months(end_tenor)
        except ValueError as e:
            raise ValueError(f"Invalid tenor in filter: {e}") from e

        # Filter columns based on tenor range
        filtered_columns = []
        for col in df.columns:
            try:
                col_months = _tenor_to_months(col)
                if start_months <= col_months <= end_months:
                    filtered_columns.append(col)
            except ValueError:
                # Skip columns that aren't valid tenors
                pass

        # Sort filtered columns by tenor
        filtered_columns = sorted(filtered_columns, key=lambda c: _tenor_to_months(c))
        df = df[filtered_columns]

    # Handle single date vs multiple dates output format
    if len(df) == 1:
        # Single date record - transform to tenor-indexed DataFrame
        record_date = df.index[0]
        rate_column_name = "zero_rate" if zero_rates else "par_rate"

        # Get tenor columns and their values
        tenor_data = []
        for col in df.columns:
            try:
                months = _tenor_to_months(col)
                years = months / MONTHS_PER_YEAR
                # Calculate estimated date as record_date + years
                # Using 365 days/year (standard financial approximation)
                estimated_date = record_date + timedelta(days=int(years * 365))
                rate_value = df[col].iloc[0]
                tenor_data.append(
                    {
                        "tenor": col,
                        "years": years,
                        "date": estimated_date,
                        rate_column_name: rate_value,
                    }
                )
            except ValueError:
                # Skip non-tenor columns
                pass

        if not tenor_data:
            return pd.DataFrame()

        result_df = pd.DataFrame(tenor_data)
        result_df = result_df.set_index("tenor")

        logger.info(f"Returning single-date yield curve with {len(result_df)} tenors")
        return result_df

    # Multiple dates - return as-is with date index
    logger.info(f"Returning {len(df)} yield curve records")
    return df
