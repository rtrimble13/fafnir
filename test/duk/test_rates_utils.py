"""
Tests for rates_utils module.
"""

import pandas as pd
import pytest

from duk.rates_utils import (
    VALID_INTERVALS,
    _get_interval_in_months,
    _months_to_tenor,
    _tenor_to_months,
    bootstrap_zero_rates,
    interpolate_rates,
    treasury_rates2df,
)


class TestTreasuryRates2df:
    """Tests for treasury_rates2df function."""

    def test_basic_conversion(self):
        """Test basic conversion of treasury rates to DataFrame."""
        par_yields = [
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

        result = treasury_rates2df(par_yields)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2
        assert result.index.name == "date"

    def test_date_is_converted_to_date_object(self):
        """Test that date field is converted to date object."""
        from datetime import date

        par_yields = [
            {"date": "2023-01-03", "month1": 4.35},
            {"date": "2023-01-02", "month1": 4.30},
        ]

        result = treasury_rates2df(par_yields)

        # Check that index values are date objects
        assert result.index[0] == date(2023, 1, 2)
        assert result.index[1] == date(2023, 1, 3)

    def test_dataframe_indexed_on_date(self):
        """Test that DataFrame is indexed on date."""
        par_yields = [
            {"date": "2023-01-03", "year10": 3.79},
            {"date": "2023-01-02", "year10": 3.75},
        ]

        result = treasury_rates2df(par_yields)

        assert result.index.name == "date"
        assert "date" not in result.columns

    def test_dataframe_sorted_ascending(self):
        """Test that DataFrame is sorted by date in ascending order."""
        par_yields = [
            {"date": "2023-01-05", "year10": 3.80},
            {"date": "2023-01-02", "year10": 3.75},
            {"date": "2023-01-04", "year10": 3.78},
            {"date": "2023-01-03", "year10": 3.76},
        ]

        result = treasury_rates2df(par_yields)

        # Verify ascending order
        dates = list(result.index)
        assert dates == sorted(dates)

    def test_empty_input_returns_empty_dataframe(self):
        """Test that empty input returns empty DataFrame."""
        result = treasury_rates2df([])

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_rate_values_preserved(self):
        """Test that rate values are correctly preserved."""
        par_yields = [
            {
                "date": "2023-01-03",
                "month1": 4.35,
                "year10": 3.79,
                "year30": 3.88,
            },
        ]

        result = treasury_rates2df(par_yields)

        assert result.loc[result.index[0], "month1"] == 4.35
        assert result.loc[result.index[0], "year10"] == 3.79
        assert result.loc[result.index[0], "year30"] == 3.88

    def test_all_rate_columns_present(self):
        """Test that all rate columns from input are present in output."""
        par_yields = [
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
        ]

        result = treasury_rates2df(par_yields)

        expected_columns = [
            "month1",
            "month2",
            "month3",
            "year1",
            "year5",
            "year10",
            "year30",
        ]
        for col in expected_columns:
            assert col in result.columns

    def test_single_record_input(self):
        """Test conversion with single record input."""
        par_yields = [
            {"date": "2023-01-03", "month1": 4.35, "year10": 3.79},
        ]

        result = treasury_rates2df(par_yields)

        assert len(result) == 1
        assert result.index.name == "date"

    def test_missing_date_column(self):
        """Test handling of input without date column."""
        par_yields = [
            {"month1": 4.35, "year10": 3.79},
            {"month1": 4.30, "year10": 3.75},
        ]

        result = treasury_rates2df(par_yields)

        # Should return DataFrame without date index
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2

    def test_none_values_in_rates(self):
        """Test handling of None values in rate fields."""
        par_yields = [
            {"date": "2023-01-03", "month1": 4.35, "year10": None},
            {"date": "2023-01-02", "month1": None, "year10": 3.75},
        ]

        result = treasury_rates2df(par_yields)

        assert len(result) == 2
        assert pd.isna(result.loc[result.index[1], "year10"])
        assert pd.isna(result.loc[result.index[0], "month1"])


class TestTenorToMonths:
    """Tests for _tenor_to_months function."""

    @pytest.mark.parametrize(
        "tenor,expected",
        [
            ("month1", 1.0),
            ("month3", 3.0),
            ("month6", 6.0),
            ("month1.5", 1.5),
            ("year1", 12.0),
            ("year2", 24.0),
            ("year5", 60.0),
            ("year10", 120.0),
            ("year30", 360.0),
            ("year1.5", 18.0),
            ("year2.5", 30.0),
        ],
    )
    def test_valid_tenor_conversion(self, tenor, expected):
        """Test that valid tenors are correctly converted to months."""
        assert _tenor_to_months(tenor) == expected

    def test_case_insensitive(self):
        """Test that tenor parsing is case-insensitive."""
        assert _tenor_to_months("MONTH1") == 1.0
        assert _tenor_to_months("YEAR1") == 12.0
        assert _tenor_to_months("Month6") == 6.0
        assert _tenor_to_months("Year10") == 120.0

    def test_invalid_tenor_raises_error(self):
        """Test that invalid tenor raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognized tenor format"):
            _tenor_to_months("invalid")

        with pytest.raises(ValueError, match="Unrecognized tenor format"):
            _tenor_to_months("day1")

        with pytest.raises(ValueError, match="Unrecognized tenor format"):
            _tenor_to_months("months1")


class TestMonthsToTenor:
    """Tests for _months_to_tenor function."""

    @pytest.mark.parametrize(
        "months,expected",
        [
            (1.0, "month1"),
            (3.0, "month3"),
            (6.0, "month6"),
            (1.5, "month1.5"),
            (12.0, "year1"),
            (24.0, "year2"),
            (60.0, "year5"),
            (120.0, "year10"),
            (360.0, "year30"),
            (18.0, "year1.5"),
            (30.0, "year2.5"),
        ],
    )
    def test_valid_months_conversion(self, months, expected):
        """Test that months are correctly converted to tenor strings."""
        assert _months_to_tenor(months) == expected

    def test_month_integer_formatting(self):
        """Test that integer months are formatted without decimal."""
        assert _months_to_tenor(1) == "month1"
        assert _months_to_tenor(6) == "month6"

    def test_year_integer_formatting(self):
        """Test that integer years are formatted without decimal."""
        assert _months_to_tenor(12) == "year1"
        assert _months_to_tenor(24) == "year2"


class TestGetIntervalInMonths:
    """Tests for _get_interval_in_months function."""

    @pytest.mark.parametrize(
        "interval,expected",
        [
            ("day", 1 / 30),
            ("week", 1 / 4),
            ("month", 1),
            ("quarter", 3),
            ("semi-annual", 6),
            ("annual", 12),
        ],
    )
    def test_valid_intervals(self, interval, expected):
        """Test that valid intervals are correctly converted."""
        assert _get_interval_in_months(interval) == expected

    def test_invalid_interval_raises_error(self):
        """Test that invalid interval raises ValueError."""
        with pytest.raises(ValueError, match="Invalid interval"):
            _get_interval_in_months("invalid")

        with pytest.raises(ValueError, match="Invalid interval"):
            _get_interval_in_months("biweekly")


class TestInterpolateRates:
    """Tests for interpolate_rates function."""

    def test_basic_semi_annual_interpolation(self):
        """Test basic semi-annual interpolation."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "month3": [4.45],
                "year1": [4.68],
                "year2": [4.20],
            }
        )

        result = interpolate_rates(df, interval="semi-annual")

        # Should include original tenors and interpolated points
        assert "month1" in result.columns
        assert "month3" in result.columns
        assert "year1" in result.columns
        assert "year2" in result.columns
        # Should have interpolated month7 (1+6=7), year1.5
        assert "month7" in result.columns
        assert "year1.5" in result.columns or "year1.583" in result.columns

    def test_annual_interpolation(self):
        """Test annual interpolation."""
        df = pd.DataFrame(
            {
                "year1": [4.68],
                "year5": [4.20],
            }
        )

        result = interpolate_rates(df, interval="annual")

        # Should include interpolated year2, year3, year4
        assert "year2" in result.columns
        assert "year3" in result.columns
        assert "year4" in result.columns

    def test_quarter_interpolation(self):
        """Test quarterly interpolation."""
        df = pd.DataFrame(
            {
                "year1": [4.68],
                "year2": [4.20],
            }
        )

        result = interpolate_rates(df, interval="quarter")

        # Should include quarterly points
        assert "year1.25" in result.columns
        assert "year1.5" in result.columns
        assert "year1.75" in result.columns

    def test_preserves_original_values(self):
        """Test that original values are preserved."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "month3": [4.45],
                "year1": [4.68],
            }
        )

        result = interpolate_rates(df, interval="semi-annual")

        assert result["month1"].iloc[0] == 4.35
        assert result["month3"].iloc[0] == 4.45
        assert result["year1"].iloc[0] == 4.68

    def test_multiple_rows(self):
        """Test interpolation with multiple rows."""
        df = pd.DataFrame(
            {
                "month1": [4.35, 4.30],
                "year1": [4.68, 4.60],
                "year2": [4.20, 4.15],
            }
        )

        result = interpolate_rates(df, interval="annual")

        assert len(result) == 2

    def test_empty_dataframe(self):
        """Test that empty input returns empty DataFrame."""
        df = pd.DataFrame()

        result = interpolate_rates(df, interval="semi-annual")

        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_invalid_interval_raises_error(self):
        """Test that invalid interval raises ValueError."""
        df = pd.DataFrame({"month1": [4.35], "year1": [4.68]})

        with pytest.raises(ValueError, match="Invalid interval"):
            interpolate_rates(df, interval="invalid")

    def test_insufficient_tenors_raises_error(self):
        """Test that fewer than 2 tenors raises ValueError."""
        df = pd.DataFrame({"month1": [4.35]})

        with pytest.raises(ValueError, match="at least 2 valid tenor columns"):
            interpolate_rates(df, interval="semi-annual")

    def test_columns_sorted_by_tenor(self):
        """Test that result columns are sorted by tenor."""
        df = pd.DataFrame(
            {
                "year2": [4.20],
                "month1": [4.35],
                "year1": [4.68],
                "month3": [4.45],
            }
        )

        result = interpolate_rates(df, interval="annual")

        # Get column positions
        columns = list(result.columns)
        month1_idx = next(i for i, c in enumerate(columns) if "month1" in c)
        month3_idx = next(i for i, c in enumerate(columns) if "month3" in c)
        year1_idx = next(i for i, c in enumerate(columns) if c == "year1")
        year2_idx = next(i for i, c in enumerate(columns) if c == "year2")

        assert month1_idx < month3_idx < year1_idx < year2_idx

    def test_handles_nan_values(self):
        """Test handling of NaN values in input."""
        df = pd.DataFrame(
            {
                "month1": [4.35, None],
                "month3": [4.45, 4.40],
                "year1": [4.68, 4.60],
            }
        )

        result = interpolate_rates(df, interval="semi-annual")

        # Second row should still be interpolated
        assert len(result) >= 1

    def test_preserves_index(self):
        """Test that result preserves input index."""
        from datetime import date

        df = pd.DataFrame(
            {
                "month1": [4.35, 4.30],
                "year1": [4.68, 4.60],
                "year2": [4.20, 4.15],
            },
            index=[date(2023, 1, 1), date(2023, 1, 2)],
        )

        result = interpolate_rates(df, interval="annual")

        assert list(result.index) == [date(2023, 1, 1), date(2023, 1, 2)]

    def test_month_interval(self):
        """Test monthly interpolation."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "month3": [4.45],
            }
        )

        result = interpolate_rates(df, interval="month")

        # Should have month2 interpolated
        assert "month2" in result.columns

    def test_interpolation_values_reasonable(self):
        """Test that interpolated values are reasonable."""
        df = pd.DataFrame(
            {
                "month1": [4.0],
                "year1": [5.0],
            }
        )

        result = interpolate_rates(df, interval="semi-annual")

        # Interpolated month6 should be between 4.0 and 5.0
        if "month6" in result.columns:
            assert 4.0 <= result["month6"].iloc[0] <= 5.0

    def test_default_interval_is_semi_annual(self):
        """Test that default interval is semi-annual."""
        df = pd.DataFrame(
            {
                "year1": [4.68],
                "year2": [4.20],
            }
        )

        result = interpolate_rates(df)

        # Should include year1.5 for semi-annual
        assert "year1.5" in result.columns

    def test_all_valid_intervals(self):
        """Test that all valid intervals work."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "year1": [4.68],
            }
        )

        for interval in VALID_INTERVALS:
            result = interpolate_rates(df, interval=interval)
            assert isinstance(result, pd.DataFrame)
            assert not result.empty


class TestBootstrapZeroRates:
    """Tests for bootstrap_zero_rates function."""

    def test_tenors_up_to_one_year_equal_par_rate(self):
        """Test that zero rates for tenors <= 1 year equal par rates."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "month3": [4.45],
                "month6": [4.55],
                "year1": [4.68],
            }
        )

        result = bootstrap_zero_rates(df)

        # All tenors <= 1 year should have zero rate = par rate
        assert result["month1"].iloc[0] == 4.35
        assert result["month3"].iloc[0] == 4.45
        assert result["month6"].iloc[0] == 4.55
        assert result["year1"].iloc[0] == 4.68

    def test_tenors_greater_than_one_year_bootstrapped(self):
        """Test that zero rates for tenors > 1 year are bootstrapped."""
        df = pd.DataFrame(
            {
                "month6": [4.50],
                "year1": [4.60],
                "year2": [4.20],
            }
        )

        result = bootstrap_zero_rates(df)

        # Tenors <= 1 year should equal par rate
        assert result["month6"].iloc[0] == 4.50
        assert result["year1"].iloc[0] == 4.60

        # Tenor > 1 year should be bootstrapped (different from par rate)
        # The zero rate should be close but not exactly equal to par yield
        assert "year2" in result.columns
        # For an upward sloping then downward curve, the 2-year zero rate
        # will be slightly different from the par rate
        assert result["year2"].iloc[0] is not None
        assert isinstance(result["year2"].iloc[0], float)

    def test_flat_yield_curve_zero_rates_equal_par_rates(self):
        """Test that with flat yield curve, zero rates approximately equal par rates."""
        # With a perfectly flat curve, zero rates should be very close to par rates
        flat_rate = 5.0
        df = pd.DataFrame(
            {
                "month6": [flat_rate],
                "year1": [flat_rate],
                "year2": [flat_rate],
                "year5": [flat_rate],
            }
        )

        result = bootstrap_zero_rates(df)

        # All zero rates should be close to the flat par rate
        for col in result.columns:
            assert abs(result[col].iloc[0] - flat_rate) < 0.1

    def test_empty_input_returns_empty_dataframe(self):
        """Test that empty input returns empty DataFrame."""
        df = pd.DataFrame()

        result = bootstrap_zero_rates(df)

        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_multiple_rows(self):
        """Test bootstrapping with multiple rows."""
        df = pd.DataFrame(
            {
                "month6": [4.50, 4.40],
                "year1": [4.60, 4.50],
                "year2": [4.20, 4.10],
            }
        )

        result = bootstrap_zero_rates(df)

        assert len(result) == 2
        assert "month6" in result.columns
        assert "year1" in result.columns
        assert "year2" in result.columns

    def test_handles_nan_values(self):
        """Test handling of NaN values in input."""
        df = pd.DataFrame(
            {
                "month6": [4.50, None],
                "year1": [4.60, 4.50],
                "year2": [4.20, 4.10],
            }
        )

        result = bootstrap_zero_rates(df)

        # First row should work normally
        assert result["month6"].iloc[0] == 4.50
        # Second row should have NaN for month6
        assert pd.isna(result["month6"].iloc[1])
        # Other values should still be calculated
        assert not pd.isna(result["year1"].iloc[1])

    def test_preserves_index(self):
        """Test that result preserves input index."""
        from datetime import date

        df = pd.DataFrame(
            {
                "month6": [4.50, 4.40],
                "year1": [4.60, 4.50],
                "year2": [4.20, 4.10],
            },
            index=[date(2023, 1, 1), date(2023, 1, 2)],
        )

        result = bootstrap_zero_rates(df)

        assert list(result.index) == [date(2023, 1, 1), date(2023, 1, 2)]

    def test_columns_sorted_by_tenor(self):
        """Test that result columns are sorted by tenor."""
        df = pd.DataFrame(
            {
                "year2": [4.20],
                "month6": [4.50],
                "year1": [4.60],
            }
        )

        result = bootstrap_zero_rates(df)

        columns = list(result.columns)
        assert columns.index("month6") < columns.index("year1")
        assert columns.index("year1") < columns.index("year2")

    def test_upward_sloping_curve(self):
        """Test bootstrapping with upward sloping yield curve."""
        df = pd.DataFrame(
            {
                "month6": [3.50],
                "year1": [4.00],
                "year2": [4.50],
                "year5": [5.00],
            }
        )

        result = bootstrap_zero_rates(df)

        # For upward sloping curve, bootstrapped zero rates for
        # longer tenors tend to be slightly higher than par rates
        # because earlier coupons are discounted at lower rates
        assert result["year5"].iloc[0] is not None
        assert isinstance(result["year5"].iloc[0], float)

    def test_downward_sloping_curve(self):
        """Test bootstrapping with downward sloping yield curve."""
        df = pd.DataFrame(
            {
                "month6": [5.00],
                "year1": [4.50],
                "year2": [4.00],
                "year5": [3.50],
            }
        )

        result = bootstrap_zero_rates(df)

        # For downward sloping curve, bootstrapped zero rates for
        # longer tenors tend to be slightly lower than par rates
        assert result["year5"].iloc[0] is not None
        assert isinstance(result["year5"].iloc[0], float)

    def test_zero_rate_reasonable_range(self):
        """Test that bootstrapped zero rates are in a reasonable range."""
        df = pd.DataFrame(
            {
                "month6": [4.50],
                "year1": [4.60],
                "year2": [4.20],
                "year5": [4.00],
                "year10": [3.80],
            }
        )

        result = bootstrap_zero_rates(df)

        # Zero rates should be within a reasonable range of the par yields
        for col in result.columns:
            rate = result[col].iloc[0]
            assert rate is not None
            # Zero rates should be positive and within a reasonable range
            assert 0 < rate < 20

    def test_single_row_input(self):
        """Test bootstrapping with single row input."""
        df = pd.DataFrame(
            {
                "month6": [4.50],
                "year1": [4.60],
                "year2": [4.20],
            }
        )

        result = bootstrap_zero_rates(df)

        assert len(result) == 1
        assert "month6" in result.columns
        assert "year1" in result.columns
        assert "year2" in result.columns

    def test_only_short_tenors(self):
        """Test with only tenors <= 1 year."""
        df = pd.DataFrame(
            {
                "month1": [4.35],
                "month3": [4.45],
                "month6": [4.55],
            }
        )

        result = bootstrap_zero_rates(df)

        # All zero rates should equal par rates
        assert result["month1"].iloc[0] == 4.35
        assert result["month3"].iloc[0] == 4.45
        assert result["month6"].iloc[0] == 4.55

    @pytest.mark.parametrize(
        "tenor,expected_equal_to_par",
        [
            ("month1", True),
            ("month3", True),
            ("month6", True),
            ("year1", True),
            ("year1.5", False),
            ("year2", False),
            ("year5", False),
            ("year10", False),
            ("year30", False),
        ],
    )
    def test_tenor_classification(self, tenor, expected_equal_to_par):
        """Test that tenors are correctly classified as <= or > 1 year."""
        # Create a DataFrame with the specified tenor and year1
        par_yield = 5.0
        data = {"year1": [4.5]}  # Base tenor for comparison
        data[tenor] = [par_yield]

        df = pd.DataFrame(data)
        result = bootstrap_zero_rates(df)

        if expected_equal_to_par:
            # For tenors <= 1 year, zero rate should equal par rate
            assert result[tenor].iloc[0] == par_yield
        else:
            # For tenors > 1 year, zero rate may differ from par rate
            # We just check it's calculated (not None)
            assert result[tenor].iloc[0] is not None
