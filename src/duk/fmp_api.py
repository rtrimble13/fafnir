"""
FMP for ``duk``: the warehouse's client, shaped into duk's frames.

**No HTTP is made here** (ADR 0017). Every request goes through
:class:`fafnir.sources.fmp.FMPClient`, the one FMP client in the repository: its
throttle, retries, byte metering and redaction, its endpoint constants, and the
window chunking that keeps a long ``from`` from being silently truncated at the
vendor's 5000-bar cap. What this module owns is what ``duk`` does *with* the rows:
the functions below keep the names and signatures the CLI, ``duk.ls_utils`` and
``duk.datasource.live`` have always called, so a reader of a vendor payload is
unchanged, and ``get_price_history`` / ``get_yield_curve`` build the frames the
compute modules share.

Pass ``client=`` to reuse one client across calls (one session, one byte count);
otherwise each call builds one from ``api_key``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from duk.date_utils import get_api_date_range
from fafnir.sources.base import SourceError, redact_secrets  # noqa: F401 -- re-exported
from fafnir.sources.fmp import FMPClient

logger = logging.getLogger(__name__)

#: One ``duk`` invocation makes a handful of requests; the client's throttle exists
#: for the loaders, which make thousands.
RATE_PER_MIN = 600


class FMPAPIError(SourceError):
    """An FMP request failed or returned an unusable payload.

    A :class:`~fafnir.sources.base.SourceError`, so one handler serves the
    warehouse's client and duk's; kept under its old name for its importers.
    """


def make_client(api_key: str) -> FMPClient:
    """The warehouse's FMP client, configured for a shell session."""
    if not api_key:
        raise ValueError("API key cannot be empty")
    return FMPClient(api_key, rate_per_min=RATE_PER_MIN)


def _iso(value: Any) -> Optional[str]:
    """A ``from``/``to`` argument as the vendor wants it: callers pass a date, a
    datetime or an ISO string, or nothing."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%d")


def _failed(what: str, exc: SourceError) -> FMPAPIError:
    # The client's message is already redacted. Raised `from None` by the caller:
    # the chained exception may hold the URL, key and all (the client's own rule).
    return FMPAPIError(f"Failed to fetch {what}: {exc}")


# -- prices -------------------------------------------------------------------


def price_history_api(
    symbol: str,
    api_key: str = "",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    *,
    client: Optional[FMPClient] = None,
) -> List[Dict[str, Any]]:
    """Unadjusted daily bars for ``symbol`` (:meth:`FMPClient.eod_raw`), ascending.

    Unadjusted, not ``full``: FMP's ``full`` payload is split-adjusted, so it
    would disagree with the warehouse's raw ``core.daily_price`` for any symbol
    that has ever split. ``duk ph`` without ``--adj`` means the prices that
    traded, which is what ``scripts/reconcile.sh`` depends on.
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    client = client or make_client(api_key)
    logger.debug(f"Requesting price history for {symbol} from FMP API")
    try:
        bars = client.eod_raw(symbol, _iso(from_date), _iso(to_date))
    except SourceError as exc:
        raise _failed(f"price history for {symbol}", exc) from None
    logger.info(f"Retrieved {len(bars)} records for {symbol}")
    return bars


def adjusted_price_history_api(
    symbol: str,
    api_key: str = "",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    *,
    client: Optional[FMPClient] = None,
) -> List[Dict[str, Any]]:
    """Split- and dividend-adjusted daily bars
    (:meth:`FMPClient.eod_dividend_adjusted`): ``duk ph --adj``."""
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    client = client or make_client(api_key)
    logger.debug(f"Requesting dividend-adjusted price history for {symbol}")
    try:
        bars = client.eod_dividend_adjusted(symbol, _iso(from_date), _iso(to_date))
    except SourceError as exc:
        raise _failed(f"dividend-adjusted price history for {symbol}", exc) from None
    logger.info(f"Retrieved {len(bars)} dividend-adjusted records for {symbol}")
    return bars


def treasury_rates_api(
    api_key: str = "",
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    *,
    client: Optional[FMPClient] = None,
) -> List[Dict[str, Any]]:
    """US Treasury par rates by date (:meth:`FMPClient.treasury_rates`)."""
    client = client or make_client(api_key)
    logger.debug("Requesting treasury rates from FMP API")
    try:
        rows = client.treasury_rates(_iso(start_date), _iso(end_date))
    except SourceError as exc:
        raise _failed("treasury rates", exc) from None
    logger.info(f"Retrieved {len(rows)} treasury rate records")
    return rows


# -- lists --------------------------------------------------------------------


def company_list_api(
    api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """Every stock symbol FMP lists (:meth:`FMPClient.stock_list`)."""
    client = client or make_client(api_key)
    try:
        return client.stock_list()
    except SourceError as exc:
        raise _failed("company list", exc) from None


def etf_symbol_list_api(
    api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """Every ETF symbol FMP lists (:meth:`FMPClient.etf_list`)."""
    client = client or make_client(api_key)
    try:
        return client.etf_list()
    except SourceError as exc:
        raise _failed("ETF symbol list", exc) from None


def actively_trading_list_api(
    api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """Every symbol FMP marks actively trading
    (:meth:`FMPClient.actively_trading_list`)."""
    client = client or make_client(api_key)
    try:
        return client.actively_trading_list()
    except SourceError as exc:
        raise _failed("actively trading list", exc) from None


def sector_list_api(
    api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """FMP's sectors as ``[{"sector": name}, ...]``, the shape
    :func:`duk.ls_utils.process_sectors` reads."""
    client = client or make_client(api_key)
    try:
        return [{"sector": name} for name in client.sectors()]
    except SourceError as exc:
        raise _failed("sector list", exc) from None


def industry_list_api(
    api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """FMP's industries as ``[{"industry": name}, ...]``, the shape
    :func:`duk.ls_utils.process_industries` reads."""
    client = client or make_client(api_key)
    try:
        return [{"industry": name} for name in client.industries()]
    except SourceError as exc:
        raise _failed("industry list", exc) from None


def screener_api(
    api_key: str = "",
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
    *,
    client: Optional[FMPClient] = None,
) -> List[Dict[str, Any]]:
    """One screener request (:meth:`FMPClient.screen`). The parameters are FMP's
    own names, passed through as given; ``None`` means not asked."""
    client = client or make_client(api_key)
    filters = {
        "marketCapMoreThan": marketCapMoreThan,
        "marketCapLowerThan": marketCapLowerThan,
        "sector": sector,
        "industry": industry,
        "betaMoreThan": betaMoreThan,
        "betaLowerThan": betaLowerThan,
        "priceMoreThan": priceMoreThan,
        "priceLowerThan": priceLowerThan,
        "dividendMoreThan": dividendMoreThan,
        "dividendLowerThan": dividendLowerThan,
        "volumeMoreThan": volumeMoreThan,
        "volumeLowerThan": volumeLowerThan,
        "exchange": exchange,
        "country": country,
        "isEtf": isEtf,
        "isFund": isFund,
        "isActivelyTrading": isActivelyTrading,
    }
    logger.debug("Requesting stock screener results from FMP API")
    try:
        rows = client.screen(filters, limit=limit)
    except SourceError as exc:
        raise _failed("stock screener results", exc) from None
    logger.info(f"Retrieved {len(rows)} stock screener records")
    return rows


# -- identifiers --------------------------------------------------------------


def identifier_search_api(
    scheme: str, value: str, api_key: str = "", *, client: Optional[FMPClient] = None
) -> List[Dict[str, Any]]:
    """The securities FMP knows under a CIK, ISIN or CUSIP
    (:meth:`FMPClient.search_identifier`). An unknown scheme or an empty value is
    refused before the key is looked at and before any request."""
    if scheme not in FMPClient.EP_IDENTIFIER_SEARCH:
        raise ValueError(f"Unknown identifier scheme '{scheme}'")
    if not value:
        raise ValueError("Identifier value cannot be empty")
    client = client or make_client(api_key)
    logger.debug(f"Searching FMP for {scheme}={value}")
    try:
        rows = client.search_identifier(scheme, value)
    except SourceError as exc:
        raise FMPAPIError(f"Failed to search {scheme} {value}: {exc}") from None
    logger.info(f"Retrieved {len(rows)} match(es) for {scheme} {value}")
    return rows


# -- frames -------------------------------------------------------------------


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

    # Strip the "adj" prefix FMP puts on OHLC fields in both the dividend-adjusted
    # and non-split-adjusted payloads, so callers always see open/high/low/close.
    # On the unadjusted feed the prefix is only a naming convention -- the values
    # are the prices as traded. Renaming a column onto one that already exists
    # would create a duplicate, so only fill gaps.
    renamed = []
    for col in df.columns:
        stripped = col.removeprefix("adj").strip()
        renamed.append(stripped if stripped and stripped not in df.columns else col)
    df.columns = renamed

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
