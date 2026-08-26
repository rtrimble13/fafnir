"""
Tests for date_utils module.
"""

from datetime import date, timedelta

import pytest

from duk.date_utils import DateRangeError, calendar_span, get_api_date_range


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

        # 10 daily bars need ceil(10 * 1.5) + 10 = 25 calendar days of window.
        assert result_start == start
        assert result_end == date(2023, 1, 26)

    def test_start_date_and_limit_with_week_frequency(self):
        """Test case: start_date and limit with week frequency."""
        start = date(2023, 1, 1)
        limit = 4
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="week"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=(4 + 1) * 7)

    def test_start_date_and_limit_with_month_frequency(self):
        """Test case: start_date and limit with month frequency."""
        start = date(2023, 1, 1)
        limit = 3
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="month"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=(3 + 1) * 31)

    def test_start_date_and_limit_with_quarter_frequency(self):
        """Test case: start_date and limit with quarter frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="quarter"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=(2 + 1) * 92)

    def test_start_date_and_limit_with_semi_annual_frequency(self):
        """Test case: start_date and limit with semi-annual frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="semi-annual"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=(2 + 1) * 184)

    def test_start_date_and_limit_with_annual_frequency(self):
        """Test case: start_date and limit with annual frequency."""
        start = date(2023, 1, 1)
        limit = 2
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="annual"
        )

        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=(2 + 1) * 366)

    def test_end_date_and_limit_with_day_frequency(self):
        """Test case: end_date and limit with day frequency."""
        end = date(2023, 12, 31)
        limit = 10
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="day"
        )

        # Same 25-day window, measured backwards from the end date.
        assert result_start == date(2023, 12, 6)
        assert result_end == end

    def test_end_date_and_limit_with_week_frequency(self):
        """Test case: end_date and limit with week frequency."""
        end = date(2023, 12, 31)
        limit = 4
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="week"
        )

        assert result_start == date(2023, 12, 31) - timedelta(days=(4 + 1) * 7)
        assert result_end == end

    def test_end_date_and_limit_with_month_frequency(self):
        """Test case: end_date and limit with month frequency."""
        end = date(2023, 12, 31)
        limit = 3
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="month"
        )

        assert result_start == date(2023, 12, 31) - timedelta(days=(3 + 1) * 31)
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

        # 5 daily bars: ceil(5 * 1.5) + 10 = 18 calendar days.
        assert result_start == start
        assert result_end == date(2023, 1, 19)

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
        assert result_end == date(2020, 1, 1) + timedelta(days=(5 + 1) * 366)

    def test_date_range_calculation_accuracy(self):
        """Test case: verify date calculation accuracy."""
        # Test specific calculation: 10 months from Jan 1
        start = date(2023, 1, 1)
        limit = 10
        result_start, result_end = get_api_date_range(
            start_date=start, limit=limit, frequency="month"
        )

        # 10 monthly bars: (10 + 1) * 31 = 341 days
        assert result_start == start
        assert result_end == date(2023, 1, 1) + timedelta(days=341)

    def test_backward_date_calculation(self):
        """Test case: backward date calculation from end_date."""
        end = date(2023, 6, 30)
        limit = 6
        result_start, result_end = get_api_date_range(
            end_date=end, limit=limit, frequency="month"
        )

        # 6 monthly bars: (6 + 1) * 31 = 217 days back
        expected_start = date(2023, 6, 30) - timedelta(days=217)
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
        # End date should be the calculated value, not capped: start + the 25-day
        # window that holds 10 daily bars.
        assert result_end == date(2020, 1, 26)
        # Verify it doesn't exceed current date
        assert result_end <= date.today()


class TestCalendarSpan:
    """Tests for calendar_span: the window that holds `limit` trading bars."""

    def test_daily_span_exceeds_weekday_ratio(self):
        # 5 trading days per 7 calendar days is the floor before holidays; the
        # span must clear it for every size, not just large ones.
        for limit in (1, 5, 21, 252, 2520):
            assert calendar_span(limit, "day") >= limit * 7 / 5

    def test_daily_span_covers_holiday_clusters_for_small_limits(self):
        # A 5-bar request over Christmas week must not be swallowed by the pad.
        assert calendar_span(5, "day") == 18

    def test_span_includes_one_spare_period(self):
        assert calendar_span(4, "week") == 5 * 7
        assert calendar_span(3, "month") == 4 * 31
        assert calendar_span(2, "quarter") == 3 * 92
        assert calendar_span(2, "semi-annual") == 3 * 184
        assert calendar_span(2, "annual") == 3 * 366

    def test_span_is_monotonic(self):
        for freq in ("day", "week", "month", "quarter", "semi-annual", "annual"):
            spans = [calendar_span(n, freq) for n in range(1, 60)]
            assert spans == sorted(spans)

    def test_non_positive_limit_has_no_span(self):
        assert calendar_span(0, "day") == 0
        assert calendar_span(-3, "week") == 0


class TestLimitCountsTradingBars:
    """`-n N` means N bars, so the window must outrun N calendar periods."""

    def test_daily_window_is_wider_than_the_bar_count(self):
        start = date(1990, 1, 1)
        _, result_end = get_api_date_range(start_date=start, limit=5, frequency="day")

        # The reported bug: a 5-calendar-day window over the New Year holiday and
        # two weekends holds only 4 trading days.
        assert (result_end - start).days > 5

    def test_end_anchored_window_is_wider_than_the_bar_count(self):
        end = date(2023, 12, 31)
        result_start, _ = get_api_date_range(end_date=end, limit=5, frequency="day")

        assert (end - result_start).days > 5

    def test_limit_only_window_is_wider_than_the_bar_count(self):
        result_start, result_end = get_api_date_range(limit=5, frequency="day")

        assert (result_end - result_start).days > 5
