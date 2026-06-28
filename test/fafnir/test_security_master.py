"""Unit tests for security-master exchange classification (no DB)."""

from __future__ import annotations

from fafnir.ingest.security_master import _is_us, _norm_exchange


def _e(code: str) -> dict:
    return {"exchangeShortName": code}


def test_norm_exchange_canonicalizes_amex():
    assert _norm_exchange(_e("NYSEAMERICAN")) == "AMEX"
    assert _norm_exchange(_e("AMEX")) == "AMEX"
    assert _norm_exchange(_e("NASDAQ")) == "NASDAQ"


def test_is_us_accepts_real_us_venues():
    for code in ("NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "BATS", "CBOE", "OTC"):
        assert _is_us(_e(code)) is True, code


def test_is_us_rejects_foreign_substring_matches():
    # Regression: substring containment used to leak these in.
    for code in ("NASDAQ DUBAI", "CBOE EUROPE", "XOTC", "OTC MARKETS LONDON"):
        assert _is_us(_e(code)) is False, code


def test_is_us_handles_missing_exchange():
    assert _is_us({}) is False
