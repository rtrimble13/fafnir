"""Unit tests for security-master exchange classification (no DB)."""

from __future__ import annotations

from fafnir.ingest.security_master import (
    SCREENER_EXCHANGES,
    _is_us,
    _norm_exchange,
    _us_entries,
)


def _e(code: str) -> dict:
    return {"exchangeShortName": code}


class _FakeFMP:
    """Screener stub: serves rows for the venues it knows, empty otherwise."""

    def __init__(self, by_exchange: dict[str, list[dict]]):
        self.by_exchange = by_exchange
        self.calls: list[str] = []

    def company_screener(self, *, exchange=None, **_kw) -> list[dict]:
        self.calls.append(exchange)
        return self.by_exchange.get(exchange, [])


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


def test_is_us_rejects_bulk_list_rows():
    # Regression: stable stock-list/etf-list return symbol + name only, so every
    # row fails the venue test. Building the US universe from them loaded 0.
    assert _is_us({"symbol": "AAPL", "companyName": "Apple Inc."}) is False


def test_us_entries_reads_the_screener_fields():
    fmp = _FakeFMP(
        {
            "NASDAQ": [
                {"symbol": "AAPL", "exchangeShortName": "NASDAQ", "isEtf": False},
                {"symbol": "QQQ", "exchangeShortName": "NASDAQ", "isEtf": True},
                {"symbol": "SAP", "exchangeShortName": "XETRA", "isEtf": False},
            ]
        }
    )
    entries = _us_entries(fmp, include_etfs=True)

    assert [(e[0]["symbol"], e[1], e[2]) for e in entries] == [
        ("AAPL", "equity", False),
        ("QQQ", "etf", True),
    ]
    assert fmp.calls == list(SCREENER_EXCHANGES)


def test_us_entries_dedups_across_venues_and_honours_no_etfs():
    row = {"symbol": "SPY", "exchangeShortName": "AMEX", "isEtf": True}
    fmp = _FakeFMP({"NASDAQ": [row], "NYSE": [row], "AMEX": [row]})

    assert len(_us_entries(fmp, include_etfs=True)) == 1
    assert _us_entries(fmp, include_etfs=False) == []
