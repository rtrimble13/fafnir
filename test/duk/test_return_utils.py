"""
Tests for return_utils module.
"""

import numpy as np
import pandas as pd
import pytest

from duk.return_utils import (
    ReturnCalculationError,
    annualized_return,
    cumulative_log_return,
    cumulative_simple_return,
    dividend_adjusted_return,
    excess_return,
    log_return,
    price_difference,
    simple_return,
)


class TestSimpleReturn:
    """Tests for simple_return function."""

    def test_basic_simple_return(self):
        """Test basic simple return calculation."""
        prices = pd.Series([100, 105, 103, 108])
        result = simple_return(prices)

        assert pd.isna(result.iloc[0])
        assert np.isclose(result.iloc[1], 0.05)
        assert np.isclose(result.iloc[2], -0.019048, atol=1e-5)
        assert np.isclose(result.iloc[3], 0.048544, atol=1e-5)

    def test_simple_return_multi_period(self):
        """Test simple return with multiple periods."""
        prices = pd.Series([100, 105, 103, 108])
        result = simple_return(prices, periods=2)

        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert np.isclose(result.iloc[2], 0.03)
        assert np.isclose(result.iloc[3], 0.028571, atol=1e-5)

    def test_simple_return_with_dataframe(self):
        """Test simple return with DataFrame input."""
        prices = pd.DataFrame({"A": [100, 105, 103], "B": [50, 52, 51]})
        result = simple_return(prices)

        assert result.shape == prices.shape
        assert pd.isna(result.iloc[0, 0])
        assert np.isclose(result.iloc[1, 0], 0.05)
        assert np.isclose(result.iloc[1, 1], 0.04)

    def test_simple_return_empty_series_raises_error(self):
        """Test that empty series raises error."""
        prices = pd.Series([])
        with pytest.raises(ReturnCalculationError, match="Price series is empty"):
            simple_return(prices)

    def test_simple_return_invalid_input_raises_error(self):
        """Test that invalid input raises error."""
        with pytest.raises(ReturnCalculationError, match="must be a pandas"):
            simple_return([100, 105, 103])

    def test_simple_return_with_nan_values(self):
        """Test simple return propagates NaN (current pandas pct_change contract)."""
        prices = pd.Series([100, 105, np.nan, 108])
        result = simple_return(prices)

        assert pd.isna(result.iloc[0])
        assert np.isclose(result.iloc[1], 0.05)
        # Modern pandas pct_change does NOT forward-fill: a NaN price yields NaN
        # for that period and the following one.
        assert pd.isna(result.iloc[2])
        assert pd.isna(result.iloc[3])

    def test_simple_return_single_value(self):
        """Test simple return with single value."""
        prices = pd.Series([100])
        result = simple_return(prices)

        assert len(result) == 1
        assert pd.isna(result.iloc[0])

    def test_simple_return_negative_prices(self):
        """Test simple return with negative prices (edge case)."""
        prices = pd.Series([100, -50, 25])
        result = simple_return(prices)

        assert pd.isna(result.iloc[0])
        assert np.isclose(result.iloc[1], -1.5)
        assert np.isclose(result.iloc[2], -1.5)


class TestLogReturn:
    """Tests for log_return function."""

    def test_basic_log_return(self):
        """Test basic log return calculation."""
        prices = pd.Series([100, 105, 103, 108])
        result = log_return(prices)

        assert pd.isna(result.iloc[0])
        assert np.isclose(result.iloc[1], 0.048790, atol=1e-5)
        assert np.isclose(result.iloc[2], -0.019231, atol=1e-5)
        assert np.isclose(result.iloc[3], 0.047402, atol=1e-5)

    def test_log_return_multi_period(self):
        """Test log return with multiple periods."""
        prices = pd.Series([100, 105, 103, 108])
        result = log_return(prices, periods=2)

        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert np.isclose(result.iloc[2], 0.029559, atol=1e-5)
        assert np.isclose(result.iloc[3], 0.028171, atol=1e-5)

    def test_log_return_with_dataframe(self):
        """Test log return with DataFrame input."""
        prices = pd.DataFrame({"A": [100, 105, 103], "B": [50, 52, 51]})
        result = log_return(prices)

        assert result.shape == prices.shape
        assert pd.isna(result.iloc[0, 0])
        assert np.isclose(result.iloc[1, 0], 0.048790, atol=1e-5)
        assert np.isclose(result.iloc[1, 1], 0.039221, atol=1e-5)

    def test_log_return_empty_series_raises_error(self):
        """Test that empty series raises error."""
        prices = pd.Series([])
        with pytest.raises(ReturnCalculationError, match="Price series is empty"):
            log_return(prices)

    def test_log_return_invalid_input_raises_error(self):
        """Test that invalid input raises error."""
        with pytest.raises(ReturnCalculationError, match="must be a pandas"):
            log_return([100, 105, 103])

    def test_log_return_with_zero_price(self):
        """Test log return with zero price (edge case)."""
        prices = pd.Series([100, 0, 50])
        result = log_return(prices)

        assert pd.isna(result.iloc[0])
        assert np.isinf(result.iloc[1])  # log(0/100) = -inf
        assert np.isinf(result.iloc[2])  # log(50/0) = inf

    def test_log_return_consistency(self):
        """Test that sum of log returns equals log of total return."""
        prices = pd.Series([100, 105, 103, 108, 110])
        log_returns = log_return(prices)

        # Sum of log returns should equal log(final/initial)
        sum_log_returns = log_returns.sum()
        total_log_return = np.log(110 / 100)

        assert np.isclose(sum_log_returns, total_log_return)


class TestPriceDifference:
    """Tests for price_difference function."""

    def test_basic_price_difference(self):
        """Test basic price difference calculation."""
        prices = pd.Series([100, 105, 103, 108])
        result = price_difference(prices)

        assert pd.isna(result.iloc[0])
        assert np.isclose(result.iloc[1], 5.0)
        assert np.isclose(result.iloc[2], -2.0)
        assert np.isclose(result.iloc[3], 5.0)

    def test_price_difference_multi_period(self):
        """Test price difference with multiple periods."""
        prices = pd.Series([100, 105, 103, 108])
        result = price_difference(prices, periods=2)

        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert np.isclose(result.iloc[2], 3.0)
        assert np.isclose(result.iloc[3], 3.0)

    def test_price_difference_with_dataframe(self):
        """Test price difference with DataFrame input."""
        prices = pd.DataFrame({"A": [100, 105, 103], "B": [50, 52, 51]})
        result = price_difference(prices)

        assert result.shape == prices.shape
        assert pd.isna(result.iloc[0, 0])
        assert np.isclose(result.iloc[1, 0], 5.0)
        assert np.isclose(result.iloc[2, 0], -2.0)

    def test_price_difference_empty_series_raises_error(self):
        """Test that empty series raises error."""
        prices = pd.Series([])
        with pytest.raises(ReturnCalculationError, match="Price series is empty"):
            price_difference(prices)

    def test_price_difference_invalid_input_raises_error(self):
        """Test that invalid input raises error."""
        with pytest.raises(ReturnCalculationError, match="must be a pandas"):
            price_difference([100, 105, 103])


class TestCumulativeSimpleReturn:
    """Tests for cumulative_simple_return function."""

    def test_basic_cumulative_simple_return(self):
        """Test basic cumulative simple return calculation."""
        returns = pd.Series([0.05, -0.02, 0.03, 0.01])
        result = cumulative_simple_return(returns)

        # Calculate expected at each observation
        assert isinstance(result, pd.Series)
        assert len(result) == 4
        assert np.isclose(result.iloc[0], 0.05)
        assert np.isclose(result.iloc[1], 1.05 * 0.98 - 1)
        assert np.isclose(result.iloc[2], 1.05 * 0.98 * 1.03 - 1)
        assert np.isclose(result.iloc[3], 1.05 * 0.98 * 1.03 * 1.01 - 1)

    def test_cumulative_simple_return_with_dataframe(self):
        """Test cumulative simple return with DataFrame input."""
        returns = pd.DataFrame({"A": [0.05, 0.03], "B": [0.02, -0.01]})
        result = cumulative_simple_return(returns)

        assert isinstance(result, pd.DataFrame)
        assert result.shape == (2, 2)
        assert np.isclose(result.loc[0, "A"], 0.05)
        assert np.isclose(result.loc[1, "A"], 1.05 * 1.03 - 1)
        assert np.isclose(result.loc[0, "B"], 0.02)
        assert np.isclose(result.loc[1, "B"], 1.02 * 0.99 - 1)

    def test_cumulative_simple_return_all_positive(self):
        """Test cumulative simple return with all positive returns."""
        returns = pd.Series([0.1, 0.1, 0.1])
        result = cumulative_simple_return(returns)

        assert isinstance(result, pd.Series)
        assert len(result) == 3
        assert np.isclose(result.iloc[0], 0.1)
        assert np.isclose(result.iloc[1], 1.1 * 1.1 - 1)
        assert np.isclose(result.iloc[2], 1.1 * 1.1 * 1.1 - 1)

    def test_cumulative_simple_return_all_negative(self):
        """Test cumulative simple return with all negative returns."""
        returns = pd.Series([-0.05, -0.05, -0.05])
        result = cumulative_simple_return(returns)

        assert isinstance(result, pd.Series)
        assert len(result) == 3
        assert np.isclose(result.iloc[0], -0.05)
        assert np.isclose(result.iloc[1], 0.95 * 0.95 - 1)
        assert np.isclose(result.iloc[2], 0.95 * 0.95 * 0.95 - 1)
        assert result.iloc[2] < 0

    def test_cumulative_simple_return_empty_raises_error(self):
        """Test that empty series raises error."""
        returns = pd.Series([])
        with pytest.raises(ReturnCalculationError, match="Returns series is empty"):
            cumulative_simple_return(returns)

    def test_cumulative_simple_return_with_nan(self):
        """Test cumulative simple return with NaN values."""
        returns = pd.Series([0.05, np.nan, 0.03, 0.01])
        result = cumulative_simple_return(returns)

        # cumprod() produces NaN at the NaN position but continues calculating
        assert isinstance(result, pd.Series)
        assert len(result) == 4
        assert np.isclose(result.iloc[0], 0.05)
        assert pd.isna(result.iloc[1])
        # After NaN, it continues from the last valid value
        assert np.isclose(result.iloc[2], 1.05 * 1.03 - 1)
        assert np.isclose(result.iloc[3], 1.05 * 1.03 * 1.01 - 1)

    def test_cumulative_simple_return_single_value(self):
        """Test cumulative simple return with single value."""
        returns = pd.Series([0.05])
        result = cumulative_simple_return(returns)

        assert isinstance(result, pd.Series)
        assert len(result) == 1
        assert np.isclose(result.iloc[0], 0.05)


class TestCumulativeLogReturn:
    """Tests for cumulative_log_return function."""

    def test_basic_cumulative_log_return(self):
        """Test basic cumulative log return calculation."""
        returns = pd.Series([0.0488, -0.0192, 0.0296, 0.0099])
        result = cumulative_log_return(returns)

        # Calculate cumulative sum at each observation
        assert isinstance(result, pd.Series)
        assert len(result) == 4
        assert np.isclose(result.iloc[0], 0.0488, atol=1e-4)
        assert np.isclose(result.iloc[1], 0.0488 - 0.0192, atol=1e-4)
        assert np.isclose(result.iloc[2], 0.0488 - 0.0192 + 0.0296, atol=1e-4)
        assert np.isclose(result.iloc[3], 0.0488 - 0.0192 + 0.0296 + 0.0099, atol=1e-4)

    def test_cumulative_log_return_with_dataframe(self):
        """Test cumulative log return with DataFrame input."""
        returns = pd.DataFrame({"A": [0.05, 0.03], "B": [0.02, -0.01]})
        result = cumulative_log_return(returns)

        assert isinstance(result, pd.DataFrame)
        assert result.shape == (2, 2)
        assert np.isclose(result.loc[0, "A"], 0.05)
        assert np.isclose(result.loc[1, "A"], 0.08)
        assert np.isclose(result.loc[0, "B"], 0.02)
        assert np.isclose(result.loc[1, "B"], 0.01)

    def test_cumulative_log_return_empty_raises_error(self):
        """Test that empty series raises error."""
        returns = pd.Series([])
        with pytest.raises(ReturnCalculationError, match="Returns series is empty"):
            cumulative_log_return(returns)

    def test_cumulative_log_return_with_nan(self):
        """Test cumulative log return with NaN values."""
        returns = pd.Series([0.05, np.nan, 0.03, 0.01])
        result = cumulative_log_return(returns)

        # cumsum() produces NaN at the NaN position but continues calculating
        assert isinstance(result, pd.Series)
        assert len(result) == 4
        assert np.isclose(result.iloc[0], 0.05)
        assert pd.isna(result.iloc[1])
        # After NaN, it continues from the last valid value
        assert np.isclose(result.iloc[2], 0.05 + 0.03)
        assert np.isclose(result.iloc[3], 0.05 + 0.03 + 0.01)

    def test_cumulative_log_return_additive_property(self):
        """Test that log returns are additive."""
        returns = pd.Series([0.1, 0.05, -0.02])
        result = cumulative_log_return(returns)

        # Should return cumulative sum at each observation
        assert isinstance(result, pd.Series)
        assert len(result) == 3
        assert np.isclose(result.iloc[0], 0.1)
        assert np.isclose(result.iloc[1], 0.15)
        assert np.isclose(result.iloc[2], 0.13)


class TestDividendAdjustedReturn:
    """Tests for dividend_adjusted_return function."""

    def test_basic_dividend_adjusted_return(self):
        """Test basic dividend-adjusted return calculation."""
        prices = pd.Series([100, 105, 103, 108])
        dividends = pd.Series([0, 2, 0, 1])
        result = dividend_adjusted_return(prices, dividends)

        assert pd.isna(result.iloc[0])
        # (105 + 2 - 100) / 100 = 0.07
        assert np.isclose(result.iloc[1], 0.07)
        # (103 + 0 - 105) / 105 = -0.019048
        assert np.isclose(result.iloc[2], -0.019048, atol=1e-5)
        # (108 + 1 - 103) / 103 = 0.058252
        assert np.isclose(result.iloc[3], 0.058252, atol=1e-5)

    def test_dividend_adjusted_return_multi_period(self):
        """Test dividend-adjusted return with multiple periods."""
        prices = pd.Series([100, 105, 103, 108])
        dividends = pd.Series([0, 2, 0, 1])
        result = dividend_adjusted_return(prices, dividends, periods=2)

        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        # (103 + 0 - 100) / 100 = 0.03
        assert np.isclose(result.iloc[2], 0.03)
        # (108 + 1 - 105) / 105 = 0.038095
        assert np.isclose(result.iloc[3], 0.038095, atol=1e-5)

    def test_dividend_adjusted_return_no_dividends(self):
        """Test dividend-adjusted return with no dividends equals simple return."""
        prices = pd.Series([100, 105, 103, 108])
        dividends = pd.Series([0, 0, 0, 0])
        result = dividend_adjusted_return(prices, dividends)
        expected = simple_return(prices)

        pd.testing.assert_series_equal(result, expected)

    def test_dividend_adjusted_return_with_dataframe(self):
        """Test dividend-adjusted return with DataFrame input."""
        prices = pd.DataFrame({"A": [100, 105, 103], "B": [50, 52, 51]})
        dividends = pd.DataFrame({"A": [0, 1, 0], "B": [0, 0, 1]})
        result = dividend_adjusted_return(prices, dividends)

        assert result.shape == prices.shape
        assert pd.isna(result.iloc[0, 0])
        # (105 + 1 - 100) / 100 = 0.06
        assert np.isclose(result.iloc[1, 0], 0.06)
        # (51 + 1 - 52) / 52 = 0
        assert np.isclose(result.iloc[2, 1], 0.0)

    def test_dividend_adjusted_return_mismatched_types_raises_error(self):
        """Test that mismatched types raise error."""
        prices = pd.Series([100, 105, 103])
        dividends = pd.DataFrame({"A": [0, 1, 0]})

        with pytest.raises(
            ReturnCalculationError, match="must both be Series or both be DataFrame"
        ):
            dividend_adjusted_return(prices, dividends)

    def test_dividend_adjusted_return_mismatched_columns_raises_error(self):
        """Test that mismatched DataFrame columns raise error."""
        prices = pd.DataFrame({"A": [100, 105, 103], "B": [50, 52, 51]})
        dividends = pd.DataFrame({"A": [0, 1, 0], "C": [0, 0, 1]})

        with pytest.raises(ReturnCalculationError, match="must have matching columns"):
            dividend_adjusted_return(prices, dividends)

    def test_dividend_adjusted_return_empty_prices_raises_error(self):
        """Test that empty prices raise error."""
        prices = pd.Series([])
        dividends = pd.Series([])

        with pytest.raises(ReturnCalculationError, match="Price series is empty"):
            dividend_adjusted_return(prices, dividends)

    def test_dividend_adjusted_return_empty_dividends_raises_error(self):
        """Test that empty dividends raise error."""
        prices = pd.Series([100, 105, 103])
        dividends = pd.Series([])

        with pytest.raises(ReturnCalculationError, match="Dividend series is empty"):
            dividend_adjusted_return(prices, dividends)


class TestExcessReturn:
    """Tests for excess_return function."""

    def test_basic_excess_return(self):
        """Test basic excess return calculation."""
        asset_returns = pd.Series([0.05, 0.02, -0.01, 0.03])
        benchmark_returns = pd.Series([0.03, 0.02, 0.01, 0.02])
        result = excess_return(asset_returns, benchmark_returns)

        assert np.isclose(result.iloc[0], 0.02)
        assert np.isclose(result.iloc[1], 0.00)
        assert np.isclose(result.iloc[2], -0.02)
        assert np.isclose(result.iloc[3], 0.01)

    def test_excess_return_with_dataframe(self):
        """Test excess return with DataFrame input."""
        assets = pd.DataFrame({"A": [0.05, 0.03], "B": [0.02, -0.01]})
        benchmark = pd.DataFrame({"A": [0.03, 0.02], "B": [0.01, 0.00]})
        result = excess_return(assets, benchmark)

        assert result.shape == assets.shape
        assert np.isclose(result.iloc[0, 0], 0.02)
        assert np.isclose(result.iloc[1, 0], 0.01)
        assert np.isclose(result.iloc[0, 1], 0.01)
        assert np.isclose(result.iloc[1, 1], -0.01)

    def test_excess_return_negative_excess(self):
        """Test excess return when asset underperforms benchmark."""
        asset_returns = pd.Series([0.01, 0.02, 0.01])
        benchmark_returns = pd.Series([0.03, 0.03, 0.03])
        result = excess_return(asset_returns, benchmark_returns)

        assert all(result < 0)

    def test_excess_return_zero_benchmark(self):
        """Test excess return with zero benchmark (absolute returns)."""
        asset_returns = pd.Series([0.05, 0.02, -0.01])
        benchmark_returns = pd.Series([0.0, 0.0, 0.0])
        result = excess_return(asset_returns, benchmark_returns)

        pd.testing.assert_series_equal(result, asset_returns)

    def test_excess_return_mismatched_types_raises_error(self):
        """Test that mismatched types raise error."""
        returns_i = pd.Series([0.05, 0.02, -0.01])
        returns_j = pd.DataFrame({"A": [0.03, 0.02, 0.01]})

        with pytest.raises(
            ReturnCalculationError, match="must both be Series or both be DataFrame"
        ):
            excess_return(returns_i, returns_j)

    def test_excess_return_mismatched_columns_raises_error(self):
        """Test that mismatched DataFrame columns raise error."""
        returns_i = pd.DataFrame({"A": [0.05, 0.02], "B": [0.02, -0.01]})
        returns_j = pd.DataFrame({"A": [0.03, 0.02], "C": [0.01, 0.00]})

        with pytest.raises(ReturnCalculationError, match="must have matching columns"):
            excess_return(returns_i, returns_j)

    def test_excess_return_empty_first_raises_error(self):
        """Test that empty first returns raise error."""
        returns_i = pd.Series([])
        returns_j = pd.Series([0.03, 0.02, 0.01])

        with pytest.raises(
            ReturnCalculationError, match="First returns series is empty"
        ):
            excess_return(returns_i, returns_j)

    def test_excess_return_empty_second_raises_error(self):
        """Test that empty second returns raise error."""
        returns_i = pd.Series([0.05, 0.02, -0.01])
        returns_j = pd.Series([])

        with pytest.raises(
            ReturnCalculationError, match="Second returns series is empty"
        ):
            excess_return(returns_i, returns_j)


class TestAnnualizedReturn:
    """Tests for annualized_return function."""

    def test_annualized_simple_return_daily(self):
        """Test annualized simple return with daily data."""
        # 0.1% daily return for 252 trading days
        returns = pd.Series([0.001] * 252)
        result = annualized_return(returns, periods_per_year=252, return_type="simple")

        # Result should be a Series with annualized returns at each observation
        assert isinstance(result, pd.Series)
        assert len(result) == 252
        # Final value should be (1.001^252) - 1 ≈ 0.2874
        expected_final = (1.001**252) - 1
        assert np.isclose(result.iloc[-1], expected_final, atol=1e-4)

    def test_annualized_log_return_monthly(self):
        """Test annualized log return with monthly data."""
        # 1% monthly log return for 12 months
        log_returns = pd.Series([0.01] * 12)
        result = annualized_return(log_returns, periods_per_year=12, return_type="log")

        # Result should be a Series with annualized returns at each observation
        assert isinstance(result, pd.Series)
        assert len(result) == 12
        # Final value should be sum of log returns (0.12)
        expected_final = 0.12
        assert np.isclose(result.iloc[-1], expected_final)

    def test_annualized_return_with_dataframe(self):
        """Test annualized return with DataFrame input."""
        returns_df = pd.DataFrame({"A": [0.001] * 252, "B": [0.002] * 252})
        result = annualized_return(
            returns_df, periods_per_year=252, return_type="simple"
        )

        # Result should be a DataFrame with annualized returns at each observation
        assert isinstance(result, pd.DataFrame)
        assert result.shape == (252, 2)
        # Check final values
        assert np.isclose(result.iloc[-1]["A"], (1.001**252) - 1, atol=1e-4)
        assert np.isclose(result.iloc[-1]["B"], (1.002**252) - 1, atol=1e-4)

    def test_annualized_return_negative_returns(self):
        """Test annualized return with negative returns."""
        returns = pd.Series([-0.001] * 252)
        result = annualized_return(returns, periods_per_year=252, return_type="simple")

        # Result should be a Series
        assert isinstance(result, pd.Series)
        assert len(result) == 252
        expected_final = (0.999**252) - 1
        assert np.isclose(result.iloc[-1], expected_final, atol=1e-4)
        assert result.iloc[-1] < 0

    def test_annualized_return_mixed_returns(self):
        """Test annualized return with mixed positive and negative returns."""
        returns = pd.Series([0.01, -0.005, 0.02, -0.01] * 63)  # 252 observations
        result = annualized_return(returns, periods_per_year=252, return_type="simple")

        # Result should be a Series with annualized returns
        assert isinstance(result, pd.Series)
        assert len(result) == 252

    def test_annualized_return_weekly_data(self):
        """Test annualized return with weekly data."""
        returns = pd.Series([0.005] * 52)  # 52 weeks
        result = annualized_return(returns, periods_per_year=52, return_type="simple")

        # Result should be a Series
        assert isinstance(result, pd.Series)
        assert len(result) == 52
        expected_final = (1.005**52) - 1
        assert np.isclose(result.iloc[-1], expected_final, atol=1e-4)

    def test_annualized_return_invalid_type_raises_error(self):
        """Test that invalid return type raises error."""
        returns = pd.Series([0.01] * 12)

        with pytest.raises(ValueError, match="return_type must be 'simple' or 'log'"):
            annualized_return(returns, periods_per_year=12, return_type="invalid")

    def test_annualized_return_empty_raises_error(self):
        """Test that empty series raises error."""
        returns = pd.Series([])

        with pytest.raises(ReturnCalculationError, match="Returns series is empty"):
            annualized_return(returns, periods_per_year=252)

    def test_annualized_return_all_nan_raises_error(self):
        """Test that all NaN returns raise error."""
        returns = pd.Series([np.nan, np.nan, np.nan])

        with pytest.raises(
            ReturnCalculationError, match="No valid return observations"
        ):
            annualized_return(returns, periods_per_year=252)

    def test_annualized_return_consistency_simple_vs_log(self):
        """Test consistency between simple and log return annualization."""
        # For small returns, simple and log returns should be similar
        small_returns = pd.Series([0.0001] * 252)

        simple_ann = annualized_return(
            small_returns, periods_per_year=252, return_type="simple"
        )
        log_ann = annualized_return(
            small_returns, periods_per_year=252, return_type="log"
        )

        # For small returns, final values should be relatively close (within 2%)
        assert np.isclose(simple_ann.iloc[-1], log_ann.iloc[-1], rtol=0.02)

    def test_annualized_simple_return_formula_verification(self):
        """Test annualized simple return formula."""
        # Create known returns
        returns = pd.Series([0.02, 0.03, 0.01, -0.01])  # 4 quarters
        result = annualized_return(returns, periods_per_year=4, return_type="simple")

        # Result should be a Series
        assert isinstance(result, pd.Series)
        assert len(result) == 4

        # Check final value manually
        cumulative = (1.02 * 1.03 * 1.01 * 0.99) - 1
        expected_final = (1 + cumulative) ** (4 / 4) - 1  # periods_per_year / n_periods
        assert np.isclose(result.iloc[-1], expected_final)

    def test_annualized_log_return_formula_verification(self):
        """Test annualized log return formula."""
        # Create known log returns
        log_returns = pd.Series([0.02, 0.03, 0.01, -0.01])  # 4 quarters
        result = annualized_return(log_returns, periods_per_year=4, return_type="log")

        # Result should be a Series
        assert isinstance(result, pd.Series)
        assert len(result) == 4

        # Check final value manually: sum * (periods_per_year / n_periods)
        expected_final = (0.02 + 0.03 + 0.01 - 0.01) * (4 / 4)
        assert np.isclose(result.iloc[-1], expected_final)
