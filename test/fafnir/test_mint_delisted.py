"""
`fafnir vendor mint-delisted` (plan SA-0502, #71; the integration tests need
FAFNIR_TEST_DSN).

Dead Sharadar issuers become fafnir securities, empty, under ADR 0016's rules, and
never onto history an FMP row already holds (#68). Every key, ticker and name is
synthetic (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import vendor_mint as vm
from fafnir.ingest import vendor_overlap as vo
from fafnir.ingest.runlog import RunLog

Y = date
integration = pytest.mark.integration


class FakeCloses:
    name = "fake"

    def closes(self, wanted):
        return {
            (t, ticker, d): Decimal(str(round(20 + (d.toordinal() % 97) / 10, 3)))
            for (t, ticker), dates in wanted.items()
            for d in dates
        }


def _scan(db):
    """The overlap scan the mint requires first."""
    fake = FakeCloses()
    return vo.scan(db, lambda n: fake)


def _vendor(
    db,
    key,
    ticker,
    first=Y(2005, 1, 3),
    last=Y(2009, 6, 30),
    *,
    delisted=True,
    category="Domestic Common Stock",
    exchange="NYSEMKT",
    related=(),
    sector=None,
    industry=None,
    cik=None,
    table="stocks",
    events=(),
):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, exchange, category, is_delisted, related_tickers, sector, industry, "
        "cik, first_price_date, last_price_date) VALUES "
        "('sharadar', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            table,
            key,
            ticker,
            f"{ticker} Holdings",
            exchange,
            category,
            delisted,
            list(related) or None,
            sector,
            industry,
            cik,
            first,
            last,
        ),
    )
    for event_date, action, t, contra in events:
        db.execute(
            "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
            "action, ticker, contra_ticker) VALUES ('sharadar', %s, %s, %s, %s, %s)",
            (key, event_date, action, t, contra),
        )


def _fmp(db, symbol, start, end):
    """An FMP-minted security with weekday bars."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db, primary_symbol=symbol, company_name=f"{symbol} Co", exchange_code="NYSE"
    )
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from) "
        "VALUES (%s, %s, %s)",
        (sid, symbol, start),
    )
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            c = Decimal(str(round(20 + (d.toordinal() % 97) / 10, 3)))
            rows.append(
                {
                    "security_id": sid,
                    "trade_date": d,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "volume": 100,
                }
            )
        d += timedelta(days=1)
    repo.upsert_daily_prices(db, rows)
    return sid


def _minted(db):
    return {
        r["vendor_key"]: r
        for r in db.fetchall(
            "SELECT l.vendor_key, l.match_method, l.is_feed, l.valid_from, "
            "l.confidence, s.* FROM core.security_vendor_xref l "
            "JOIN core.security s USING (security_id) "
            "WHERE l.match_method = 'minted' AND l.valid_to IS NULL"
        )
    }


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_ticker_periods_cut_at_each_change():
    events = [
        # The vendor files a change under both tickers; one period each side.
        {"action": "tickerchangefrom", "event_date": Y(2007, 3, 1), "ticker": "NEW",
         "contra_ticker": "OLD"},
        {"action": "tickerchangeto", "event_date": Y(2007, 3, 1), "ticker": "OLD",
         "contra_ticker": "NEW"},
        # Outside the price span, and naming no new ticker: ignored.
        {"action": "tickerchangefrom", "event_date": Y(2011, 1, 3), "ticker": "X",
         "contra_ticker": "NEW"},
        {"action": "tickerchangefrom", "event_date": Y(2008, 1, 2), "ticker": "NEW",
         "contra_ticker": None},
        {"action": "delisted", "event_date": Y(2009, 7, 1), "ticker": "NEW",
         "contra_ticker": None},
    ]  # fmt: skip
    assert vm.ticker_periods("NEW1", Y(2005, 1, 3), Y(2009, 6, 30), events) == [
        ("OLD", Y(2005, 1, 3), Y(2007, 2, 28)),
        ("NEW1", Y(2007, 3, 1), Y(2009, 6, 30)),
    ]
    assert vm.ticker_periods("A", Y(2005, 1, 3), Y(2005, 6, 30), []) == [
        ("A", Y(2005, 1, 3), Y(2005, 6, 30))
    ]


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


@integration
def test_a_dead_issuer_is_minted_empty_and_resolves(db):
    sector = repo.get_or_create_sector(db, "Technology")
    industry = repo.get_or_create_industry(db, "Software - Application")
    _vendor(
        db,
        "900100",
        "ZZDEAD1",
        sector="Technology",
        industry="Software - Application",
        cik="0000123456",
        events=[
            (Y(2005, 1, 3), "listed", "ZZOLD", None),
            (Y(2007, 3, 1), "tickerchangefrom", "ZZDEAD", "ZZOLD"),
            (Y(2009, 7, 1), "delisted", "ZZDEAD", None),
        ],
    )
    _scan(db)
    result = vm.mint(db)
    assert (result.minted, result.held_back, result.unclassified) == (1, 0, 0)
    m = _minted(db)["900100"]
    assert (m["source"], m["primary_symbol"], m["company_name"]) == (
        "sharadar",
        "ZZDEAD1",
        "ZZDEAD1 Holdings",
    )
    assert (m["asset_type"], m["is_etf"], m["is_fund"], m["exchange_code"]) == (
        "equity",
        False,
        False,
        "AMEX",
    )
    assert (m["sector_id"], m["industry_id"], m["cik"]) == (
        sector,
        industry,
        "0000123456",
    )
    assert (m["is_actively_trading"], m["delisted_date"], m["ipo_date"]) == (
        False,
        Y(2009, 7, 1),
        Y(2005, 1, 3),
    )
    assert (m["is_feed"], m["valid_from"], m["confidence"]) == (
        True,
        Y(2005, 1, 3),
        None,
    )
    periods = db.fetchall(
        "SELECT symbol, valid_from, valid_to, source FROM core.symbol_xref "
        "WHERE security_id = %s ORDER BY valid_from",
        (m["security_id"],),
    )
    assert [tuple(p.values()) for p in periods] == [
        ("ZZOLD", Y(2005, 1, 3), Y(2007, 2, 28), "sharadar"),
        ("ZZDEAD1", Y(2007, 3, 1), Y(2009, 6, 30), "sharadar"),
    ]
    # `duk -S db ph ZZDEAD1` and its old ticker resolve to it.
    assert repo.resolve_security_id(db, "ZZDEAD1") == m["security_id"]
    assert repo.resolve_security_id(db, "ZZOLD") == m["security_id"]
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s",
            (m["security_id"],),
        )
        == 0
    )
    # A re-run mints nothing new: the permaticker is linked.
    _scan(db)
    again = vm.mint(db)
    assert again.minted == 0 and len(_minted(db)) == 1


@integration
def test_kinds_rules_and_unclassified(db):
    _vendor(db, "900110", "ZZETN", category="ETN", exchange="NYSEARCA")
    _vendor(db, "900111", "ZZCEF", category="CEF", exchange="NYSE", sector="Nowhere")
    _vendor(db, "900112", "ZZOTC", exchange="OTC")
    _vendor(db, "900113", "ZZOLDY", Y(1990, 1, 2), Y(1997, 6, 30))
    _vendor(db, "900114", "ZZLIVE", delisted=False)
    _vendor(db, "900115", "ZZUNIT", category="UNIT")
    _scan(db)
    plan = vm.mint(db, dry_run=True)
    assert {m.vendor_key for m in plan.mints} == {"900110", "900111"}
    assert plan.by("exchange") == {"AMEX": 1, "NYSE": 1}
    assert plan.by("death year") == {2009: 2}
    assert _minted(db) == {}  # a dry run writes nothing
    result = vm.mint(db)
    assert result.unclassified == 2  # neither maps to a sector fafnir knows
    minted = _minted(db)
    assert (minted["900110"]["asset_type"], minted["900110"]["is_etf"]) == ("etf", True)
    assert (minted["900111"]["asset_type"], minted["900111"]["is_fund"]) == (
        "equity",
        True,
    )
    assert minted["900111"]["sector_id"] is None


@integration
def test_an_overlap_holds_an_issuer_back_until_asked(db):
    # The BID shape: the FMP row holds the dead issuer's 2005-2009 bars.
    _fmp(db, "ZZBID", Y(2005, 1, 3), Y(2012, 12, 31))
    _vendor(db, "900120", "ZZBID1", related=["ZZBID"])
    _vendor(db, "900121", "ZZFREE")
    _scan(db)
    result = vm.mint(db)
    assert result.held_back == 1 and set(_minted(db)) == {"900121"}
    with pytest.raises(vm.MintError, match="--allow-overlap"):
        vm.mint(db, permaticker="900120")
    one = vm.mint(db, permaticker="900120", allow_overlap=True)
    assert one.minted == 1 and set(_minted(db)) == {"900120", "900121"}
    with pytest.raises(vm.MintError, match="already linked"):
        vm.mint(db, permaticker="900120", allow_overlap=True)


@integration
def test_a_permaticker_outside_the_rules_or_unknown_is_refused(db):
    _vendor(db, "900130", "ZZLIVE", delisted=False)
    _scan(db)
    with pytest.raises(vm.MintError, match="outside ADR 0016"):
        vm.mint(db, permaticker="900130")
    with pytest.raises(vm.MintError, match="no Sharadar stock or fund"):
        vm.mint(db, permaticker="999999")


@integration
def test_the_overlap_scan_must_be_newer_than_the_master(db):
    _vendor(db, "900140", "ZZX")
    with pytest.raises(vm.MintError, match="vendor overlap"):
        vm.mint(db, dry_run=True)
    _scan(db)
    assert len(vm.mint(db, dry_run=True).mints) == 1
    with RunLog(db, source="sharadar", endpoint="master:tickers", params={}):
        db.commit()
    with pytest.raises(vm.MintError, match="since the Sharadar master"):
        vm.mint(db, dry_run=True)


@integration
def test_limit_and_the_command(db, tmp_path, monkeypatch):
    for i in range(3):
        _vendor(db, f"90015{i}", f"ZZL{i}")
    _scan(db)
    assert vm.mint(db, limit=2).minted == 2
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text("[general]\n")

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "mint-delisted", *args]
        )

    out = run("--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. 1 to mint; 0 held back" in out.output
    assert "By exchange:" in out.output and "AMEX" in out.output
    assert "needs --permaticker" in run("--allow-overlap").output
    out = run()
    assert out.exit_code == 0, out.output
    assert "Minted 1;" in out.output
    assert len(_minted(db)) == 3


@integration
def test_a_seeded_sample_is_random_and_repeatable(db):
    for i in range(10):
        _vendor(db, f"9002{i:02d}", f"ZZS{i}")
    _scan(db)
    one = {m.vendor_key for m in vm.mint(db, sample=3, seed=7, dry_run=True).mints}
    two = {m.vendor_key for m in vm.mint(db, sample=3, seed=7, dry_run=True).mints}
    other = {m.vendor_key for m in vm.mint(db, sample=3, seed=8, dry_run=True).mints}
    assert len(one) == 3 and one == two and one != other
    # Not the first three by key, which is what --limit takes.
    first = {m.vendor_key for m in vm.mint(db, limit=3, dry_run=True).mints}
    assert first == {"900200", "900201", "900202"}
    result = vm.mint(db, sample=3, seed=7)
    assert result.minted == 3
    assert set(_minted(db)) == one
    (params,) = db.fetchall(
        "SELECT params FROM ops.ingestion_run WHERE ingestion_run_id = %s",
        (result.run_id,),
    )
    assert (params["params"]["sample"], params["params"]["seed"]) == (3, 7)
