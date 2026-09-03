"""
Unit tests for the result envelope: row caps, the truncation flag, and coercion.

ADR 0008 requires every result to be row-capped with an explicit ``truncated``
flag. The flag is the half that matters: a silently truncated result is worse than
a refused one, because an agent that cannot tell 200 rows from "the first 200 of
40,000" will reason over the head of a series and report it as the whole.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fafnir_mcp.errors import ToolError, scrub_dsn
from fafnir_mcp.results import clamp_limit, envelope, jsonable


class TestEnvelope:
    def test_a_complete_result_is_not_flagged_truncated(self):
        result = envelope([{"i": 1}, {"i": 2}], max_rows=10)
        assert result["truncated"] is False
        assert result["row_count"] == 2

    def test_a_clipped_result_is_flagged_and_clipped(self):
        result = envelope([{"i": i} for i in range(10)], max_rows=3)
        assert result["truncated"] is True
        assert result["row_count"] == 3
        assert [r["i"] for r in result["rows"]] == [0, 1, 2]

    def test_exactly_at_the_cap_is_not_truncated(self):
        """The off-by-one that would cry wolf on every full page."""
        result = envelope([{"i": i} for i in range(5)], max_rows=5)
        assert result["truncated"] is False
        assert result["row_count"] == 5

    def test_extra_keys_pass_through(self):
        result = envelope([], symbol="AAPL", series="raw")
        assert result["symbol"] == "AAPL"
        assert result["series"] == "raw"


class TestJsonable:
    def test_dates_are_iso(self):
        assert jsonable(dt.date(2026, 3, 1)) == "2026-03-01"

    def test_timestamps_are_iso(self):
        value = dt.datetime(2026, 3, 1, 12, 30, tzinfo=dt.timezone.utc)
        assert jsonable(value).startswith("2026-03-01T12:30")

    def test_decimals_become_strings_not_floats(self):
        """Money in this warehouse is exact NUMERIC by construction (ADR 0001).

        Rendering it as a float here would undo that at the very last step, and
        `0.30000000000000004` arriving at an agent is the kind of thing that gets
        reported as a data defect. A string round-trips exactly.
        """
        assert jsonable(Decimal("0.1")) == "0.1"
        assert jsonable(Decimal("1234567890.123456")) == "1234567890.123456"
        assert isinstance(jsonable(Decimal("1.5")), str)

    def test_jsonb_columns_recurse(self):
        """record_key and detail are JSONB and can carry dates of their own."""
        value = {"trade_date": dt.date(2026, 3, 1), "moves": [Decimal("0.75")]}
        assert jsonable(value) == {"trade_date": "2026-03-01", "moves": ["0.75"]}

    def test_binary_is_described_not_dumped(self):
        assert "bytes" in jsonable(b"\x00\x01\x02")

    def test_scalars_pass_through(self):
        assert jsonable(None) is None
        assert jsonable(True) is True
        assert jsonable(7) == 7
        assert jsonable("x") == "x"


class TestClampLimit:
    def test_none_means_the_ceiling(self):
        assert clamp_limit(None, 100) == 100

    def test_a_limit_above_the_ceiling_is_clamped_not_refused(self):
        """Clamping plus the truncated flag beats an error telling the caller to
        ask again for less."""
        assert clamp_limit(10_000, 100) == 100

    def test_a_reasonable_limit_is_honoured(self):
        assert clamp_limit(25, 100) == 25

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_non_positive_is_refused(self, bad):
        with pytest.raises(ToolError):
            clamp_limit(bad, 100)

    @pytest.mark.parametrize("bad", ["abc", "", object()])
    def test_non_numeric_is_refused(self, bad):
        with pytest.raises(ToolError):
            clamp_limit(bad, 100)

    def test_a_numeric_string_is_accepted(self):
        """MCP arguments arrive as JSON, and a model writes "10" as often as 10."""
        assert clamp_limit("10", 100) == 10


class TestScrubDsn:
    def test_the_password_is_removed(self):
        scrubbed = scrub_dsn("host=x user=y password=hunter2 dbname=z")
        assert "hunter2" not in scrubbed
        assert "password=***" in scrubbed

    def test_everything_else_survives(self):
        scrubbed = scrub_dsn("host=x port=15432 user=claude_ops")
        assert "claude_ops" in scrubbed and "15432" in scrubbed

    def test_empty_is_safe(self):
        assert scrub_dsn("") == ""
