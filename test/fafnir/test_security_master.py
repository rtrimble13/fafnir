"""Unit tests for security-master exchange classification (no DB)."""

from __future__ import annotations

import pytest

from fafnir.ingest.security_master import (
    COMPANY_NAME_DRIFT_RATIO,
    SCREENER_EXCHANGES,
    SourceError,
    _is_us,
    _norm_exchange,
    _normalize_company_name,
    _us_entries,
    company_name_similarity,
)


def _e(code: str) -> dict:
    return {"exchangeShortName": code}


class _FakeFMP:
    """Screener stub: serves rows for the venues it knows, empty otherwise.

    Venues listed in ``unavailable`` raise, standing in for FMP's 402 on a
    venue the plan does not cover.
    """

    def __init__(
        self,
        by_exchange: dict[str, list[dict]],
        unavailable: frozenset[str] = frozenset(),
    ):
        self.by_exchange = by_exchange
        self.unavailable = unavailable
        self.calls: list[str] = []

    def company_screener(self, *, exchange=None, **_kw) -> list[dict]:
        self.calls.append(exchange)
        if exchange in self.unavailable:
            raise SourceError("GET company-screener returned 402")
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


def test_us_entries_survives_a_venue_the_plan_does_not_cover():
    # Regression: FMP answers 402 for BATS on the Professional plan, and that
    # used to abort the whole load after NASDAQ/NYSE/AMEX had already succeeded.
    fmp = _FakeFMP(
        {"NASDAQ": [{"symbol": "AAPL", "exchangeShortName": "NASDAQ"}]},
        unavailable=frozenset({"BATS", "CBOE", "OTC"}),
    )
    entries = _us_entries(fmp, include_etfs=True)

    assert [e[0]["symbol"] for e in entries] == ["AAPL"]
    assert fmp.calls == list(SCREENER_EXCHANGES)  # kept going past the failures


def test_us_entries_raises_when_every_venue_fails():
    # A zero universe must be an error, never a silent "Loaded 0 securities".
    fmp = _FakeFMP({}, unavailable=frozenset(SCREENER_EXCHANGES))
    with pytest.raises(SourceError, match="returned nothing for any venue"):
        _us_entries(fmp, include_etfs=True)


def test_us_entries_raises_when_rows_carry_no_venue():
    # The original regression, as an error this time: rows arrive but none has
    # an exchange, so the universe is empty for a reason worth shouting about.
    fmp = _FakeFMP({"NASDAQ": [{"symbol": "AAPL", "companyName": "Apple Inc."}]})
    with pytest.raises(SourceError, match="none carrying a US venue"):
        _us_entries(fmp, include_etfs=True)


def test_us_entries_allows_an_empty_result_when_etfs_are_excluded():
    # Not an error: the venue answered, the rows were US, --no-etfs removed them.
    spy = {"symbol": "SPY", "exchangeShortName": "AMEX", "isEtf": True}
    assert _us_entries(_FakeFMP({"AMEX": [spy]}), include_etfs=False) == []


# ---------------------------------------------------------------------------
# Company-name drift: the safety net under 0012's identity key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored,incoming",
    [
        # Pure styling churn between feed revisions.
        ("Apple Inc.", "Apple Inc"),
        ("Apple, Inc.", "Apple Inc."),
        ("The Walt Disney Company", "Walt Disney Co"),
        ("Alphabet Inc. Class A", "Alphabet Inc."),
        ("JPMorgan Chase & Co.", "JPMorgan Chase and Co"),
        # A rebrand that extends or trims the name is still one company.
        ("Meta Platforms", "Meta Platforms, Inc."),
        # Nothing to compare against.
        (None, "Acme Corp"),
        ("", "Acme Corp"),
        ("Acme Corp", None),
    ],
)
def test_company_name_similarity_has_no_opinion_on_restyling(stored, incoming):
    assert company_name_similarity(stored, incoming) is None


@pytest.mark.parametrize(
    "stored,incoming",
    [
        ("Acme Corporation", "Zebra Industries Inc"),
        ("Circuit City Stores, Inc.", "The Chemours Company"),
        ("Apple Inc.", "Microsoft Corporation"),
    ],
)
def test_company_name_similarity_scores_unrelated_names_below_the_threshold(
    stored, incoming
):
    ratio = company_name_similarity(stored, incoming)
    assert ratio is not None
    assert ratio < COMPANY_NAME_DRIFT_RATIO, (stored, incoming, ratio)


def test_noise_stripping_keeps_the_identifying_words():
    # "co" as a standalone token is boilerplate; inside a word it is not.
    assert _normalize_company_name("The Coca-Cola Company") == "coca cola"
    assert _normalize_company_name("Ford Motor Co.") == "ford motor"
