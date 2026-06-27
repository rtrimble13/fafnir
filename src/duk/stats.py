"""
Summary statistics module for duk.

This module provides functions to compute and format summary statistics
for financial data, including date ranges and column-wise statistics.
"""

import logging

import pandas as pd


def compute_summary_stats(df, date_column="date"):
    """
    Compute summary statistics for a DataFrame.

    Args:
        df: pandas DataFrame with financial data
        date_column: Name of the date column (default: "date")

    Returns:
        dict: Dictionary containing summary statistics with keys:
            - n_observations: Number of observations
            - min_date: Earliest date
            - max_date: Latest date
            - column_stats: Dict of column-level statistics
    """
    logger = logging.getLogger("duk")

    if df.empty:
        logger.warning("Cannot compute statistics on empty DataFrame")
        return None

    stats = {}

    # Number of observations
    stats["n_observations"] = len(df)

    # Date range statistics
    if date_column in df.columns:
        date_col = df[date_column]
    elif date_column in df.index.names or (df.index.name == date_column):
        date_col = df.index
    else:
        # Try to find a date-like column
        date_col = None
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                date_col = df[col]
                break

        # If still not found, check index
        if date_col is None and pd.api.types.is_datetime64_any_dtype(df.index):
            date_col = df.index

    if date_col is not None:
        try:
            stats["min_date"] = pd.to_datetime(date_col).min()
            stats["max_date"] = pd.to_datetime(date_col).max()
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not compute date range: {e}")
            stats["min_date"] = None
            stats["max_date"] = None
    else:
        stats["min_date"] = None
        stats["max_date"] = None

    # Column-level statistics
    numeric_columns = df.select_dtypes(include=["number"]).columns
    column_stats = {}

    for col in numeric_columns:
        col_data = df[col].dropna()  # Remove NaN values for statistics

        if len(col_data) > 0:
            column_stats[col] = {
                "min": col_data.min(),
                "max": col_data.max(),
                "mean": col_data.mean(),
                "median": col_data.median(),
                "p25": col_data.quantile(0.25),
                "p75": col_data.quantile(0.75),
            }
        else:
            column_stats[col] = {
                "min": None,
                "max": None,
                "mean": None,
                "median": None,
                "p25": None,
                "p75": None,
            }

    stats["column_stats"] = column_stats

    return stats


def format_summary_stats(stats, precision=4):
    """
    Format summary statistics as a human-readable string.

    Args:
        stats: Dictionary of summary statistics from compute_summary_stats
        precision: Number of decimal places for numeric values (default: 4)

    Returns:
        str: Formatted summary statistics table
    """
    if stats is None:
        return "No statistics available"

    lines = []
    lines.append("=" * 60)
    lines.append("SUMMARY STATISTICS")
    lines.append("=" * 60)
    lines.append("")

    # General statistics
    lines.append(f"Number of observations: {stats['n_observations']}")

    if stats["min_date"] is not None and stats["max_date"] is not None:
        lines.append(f"Date range: {stats['min_date']} to {stats['max_date']}")

    lines.append("")

    # Column statistics
    if stats["column_stats"]:
        lines.append("Column Statistics:")
        lines.append("-" * 60)

        for col_name, col_stats in stats["column_stats"].items():
            lines.append(f"\n{col_name}:")

            if col_stats["min"] is not None:
                lines.append(f"  Min:        {col_stats['min']:.{precision}f}")
                lines.append(f"  25th pctl:  {col_stats['p25']:.{precision}f}")
                lines.append(f"  Median:     {col_stats['median']:.{precision}f}")
                lines.append(f"  Mean:       {col_stats['mean']:.{precision}f}")
                lines.append(f"  75th pctl:  {col_stats['p75']:.{precision}f}")
                lines.append(f"  Max:        {col_stats['max']:.{precision}f}")
            else:
                lines.append("  No data available")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
