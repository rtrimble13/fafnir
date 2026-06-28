"""Unit tests for the source HTTP client helpers (no network)."""

from __future__ import annotations

from fafnir.sources.base import MAX_RETRY_AFTER_SECONDS, BaseSource


def test_retry_after_parses_delta_seconds():
    assert BaseSource._retry_after_seconds("30") == 30
    assert BaseSource._retry_after_seconds("0") == 0


def test_retry_after_capped():
    assert (
        BaseSource._retry_after_seconds(str(MAX_RETRY_AFTER_SECONDS + 1000))
        == MAX_RETRY_AFTER_SECONDS
    )


def test_retry_after_none_for_absent_or_http_date():
    assert BaseSource._retry_after_seconds(None) is None
    assert BaseSource._retry_after_seconds("") is None
    # HTTP-date form is not parsed -> None (caller falls back to exponential).
    assert BaseSource._retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
