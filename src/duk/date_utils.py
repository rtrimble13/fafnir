"""
Date utilities for processing and calculating date ranges.

This module provides functions for determining date parameters for API calls,
particularly for historical price data APIs.
"""

import logging
from datetime import date, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class DateRangeError(Exception):
    """Exception raised for date range errors."""

    pass


# Calendar days that reliably contain one period of each frequency. ``limit`` counts
# *trading* periods (bars), but both the FMP API and the warehouse are queried by
# calendar date, so the window has to be widened before it is requested: markets are
# shut on weekends and holidays, and real months/quarters/years run longer than the
# round numbers used to name them. Callers trim the fetched frame back to exactly
# ``limit`` rows (head/tail), so over-shooting here costs a little I/O, never accuracy
# -- under-shooting silently returns fewer bars than the user asked for.
_CALENDAR_DAYS_PER_PERIOD = {
    "week": 7,
    "month": 31,
    "quarter": 92,
    "semi-annual": 184,
    "annual": 366,
}

# Daily bars: ~252 trading days per 365 calendar days (a ratio of 1.449 calendar days
# per bar). 1.5 clears that with ~3.5% headroom -- more than the ~2.5% of weekdays lost
# to holidays -- and the flat pad covers holiday clusters that dominate a small limit
# (a Thanksgiving or Christmas week swallowing most of a 5-bar request).
_TRADING_DAY_RATIO_NUM = 3
_TRADING_DAY_RATIO_DEN = 2
_TRADING_DAY_PAD = 10


def calendar_span(limit: int, frequency: str) -> int:
    """Calendar days wide enough to contain ``limit`` trading periods.

    Args:
        limit: Number of bars requested.
        frequency: One of the keys accepted by :func:`get_api_date_range`.

    Returns:
        A day count, 0 when ``limit`` is non-positive.
    """
    if limit <= 0:
        return 0
    if frequency == "day":
        span = -(-limit * _TRADING_DAY_RATIO_NUM // _TRADING_DAY_RATIO_DEN)  # ceil
        return span + _TRADING_DAY_PAD
    # One spare period absorbs the partial bucket at whichever end the range is
    # anchored to (a window starting mid-week yields limit+1 weekly buckets, the
    # first of them short).
    return (limit + 1) * _CALENDAR_DAYS_PER_PERIOD[frequency]


def get_api_date_range(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: Optional[int] = None,
    frequency: str = "day",
) -> Tuple[Optional[date], Optional[date]]:
    """
    Calculate date range parameters for API calls.

    This function determines the start and end dates for retrieving historical data
    based on the provided parameters. It handles various combinations of start_date,
    end_date, limit, and frequency to calculate the appropriate date range.

    Args:
        start_date: Optional start date for the range
        end_date: Optional end date for the range
        limit: Optional number of *bars* (trading periods) to include. The
            returned window is widened past limit calendar periods so that it
            actually contains limit bars once weekends and holidays are removed;
            callers trim to exactly limit rows after fetching. See
            :func:`calendar_span`.
        frequency: Frequency of data points. Valid values are:
            'day', 'week', 'month', 'quarter', 'semi-annual', 'annual'.
            Default is 'day'.

    Returns:
        Tuple containing (start_date, end_date). Either value may be None.

    Raises:
        ValueError: If frequency is invalid
        DateRangeError: If start_date, end_date, and limit are all provided

    Examples:
        >>> # Only start_date provided
        >>> get_api_date_range(start_date=date(2023, 1, 1))
        (date(2023, 1, 1), date(2025, 11, 23))

        >>> # Only end_date provided
        >>> get_api_date_range(end_date=date(2023, 12, 31))
        (None, date(2023, 12, 31))

        >>> # start_date and limit provided (25 calendar days holds 10 bars)
        >>> get_api_date_range(start_date=date(2023, 1, 1), limit=10, frequency='day')
        (date(2023, 1, 1), date(2023, 1, 26))

        >>> # end_date and limit provided
        >>> get_api_date_range(end_date=date(2023, 12, 31), limit=10, frequency='day')
        (date(2023, 12, 6), date(2023, 12, 31))

        >>> # Only limit provided
        >>> get_api_date_range(limit=30)
        (date(2025, 9, 29), date(2025, 11, 23))

        >>> # All three provided (raises error)
        >>> get_api_date_range(
        ...     start_date=date(2023, 1, 1),
        ...     end_date=date(2023, 12, 31),
        ...     limit=10
        ... )
        Traceback (most recent call last):
            ...
        DateRangeError: Cannot specify start_date, end_date, and limit together
    """
    valid_frequencies = ("day", "week", "month", "quarter", "semi-annual", "annual")

    if frequency not in valid_frequencies:
        valid_freqs = ", ".join(valid_frequencies)
        raise ValueError(
            f"Invalid frequency '{frequency}'. Must be one of: {valid_freqs}"
        )

    # Calendar width of the requested bar count (0 when limit is None/non-positive).
    span_days = calendar_span(limit, frequency) if limit is not None else 0

    # Get current date for calculations
    current_date = date.today()

    # Case: start_date, end_date, and limit are all provided (error)
    if start_date is not None and end_date is not None and limit is not None:
        raise DateRangeError("Cannot specify start_date, end_date, and limit together")

    # Case: only start_date is provided
    if start_date is not None and end_date is None and limit is None:
        logger.debug(f"Returning range from {start_date} to {current_date}")
        return (start_date, current_date)

    # Case: only end_date is provided
    if start_date is None and end_date is not None and limit is None:
        logger.debug(f"Returning range from None to {end_date}")
        return (None, end_date)

    # Case: only start_date and end_date are provided
    if start_date is not None and end_date is not None and limit is None:
        logger.debug(f"Returning range from {start_date} to {end_date}")
        return (start_date, end_date)

    # Case: only start_date and limit are provided
    if start_date is not None and end_date is None and limit is not None:
        calculated_end = start_date + timedelta(days=span_days)
        # Cap the end date at current_date to avoid requesting future data
        if calculated_end > current_date:
            calculated_end = current_date
            logger.debug(
                f"Capped end date to current date {current_date} "
                f"(original calculation would exceed current date)"
            )
        logger.debug(
            f"Calculated end date {calculated_end} from start {start_date} "
            f"+ {span_days} days (window for {limit} {frequency} bars)"
        )
        return (start_date, calculated_end)

    # Case: only end_date and limit are provided
    if start_date is None and end_date is not None and limit is not None:
        calculated_start = end_date - timedelta(days=span_days)
        logger.debug(
            f"Calculated start date {calculated_start} from end {end_date} "
            f"- {span_days} days (window for {limit} {frequency} bars)"
        )
        return (calculated_start, end_date)

    # Case: only limit is provided
    if start_date is None and end_date is None and limit is not None:
        calculated_start = current_date - timedelta(days=span_days)
        logger.debug(
            f"Calculated range from {calculated_start} to {current_date} "
            f"({span_days}-day window for {limit} {frequency} bars)"
        )
        return (calculated_start, current_date)

    # Case: no parameters provided (should not happen, but handle gracefully)
    logger.debug("No parameters provided, returning (None, None)")
    return (None, None)
