"""
duk - A CLI tool and library for downloading markets and financial data.
"""

from duk.date_utils import DateRangeError, get_api_date_range
from duk.fmp_api import FMPAPIError, get_price_history, price_history_api, screener_api
from duk.indicators import (
    IndicatorCalculationError,
    calculate_ema,
    calculate_sma,
)

__version__ = "1.1.0"

__all__ = [
    "__version__",
    "get_api_date_range",
    "DateRangeError",
    "FMPAPIError",
    "price_history_api",
    "screener_api",
    "get_price_history",
    "calculate_sma",
    "calculate_ema",
    "IndicatorCalculationError",
]
