"""A venue transfer must not freeze a security (ADR 0014), against a real database.

ET, SUN, SUNC, USAC, DDS and DDT moved from the NYSE to the Texas Stock Exchange
on 2026-10-05 under the same tickers. Two paths froze them, or would have:

* the delisting sweep stamped each delisted from FMP's delisted feed;
* the universe load copies FMP's isActivelyTrading, false for each of them after the
  move, and the price step polled active rows only.

These tests cover the sweep's price-feed check, the wider price universe, the
operator's venue override, and `security venue-transfer` / `security delist`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import delisted, security_master
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT

pytestmark = pytest.mark.integration

MOVED = dt.date(2026, 10, 5)
TODAY = dt.date(2026, 10, 12)
ET_NAME = "Energy Transfer LP"
CONTRADICTED = "security_delisting_contradicted"


def _mk(db, symbol="ET", *, venue="NYSE", last_bar=MOVED, name=ET_NAME, bars=6):
    """A security on ``venue`` with weekday bars up to ``last_bar`` and a price
    watermark there."""
    security_master.ensure_venue(db, venue)
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code=venue,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    days, d = [], last_bar
    while len(days) < bars:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": 20,
                "high": 21,
                "low": 19,
                "close": 20.5,
                "volume": 7_600_000,
            }
            for d in days
        ],
    )
    repo.set_watermark(db, "fmp", PRICE_ENDPOINT, last_bar, sid)
    return sid


def _security(db, sid):
    return db.fetchone(
        "SELECT is_actively_trading, delisted_date, exchange_code, venue_override,"
        " venue_override_left FROM core.security WHERE security_id = %s",
        (sid,),
    )


def _xref(db, sid):
    return db.fetchall(
        "SELECT symbol, valid_from, valid_to FROM core.symbol_xref"
        " WHERE security_id = %s ORDER BY valid_from",
        (sid,),
    )


def _flags(db, check, sid=None):
    return db.fetchall(
        "SELECT dq_flag_id, security_id, severity, record_key, detail, resolved_at,"
        " accepted_at, resolution_note FROM ops.data_quality_flag"
        " WHERE check_name = %s AND (%s::bigint IS NULL OR security_id = %s)"
        " ORDER BY dq_flag_id",
        (check, sid, sid),
    )


def _bar(day, volume=7_200_000):
    return {"date": day, "close": 20.62, "volume": volume}


# What FMP served for ET after the move: bars on and after 2026-10-05.
_TXSE_BARS = [_bar(d) for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08")]
_NYSE_DELISTING = {"symbol": "ET", "exchange": "NYSE", "delistedDate": "2026-10-05"}


class _FakeFMP:
    """The delisted feed, the price feed and the screener, as FMP served them."""

    bytes_downloaded = 0

    def __init__(self, *, feed=(), bars=None, screener=()):
        self.feed = list(feed)
        self.bars = bars or {}
        self.screener = list(screener)

    def delisted_companies(self, *, max_pages=5):
        return self.feed

    def eod_raw(self, symbol, from_date=None, to_date=None):
        return [b for b in self.bars.get(symbol, []) if b["date"] >= from_date]

    def company_screener(self, *, exchange=None, **_kw):
        # An unfiltered page on the first venue asked: each row names its own venue.
        return self.screener if exchange == "NASDAQ" else []


def _screener_row(symbol="ET", venue="NYSE", active=False, name=ET_NAME):
    """ET as FMP's screener had it after the move: on the NYSE, and inactive."""
    return {
        "symbol": symbol,
        "name": name,
        "exchangeShortName": venue,
        "isActivelyTrading": active,
        "isEtf": False,
    }


def _sweep_run(db):
    return db.fetchone(
        "SELECT rows_inserted, rows_quarantined, status, params FROM ops.ingestion_run"
        " WHERE endpoint = 'delisted-companies' ORDER BY ingestion_run_id DESC LIMIT 1"
    )


# ---------------------------------------------------------------------------
# A: the delisting sweep asks the price feed
# ---------------------------------------------------------------------------


def test_the_sweep_declines_a_delisting_the_vendor_has_bars_after(db):
    sid = _mk(db)
    fmp = _FakeFMP(feed=[_NYSE_DELISTING], bars={"ET": _TXSE_BARS})

    assert delisted.load_delisted(db, fmp, today=TODAY) == (0, 1)

    assert _security(db, sid)["delisted_date"] is None
    assert _xref(db, sid)[-1]["valid_to"] is None
    (flag,) = _flags(db, CONTRADICTED, sid)
    assert flag["severity"] == "warn" and flag["resolved_at"] is None
    assert flag["record_key"] == {"symbol": "ET", "delisted_date": "2026-10-05"}
    assert flag["detail"]["bars_after"] == 3
    assert flag["detail"]["first_after"] == "2026-10-06"
    assert flag["detail"]["median_volume_before_20"] == 7_600_000
    run = _sweep_run(db)
    assert run["rows_quarantined"] == 1 and run["status"] == "partial"
    assert run["params"]["declined"] == {"still_trading": 1}

    # A second night raises no second flag.
    assert delisted.load_delisted(db, fmp, today=TODAY) == (0, 1)
    assert len(_flags(db, CONTRADICTED, sid)) == 1


def test_the_sweep_marks_a_delisting_with_no_bar_after_it(db):
    sid = _mk(db)
    fmp = _FakeFMP(feed=[_NYSE_DELISTING], bars={"ET": _TXSE_BARS[:1]})

    assert delisted.load_delisted(db, fmp, today=TODAY) == (1, 1)
    assert _security(db, sid)["delisted_date"] == MOVED


# ---------------------------------------------------------------------------
# B: the nightly price universe
# ---------------------------------------------------------------------------


def _universe(db):
    return {r["symbol"] for r in repo.universe_securities(db)}


def test_an_inactive_security_that_traded_recently_stays_in_the_universe(db):
    today = dt.date.today()
    recent = _mk(db, "RCNT", last_bar=today - dt.timedelta(days=3))
    stale = _mk(db, "OLDX", last_bar=today - dt.timedelta(days=31))
    gone = _mk(db, "GONE", last_bar=today - dt.timedelta(days=3))
    _mk(db, "ACTV", last_bar=today - dt.timedelta(days=90))
    db.execute(
        "UPDATE core.security SET is_actively_trading = FALSE"
        " WHERE security_id = ANY(%s)",
        ([recent, stale],),
    )
    repo.mark_delisted(db, security_id=gone, delisted_date=today)
    split = _mk(db, "SPLT", last_bar=today - dt.timedelta(days=3))
    db.execute(
        "UPDATE core.security SET source = 'operator' WHERE security_id = %s", (split,)
    )

    assert _universe(db) == {"RCNT", "ACTV"}
    assert {"OLDX", "GONE"} <= {
        r["symbol"] for r in repo.universe_securities(db, include_inactive=True)
    }


def test_ingest_prices_polls_the_nightly_universe(db, tmp_path, monkeypatch):
    from fafnir.ingest import daily_price

    today = dt.date.today()
    _mk(db, "RCNT", last_bar=today - dt.timedelta(days=3))
    _mk(db, "OLDX", last_bar=today - dt.timedelta(days=31))
    db.execute(
        "UPDATE core.security SET is_actively_trading = FALSE"
        " WHERE primary_symbol IN ('RCNT', 'OLDX')"
    )
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.setattr(cli, "_fmp_client", lambda cfg: _FakeFMP())
    asked = []

    def load_prices(database, fmp, symbols, **kw):
        asked.extend(symbols)
        return 0

    monkeypatch.setattr(daily_price, "load_prices", load_prices)
    result = CliRunner().invoke(cli.main, ["--config", str(rc), "ingest", "prices"])

    assert result.exit_code == 0, result.output
    assert asked == ["RCNT"]


def test_the_load_flags_a_row_it_turns_inactive_while_still_trading(db):
    """TCBI's exposure: FMP calls a moved security inactive, and the load copies it.
    A real wind-down the vendor reports late raises nothing."""
    today = dt.date.today()
    sid = _mk(db, "TCBI", venue="NASDAQ", last_bar=today - dt.timedelta(days=1))
    _mk(db, "WIND", venue="NASDAQ", last_bar=today - dt.timedelta(days=40))
    fmp = _FakeFMP(
        screener=[_screener_row("TCBI", "NASDAQ"), _screener_row("WIND", "NASDAQ")]
    )

    security_master.load_securities(db, fmp)

    assert _security(db, sid)["is_actively_trading"] is False
    (flag,) = _flags(db, "security_inactive_but_trading")
    assert flag["security_id"] == sid and flag["severity"] == "info"
    assert flag["record_key"] == {"symbol": "TCBI"}
    # Still polled, though inactive; the wind-down is not.
    assert "TCBI" in _universe(db) and "WIND" not in _universe(db)


# ---------------------------------------------------------------------------
# C and D: security venue-transfer, the override, security delist
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_cli(db, tmp_path, monkeypatch):
    """Run a `fafnir security` command against the test database and a fake vendor."""
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    vendor = _FakeFMP(bars={"ET": _TXSE_BARS})
    monkeypatch.setattr(cli, "_fmp_client", lambda cfg: vendor)

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "security", *map(str, args)]
        )

    run.vendor = vendor
    return run


def _stamped_et(db):
    """ET as the 2026-10-07 sweep left it: delisted 2026-10-05, ticker closed."""
    sid = _mk(db)
    assert repo.mark_delisted(db, security_id=sid, delisted_date=MOVED)
    return sid


def _transfer(run_cli, sid, *extra):
    return run_cli(
        "venue-transfer",
        sid,
        "--venue",
        "TXSE",
        "--date",
        "2026-10-05",
        "-m",
        "NYSE->TXSE 2026-10-05",
        "--by",
        "t",
        *extra,
    )


def test_venue_transfer_dry_run_shows_the_change_and_changes_nothing(db, run_cli):
    sid = _stamped_et(db)
    before = (_security(db, sid), _xref(db, sid))

    result = _transfer(run_cli, sid, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "venue      NYSE         TXSE, held until" in result.output
    assert "Ticker period re-opened: ET 1900-01-01..2026-10-05" in result.output
    assert "Vendor bars after 2026-10-05: 3, 2026-10-06..2026-10-08" in result.output
    assert "volume 7,200,000" in result.output
    assert "watermark 2026-10-05" in result.output
    assert "ET 2026-10-05" in result.output
    assert "Dry run: nothing changed." in result.output
    assert (_security(db, sid), _xref(db, sid)) == before
    assert _flags(db, CONTRADICTED) == []


def test_venue_transfer_reverses_a_stamped_delisting(db, run_cli):
    """The ET shape. Afterwards the sweep declines the old NYSE record unasked."""
    sid = _stamped_et(db)

    result = _transfer(run_cli, sid, "--yes")

    assert result.exit_code == 0, result.output
    assert _security(db, sid) == {
        "is_actively_trading": True,
        "delisted_date": None,
        "exchange_code": "TXSE",
        "venue_override": "TXSE",
        "venue_override_left": "NYSE",
    }
    # The same period, re-opened: the ticker never changed hands.
    assert _xref(db, sid) == [
        {"symbol": "ET", "valid_from": dt.date(1900, 1, 1), "valid_to": None}
    ]
    (flag,) = _flags(db, CONTRADICTED, sid)
    assert flag["accepted_at"] is not None
    assert flag["record_key"] == {"symbol": "ET", "delisted_date": "2026-10-05"}
    audit = db.fetchone(
        "SELECT source, status, params FROM ops.ingestion_run"
        " WHERE endpoint = 'security-venue-transfer'"
    )
    assert audit["source"] == "operator" and audit["status"] == "success"
    params = audit["params"]
    assert params["before"]["delisted_date"] == "2026-10-05"
    assert params["before"]["exchange_code"] == "NYSE"
    assert params["after"]["exchange_code"] == "TXSE"
    assert params["note"] == "NYSE->TXSE 2026-10-05" and params["by"] == "t"
    assert params["periods_reopened"] == ["ET 1900-01-01..2026-10-05"]
    assert params["flags_accepted"] == [flag["dq_flag_id"]]

    # The feed still carries the NYSE record. The sweep declines it, unasked.
    feed = _FakeFMP(feed=[_NYSE_DELISTING])  # no bars: asking would mark it
    assert delisted.load_delisted(db, feed, today=TODAY) == (0, 1)
    assert _security(db, sid)["delisted_date"] is None
    run = _sweep_run(db)
    assert run["params"]["declined"] == {"known_transfer": 1}
    assert run["params"]["prices_requested"] == 0
    assert run["status"] == "success"


def test_venue_transfer_records_a_move_the_sweep_has_not_stamped(db, run_cli):
    """The TCBI shape: still listed, but FMP's load has turned it inactive, and the
    sweep had already flagged its NASDAQ record."""
    sid = _mk(db, "TCBI", venue="NASDAQ", last_bar=dt.date(2026, 10, 9))
    db.execute(
        "UPDATE core.security SET is_actively_trading = FALSE WHERE security_id = %s",
        (sid,),
    )
    repo.add_dq_flag_once(
        db,
        check_name=CONTRADICTED,
        security_id=sid,
        table_name="core.security",
        record_key={"symbol": "TCBI", "delisted_date": "2026-10-07"},
    )
    repo.add_dq_flag_once(
        db,
        check_name="security_inactive_but_trading",
        severity="info",
        security_id=sid,
        table_name="core.security",
        record_key={"symbol": "TCBI"},
    )
    run_cli.vendor.bars["TCBI"] = [_bar("2026-10-08"), _bar("2026-10-09")]

    result = run_cli(
        "venue-transfer",
        sid,
        "--venue",
        "TXSE",
        "--date",
        "2026-10-08",
        "-m",
        "NASDAQ->TXSE 2026-10-08",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    row = _security(db, sid)
    assert row["is_actively_trading"] is True and row["exchange_code"] == "TXSE"
    assert row["venue_override_left"] == "NASDAQ"
    accepted = {
        f["record_key"]["delisted_date"]
        for f in _flags(db, CONTRADICTED, sid)
        if f["accepted_at"] is not None
    }
    # The record the sweep flagged, and one dated the move, pre-recorded.
    assert accepted == {"2026-10-07", "2026-10-08"}
    (info,) = _flags(db, "security_inactive_but_trading", sid)
    assert info["resolved_at"] is not None and info["accepted_at"] is None


def test_the_override_holds_against_the_nightly_load_until_fmp_catches_up(db, run_cli):
    sid = _stamped_et(db)
    assert _transfer(run_cli, sid, "--yes").exit_code == 0

    # FMP's screener after the move: ET on the NYSE, inactive. Held.
    security_master.load_securities(db, _FakeFMP(screener=[_screener_row()]))
    row = _security(db, sid)
    assert row["exchange_code"] == "TXSE" and row["is_actively_trading"] is True
    assert row["venue_override"] == "TXSE"
    assert _flags(db, "security_inactive_but_trading") == []

    # FMP names another venue: it has caught up, and the override clears.
    security_master.load_securities(
        db, _FakeFMP(screener=[_screener_row(venue="TXSE", active=True)])
    )
    row = _security(db, sid)
    assert row["exchange_code"] == "TXSE" and row["venue_override"] is None
    assert row["venue_override_left"] is None and row["is_actively_trading"] is True


def test_venue_transfer_refuses_a_ticker_another_listed_security_holds(db, run_cli):
    sid = _stamped_et(db)
    newer = _mk(db, venue="TXSE")  # the fork a security-master load could make
    assert newer != sid

    result = _transfer(run_cli, sid, "--yes")

    assert result.exit_code != 0
    assert f"ET is held by security {newer}" in result.output
    assert "security merge" in result.output
    assert _security(db, sid)["delisted_date"] == MOVED


def test_venue_transfer_refuses_without_a_bar_after_the_date(db, run_cli):
    sid = _stamped_et(db)
    run_cli.vendor.bars["ET"] = _TXSE_BARS[:1]  # only the move date itself

    result = _transfer(run_cli, sid, "--yes")

    assert result.exit_code != 0
    assert "no bar for ET after 2026-10-05" in result.output
    assert _security(db, sid)["delisted_date"] == MOVED


def test_venue_transfer_refuses_otc(db, run_cli):
    sid = _stamped_et(db)
    result = run_cli(
        "venue-transfer", sid, "--venue", "OTC", "--date", "2026-10-05", "-m", "x"
    )
    assert result.exit_code != 0
    said = " ".join(result.output.split())  # refusals are wrapped
    assert "not a listed US venue" in said and "`security delist`" in said


def test_delist_stamps_a_declined_delisting_and_resolves_its_flag(db, run_cli):
    """The GRP-UN shape: it left for OTC, so the sweep declined and flagged it."""
    sid = _mk(db)
    fmp = _FakeFMP(feed=[_NYSE_DELISTING], bars={"ET": _TXSE_BARS})
    delisted.load_delisted(db, fmp, today=TODAY)
    assert len(_flags(db, CONTRADICTED, sid)) == 1

    dry = run_cli(
        "delist", sid, "--date", "2026-10-05", "-m", "left for OTC", "--dry-run"
    )
    assert dry.exit_code == 0, dry.output
    assert "Ticker period closed: ET 1900-01-01.." in dry.output
    assert _security(db, sid)["delisted_date"] is None

    result = run_cli(
        "delist", sid, "--date", "2026-10-05", "-m", "left for OTC", "--yes"
    )

    assert result.exit_code == 0, result.output
    assert _security(db, sid)["delisted_date"] == MOVED
    assert _xref(db, sid)[-1]["valid_to"] == MOVED
    (flag,) = _flags(db, CONTRADICTED, sid)
    assert flag["resolved_at"] is not None and flag["accepted_at"] is None
    assert flag["resolution_note"] == "Delisted on 2026-10-05: left for OTC"
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.ingestion_run WHERE endpoint = 'security-delist'"
            " AND status = 'success'"
        )
        == 1
    )

    again = run_cli("delist", sid, "--date", "2026-10-05", "-m", "x", "--yes")
    assert again.exit_code != 0 and "already delisted" in again.output
