"""
Tests for date_utils module.
"""

from datetime import date, timedelta

import pytest

from duk.date_utils import DateRangeError, get_api_date_range


class TestGetApiDateRange:
    """Tests for get_api_date_range function."""

    def test_only_start_date_provided(self):
        """Test case: only start_date is provided."""
        start = date(2023, 1, 1)
        result_start, result_end = get_api_date_range(start_date=start)

        assert result_start == start
        assert result_end == date.today()

    def test_only_end_date_provided(self):
        """Test case: only end_date is provided."""
        end = date(2023, 12, 31)
        result_start, result_end = get_api_date_range(end_date=end)

        assert result_start is None
        assert result_end == end

    def test_start_date_and_end_date_provided(self):
        """Test case: both start_date and end_date are provided."""
        start = date(2023, 1, 1)
        end = date(2023, 12, 31)
        result_start, result_end = get_api_date_range(start_date=start, end_date=end)

        assert result_start == start
        assert result_end == end

    def test_start_date_and_limit_with_day_frequency(self):
        """Test case: start_date and limit with day frequency."""
        start = date(2023, 1, 1)
        limit = 10
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="day"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 11)

    def test_start_date_and_limit_with_week_frequency(self):
        """Test case: start_date and limit with week frequency."""
        start = date(2023, 1, 1)
        limit = 4
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="week"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=4 * 7)

    def test_start_date_and_limit_with_month_frequency(self):
        """Test case: start_date and limit with month frequency."""
        start = date(2023, 1, 1)
        limit = 3
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="month"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=3 * 30)

    def test_start_date_and_limit_with_quarter_frequency(self):
        """Test case: start_date and limit with quarter frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="quarter"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=2 * 90)

    def test_start_date_and_limit_with_semi_annual_frequency(self):
        """Test case: start_date and limit with semi-annual frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="semi-annual"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=2 * 180)

    def test_start_date_and_limit_with_annual_frequency(self):
        """Test case: start_date and limit with annual frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="annual"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=2 * 365)

    def test_end_date_and_limit_with_day_frequency(self):
        """Test case: end_date and limit with day frequency."""
        end = date(2023, 12, 31)
        limit = 10
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="day"
        )

        assert result_start == date(2023, 12, 21)
        assert result_end == end

    def test_end_date_and_limit_with_week_frequency(self):
        """Test case: end_date and limit with week frequency."""
        end = date(2023, 12, 31)
        limit = 4
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="week"
        )

        assert result_start == date(2023, 12, 31) - timedelta(days=4 * 7)
        assert result_end == end

    def test_end_date_and_limit_with_month_frequency(self):
        """Test case: end_date and limit with month frequency."""
        end = date(2023, 12, 31)
        limit = 3
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="month"
        )

        assert result_start == date(2023, 12, 31) - timedelta(days=3 * 30)
        assert result_end == end

    def test_only_limit_provided(self):
        """Test case: only limit is provided."""
        limit = 30
        result_start, result_end = get_api_date_range(limit=limit, frequency="day")

        today = date.today()
        expected_start = today - timedelta(days=30)

        assert result_start <= expected_start
        assert result_end == today

    def test_only_limit_with_week_frequency(self):
        """Test case: only limit with week frequency."""
        limit = 12
        result_start, result_end = get_api_date_range(limit=limit, frequency="week")

        today = date.today()
        expected_start = today - timedelta(days=12 * 7)

        assert result_start <= expected_start
        assert result_end == today

    def test_all_three_parameters_raises_error(self):
        """Test case: providing all three parameters raises error."""
        start = date(2023, 1, 1)
        end = date(2023, 12, 31)
        limit = 10

        with pytest.raises(
            DateRangeError, match="Cannot specify start_date, end_date, and limit"
        ):
            get_api_date_range(start_date=start, end_date=end, limit=limit)

    def test_invalid_frequency_raises_error(self):
        """Test case: invalid frequency raises ValueError."""
        with pytest.raises(ValueError, match="Invalid frequency"):
            get_api_date_range(limit=10, frequency="invalid")

    def test_invalid_frequency_error_message_includes_valid_frequencies(self):
        """Test case: error message includes valid frequency options."""
        with pytest.raises(ValueError) as excinfo:
            get_api_date_range(limit=10, frequency="biweekly")

        error_message = str(excinfo.value)
        assert "day" in error_message
        assert "week" in error_message
        assert "month" in error_message
        assert "quarter" in error_message
        assert "semi-annual" in error_message
        assert "annual" in error_message

    def test_no_parameters_provided(self):
        """Test case: no parameters provided returns (None, None)."""
        result_start, result_end = get_api_date_range()

        assert result_start is None
        assert result_end is None

    def test_default_frequency_is_day(self):
        """Test case: default frequency is 'day'."""
        start = date(2023, 1, 1)
        limit = 5
        result_start, result_end = get_api_date_range(start_date=start, limit=limit)

        assert result_start == start
        assert result_end == date(2023, 1, 6)

    def test_limit_zero_with_start_date(self):
        """Test case: limit of 0 with start_date."""
        start = date(2023, 1, 1)
        limit = 0
        result_start, result_end = get_api_date_range(start_date=start, limit=limit)

        assert result_start == start
        assert result_end == start

    def test_limit_zero_with_end_date(self):
        """Test case: limit of 0 with end_date."""
        end = date(2023, 12, 31)
        limit = 0
        result_start, result_end = get_api_date_range(end_date=end, limit=limit)

        assert result_start == end
        assert result_end == end

    def test_large_limit_with_annual_frequency(self):
        """Test case: large limit with annual frequency."""
        start = date(2020, 1, 1)
        limit = 5
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="annual"
        )

        assert result_start == start
        assert result_end == date(2020, 1, 1) + timedelta(days=5 * 365)

    def test_date_range_calculation_accuracy(self):
        """Test case: verify date calculation accuracy."""
        # Test specific calculation: 10 months from Jan 1
        start = date(2023, 1, 1)
        limit = 10
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="month"
        )

        # 10 * 30 = 300 days
        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=300)

    def test_backward_date_calculation(self):
        """Test case: backward date calculation from end_date."""
        end = date(2023, 6, 30)
        limit = 6
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="month"
        )

        # 6 * 30 = 180 days back
        expected_start = date(2023, 6, 30) - timedelta(days=180)
        assert result_start == expected_start
        assert result_end == end

    def test_start_date_and_limit_capped_at_current_date(self):
        """Test case: calculated end date is capped at current date."""
        # Use a start date in the recent past
        start = date.today() - timedelta(days=5)
        # Request a large limit that would exceed current date
        limit = 100
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="day"
        )

        assert result_start == start
        # End date should be capped at current date
        assert result_end == date.today()
        # Verify it doesn't exceed current date
        assert result_end <= date.today()

    def test_start_date_and_limit_not_capped_when_within_range(self):
        """Test case: calculated end date is not capped when within valid range."""
        # Use a start date far in the past
        start = date(2020, 1, 1)
        limit = 10
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="day"
        )

        assert result_start == start
        # End date should be the calculated value, not capped
        assert result_end == date(2020, 1, 11)
        # Verify it doesn't exceed current date
        assert result_end <= date.today()
