"""
duk - A CLI tool and library for downloading markets and financial data.

The library names below are loaded when first used (PEP 562), not when ``duk``
is imported: the CLI is a process per command, and importing them pulled in
pandas and the vendor HTTP clients for commands that use neither.
"""

from fafnir import __version__  # one repository, one version -- see ADR 0011

#: Each re-exported name and the module that defines it.
_EXPORTS = {
    "get_api_date_range": "duk.date_utils",
    "DateRangeError": "duk.date_utils",
    "FMPAPIError": "duk.fmp_api",
    "price_history_api": "duk.fmp_api",
    "screener_api": "duk.fmp_api",
    "get_price_history": "duk.fmp_api",
    "calculate_sma": "duk.indicators",
    "calculate_ema": "duk.indicators",
    "IndicatorCalculationError": "duk.indicators",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'duk' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
