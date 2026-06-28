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
        limit: Optional number of periods to include
        frequency: Frequency of data points. Valid values are:
            'day' (1 day), 'week' (7 days), 'month' (30 days),
            'quarter' (90 days), 'semi-annual' (180 days), 'annual' (365 days).
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

        >>> # start_date and limit provided
        >>> get_api_date_range(start_date=date(2023, 1, 1), limit=10, frequency='day')
        (date(2023, 1, 1), date(2023, 1, 11))

        >>> # end_date and limit provided
        >>> get_api_date_range(end_date=date(2023, 12, 31), limit=10, frequency='day')
        (date(2023, 12, 21), date(2023, 12, 31))

        >>> # Only limit provided
        >>> get_api_date_range(limit=30)
        (date(2025, 10, 24), date(2025, 11, 23))

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
    # Validate frequency and convert to day multiplier
    frequency_map = {
        "day": 1,
        "week": 7,
        "month": 30,
        "quarter": 90,
        "semi-annual": 180,
        "annual": 365,
    }

    if frequency not in frequency_map:
        valid_freqs = ", ".join(frequency_map.keys())
        raise ValueError(
            f"Invalid frequency '{frequency}'. Must be one of: {valid_freqs}"
        )

    frequency_multiplier = frequency_map[frequency]

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
        calculated_end = start_date + timedelta(days=limit * frequency_multiplier)
        # Cap the end date at current_date to avoid requesting future data
        if calculated_end > current_date:
            calculated_end = current_date
            logger.debug(
                f"Capped end date to current date {current_date} "
                f"(original calculation would exceed current date)"
            )
        logger.debug(
            f"Calculated end date {calculated_end} from start {start_date} "
            f"+ {limit} * {frequency_multiplier} days"
        )
        return (start_date, calculated_end)

    # Case: only end_date and limit are provided
    if start_date is None and end_date is not None and limit is not None:
        calculated_start = end_date - timedelta(days=limit * frequency_multiplier)
        logger.debug(
            f"Calculated start date {calculated_start} from end {end_date} "
            f"- {limit} * {frequency_multiplier} days"
        )
        return (calculated_start, end_date)

    # Case: only limit is provided
    if start_date is None and end_date is None and limit is not None:
        calculated_start = current_date - timedelta(
            days=limit * frequency_multiplier * 2
        )
        logger.debug(
            f"Calculated range from {calculated_start} to {current_date} "
            f"using limit {limit} and frequency {frequency}"
        )
        return (calculated_start, current_date)

    # Case: no parameters provided (should not happen, but handle gracefully)
    logger.debug("No parameters provided, returning (None, None)")
    return (None, None)
