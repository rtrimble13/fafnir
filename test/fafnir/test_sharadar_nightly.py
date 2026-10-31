"""
The Sharadar-primary night (plan SA-1005, #98; needs FAFNIR_TEST_DSN): the price
deltas (`source sharadar-delta`), their nightly load (`ingest sharadar-prices
--nightly`, and its `--as-if-fed` comparison with FMP), the actions guard for fed
securities, and `ingest sharadar`, which runs the night in order.

The client is real down to the HTTP: only the table API's answers and the bulk
download are faked. Every key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sharadar_actions as sa
from fafnir.ingest import sharadar_bulk as bulk
from fafnir.ingest import sharadar_prices as sp
from fafnir.sources.base import TransientSourceError
from fafnir.sources.sharadar import SharadarClient

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


def _json_bar(ticker, day, close, *, stamp=None, unadj=None):
    """A SEP row as the table API answers it: dates as text, prices as numbers."""
    return {
        "ticker": ticker,
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "closeadj": close,
        "closeunadj": unadj if unadj is not None else close,
        "lastupdated": stamp or day,
    }


def _json_sf1(dim, period, filed, *, stamp, ticker="ZZA", **values):
    """An SF1 row as the table API answers it: ``date`` is the filing date, and a
    metric is a number or null."""
    return {
        "ticker": ticker,
        "dimension": dim,
        "calendardate": period,
        "date": filed,
        "reportperiod": period,
        "fiscalperiod": "2026-Q2",
        "lastupdated": stamp,
        "revenue": 1000.5,
        "netinc": None,
        **values,
    }


def _csv_bar(ticker, day, close, *, stamp=None):
    return f"{ticker},{day},{close},{close},{close},{close},100,{close},{close},{stamp or day}\n"


class _FakeNight(SharadarClient):
    """Answers SEP/SFP queries from ``tables`` (applying ``lastupdated.gte`` unless
    ``ignore_filter``, and ``from``, ``to`` and ``sort=date``) and serves ``files``
    as bulk zips."""

    def __init__(self, tables=None, files=None, *, ignore_filter=False):
        super().__init__("synthetic-key", rate_per_min=100_000)
        self.tables = tables or {}
        self.files = files or {}
        self.ignore_filter = ignore_filter
        self.asked: list[tuple[str, dict]] = []

    def _call(self, table, params=None):
        params = dict(params or {})
        self.asked.append((table, params))
        since = params.get("lastupdated.gte")
        rows = [
            r
            for r in self.tables.get(table, [])
            if (self.ignore_filter or since is None or r["lastupdated"] >= since)
            and params.get("from", "") <= r["date"] <= params.get("to", "9999-12-31")
        ]
        if params.get("sort") == "date":
            rows.sort(key=lambda r: r["date"])
        offset = params.get("offset", 0)
        page = rows[offset : offset + params["limit"]]
        names = list(page[0]) if page else list(sp_columns())
        payload = {
            "datatable": {
                "data": [[r[c] for c in names] for r in page],
                "columns": [{"name": c} for c in names],
            }
        }
        self.bytes_downloaded += 100
        return payload, 200, 100

    def _bulk_link(self, table, years):
        return f"https://bulk.example/bulk-sharadar/{table}.csv.zip?X-Amz-Signature=s"

    def _stream(self, url):
        table = url.split("/bulk-sharadar/")[1].split(".csv.zip")[0]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"{table}.csv", self.files[table])
        self.bytes_downloaded += len(buf.getvalue())
        yield buf.getvalue()


def sp_columns():
    return PRICES.strip().split(",")


def _record(db, tmp_path, table, body, *, kind="full", since=None):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind=kind,
        filters={"lastupdated.gte": since} if kind == "delta" else {"years": "full"},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )
    return entry["file_id"]


def _vendor(db, key, ticker, table="stocks"):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', %s, %s, %s)",
        (table, key, ticker),
    )


def _security(db, symbol, *, source, key, feed, valid_from=Y(2000, 1, 3)):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        source=source,
    )
    _vendor(db, key, symbol)
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


def _store(db, sid, day, close, source):
    c = D(close)
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 100,
            }
        ],
        source=source,
    )


def _bars(db, sid):
    return {
        r["trade_date"]: (r["close"], r["source"])
        for r in db.fetchall(
            "SELECT trade_date, close, source FROM core.daily_price "
            "WHERE security_id = %s",
            (sid,),
        )
    }


def _fulls(db, tmp_path):
    """The backfill's full files: what the first nightly starts after. (A file is
    its content: two of the same would be one row.)"""
    _record(db, tmp_path, "stocks", PRICES + _csv_bar("ZZFULLS", "2026-10-15", "1"))
    _record(db, tmp_path, "funds", PRICES + _csv_bar("ZZFULLF", "2026-10-15", "1"))
    _record(db, tmp_path, "actions", ACTIONS)


def _other_delta(db, tmp_path, table, since):
    """A delta of nobody in scope."""
    body = PRICES + _csv_bar(f"ZZX{table}", since, "1")
    return _record(db, tmp_path, table, body, kind="delta", since=since)


# ---------------------------------------------------------------------------
# source sharadar-delta
# ---------------------------------------------------------------------------


def test_a_delta_is_recorded_in_the_bulk_column_order(db, tmp_path):
    today = date.today()
    rows = [_json_bar("ZZA", "2026-10-20", 10.125, stamp=today.isoformat())]
    rows[0] = {k: rows[0][k] for k in reversed(list(rows[0]))}  # any order served
    client = _FakeNight({"SEP": rows + [_json_bar("ZZA", "2026-10-01", 9.5)]})

    missing = bulk.fetch_delta(db, client, "SEP", tmp_path)
    assert missing.outcome == "failed" and "sharadar-bulk --tables SEP" in missing.error

    _record(db, tmp_path, "stocks", PRICES)
    result = bulk.fetch_delta(db, client, "SEP", tmp_path)
    since = (today - timedelta(days=bulk.DELTA_OVERLAP_DAYS)).isoformat()
    assert client.asked[-1][1]["lastupdated.gte"] == since
    assert (result.outcome, result.rows) == ("recorded", 1)
    entry = repo.sharadar_file(db, result.file_id)
    assert (entry["kind"], entry["filters"], entry["member"]) == (
        "delta",
        {"lastupdated.gte": since},
        None,
    )
    text = (tmp_path / entry["path"]).read_text()
    assert text == PRICES + (
        f"ZZA,2026-10-20,10.125,10.125,10.125,10.125,100,10.125,10.125,{today}\n"
    )
    assert entry["path"] == hashlib.sha256(text.encode()).hexdigest() + ".csv"
    run = db.fetchone(
        "SELECT endpoint, status, params FROM ops.ingestion_run "
        "WHERE ingestion_run_id = %s",
        (entry["ingestion_run_id"],),
    )
    assert (run["endpoint"], run["status"]) == ("delta:stocks", "success")
    assert run["params"]["dates"] == ["2026-10-20", "2026-10-20"]

    # The next starts from that file's fetch; the same content is one file.
    again = bulk.fetch_delta(db, client, "stocks", tmp_path, since=today)
    assert (again.outcome, again.file_id) == ("unchanged", result.file_id)
    empty = bulk.fetch_delta(db, client, "SEP", tmp_path, since=today + timedelta(1))
    assert (empty.outcome, empty.rows) == ("empty", 0)
    assert bulk.fetch_delta(db, client, "actions", tmp_path).outcome == "failed"


def test_a_delta_the_filter_did_not_apply_to_is_refused(db, tmp_path):
    _record(db, tmp_path, "stocks", PRICES)
    client = _FakeNight(
        {"SEP": [_json_bar("ZZA", "2020-01-02", 9.5)]}, ignore_filter=True
    )
    result = bulk.fetch_delta(db, client, "SEP", tmp_path, since=Y(2026, 10, 19))
    assert result.outcome == "failed"
    assert "the lastupdated filter was not applied" in result.error
    assert not repo.sharadar_deltas_after(db, "stocks", 0)


def test_a_delta_that_repeats_a_row_is_refused(db, tmp_path):
    # A repeated (ticker, date) is how rows lost to overlapping reads show.
    _record(db, tmp_path, "stocks", PRICES)
    bar = _json_bar("ZZA", "2026-10-20", 9.5, stamp="2026-10-20")
    client = _FakeNight({"SEP": [bar, dict(bar)]})
    result = bulk.fetch_delta(db, client, "SEP", tmp_path, since=Y(2026, 10, 19))
    assert result.outcome == "failed"
    assert "1 of 2 rows repeat a (ticker, date)" in result.error
    assert not repo.sharadar_deltas_after(db, "stocks", 0)


def test_an_sf1_delta_is_read_by_filing_date_and_keeps_its_twins(db, tmp_path):
    today = date.today().isoformat()
    first = _json_sf1("ARQ", "2026-06-30", "2026-08-01", stamp=today)
    # The same period filed again. The loader loads its first filing and counts the
    # later one (§6.3); the fetch passes both on, as the bulk file would.
    twin = {**first, "date": "2026-08-15", "revenue": 1001}
    old = _json_sf1("ARQ", "2026-03-31", "2026-05-01", stamp="2026-05-01")
    client = _FakeNight({"SF1": [twin, first, old]})
    _record(db, tmp_path, "fundamentals", "ticker,dimension\n")

    result = bulk.fetch_delta(db, client, "SF1", tmp_path)
    assert (result.outcome, result.rows, result.unmapped_columns) == (
        "recorded",
        2,
        [],  # SF1's metrics are numeric by default, not unknown
    )
    table, params = client.asked[-1]
    assert table == "SF1" and params["sort"] == "date" and "offset" not in params
    entry = repo.sharadar_file(db, result.file_id)
    assert (entry["vendor_table"], entry["kind"]) == ("fundamentals", "delta")
    assert (tmp_path / entry["path"]).read_text().splitlines() == [
        "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
        "revenue,netinc",
        f"ZZA,ARQ,2026-06-30,2026-08-01,2026-06-30,2026-Q2,{today},1000.5,",
        f"ZZA,ARQ,2026-06-30,2026-08-15,2026-06-30,2026-Q2,{today},1001,",
    ]
    run = db.fetchone(
        "SELECT endpoint, status, params FROM ops.ingestion_run "
        "WHERE ingestion_run_id = %s",
        (entry["ingestion_run_id"],),
    )
    assert (run["endpoint"], run["status"]) == ("delta:fundamentals", "success")
    assert run["params"]["dates"] == ["2026-08-01", "2026-08-15"]

    # A row read twice is the repeat; for SF1 that is the whole row.
    again = _FakeNight({"SF1": [first, dict(first)]})
    repeated = bulk.fetch_delta(db, again, "fundamentals", tmp_path)
    assert repeated.outcome == "failed"
    assert "1 of 2 rows repeat a row" in repeated.error


# ---------------------------------------------------------------------------
# ingest sharadar-prices --nightly
# ---------------------------------------------------------------------------


def test_the_nightly_writes_new_dates_revises_its_own_and_keeps_others(db, tmp_path):
    fed = _security(db, "ZZN", source="sharadar", key="900101", feed=True)
    _store(db, fed, Y(2026, 10, 19), "10", "sharadar")
    _store(db, fed, Y(2026, 10, 20), "11", "fmp")  # FMP's, from before the switch
    _fulls(db, tmp_path)
    first = _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZN", "2026-10-16", "9", stamp="2026-10-16")  # stored: no
        + _csv_bar("ZZN", "2026-10-19", "10.5", stamp="2026-10-21")  # revised
        + _csv_bar("ZZN", "2026-10-20", "11.2", stamp="2026-10-21")  # FMP's: kept
        + _csv_bar("ZZN", "2026-10-21", "12")
        + _csv_bar("ZZN", "2030-01-02", "12")  # not yet
        + _csv_bar("ZZOTHER", "2026-10-21", "1"),
        kind="delta",
        since="2026-10-16",
    )
    _other_delta(db, tmp_path, "funds", "2026-10-16")

    plan = sp.nightly(db, tmp_path, dry_run=True, as_of=Y(2026, 10, 22))
    assert plan.counts["to_write"] == 3 and _bars(db, fed)[Y(2026, 10, 19)][0] == D(10)

    done = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 22))
    assert (done.securities, done.read, done.written) == (1, 5, 3)
    assert {k: done.counts[k] for k in ("new", "revised", "kept:fmp")} == {
        "new": 2,
        "revised": 1,
        "kept:fmp": 1,
    }
    assert done.counts["future_or_outside"] == 1
    assert _bars(db, fed) == {
        Y(2026, 10, 16): (D(9), "sharadar"),
        Y(2026, 10, 19): (D("10.5"), "sharadar"),
        Y(2026, 10, 20): (D(11), "fmp"),
        Y(2026, 10, 21): (D(12), "sharadar"),
    }
    assert repo.get_watermark(db, "sharadar", "SEP", fed) == Y(2026, 10, 21)
    run = repo.last_successful_run(db, "sharadar", sp.NIGHTLY_ENDPOINT)
    assert run["params"]["through"]["stocks"] == first
    assert run["params"]["by_date"]["2026-10-21"] == {"new": 1}

    # Applied: the next night reads only what came after it, and the later stamp of
    # a bar two files hold wins.
    assert sp.nightly(db, tmp_path, as_of=Y(2026, 10, 22)).read == 0
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZN", "2026-10-21", "12.004", stamp="2026-10-22")  # rounding
        + _csv_bar("ZZN", "2026-10-22", "13", stamp="2026-10-23"),
        kind="delta",
        since="2026-10-19",
    )
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES + _csv_bar("ZZN", "2026-10-22", "12.5", stamp="2026-10-22"),
        kind="delta",
        since="2026-10-20",
    )
    later = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 23))
    assert (later.counts["unchanged"], later.counts["new"], later.written) == (1, 1, 1)
    assert _bars(db, fed)[Y(2026, 10, 22)] == (D(13), "sharadar")
    assert _bars(db, fed)[Y(2026, 10, 21)] == (D(12), "sharadar")


def test_a_day_the_master_prices_and_the_files_lack_is_counted(db, tmp_path):
    served = _security(db, "ZZS", source="sharadar", key="900301", feed=True)
    _security(db, "ZZU", source="sharadar", key="900302", feed=True)
    _security(db, "ZZG", source="sharadar", key="900303", feed=True)
    for key, day in (("900301", 22), ("900302", 22), ("900303", 21)):
        db.execute(
            "UPDATE core.vendor_security SET last_price_date = %s "
            "WHERE vendor = 'sharadar' AND vendor_key = %s",
            (Y(2026, 10, day), key),
        )
    _fulls(db, tmp_path)
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZS", "2026-10-21", "10")
        + _csv_bar("ZZS", "2026-10-22", "10")
        + _csv_bar("ZZU", "2026-10-21", "10")  # the master says 10-22 too
        + _csv_bar("ZZG", "2026-10-21", "10"),  # its last price: nothing missing
        kind="delta",
        since="2026-10-21",
    )
    _other_delta(db, tmp_path, "funds", "2026-10-21")

    plan = sp.nightly(db, tmp_path, dry_run=True, as_of=Y(2026, 10, 23))
    assert "unserved" not in plan.counts  # counted on what a night wrote
    done = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 23))
    assert done.counts["unserved"] == 1
    assert Y(2026, 10, 22) in _bars(db, served)
    run = repo.last_successful_run(db, "sharadar", sp.NIGHTLY_ENDPOINT)
    assert run["params"]["counts"]["unserved"] == 1
    assert run["params"]["by_date"]["2026-10-22"] == {"new": 1, "unserved": 1}


def test_a_run_for_some_securities_leaves_the_files_pending(db, tmp_path):
    ids = [
        _security(db, f"ZZS{n}", source="sharadar", key=f"90011{n}", feed=True)
        for n in (1, 2)
    ]
    for sid in ids:
        _store(db, sid, Y(2026, 10, 19), "5", "sharadar")
    _fulls(db, tmp_path)
    delta = _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZS1", "2026-10-20", "6")
        + _csv_bar("ZZS2", "2026-10-20", "7"),
        kind="delta",
        since="2026-10-17",
    )
    some = sp.nightly(db, tmp_path, security_ids=ids[:1], as_of=Y(2026, 10, 21))
    assert some.written == 1 and some.through["stocks"] < delta
    rest = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 21))
    assert (rest.counts["new"], rest.counts["unchanged"]) == (1, 1)
    assert rest.through["stocks"] == delta


def test_a_capture_day_ratio_off_one_is_flagged(db, tmp_path):
    fed = _security(db, "ZZQ", source="sharadar", key="900102", feed=True)
    _fulls(db, tmp_path)
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES + "ZZQ,2026-10-21,5,5,5,5,200,5,10,2026-10-21\n",  # a 2:1 ACTIONS lacks
        kind="delta",
        since="2026-10-18",
    )
    result = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 22))
    assert result.mismatched_securities == 1
    assert _bars(db, fed)[Y(2026, 10, 21)] == (D(10), "sharadar")
    (flag,) = db.fetchall(
        "SELECT detail FROM ops.data_quality_flag WHERE check_name = %s "
        "AND security_id = %s",
        (sp.RATIO_CHECK, fed),
    )
    assert flag["detail"] == {"bars": 1, "first": "2026-10-21", "last": "2026-10-21"}


def test_a_security_with_no_bars_reads_every_delta_since_the_full(db, tmp_path):
    _fulls(db, tmp_path)
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES + _csv_bar("ZZF", "2026-10-19", "7"),
        kind="delta",
        since="2026-10-16",
    )
    assert sp.nightly(db, tmp_path, as_of=Y(2026, 10, 20)).securities == 0
    # Minted the next night (a new listing): the applied delta is read again for it.
    fresh = _security(db, "ZZF", source="sharadar", key="900103", feed=True)
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES + _csv_bar("ZZF", "2026-10-20", "8"),
        kind="delta",
        since="2026-10-17",
    )
    sp.nightly(db, tmp_path, as_of=Y(2026, 10, 21))
    assert _bars(db, fresh) == {
        Y(2026, 10, 19): (D(7), "sharadar"),
        Y(2026, 10, 20): (D(8), "sharadar"),
    }


def test_a_new_listing_reads_every_delta_on_disk_from_its_first_price(db, tmp_path):
    _fulls(db, tmp_path)
    # A delta from before a full refresh files the ticker's previous holder too.
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZR", "2026-10-02", "50")
        + _csv_bar("ZZR", "2026-10-13", "5"),
        kind="delta",
        since="2026-10-01",
    )
    _record(db, tmp_path, "stocks", PRICES + _csv_bar("ZZFULL2", "2026-10-14", "1"))
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES + _csv_bar("ZZR", "2026-10-16", "6"),
        kind="delta",
        since="2026-10-14",
    )
    # Minted tonight, first priced on 2026-10-13: its days before the refreshed full
    # are read, and the bar before its first price is the previous holder's.
    new = _security(
        db,
        "ZZR",
        source="sharadar",
        key="900120",
        feed=True,
        valid_from=Y(2026, 10, 13),
    )
    result = sp.nightly(db, tmp_path, as_of=Y(2026, 10, 17))
    assert _bars(db, new) == {
        Y(2026, 10, 13): (D(5), "sharadar"),
        Y(2026, 10, 16): (D(6), "sharadar"),
    }
    assert result.counts["future_or_outside"] == 1


def test_as_if_fed_compares_the_linked_fmp_securities(db, tmp_path):
    fmp = _security(
        db, "ZZA", source="fmp", key="900104", feed=False, valid_from=Y(2026, 10, 1)
    )
    for day, close in ((19, "20"), (20, "21"), (21, "22")):
        _store(db, fmp, Y(2026, 10, day), close, "fmp")
    _fulls(db, tmp_path)
    _record(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _csv_bar("ZZA", "2026-09-30", "19")  # before the link: another era
        + _csv_bar("ZZA", "2026-10-19", "20")
        + _csv_bar("ZZA", "2026-10-20", "21.5")
        + _csv_bar("ZZA", "2026-10-22", "23"),
        kind="delta",
        since="2026-10-19",
    )
    _other_delta(db, tmp_path, "funds", "2026-10-19")

    with pytest.raises(ValueError, match="dry run"):
        sp.nightly(db, tmp_path, as_if_fed=True, as_of=Y(2026, 10, 22))
    assert sp.nightly(db, tmp_path, dry_run=True, as_of=Y(2026, 10, 22)).read == 0

    result = sp.nightly(
        db, tmp_path, dry_run=True, as_if_fed=True, as_of=Y(2026, 10, 22)
    )
    assert (result.as_if_fed, result.complete_from) == (1, Y(2026, 10, 19))
    assert {d: dict(c) for d, c in result.by_date.items()} == {
        "2026-10-19": {"fmp_agrees": 1},
        "2026-10-20": {"fmp_differs": 1},
        "2026-10-21": {"fmp_only": 1},
        "2026-10-22": {"new": 1},
    }
    assert result.counts["future_or_outside"] == 1
    assert len(_bars(db, fmp)) == 3  # nothing written

    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "ingest", "sharadar-prices", "--nightly", "--as-if-fed"],
        env={"FAFNIR_DSN": db.dsn},
    )
    assert out.exit_code != 0 and "--nightly --dry-run" in out.output
    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "ingest", "sharadar-prices", "--nightly", "--backfill"],
        env={"FAFNIR_DSN": db.dsn},
    )
    assert out.exit_code != 0 and "--nightly runs alone" in out.output


# ---------------------------------------------------------------------------
# ingest sharadar-actions: the guard holds for fed securities
# ---------------------------------------------------------------------------


def test_a_fed_security_keeps_the_dividends_another_vendor_holds(db, tmp_path):
    fed = _security(db, "ZZV", source="fmp", key="900105", feed=True)
    repo.upsert_corporate_action(
        db,
        security_id=fed,
        action_type="dividend",
        ex_date=Y(2026, 3, 10),
        dividend_amount=D("0.25"),
        currency="USD",
        source="fmp",
    )
    _record(
        db,
        tmp_path,
        "actions",
        ACTIONS
        + "2026-03-11,dividend,ZZV,ZZV Co,0.25,,\n"  # FMP's, a day apart
        + "2026-06-10,dividend,ZZV,ZZV Co,0.25,,\n",
    )
    result = sa.load(
        db, tmp_path, backfill=True, gap_fill=False, dry_run=True, as_of=Y(2026, 10, 22)
    )
    assert result.skipped["near_existing"] == 1
    assert [p.ex_date for p in result.planned] == [Y(2026, 6, 10)]


# ---------------------------------------------------------------------------
# ingest sharadar
# ---------------------------------------------------------------------------


def test_the_night_runs_in_order_and_waits_for_the_cutover(db, tmp_path, monkeypatch):
    fmp = _security(
        db, "ZZA", source="fmp", key="900106", feed=False, valid_from=Y(2026, 10, 1)
    )
    _store(db, fmp, Y(2026, 10, 21), "22", "fmp")
    _fulls(db, tmp_path)
    today = date.today().isoformat()
    client = _FakeNight(
        {
            "SEP": [_json_bar("ZZA", "2026-10-21", 22, stamp=today)],
            "SFP": [_json_bar("ZZFUND", "2026-10-21", 30, stamp=today)],
        },
        {
            "tickers": TICKERS
            + "stocks,900106,ZZA,ZZA CO,NYSE,N,Domestic Common Stock,,3571,,,,,,,,,,"
            "USD,,2026-10-21,2020-01-02,2020-01-02,2026-10-21,,,,\n",
            "actions": ACTIONS,
        },
    )
    monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: client)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def night(*args):
        return CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "ingest", "sharadar", *args],
            env={"FAFNIR_DSN": db.dsn},
        )

    out = night()
    assert out.exit_code != 0 and "primary_price_vendor" in out.output
    out = night("--dry-run")
    assert out.exit_code == 0, out.output
    steps = [line for line in out.output.splitlines() if line.startswith("[")]
    assert [s.split("]")[0] + "]" for s in steps] == [f"[{n}/7]" for n in range(1, 8)]
    assert "[7/7] source sharadar-prune" in out.output  # it prunes in a dry run too
    assert "1 linked securities compared as if fed" in out.output
    assert "fmp_agrees 1" in out.output
    assert _bars(db, fmp) == {Y(2026, 10, 21): (D(22), "fmp")}
    # The files and the vendor tables are refreshed, a dry run included.
    assert len(repo.sharadar_deltas_after(db, "stocks", 0)) == 1
    assert len(repo.sharadar_deltas_after(db, "funds", 0)) == 1
    name = "SELECT name FROM core.vendor_security WHERE vendor_key = '900106'"
    assert db.fetchval(name) == "ZZA CO"


def test_a_failed_sf1_delta_never_stops_the_night(db, tmp_path, monkeypatch):
    _fulls(db, tmp_path)
    _record(db, tmp_path, "fundamentals", "ticker,dimension\n")
    today = date.today().isoformat()
    filing = _json_sf1("ARQ", "2026-06-30", "2026-08-01", stamp=today)
    bar = _json_bar("ZZA", "2026-10-21", 22, stamp=today)
    tables = {
        "SEP": [bar],
        "SFP": [_json_bar("ZZFUND", "2026-10-21", 30, stamp=today)],
        # Read twice: the SF1 delta fails.
        "SF1": [filing, dict(filing)],
    }
    tickers = (
        TICKERS
        + "stocks,900107,ZZA,ZZA CO,NYSE,N,Domestic Common Stock,,3571,,,,,,,,,,"
        "USD,,2026-10-21,2020-01-02,2020-01-02,2026-10-21,,,,\n"
    )
    client = _FakeNight(tables, {"tickers": tickers, "actions": ACTIONS})
    monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: client)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def night():
        return CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "ingest", "sharadar", "--dry-run"],
            env={"FAFNIR_DSN": db.dsn},
        )

    out = night()
    assert out.exit_code == 0, out.output
    assert "fundamentals  FAILED: fundamentals: 1 of 2 rows repeat a row" in out.output
    assert "!! fundamentals delta failed: the fundamentals wait" in out.output
    assert "[7/7] source sharadar-prune" in out.output
    assert len(repo.sharadar_deltas_after(db, "stocks", 0)) == 1
    assert not repo.sharadar_deltas_after(db, "fundamentals", 0)
    failed = db.fetchval(
        "SELECT count(*) FROM ops.ingestion_run "
        "WHERE endpoint = 'delta:fundamentals' AND status = 'failed'"
    )
    assert failed == 1  # on record, for the operator

    # A price table's failure still stops it at step 2: a fault in what the vendor
    # sent, not an outage, so exit 1 and daily_update.sh does not try again.
    tables["SEP"] = [bar, dict(bar)]
    out = night()
    assert out.exit_code == 1
    assert "step 2 failed: stocks, fundamentals. The night stopped there." in out.output
    assert "[3/7]" not in out.output


class _Outage(_FakeNight):
    """Sharadar unreachable for ``down`` (tables queried, or bulk tables): what the
    client raises once its own retries run out."""

    def __init__(self, *args, down=(), **kw):
        super().__init__(*args, **kw)
        self.down = set(down)

    def _call(self, table, params=None):
        if table in self.down:
            raise TransientSourceError(
                f"GET https://api/{table} exhausted retries (last status 503)"
            )
        return super()._call(table, params)

    def _bulk_link(self, table, years):
        if table in self.down:
            raise TransientSourceError(f"Sharadar {table} bulk answered HTTP 503")
        return super()._bulk_link(table, years)


@pytest.mark.parametrize(
    "down, at",
    [({"SFP"}, 2), ({"tickers"}, 1), ({"SEP", "SFP"}, 2)],
)
def test_a_vendor_outage_exits_75_so_the_night_is_tried_again(
    db, tmp_path, monkeypatch, down, at
):
    """2026-10-31: the SFP delta answered 503 past every retry. The night stops
    as before, but with EX_TEMPFAIL, which daily_update.sh retries."""
    _fulls(db, tmp_path)
    today = date.today().isoformat()
    tables = {
        "SEP": [_json_bar("ZZA", "2026-10-21", 22, stamp=today)],
        "SFP": [_json_bar("ZZFUND", "2026-10-21", 30, stamp=today)],
        "SF1": [],
    }
    client = _Outage(tables, {"tickers": TICKERS, "actions": ACTIONS}, down=down)
    monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: client)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "ingest", "sharadar", "--dry-run"],
        env={"FAFNIR_DSN": db.dsn},
    )
    assert out.exit_code == cli.EX_TEMPFAIL == 75, out.output
    assert f"step {at} failed" in out.output
    if at == 2:
        # SF1's delta fails too here, as a fault (no full file on record), and
        # still the night is retried: SF1 never stops the night, so it does not
        # count against the outage.
        assert "fundamentals  FAILED: the ledger holds no fundamentals" in out.output
    assert "Sharadar could not be reached, so a later run may succeed" in out.output
    assert f"[{at + 1}/7]" not in out.output


def test_an_outage_beside_a_fault_is_a_fault(db, tmp_path, monkeypatch):
    """Retrying cannot fix the half that is a fault, so the night exits 1."""
    _fulls(db, tmp_path)
    today = date.today().isoformat()
    bar = _json_bar("ZZA", "2026-10-21", 22, stamp=today)
    tables = {"SEP": [bar, dict(bar)], "SFP": [], "SF1": []}  # SEP repeats a row
    client = _Outage(tables, {"tickers": TICKERS, "actions": ACTIONS}, down={"SFP"})
    monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: client)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "ingest", "sharadar", "--dry-run"],
        env={"FAFNIR_DSN": db.dsn},
    )
    assert out.exit_code == 1, out.output
    assert "stocks        FAILED: stocks: 1 of 2 rows repeat" in out.output
    assert "funds         FAILED: GET https://api/SFP exhausted retries" in out.output
    assert "step 2 failed: stocks, funds, fundamentals. The night stopped" in out.output
