"""
Return calculation utilities for financial time series data.

This module provides functions for computing various types of returns
from price data, including simple returns, log returns, cumulative returns,
and annualized returns.
"""

import logging
from typing import Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class ReturnCalculationError(Exception):
    """Exception raised for return calculation errors."""

    pass


def simple_return(
    prices: Union[pd.Series, pd.DataFrame], periods: int = 1
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate simple returns from a price series.

    Simple return is calculated as: (P_t - P_{t-1}) / P_{t-1}

    Args:
        prices: Price series or DataFrame. If DataFrame, returns are calculated
            for each column independently.
        periods: Number of periods to look back. Default is 1 (single-period return).

    Returns:
        Series or DataFrame of simple returns with the same structure as input.
        First 'periods' observations will be NaN.

    Raises:
        ReturnCalculationError: If prices contain invalid values or are empty.

    Examples:
        >>> prices = pd.Series([100, 105, 103, 108])
        >>> simple_return(prices)
        0         NaN
        1    0.050000
        2   -0.019048
        3    0.048544
        dtype: float64

        >>> # Multi-period return
        >>> simple_return(prices, periods=2)
        0         NaN
        1         NaN
        2    0.030000
        3    0.028571
        dtype: float64
    """
    if isinstance(prices, (pd.Series, pd.DataFrame)):
        if prices.empty:
            raise ReturnCalculationError("Price series is empty")
    else:
        raise ReturnCalculationError("prices must be a pandas Series or DataFrame")

    logger.debug(f"Calculating simple return with periods={periods}")

    # Calculate simple return: (P_t - P_{t-periods}) / P_{t-periods}
    returns = prices.pct_change(periods=periods)

    logger.debug(f"Calculated {returns.notna().sum()} non-null return values")
    return returns


def log_return(
    prices: Union[pd.Series, pd.DataFrame], periods: int = 1
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate log returns from a price series.

    Log return is calculated as: ln(P_t) - ln(P_{t-1}) = ln(P_t / P_{t-1})

    Args:
        prices: Price series or DataFrame. If DataFrame, returns are calculated
            for each column independently.
        periods: Number of periods to look back. Default is 1 (single-period return).

    Returns:
        Series or DataFrame of log returns with the same structure as input.
        First 'periods' observations will be NaN.

    Raises:
        ReturnCalculationError: If prices contain invalid values or are empty.

    Examples:
        >>> prices = pd.Series([100, 105, 103, 108])
        >>> log_return(prices)
        0         NaN
        1    0.048790
        2   -0.019221
        3    0.047376
        dtype: float64

        >>> # Multi-period log return
        >>> log_return(prices, periods=2)
        0         NaN
        1         NaN
        2    0.029559
        3    0.028155
        dtype: float64
    """
    if isinstance(prices, (pd.Series, pd.DataFrame)):
        if prices.empty:
            raise ReturnCalculationError("Price series is empty")
    else:
        raise ReturnCalculationError("prices must be a pandas Series or DataFrame")

    logger.debug(f"Calculating log return with periods={periods}")

    # Calculate log return: ln(P_t / P_{t-periods})
    returns = np.log(prices / prices.shift(periods))

    logger.debug(f"Calculated {returns.notna().sum()} non-null return values")
    return returns


def price_difference(
    prices: Union[pd.Series, pd.DataFrame], periods: int = 1
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate price differences from a price series.

    Price difference is calculated as: P_t - P_{t-1}

    Args:
        prices: Price series or DataFrame. If DataFrame, differences are
            calculated for each column independently.
        periods: Number of periods to look back. Default is 1
            (single-period difference).

    Returns:
        Series or DataFrame of price differences with the same structure as input.
        First 'periods' observations will be NaN.

    Raises:
        ReturnCalculationError: If prices contain invalid values or are empty.

    Examples:
        >>> prices = pd.Series([100, 105, 103, 108])
        >>> price_difference(prices)
        0    NaN
        1    5.0
        2   -2.0
        3    5.0
        dtype: float64

        >>> # Multi-period difference
        >>> price_difference(prices, periods=2)
        0    NaN
        1    NaN
        2    3.0
        3    3.0
        dtype: float64
    """
    if isinstance(prices, (pd.Series, pd.DataFrame)):
        if prices.empty:
            raise ReturnCalculationError("Price series is empty")
    else:
        raise ReturnCalculationError("prices must be a pandas Series or DataFrame")

    logger.debug(f"Calculating price difference with periods={periods}")

    # Calculate price difference: P_t - P_{t-periods}
    differences = prices.diff(periods=periods)

    logger.debug(f"Calculated {differences.notna().sum()} non-null difference values")
    return differences


def cumulative_simple_return(
    returns: Union[pd.Series, pd.DataFrame],
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate cumulative simple return from a series of simple returns.

    Cumulative simple return is calculated as: R_t = prod(1 + r_i) - 1 for i=1 to t

    This function is vectorized and returns the cumulative return at each observation.

    Args:
        returns: Series or DataFrame of simple returns. If DataFrame, cumulative
            returns are calculated for each column independently.

    Returns:
        Series or DataFrame of cumulative returns at each observation, with the
        same structure as the input.

    Raises:
        ReturnCalculationError: If returns contain invalid values or are empty.

    Examples:
        >>> returns = pd.Series([0.05, -0.02, 0.03, 0.01])
        >>> cumulative_simple_return(returns)
        0    0.050000
        1    0.029000
        2    0.059870
        3    0.070147
        dtype: float64

        >>> # With DataFrame input
        >>> returns_df = pd.DataFrame({'A': [0.05, 0.03], 'B': [0.02, -0.01]})
        >>> cumulative_simple_return(returns_df)
             A         B
        0  0.05  0.020000
        1  0.0815  0.009800
    """
    if isinstance(returns, (pd.Series, pd.DataFrame)):
        if returns.empty:
            raise ReturnCalculationError("Returns series is empty")
    else:
        raise ReturnCalculationError("returns must be a pandas Series or DataFrame")

    logger.debug("Calculating cumulative simple return at each observation")

    # Calculate cumulative return: cumprod(1 + r_t) - 1
    cumulative = (1 + returns).cumprod() - 1

    if isinstance(returns, pd.Series):
        logger.debug(f"Calculated {cumulative.notna().sum()} cumulative return values")
    else:
        logger.debug(
            f"Calculated cumulative returns for {len(cumulative.columns)} columns"
        )

    return cumulative


def cumulative_log_return(
    returns: Union[pd.Series, pd.DataFrame],
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate cumulative log return from a series of log returns.

    Cumulative log return is calculated as: R_t = sum(r_i) for i=1 to t

    This function is vectorized and returns the cumulative log return at
    each observation.

    Args:
        returns: Series or DataFrame of log returns. If DataFrame, cumulative
            returns are calculated for each column independently.

    Returns:
        Series or DataFrame of cumulative log returns at each observation, with
        the same structure as the input.

    Raises:
        ReturnCalculationError: If returns contain invalid values or are empty.

    Examples:
        >>> returns = pd.Series([0.0488, -0.0192, 0.0296, 0.0099])
        >>> cumulative_log_return(returns)
        0    0.0488
        1    0.0296
        2    0.0592
        3    0.0691
        dtype: float64

        >>> # With DataFrame input
        >>> returns_df = pd.DataFrame({'A': [0.05, 0.03], 'B': [0.02, -0.01]})
        >>> cumulative_log_return(returns_df)
              A     B
        0  0.05  0.02
        1  0.08  0.01
    """
    if isinstance(returns, (pd.Series, pd.DataFrame)):
        if returns.empty:
            raise ReturnCalculationError("Returns series is empty")
    else:
        raise ReturnCalculationError("returns must be a pandas Series or DataFrame")

    logger.debug("Calculating cumulative log return at each observation")

    # Calculate cumulative log return: cumsum(r_t)
    cumulative = returns.cumsum()

    if isinstance(returns, pd.Series):
        logger.debug(
            f"Calculated {cumulative.notna().sum()} cumulative log return values"
        )
    else:
        logger.debug(
            f"Calculated cumulative log returns for {len(cumulative.columns)} columns"
        )

    return cumulative


def dividend_adjusted_return(
    prices: Union[pd.Series, pd.DataFrame],
    dividends: Union[pd.Series, pd.DataFrame],
    periods: int = 1,
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate dividend-adjusted returns from price and dividend series.

    Dividend-adjusted return is calculated as: (P_t + D_t - P_{t-1}) / P_{t-1}

    Args:
        prices: Price series or DataFrame. If DataFrame, returns are
            calculated for each column independently.
        dividends: Dividend series or DataFrame matching the structure of
            prices. Should contain dividend amounts paid at each period
            (0 if no dividend).
        periods: Number of periods to look back. Default is 1
            (single-period return).

    Returns:
        Series or DataFrame of dividend-adjusted returns with the same
        structure as input. First 'periods' observations will be NaN.

    Raises:
        ReturnCalculationError: If inputs are invalid, empty, or mismatched
            in structure.

    Examples:
        >>> prices = pd.Series([100, 105, 103, 108])
        >>> dividends = pd.Series([0, 2, 0, 1])
        >>> dividend_adjusted_return(prices, dividends)
        0         NaN
        1    0.070000
        2   -0.019048
        3    0.057282
        dtype: float64

        >>> # Multi-period dividend-adjusted return
        >>> dividend_adjusted_return(prices, dividends, periods=2)
        0         NaN
        1         NaN
        2    0.050000
        3    0.038095
        dtype: float64
    """
    if isinstance(prices, (pd.Series, pd.DataFrame)):
        if prices.empty:
            raise ReturnCalculationError("Price series is empty")
    else:
        raise ReturnCalculationError("prices must be a pandas Series or DataFrame")

    if isinstance(dividends, (pd.Series, pd.DataFrame)):
        if dividends.empty:
            raise ReturnCalculationError("Dividend series is empty")
    else:
        raise ReturnCalculationError("dividends must be a pandas Series or DataFrame")

    # Check that prices and dividends have compatible structures
    if isinstance(prices, pd.Series) != isinstance(dividends, pd.Series):
        raise ReturnCalculationError(
            "prices and dividends must both be Series or both be DataFrame"
        )

    if isinstance(prices, pd.DataFrame) and isinstance(dividends, pd.DataFrame):
        if not prices.columns.equals(dividends.columns):
            raise ReturnCalculationError(
                "prices and dividends DataFrames must have matching columns"
            )

    logger.debug(f"Calculating dividend-adjusted return with periods={periods}")

    # Calculate dividend-adjusted return: (P_t + D_t - P_{t-periods}) / P_{t-periods}
    lagged_prices = prices.shift(periods)
    returns = (prices + dividends - lagged_prices) / lagged_prices

    logger.debug(
        f"Calculated {returns.notna().sum()} non-null dividend-adjusted return values"
    )
    return returns


def excess_return(
    returns_i: Union[pd.Series, pd.DataFrame],
    returns_j: Union[pd.Series, pd.DataFrame],
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate excess returns between two return series.

    Excess return is calculated as: r_{t,i} - r_{t,j}

    Args:
        returns_i: First return series or DataFrame (typically the asset return).
        returns_j: Second return series or DataFrame (typically the
            benchmark return). Must have compatible structure with returns_i.

    Returns:
        Series or DataFrame of excess returns with the same structure as input.

    Raises:
        ReturnCalculationError: If inputs are invalid, empty, or mismatched
            in structure.

    Examples:
        >>> asset_returns = pd.Series([0.05, 0.02, -0.01, 0.03])
        >>> benchmark_returns = pd.Series([0.03, 0.02, 0.01, 0.02])
        >>> excess_return(asset_returns, benchmark_returns)
        0    0.02
        1    0.00
        2   -0.02
        3    0.01
        dtype: float64

        >>> # With DataFrame inputs
        >>> assets = pd.DataFrame({'A': [0.05, 0.03], 'B': [0.02, -0.01]})
        >>> benchmark = pd.DataFrame({'A': [0.03, 0.02], 'B': [0.01, 0.00]})
        >>> excess_return(assets, benchmark)
             A     B
        0  0.02  0.01
        1  0.01 -0.01
    """
    if isinstance(returns_i, (pd.Series, pd.DataFrame)):
        if returns_i.empty:
            raise ReturnCalculationError("First returns series is empty")
    else:
        raise ReturnCalculationError("returns_i must be a pandas Series or DataFrame")

    if isinstance(returns_j, (pd.Series, pd.DataFrame)):
        if returns_j.empty:
            raise ReturnCalculationError("Second returns series is empty")
    else:
        raise ReturnCalculationError("returns_j must be a pandas Series or DataFrame")

    # Check that returns have compatible structures
    if isinstance(returns_i, pd.Series) != isinstance(returns_j, pd.Series):
        raise ReturnCalculationError(
            "returns_i and returns_j must both be Series or both be DataFrame"
        )

    if isinstance(returns_i, pd.DataFrame) and isinstance(returns_j, pd.DataFrame):
        if not returns_i.columns.equals(returns_j.columns):
            raise ReturnCalculationError(
                "returns_i and returns_j DataFrames must have matching columns"
            )

    logger.debug("Calculating excess return")

    # Calculate excess return: r_i - r_j
    excess = returns_i - returns_j

    logger.debug(f"Calculated {excess.notna().sum()} non-null excess return values")
    return excess


def annualized_return(
    returns: Union[pd.Series, pd.DataFrame],
    periods_per_year: int = 252,
    return_type: str = "simple",
) -> Union[pd.Series, pd.DataFrame]:
    """
    Calculate annualized return from a series of returns.

    For simple returns: (1 + R_cumulative)^(periods_per_year / N) - 1
    For log returns: R_cumulative * (periods_per_year / N)

    This function is vectorized and returns the annualized return at each observation,
    where N is the count of observations from the start to that point.

    Args:
        returns: Series or DataFrame of returns. If DataFrame, annualized
            returns are calculated for each column independently.
        periods_per_year: Number of periods in a year. Default is 252
            (trading days). Use 12 for monthly data, 52 for weekly data, etc.
        return_type: Type of returns - either 'simple' or 'log'.
            Default is 'simple'.

    Returns:
        Series or DataFrame of annualized returns at each observation, with
        the same structure as the input.

    Raises:
        ReturnCalculationError: If returns contain invalid values or are empty.
        ValueError: If return_type is not 'simple' or 'log'.

    Examples:
        >>> # Simple returns over 252 trading days
        >>> returns = pd.Series([0.001] * 252)  # 0.1% daily return
        >>> ann_ret = annualized_return(
        ...     returns, periods_per_year=252, return_type='simple'
        ... )
        >>> ann_ret.iloc[-1]  # Final annualized return
        0.287417

        >>> # Log returns over 12 months
        >>> log_returns = pd.Series([0.01] * 12)  # 1% monthly log return
        >>> ann_ret = annualized_return(
        ...     log_returns, periods_per_year=12, return_type='log'
        ... )
        >>> ann_ret.iloc[-1]  # Final annualized return
        0.12

        >>> # DataFrame with multiple assets
        >>> returns_df = pd.DataFrame({
        ...     'A': [0.001] * 252,
        ...     'B': [0.002] * 252
        ... })
        >>> ann_ret = annualized_return(
        ...     returns_df, periods_per_year=252, return_type='simple'
        ... )
        >>> ann_ret.iloc[-1]  # Final annualized returns
        A    0.287417
        B    0.650612
        Name: 251, dtype: float64
    """
    if return_type not in ["simple", "log"]:
        raise ValueError("return_type must be 'simple' or 'log'")

    if isinstance(returns, (pd.Series, pd.DataFrame)):
        if returns.empty:
            raise ReturnCalculationError("Returns series is empty")
    else:
        raise ReturnCalculationError("returns must be a pandas Series or DataFrame")

    logger.debug(
        f"Calculating annualized {return_type} return at each observation "
        f"(periods_per_year={periods_per_year})"
    )

    # Get cumulative returns at each observation
    if return_type == "simple":
        cumulative = cumulative_simple_return(returns)
    else:  # log returns
        cumulative = cumulative_log_return(returns)

    # Create a count of non-null observations up to each point
    if isinstance(returns, pd.Series):
        # For Series, create expanding count
        n_periods = returns.notna().cumsum()

        if n_periods.sum() == 0:
            raise ReturnCalculationError("No valid return observations")

        if return_type == "simple":
            # For simple returns: (1 + R_cumulative)^(periods_per_year / N) - 1
            annualized = (1 + cumulative) ** (periods_per_year / n_periods) - 1
            logger.debug(
                f"Calculated {annualized.notna().sum()} annualized simple return values"
            )
        else:  # log returns
            # For log returns: R_cumulative * (periods_per_year / N)
            annualized = cumulative * (periods_per_year / n_periods)
            logger.debug(
                f"Calculated {annualized.notna().sum()} annualized log return values"
            )
    else:
        # For DataFrame, calculate for each column
        n_periods = returns.notna().cumsum()

        if (n_periods.sum() == 0).all():
            raise ReturnCalculationError("No valid return observations")

        if return_type == "simple":
            # For simple returns: (1 + R_cumulative)^(periods_per_year / N) - 1
            annualized = (1 + cumulative) ** (periods_per_year / n_periods) - 1
            logger.debug(
                f"Calculated annualized simple returns for "
                f"{len(annualized.columns)} columns"
            )
        else:  # log returns
            # For log returns: R_cumulative * (periods_per_year / N)
            annualized = cumulative * (periods_per_year / n_periods)
            logger.debug(
                f"Calculated annualized log returns for "
                f"{len(annualized.columns)} columns"
            )

    return annualized
