"""
`fafnir ingest sharadar-universe` (plan SA-0904, #93; the integration tests need
FAFNIR_TEST_DSN).

Sharadar's renames, delistings and new listings, resolved by the permaticker's open
link rather than by ticker. Every key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sharadar_universe as su

integration = pytest.mark.integration
Y = date
SINCE = Y(2026, 9, 1)
STOCK = "Domestic Common Stock"


def test_renames_read_a_chain_from_the_other_parties():
    """Both events are filed under the issuer's ticker at load time (ZZE here), so
    the old and new tickers come from the other parties."""

    def e(day, action, ticker, contra):
        return {
            "event_date": day,
            "action": action,
            "ticker": ticker,
            "contra_ticker": contra,
        }

    events = [
        e(Y(2026, 10, 2), "tickerchangefrom", "ZZE", "ZZC"),
        e(Y(2026, 10, 2), "tickerchangeto", "ZZE", "ZZD"),
        e(Y(2026, 10, 9), "tickerchangefrom", "ZZE", "ZZD"),
        e(Y(2026, 10, 9), "tickerchangeto", "ZZE", "ZZE"),
        e(Y(2026, 10, 12), "tickerchangeto", "ZZE", "N/A"),  # no change named
    ]
    assert su.renames(events) == [
        (Y(2026, 10, 2), "ZZC", "ZZD"),
        (Y(2026, 10, 9), "ZZD", "ZZE"),
    ]


def _vendor(
    db,
    key,
    ticker,
    *,
    delisted=False,
    first=Y(2000, 1, 3),
    last=Y(2026, 10, 20),
    category=STOCK,
    exchange="NYSE",
    table="stocks",
):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, category, exchange, is_delisted, first_price_date, last_price_date) "
        "VALUES ('sharadar', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            table,
            key,
            ticker,
            f"{ticker} CORP",
            category,
            exchange,
            delisted,
            first,
            last,
        ),
    )


def _event(db, key, day, action, ticker, contra="N/A"):
    db.execute(
        "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
        "action, ticker, contra_ticker, contra_name) "
        "VALUES ('sharadar', %s, %s, %s, %s, %s, 'N/A')",
        (key, day, action, ticker, contra),
    )


def _security(db, symbol, key=None, *, delisted=None):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        is_actively_trading=delisted is None,
        delisted_date=delisted,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    if delisted is not None:
        db.execute(
            "UPDATE core.symbol_xref SET valid_to = %s WHERE security_id = %s",
            (delisted, sid),
        )
    if key:
        repo.insert_vendor_link(
            db,
            security_id=sid,
            vendor="sharadar",
            vendor_key=key,
            valid_from=Y(2000, 1, 3),
            match_method="manual",
            confidence=None,
            evidence={},
            created_by="tester",
        )
    return sid


def _bars(db, sid, *days):
    c = Decimal("10")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1,
            }
            for d in days
        ],
    )


def _security_row(db, sid):
    return db.fetchone("SELECT * FROM core.security WHERE security_id = %s", (sid,))


def _outcomes(result, step):
    return {c.symbol: c.outcome for c in result.changes if c.step == step}


def _renames_fixture(db):
    ids = {}
    # A plain rename.
    _vendor(db, "900401", "ZZB")
    ids["plain"] = _security(db, "ZZA", "900401")
    _event(db, "900401", Y(2026, 10, 5), "tickerchangefrom", "ZZB", "ZZA")
    _event(db, "900401", Y(2026, 10, 5), "tickerchangeto", "ZZB", "ZZB")
    # A chain, applied in order.
    _vendor(db, "900402", "ZZE")
    ids["chain"] = _security(db, "ZZC", "900402")
    _event(db, "900402", Y(2026, 10, 2), "tickerchangefrom", "ZZE", "ZZC")
    _event(db, "900402", Y(2026, 10, 2), "tickerchangeto", "ZZE", "ZZD")
    _event(db, "900402", Y(2026, 10, 9), "tickerchangefrom", "ZZE", "ZZD")
    _event(db, "900402", Y(2026, 10, 9), "tickerchangeto", "ZZE", "ZZE")
    # FMP's sweep already moved it.
    _vendor(db, "900403", "ZZG")
    ids["already"] = _security(db, "ZZG", "900403")
    _event(db, "900403", Y(2026, 10, 6), "tickerchangefrom", "ZZG", "ZZF")
    _event(db, "900403", Y(2026, 10, 6), "tickerchangeto", "ZZG", "ZZG")
    # On a third ticker: not this rename's to make.
    _vendor(db, "900404", "ZZI")
    ids["mismatch"] = _security(db, "ZZX", "900404")
    _event(db, "900404", Y(2026, 10, 7), "tickerchangefrom", "ZZI", "ZZH")
    _event(db, "900404", Y(2026, 10, 7), "tickerchangeto", "ZZI", "ZZI")
    # Not ours.
    _vendor(db, "900405", "ZZK")
    _event(db, "900405", Y(2026, 10, 8), "tickerchangefrom", "ZZK", "ZZJ")
    _event(db, "900405", Y(2026, 10, 8), "tickerchangeto", "ZZK", "ZZK")
    # Before the window.
    _event(db, "900401", Y(2026, 8, 1), "tickerchangefrom", "ZZB", "ZZOLD")
    _event(db, "900401", Y(2026, 8, 1), "tickerchangeto", "ZZB", "ZZA")
    return ids


@integration
def test_renames_move_the_linked_security(db):
    ids = _renames_fixture(db)
    result = su.run(db, since=SINCE)
    assert _outcomes(result, "rename") == {
        "ZZA->ZZB": "applied",
        "ZZC->ZZD": "applied",
        "ZZD->ZZE": "applied",
        "ZZF->ZZG": "already",
        "ZZH->ZZI": "conflict",
        "ZZJ->ZZK": "not_linked",
    }
    assert _security_row(db, ids["plain"])["primary_symbol"] == "ZZB"
    assert _security_row(db, ids["chain"])["primary_symbol"] == "ZZE"
    assert _security_row(db, ids["mismatch"])["primary_symbol"] == "ZZX"
    periods = db.fetchall(
        "SELECT symbol, valid_from, valid_to FROM core.symbol_xref "
        "WHERE security_id = %s ORDER BY valid_from",
        (ids["plain"],),
    )
    assert [(p["symbol"], p["valid_to"]) for p in periods] == [
        ("ZZA", Y(2026, 10, 4)),
        ("ZZB", None),
    ]
    recorded = {
        (r["old_symbol"], r["new_symbol"]): (r["status"], r["source"])
        for r in db.fetchall("SELECT * FROM core.symbol_change")
    }
    assert recorded[("ZZA", "ZZB")] == ("applied", "sharadar")
    assert recorded[("ZZH", "ZZI")] == ("conflict", "sharadar")
    assert ("ZZJ", "ZZK") not in recorded
    (flag,) = db.fetchall(
        "SELECT security_id, record_key, detail FROM ops.data_quality_flag "
        "WHERE check_name = 'symbol_change_conflict'"
    )
    assert flag["security_id"] == ids["mismatch"]
    assert flag["record_key"] == {"old_symbol": "ZZH", "new_symbol": "ZZI"}
    assert flag["detail"]["reason"] == "ticker_mismatch:ZZX"

    # A second run over the same window changes nothing applied twice; the
    # conflict is reconsidered.
    again = su.run(db, since=SINCE)
    assert _outcomes(again, "rename")["ZZA->ZZB"] == "recorded_applied"
    assert _outcomes(again, "rename")["ZZH->ZZI"] == "conflict"
    assert _security_row(db, ids["plain"])["primary_symbol"] == "ZZB"
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag "
            "WHERE check_name = 'symbol_change_conflict'"
        )
        == 1
    )

    # Fixed by hand: the next run applies it as "already", and closes the flag.
    db.execute(
        "UPDATE core.security SET primary_symbol = 'ZZI' WHERE security_id = %s",
        (ids["mismatch"],),
    )
    fixed = su.run(db, since=SINCE)
    assert _outcomes(fixed, "rename")["ZZH->ZZI"] == "already"
    assert not db.fetchall(
        "SELECT 1 FROM ops.data_quality_flag WHERE check_name = "
        "'symbol_change_conflict' AND resolved_at IS NULL"
    )


@integration
def test_delistings_are_marked_unless_something_contradicts_them(db):
    day = Y(2026, 10, 6)
    _vendor(db, "900411", "ZZD1", delisted=True, last=day)
    marked = _security(db, "ZZD1", "900411")
    _bars(db, marked, Y(2026, 10, 5), day)
    _event(db, "900411", day, "delisted", "ZZD1")
    _event(db, "900411", day, "acquisitionby", "ZZD1", "ZZACQ")

    _vendor(db, "900412", "ZZD2", delisted=True)
    _security(db, "ZZD2", "900412", delisted=Y(2026, 10, 1))
    _event(db, "900412", Y(2026, 10, 1), "delisted", "ZZD2")

    _vendor(db, "900413", "ZZD3", delisted=False)  # TICKERS lists it
    listed = _security(db, "ZZD3", "900413")
    _event(db, "900413", day, "delisted", "ZZD3")

    _vendor(db, "900414", "ZZD4", delisted=True)
    relisted = _security(db, "ZZD4", "900414")
    _event(db, "900414", Y(2026, 9, 10), "delisted", "ZZD4")
    _event(db, "900414", Y(2026, 9, 20), "listed", "ZZD4")

    _vendor(db, "900415", "ZZD5", delisted=True)
    trading = _security(db, "ZZD5", "900415")
    _bars(db, trading, Y(2026, 9, 1), Y(2026, 10, 15))
    _event(db, "900415", Y(2026, 9, 2), "delisted", "ZZD5")

    result = su.run(db, since=SINCE)
    assert _outcomes(result, "delisting") == {
        "ZZD1": "marked",
        "ZZD2": "already",
        "ZZD3": "declined:vendor_lists_it",
        "ZZD4": "declined:relisted",
        "ZZD5": "declined:traded_after",
    }
    (acquired,) = [c for c in result.changes if c.symbol == "ZZD1"]
    assert acquired.detail == "acquisitionby"
    row = _security_row(db, marked)
    assert (row["delisted_date"], row["is_actively_trading"]) == (day, False)
    for sid in (listed, relisted, trading):
        assert _security_row(db, sid)["delisted_date"] is None
    (flag,) = db.fetchall(
        "SELECT security_id, detail FROM ops.data_quality_flag "
        "WHERE check_name = 'security_delisting_contradicted'"
    )
    assert flag["security_id"] == trading and flag["detail"]["source"] == "sharadar"

    # The open flag holds the delisting back on the next run, without asking again.
    again = su.run(db, since=SINCE)
    assert _outcomes(again, "delisting")["ZZD5"] == "declined:pending_decision"


@integration
def test_new_listings_are_minted_unless_a_security_holds_the_ticker(db):
    new_day = Y(2026, 10, 10)
    _vendor(db, "900421", "ZZN1", first=new_day, exchange="NASDAQ")
    _event(db, "900421", new_day, "listed", "ZZN1")
    _vendor(db, "900422", "ZZN2", first=new_day)
    held = _security(db, "ZZN2")  # FMP minted it; unlinked
    _vendor(db, "900423", "ZZN3", first=Y(2020, 1, 2))  # not new
    _vendor(db, "900424", "ZZN4", first=new_day, category="MF", table="funds")
    _vendor(db, "900425", "ZZN5", first=new_day, exchange="OTC")
    _vendor(db, "900426", "ZZN6", first=new_day, delisted=True)

    result = su.run(db, since=SINCE)
    assert _outcomes(result, "listing") == {
        "ZZN1": "minted",
        "ZZN2": "held",
        "ZZN4": "out_of_scope",
        "ZZN5": "out_of_scope",
    }
    (minted,) = [c.security_id for c in result.changes if c.outcome == "minted"]
    row = _security_row(db, minted)
    assert (row["source"], row["is_actively_trading"], row["exchange_code"]) == (
        "sharadar",
        True,
        "NASDAQ",
    )
    assert row["ipo_date"] == new_day
    (period,) = db.fetchall(
        "SELECT valid_from, valid_to FROM core.symbol_xref WHERE security_id = %s",
        (minted,),
    )
    assert (period["valid_from"], period["valid_to"]) == (new_day, None)
    (link,) = repo.vendor_links_of(db, minted, "sharadar")
    assert (link["vendor_key"], link["match_method"], link["is_feed"]) == (
        "900421",
        "minted",
        True,
    )
    assert repo.resolve_security_id(db, "ZZN2") == held
    assert result.counts()[("listing", "held")] == 1

    # Linked now, so the next run leaves it alone.
    assert "ZZN1" not in _outcomes(su.run(db, since=SINCE), "listing")


@integration
def test_a_dry_run_reports_and_writes_nothing(db):
    ids = _renames_fixture(db)
    _vendor(db, "900421", "ZZN1", first=Y(2026, 10, 10))
    before = db.fetchval("SELECT count(*) FROM core.security")

    result = su.run(db, since=SINCE, dry_run=True)
    assert _outcomes(result, "rename")["ZZD->ZZE"] == "applied"
    assert _outcomes(result, "listing") == {"ZZN1": "minted"}
    assert result.run_id is None
    assert _security_row(db, ids["chain"])["primary_symbol"] == "ZZC"
    assert db.fetchval("SELECT count(*) FROM core.security") == before
    assert not db.fetchall("SELECT 1 FROM core.symbol_change")
    assert not db.fetchall("SELECT 1 FROM ops.ingestion_run")


@integration
def test_the_window_opens_before_the_last_real_run(db):
    today = Y(2026, 10, 22)
    assert su.default_since(db, today) == today - timedelta(days=su.LOOKBACK_DAYS)
    _vendor(db, "900401", "ZZB")
    su.run(db, since=SINCE)
    started = db.fetchval("SELECT started_at FROM ops.ingestion_run")
    assert su.default_since(db, today) == started.date() - timedelta(days=30)


@integration
def test_the_command_refuses_a_real_run_before_the_cutover(db, tmp_path, monkeypatch):
    _vendor(db, "900421", "ZZN1", first=Y(2026, 10, 10))
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)

    def invoke(vendor, *args):
        rc = tmp_path / f"rc-{vendor}"
        rc.write_text(f'[general]\nprimary_price_vendor = "{vendor}"\n')
        return CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "ingest", "sharadar-universe", *args]
            + ["--since", "2026-09-01"],
        )

    out = invoke("fmp")
    assert out.exit_code != 0 and "--dry-run" in out.output
    out = invoke("fmp", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. Events from 2026-09-01." in out.output
    assert "listing   minted" in out.output and "ZZN1 (900421)" in out.output
    assert db.fetchval("SELECT count(*) FROM core.security") == 0
    out = invoke("sharadar")
    assert out.exit_code == 0, out.output
    assert db.fetchval("SELECT count(*) FROM core.security") == 1


@integration
def test_an_empty_vendor_master_is_an_error(db):
    with pytest.raises(su.UniverseError, match="sharadar-master"):
        su.run(db, since=SINCE, dry_run=True)
