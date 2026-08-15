"""Unit tests for the source HTTP client helpers (no network)."""

from __future__ import annotations

from fafnir.sources.base import (
    MAX_RETRY_AFTER_SECONDS,
    BaseSource,
    redact_secrets,
)

# The exact string requests builds for a failed response -- this is what leaked
# the key into a traceback before redaction.
_HTTP_ERROR = (
    "402 Client Error: Payment Required for url: "
    "https://financialmodelingprep.com/stable/company-screener"
    "?limit=1000&page=0&exchange=BATS&apikey=2F6Gza19PPjluyAomWz16vmQGwH2DwvW"
)


def test_redact_masks_the_api_key_requests_puts_in_its_error():
    out = redact_secrets(_HTTP_ERROR)
    assert "2F6Gza19PPjluyAomWz16vmQGwH2DwvW" not in out
    assert "apikey=***" in out
    # The diagnostic parts must survive -- that is the point of scrubbing rather
    # than suppressing the upstream message.
    assert "402 Client Error: Payment Required" in out
    assert "exchange=BATS" in out and "limit=1000" in out


def test_redact_covers_the_other_secret_param_spellings():
    for param in ("api_key", "token", "access_key", "secret", "APIKEY"):
        out = redact_secrets(f"https://x/y?{param}=hunter2&page=1")
        assert "hunter2" not in out, param
        assert "page=1" in out, param


def test_redact_stops_at_the_parameter_boundary():
    out = redact_secrets("?apikey=abc123&symbol=AAPL")
    assert out == "?apikey=***&symbol=AAPL"


def test_redact_leaves_clean_text_alone():
    assert redact_secrets("GET .../stock-list returned 401") == (
        "GET .../stock-list returned 401"
    )


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
