"""
A Sharadar full price file is read by the tickers it was fetched with, not
tonight's (#238; the tests need FAFNIR_TEST_DSN).

SEP and SFP rows carry no permaticker, only the ticker of the day the file was
fetched, and Sharadar files a key's history under its current ticker. Here two
keys pass a ticker between the full file and tonight's TICKERS, as on 2026-11-03:
key 900201 is ZZOLD in the file and ZZNEW tonight, and key 900202, ZZNEW's holder
in the file, is ZZNEW1 tonight. Read by tonight's tickers, 900201 would take
900202's rows. Every key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fafnir.db import repository as repo
from fafnir.ingest import sharadar_prices as sp
from fafnir.ingest import sharadar_prune as pr
from fafnir.ingest import vendor_correct as vc

pytestmark = pytest.mark.integration

D = Decimal
Y = date
PRICES = "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"
ACTIONS = "date,action,ticker,name,value,contraticker,contraname\n"
TICKERS = (
    "table,permaticker,ticker,name,exchange,isdelisted,category,cusips,siccode,"
    "sicsector,sicindustry,figi,famaindustry,sector,industry,scalemarketcap,"
    "scalerevenue,relatedtickers,currency,location,lastupdated,firstadded,"
    "firstpricedate,lastpricedate,firstquarter,lastquarter,secfilings,companysite\n"
)
AS_OF = Y(2026, 1, 2)

# 900201's own rows, under the ticker the file names it by.
OLD_ROWS = ("2010-01-04", "2010-01-05", "2010-01-06")
# 900202's, under ZZNEW: the ticker 900201 holds tonight.
NEW_ROWS = ("2005-03-01", "2010-01-05", "2010-01-06")


def _bar(ticker, day, close):
    return f"{ticker},{day},{close},{close},{close},{close},100,{close},{close},{day}\n"


def _file(db, bulk, table, body, *, kind="full"):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (bulk / f"{sha}.csv").write_bytes(data)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind=kind,
        filters={"lastupdated.gte": "2026-01-01"} if kind == "delta" else {},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )
    if kind == "full":
        repo.supersede_sharadar_fulls(db, table, entry["file_id"])
    return entry


def _tickers(db, bulk, listing):
    """A TICKERS file listing ``{key: ticker}`` in SEP."""
    body = TICKERS + "".join(
        f"SEP,{key},{ticker},{ticker} CO,NYSE,N,Domestic Common Stock,,,,,,,,,,,,"
        "USD,,2026-01-02,,,,,,,\n"
        for key, ticker in listing.items()
    )
    return _file(db, bulk, "tickers", body)


def _vendor(db, key, ticker):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', %s, %s)",
        (key, ticker),
    )


def _security(db, symbol, key, *, source, feed, valid_from=Y(2000, 1, 3)):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        is_actively_trading=source == "fmp",
        delisted_date=None if source == "fmp" else Y(2017, 9, 1),
        source=source,
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=valid_from,
        match_method="minted" if feed else "manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=feed,
    )
    return sid


def _rename(db, key, old, new, day=None):
    """ACTIONS' two rows for a ticker change, resolved to ``key``."""
    day = day or date.today()
    for action, ticker, contra in (
        ("tickerchangefrom", new, old),
        ("tickerchangeto", old, new),
    ):
        db.execute(
            "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
            "action, ticker, contra_ticker) VALUES ('sharadar', %s, %s, %s, %s, %s)",
            (key, day, action, ticker, contra),
        )


def _swap(db, bulk, *, paired=True):
    """The full SEP file and tonight's master: 900201 ZZOLD -> ZZNEW, 900202
    ZZNEW -> ZZNEW1, and 900203 ZZC throughout. The SEP file, and the TICKERS file
    fetched with it when ``paired``."""
    tickers = _tickers(
        db, bulk, {"900201": "ZZOLD", "900202": "ZZNEW", "900203": "ZZC"}
    )
    # Tonight's TICKERS, loaded since: the newer file, and core.vendor_security.
    _tickers(db, bulk, {"900201": "ZZNEW", "900202": "ZZNEW1", "900203": "ZZC"})
    for key, ticker in (("900201", "ZZNEW"), ("900202", "ZZNEW1"), ("900203", "ZZC")):
        _vendor(db, key, ticker)
    _rename(db, "900201", "ZZOLD", "ZZNEW")
    _rename(db, "900202", "ZZNEW", "ZZNEW1")
    _rename(db, "900203", "ZZB", "ZZC", Y(2001, 6, 1))  # long before the file
    body = (
        PRICES
        + "".join(_bar("ZZOLD", d, "10") for d in OLD_ROWS)
        + "".join(_bar("ZZNEW", d, "50") for d in NEW_ROWS)
        + _bar("ZZC", "2010-01-05", "30")
    )
    sep = _file(db, bulk, "stocks", body)
    if paired:
        repo.pair_sharadar_tickers(db, sep["file_id"], tickers["file_id"])
    _file(db, bulk, "actions", ACTIONS)
    return sep, tickers


def _bars(db, sid):
    return {
        r["trade_date"]: (r["close"], r["source"])
        for r in db.fetchall(
            "SELECT trade_date, close, source FROM core.daily_price "
            "WHERE security_id = %s",
            (sid,),
        )
    }


def _days(*days):
    return {Y.fromisoformat(d) for d in days}


# ---------------------------------------------------------------------------
# Backfill and gap-fill
# ---------------------------------------------------------------------------


def test_the_backfill_reads_each_key_by_the_ticker_its_file_names(db, tmp_path):
    renamed = _security(db, "ZZNEW", "900201", source="sharadar", feed=True)
    previous = _security(db, "ZZNEW1", "900202", source="sharadar", feed=True)
    _swap(db, tmp_path)
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=AS_OF)
    assert result.counts["ticker_as_of_file"] == 2
    assert result.counts["ticker_changed_since_file"] == 0
    # Each key gets its own rows: 900201 ZZOLD's, not ZZNEW's (900202's).
    assert set(_bars(db, renamed)) == _days(*OLD_ROWS)
    assert {c for c, _ in _bars(db, renamed).values()} == {D("10")}
    assert set(_bars(db, previous)) == _days(*NEW_ROWS)
    assert {c for c, _ in _bars(db, previous).values()} == {D("50")}


def test_the_gap_fill_reads_the_linked_keys_own_rows(db, tmp_path):
    fmp = _security(
        db, "ZZNEW", "900201", source="fmp", feed=False, valid_from=Y(2010, 1, 4)
    )
    _swap(db, tmp_path)
    result = sp.load(db, tmp_path, backfill=False, gap_fill=True, as_of=AS_OF)
    assert result.counts["ticker_as_of_file"] == 1
    assert _bars(db, fmp) == {d: (D("10"), "sharadar") for d in _days(*OLD_ROWS)}


def test_a_file_fetched_without_tickers_skips_the_keys_renamed_since(
    db, tmp_path, caplog
):
    renamed = _security(db, "ZZNEW", "900201", source="sharadar", feed=True)
    previous = _security(db, "ZZNEW1", "900202", source="sharadar", feed=True)
    steady = _security(db, "ZZC", "900203", source="sharadar", feed=True)
    _swap(db, tmp_path, paired=False)  # fetched before #238
    plan = sp.load(
        db, tmp_path, backfill=True, gap_fill=False, dry_run=True, as_of=AS_OF
    )
    assert plan.counts["ticker_changed_since_file"] == 2
    assert "--tables stocks --force" in caplog.text
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=AS_OF)
    assert _bars(db, renamed) == {} and _bars(db, previous) == {}
    # A rename long before the file is in it: that key reads.
    assert _bars(db, steady) == {Y(2010, 1, 5): (D("30"), "sharadar")}
    assert result.written == 1


def test_a_tickers_file_no_longer_on_disk_falls_back_to_skipping(db, tmp_path):
    _security(db, "ZZNEW", "900201", source="sharadar", feed=True)
    _, tickers = _swap(db, tmp_path)
    (tmp_path / tickers["path"]).unlink()
    result = sp.load(
        db, tmp_path, backfill=True, gap_fill=False, dry_run=True, as_of=AS_OF
    )
    assert result.counts["ticker_changed_since_file"] == 1
    assert result.read == 0


def test_a_key_the_tickers_file_lacks_is_not_in_the_file(db, tmp_path):
    late = _security(db, "ZZLATE", "900204", source="sharadar", feed=True)
    _vendor(db, "900204", "ZZLATE")
    _swap(db, tmp_path)
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=AS_OF)
    assert result.counts["not_in_file"] == 1 and _bars(db, late) == {}


def test_two_keys_on_one_ticker_read_neither(db, tmp_path):
    one = _security(db, "ZZS", "900301", source="sharadar", feed=True)
    two = _security(db, "ZZS1", "900302", source="sharadar", feed=True)
    _vendor(db, "900301", "ZZS")
    _vendor(db, "900302", "ZZS")
    _file(db, tmp_path, "stocks", PRICES + _bar("ZZS", "2010-01-05", "5"))
    _file(db, tmp_path, "actions", ACTIONS)
    result = sp.load(db, tmp_path, backfill=True, gap_fill=False, as_of=AS_OF)
    assert result.counts["ticker_shared"] == 2
    assert _bars(db, one) == {} and _bars(db, two) == {}


# ---------------------------------------------------------------------------
# vendor correct
# ---------------------------------------------------------------------------


def _fmp_bars(db, sid, close):
    c = D(close)
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
                "volume": 100,
            }
            for d in sorted(_days(*OLD_ROWS))
        ],
    )


@pytest.mark.parametrize("paired", [True, False])
def test_vendor_correct_never_compares_another_issuers_rows(db, tmp_path, paired):
    # FMP's bars agree with 900201's own rows. Read by tonight's ticker, the file's
    # ZZNEW rows (900202's, at 50) would differ on two dates and replace them.
    fmp = _security(
        db, "ZZNEW", "900201", source="fmp", feed=False, valid_from=Y(2010, 1, 4)
    )
    _fmp_bars(db, fmp, "10")
    _swap(db, tmp_path, paired=paired)
    result = vc.correct(db, tmp_path, as_of=AS_OF)
    assert (result.counts["differ"], result.corrected) == (0, 0)
    if paired:
        assert result.counts["ticker_as_of_file"] == 1
        assert result.read == len(OLD_ROWS)
    else:
        assert result.counts["ticker_changed_since_file"] == 1
        assert result.read == 0
    assert {s for _, s in _bars(db, fmp).values()} == {"fmp"}


# ---------------------------------------------------------------------------
# The nightly
# ---------------------------------------------------------------------------


def test_a_fresh_key_renamed_since_reads_only_tonights_deltas(db, tmp_path):
    _file(db, tmp_path, "stocks", PRICES + _bar("ZZFULLS", "2026-10-15", "1"))
    _file(db, tmp_path, "funds", PRICES + _bar("ZZFULLF", "2026-10-15", "1"))
    _file(db, tmp_path, "actions", ACTIONS)
    # An applied delta files ZZNEW's previous holder.
    _file(
        db, tmp_path, "stocks", PRICES + _bar("ZZNEW", "2026-10-19", "50"), kind="delta"
    )
    assert sp.nightly(db, tmp_path, as_of=Y(2026, 10, 20)).securities == 0
    # Tonight: 900201 is renamed to ZZNEW and minted, with tonight's delta.
    _vendor(db, "900201", "ZZNEW")
    _rename(db, "900201", "ZZOLD", "ZZNEW")
    fresh = _security(db, "ZZNEW", "900201", source="sharadar", feed=True)
    _file(
        db, tmp_path, "stocks", PRICES + _bar("ZZNEW", "2026-10-20", "8"), kind="delta"
    )
    result = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 21))
    assert result.counts["ticker_changed_since_file"] == 1
    assert _bars(db, fresh) == {Y(2026, 10, 20): (D("8"), "sharadar")}


# ---------------------------------------------------------------------------
# sharadar-prune
# ---------------------------------------------------------------------------


def test_the_prune_keeps_the_tickers_file_a_kept_price_file_names(db, tmp_path):
    unpaired = _tickers(db, tmp_path, {"900201": "ZZOLD"})
    _, paired = _swap(db, tmp_path)
    _tickers(db, tmp_path, {"900201": "ZZNEW"})  # the next night's: current
    # Two months on: every superseded TICKERS file is past keep_days, and the
    # newest of them is kept as the previous one.
    later = datetime.now(timezone.utc) + timedelta(days=60)
    plan = pr.plan(db, tmp_path, keep_days=7, now=later)
    removed = {r.file_id: r.reason for r in plan.removals}
    assert removed == {unpaired["file_id"]: "superseded"}
    assert plan.kept["paired"] == 1 and plan.kept["previous"] == 1
