"""
Unit tests for stats module.
"""

import pandas as pd

from duk.stats import compute_summary_stats, format_summary_stats


class TestComputeSummaryStats:
    """Test cases for compute_summary_stats function."""

    def test_basic_stats(self):
        """Test basic summary statistics computation."""
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
                "close": [100.0, 110.0, 105.0],
                "volume": [1000, 1100, 1050],
            }
        )

        stats = compute_summary_stats(df)

        assert stats is not None
        assert stats["n_observations"] == 3
        assert stats["min_date"] == pd.Timestamp("2023-01-01")
        assert stats["max_date"] == pd.Timestamp("2023-01-03")
        assert "close" in stats["column_stats"]
        assert "volume" in stats["column_stats"]

    def test_column_statistics(self):
        """Test column-level statistics."""
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
                "close": [100.0, 110.0, 120.0],
            }
        )

        stats = compute_summary_stats(df)

        close_stats = stats["column_stats"]["close"]
        assert close_stats["min"] == 100.0
        assert close_stats["max"] == 120.0
        assert close_stats["mean"] == 110.0
        assert close_stats["median"] == 110.0
        assert close_stats["p25"] == 105.0
        assert close_stats["p75"] == 115.0

    def test_with_date_index(self):
        """Test with date as index."""
        df = pd.DataFrame(
            {
                "close": [100.0, 110.0, 105.0],
            },
            index=pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
        )
        df.index.name = "date"

        stats = compute_summary_stats(df)

        assert stats is not None
        assert stats["n_observations"] == 3
        assert stats["min_date"] == pd.Timestamp("2023-01-01")
        assert stats["max_date"] == pd.Timestamp("2023-01-03")

    def test_empty_dataframe(self):
        """Test with empty DataFrame."""
        df = pd.DataFrame()

        stats = compute_summary_stats(df)

        assert stats is None

    def test_with_nan_values(self):
        """Test with NaN values in data."""
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
                "close": [100.0, None, 120.0],
            }
        )

        stats = compute_summary_stats(df)

        close_stats = stats["column_stats"]["close"]
        # Statistics should be computed on non-NaN values only
        assert close_stats["min"] == 100.0
        assert close_stats["max"] == 120.0
        assert close_stats["mean"] == 110.0

    def test_multiple_numeric_columns(self):
        """Test with multiple numeric columns."""
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2023-01-01", "2023-01-02"]),
                "open": [99.0, 109.0],
                "high": [102.0, 112.0],
                "low": [98.0, 108.0],
                "close": [100.0, 110.0],
            }
        )

        stats = compute_summary_stats(df)

        assert len(stats["column_stats"]) == 4
        assert "open" in stats["column_stats"]
        assert "high" in stats["column_stats"]
        assert "low" in stats["column_stats"]
        assert "close" in stats["column_stats"]

    def test_no_date_column(self):
        """Test with no date column."""
        df = pd.DataFrame(
            {
                "value": [1.0, 2.0, 3.0],
            }
        )

        stats = compute_summary_stats(df)

        assert stats is not None
        assert stats["n_observations"] == 3
        assert stats["min_date"] is None
        assert stats["max_date"] is None


class TestFormatSummaryStats:
    """Test cases for format_summary_stats function."""

    def test_basic_formatting(self):
        """Test basic formatting of summary statistics."""
        stats = {
            "n_observations": 3,
            "min_date": pd.Timestamp("2023-01-01"),
            "max_date": pd.Timestamp("2023-01-03"),
            "column_stats": {
                "close": {
                    "min": 100.0,
                    "max": 120.0,
                    "mean": 110.0,
                    "median": 110.0,
                    "p25": 105.0,
                    "p75": 115.0,
                }
            },
        }

        output = format_summary_stats(stats)

        assert "SUMMARY STATISTICS" in output
        assert "Number of observations: 3" in output
        assert "Date range: 2023-01-01" in output
        assert "close:" in output
        assert "Min:" in output
        assert "Max:" in output
        assert "Mean:" in output
        assert "Median:" in output

    def test_precision_formatting(self):
        """Test precision parameter in formatting."""
        stats = {
            "n_observations": 2,
            "min_date": None,
            "max_date": None,
            "column_stats": {
                "value": {
                    "min": 1.23456789,
                    "max": 2.34567890,
                    "mean": 1.79012345,
                    "median": 1.79012345,
                    "p25": 1.51234567,
                    "p75": 2.06790123,
                }
            },
        }

        output = format_summary_stats(stats, precision=2)

        assert "1.23" in output
        assert "2.35" in output
        # Should not have more than 2 decimal places
        assert "1.234567" not in output

    def test_none_stats(self):
        """Test formatting when stats is None."""
        output = format_summary_stats(None)

        assert "No statistics available" in output

    def test_no_date_range(self):
        """Test formatting when date range is None."""
        stats = {
            "n_observations": 2,
            "min_date": None,
            "max_date": None,
            "column_stats": {
                "value": {
                    "min": 1.0,
                    "max": 2.0,
                    "mean": 1.5,
                    "median": 1.5,
                    "p25": 1.25,
                    "p75": 1.75,
                }
            },
        }

        output = format_summary_stats(stats)

        assert "Date range:" not in output
        assert "Number of observations: 2" in output
