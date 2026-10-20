"""
`fafnir vendor overlap` (plan SA-0503, #68; the integration tests need
FAFNIR_TEST_DSN).

The golden case is the BID shape: one FMP row holding a dead issuer's history (filed
by Sharadar under ``BID1``) before its current issuer's. Sharadar's closes come from
a fake source keyed by ticker, so an overlap can agree or disagree on purpose. Every
key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import vendor_overlap as vo

Y = date
integration = pytest.mark.integration


def close_of(d: date, level: float = 50.0) -> Decimal:
    """A price path that differs every day, so a wrong date never agrees."""
    return Decimal(str(round(level + (d.toordinal() % 97) / 10, 3)))


class FakeCloses:
    name = "fake"

    def __init__(self, prices):
        self.prices = prices

    def closes(self, wanted):
        return {
            (t, ticker, d): self.prices[ticker](d)
            for (t, ticker), dates in wanted.items()
            if ticker in self.prices
            for d in dates
        }


def _source(prices):
    fake = FakeCloses(prices)
    return lambda n: fake


def _sec(db, symbol, start, end, *, price=close_of, tickers=(), **kw):
    """An FMP-minted security with weekday bars ``start..end`` priced by
    ``price(date)``."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        **kw,
    )
    for t, lo, hi in tickers or [(symbol, start, None)]:
        db.execute(
            "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s)",
            (sid, t, lo, hi),
        )
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            c = price(d)
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


def _vendor(
    db,
    key,
    ticker,
    first,
    last,
    *,
    delisted=True,
    related=(),
    category="Domestic Common Stock",
    exchange="NYSE",
    table="stocks",
):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, exchange, category, is_delisted, related_tickers, first_price_date, "
        "last_price_date) VALUES ('sharadar', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            table,
            key,
            ticker,
            f"{ticker} Co",
            exchange,
            category,
            delisted,
            list(related) or None,
            first,
            last,
        ),
    )


def _link(db, sid, key, valid_from):
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=valid_from,
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
    )


def _flags(db, state="open"):
    cond = "resolved_at IS NULL" if state == "open" else "resolved_at IS NOT NULL"
    return db.fetchall(
        "SELECT dq_flag_id, security_id, severity, table_name, record_key, detail, "
        "resolved_by FROM ops.data_quality_flag "
        f"WHERE check_name = 'vendor_history_overlap' AND {cond} "
        "ORDER BY security_id, record_key->>'kind'"
    )


def _weekdays(lo: date, hi: date) -> int:
    return sum(
        1 for n in range((hi - lo).days + 1) if (lo + timedelta(n)).weekday() < 5
    )


DEAD = (Y(2010, 1, 4), Y(2014, 12, 31))
LIVE = (Y(2016, 1, 4), Y(2020, 12, 31))


def _bid(db):
    """The BID shape: ZZBID holds the dead issuer's 2010-2014 bars (level 20) and
    the live issuer's 2016-2020 bars (level 80), and is linked to the live one."""
    sid = _sec(
        db,
        "ZZBID",
        DEAD[0],
        LIVE[1],
        price=lambda d: close_of(d, 20.0) if d <= DEAD[1] else close_of(d, 80.0),
    )
    _vendor(db, "900010", "ZZBID1", *DEAD, related=["ZZBID"])
    _vendor(db, "900011", "ZZBID", *LIVE, delisted=False)
    _link(db, sid, "900011", LIVE[0])
    return sid


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def test_mintable_is_adr_0016():
    base = {
        "vendor_table": "stocks",
        "is_delisted": True,
        "last_price_date": Y(2005, 6, 30),
        "category": "Domestic Common Stock",
        "exchange": "NYSEMKT",
    }
    assert vo.mintable(base)
    assert vo.mintable(dict(base, category="ETN", exchange="NYSEARCA"))
    assert vo.mintable(dict(base, category="CEF", exchange="NASDAQ"))
    for change in (
        {"is_delisted": False},
        {"last_price_date": Y(1997, 12, 31)},
        {"exchange": "OTC"},
        {"exchange": None},
        {"category": "UNIT"},
        {"category": "IDX"},
        {"vendor_table": "fundamentals"},
    ):
        assert not vo.mintable(dict(base, **change)), change


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


@integration
def test_the_bid_shape_is_flagged_both_ways(db):
    sid = _bid(db)
    result = vo.scan(
        db,
        _source(
            {
                "ZZBID1": lambda d: close_of(d, 20.0),
                "ZZBID": lambda d: close_of(d, 80.0),
            }
        ),
    )
    assert result.dead_issuers == 1
    assert result.counts() == {
        ("dead_issuer", "agree"): 1,
        ("earlier_history", "-"): 1,
    }
    assert result.flagged_keys() == 1
    dead, earlier = _flags(db)
    assert dead["security_id"] == earlier["security_id"] == sid
    assert (dead["severity"], dead["table_name"]) == ("warn", "core.daily_price")
    assert dead["record_key"] == {
        "vendor": "sharadar",
        "vendor_key": "900010",
        "kind": "dead_issuer",
    }
    d = dead["detail"]
    assert (d["from"], d["to"]) == ("2010-01-04", "2014-12-31")
    assert (d["agreed"], d["of"], d["linked_key"]) == (5, 5, "900011")
    assert d["bars"] == _weekdays(*DEAD)
    e = earlier["detail"]
    assert (e["kind"], e["vendor_key"], e["from"], e["to"]) == (
        "earlier_history",
        "900011",
        "2010-01-04",
        "2016-01-03",
    )
    # Everything before the live issuer's first price, 2015 included.
    assert e["bars"] == _weekdays(DEAD[0], Y(2016, 1, 3))


@integration
def test_disagreeing_bars_are_another_issuers(db):
    _sec(db, "ZZQ", *DEAD, price=lambda d: close_of(d, 70.0))
    _vendor(db, "900020", "ZZQ1", *DEAD, related=["ZZQ"])
    result = vo.scan(db, _source({"ZZQ1": lambda d: close_of(d, 20.0)}))
    assert result.counts() == {("dead_issuer", "disagree"): 1}
    (flag,) = _flags(db)
    assert (flag["detail"]["agreed"], flag["detail"]["linked_key"]) == (0, None)


@integration
def test_what_is_not_flagged(db):
    _sec(db, "ZZN", *LIVE)
    # Dates that never meet the FMP bars.
    _vendor(db, "900030", "ZZN1", *DEAD, related=["ZZN"])
    # Not mintable: OTC, before the floor, out of scope, alive.
    _sec(db, "ZZO", *DEAD)
    _vendor(db, "900031", "ZZO1", *DEAD, related=["ZZO"], exchange="OTC")
    _sec(db, "ZZF", Y(1995, 1, 2), Y(1997, 6, 30))
    _vendor(db, "900032", "ZZF1", Y(1995, 1, 2), Y(1997, 6, 30), related=["ZZF"])
    _sec(db, "ZZU", *DEAD)
    _vendor(db, "900033", "ZZU1", *DEAD, related=["ZZU"], category="UNIT")
    # A linked security listed before 1998: Sharadar starts at its floor, FMP
    # earlier. That is coverage, not a second issuer.
    old = _sec(db, "ZZOLD", Y(1995, 1, 2), Y(2000, 12, 29))
    _vendor(db, "900034", "ZZOLD", Y(1997, 12, 31), Y(2000, 12, 29), delisted=False)
    _link(db, old, "900034", Y(1997, 12, 31))
    # A linked security whose history starts with its permaticker's, near enough.
    near = _sec(db, "ZZNEAR", Y(2016, 1, 4), Y(2017, 12, 29))
    _vendor(db, "900035", "ZZNEAR", Y(2016, 1, 20), Y(2017, 12, 29), delisted=False)
    _link(db, near, "900035", Y(2016, 1, 20))
    result = vo.scan(db, _source({}))
    assert result.overlaps == [] and _flags(db) == []
    assert result.dead_issuers == 1  # ZZN1 alone passes ADR 0016's rules


@integration
def test_a_rerun_refreshes_and_a_gone_overlap_resolves(db):
    sid = _bid(db)
    src = _source({"ZZBID1": lambda d: close_of(d, 20.0)})
    first = vo.scan(db, src)
    before = {f["record_key"]["kind"]: f for f in _flags(db)}
    second = vo.scan(db, src)
    after = {f["record_key"]["kind"]: f for f in _flags(db)}
    assert {k: f["dq_flag_id"] for k, f in after.items()} == {
        k: f["dq_flag_id"] for k, f in before.items()
    }
    assert second.resolved == 0 and first.run_id != second.run_id
    # The bars before the live issuer move off the row (what split-history
    # does): both overlaps are gone, and the next run resolves their flags.
    db.execute(
        "DELETE FROM core.daily_price WHERE security_id = %s AND trade_date < %s",
        (sid, LIVE[0]),
    )
    third = vo.scan(db, src)
    assert third.overlaps == [] and third.resolved == 2
    assert _flags(db) == []
    assert {f["resolved_by"] for f in _flags(db, "resolved")} == {
        "fafnir vendor overlap"
    }


@integration
def test_a_dry_run_writes_nothing(db):
    _bid(db)
    result = vo.scan(db, _source({"ZZBID1": lambda d: close_of(d, 20.0)}), dry_run=True)
    assert len(result.overlaps) == 2 and result.run_id is None
    assert _flags(db) == []
    assert not db.fetchall("SELECT 1 FROM ops.ingestion_run WHERE endpoint = 'overlap'")


@integration
def test_the_command(db, tmp_path, monkeypatch):
    _bid(db)
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    fake = FakeCloses({"ZZBID1": lambda d: close_of(d, 20.0)})
    monkeypatch.setattr(cli, "_link_closes", lambda cfg, db_, prices: lambda n: fake)

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "overlap", *args]
        )

    out = run("--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. 1 dead issuers evaluated" in out.output
    assert "Sharadar closes from fake" in out.output
    assert "dead_issuer      agree" in out.output
    out = run()
    assert out.exit_code == 0, out.output
    assert "1 permatickers held back from minting; 0 flags resolved" in out.output
    assert len(_flags(db)) == 2
