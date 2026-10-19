"""
`fafnir ingest sharadar-master` (plan SA-0304, #58; needs FAFNIR_TEST_DSN).

TICKERS and ACTIONS files are written to a temporary bulk_dir and recorded in the
ledger, as #52's downloader leaves them; the loader reads them through the ledger.
Every key and value is synthetic. No vendor row is committed (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sharadar_master as sm

pytestmark = pytest.mark.integration

TICKERS_HEADER = (
    "table,permaticker,ticker,name,exchange,isdelisted,category,cusips,siccode,"
    "sicsector,sicindustry,figi,famaindustry,sector,industry,scalemarketcap,"
    "scalerevenue,relatedtickers,currency,location,lastupdated,firstadded,"
    "firstpricedate,lastpricedate,firstquarter,lastquarter,secfilings,companysite\n"
)
EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
TICKERS = TICKERS_HEADER + (
    # Legacy codes in the table column, as the bulk file writes them.
    f"SEP,900001,ZZA,ZZ A CO,NYSE,N,Domestic Common Stock,00000A101 00000A102,3571,"
    f"Manufacturing,Computers,BBG000000001,Computers,Technology,Consumer Electronics,"
    f"5 - Large,5 - Large,ZZOLD,USD,California; U.S.A,2024-01-04,2015-01-02,"
    f"1998-01-02,2024-01-03,1998-03-31,2023-12-31,{EDGAR}0000900001,https://zza.example\n"
    # A dead issuer whose ticker was reused: the numeric suffix.
    "SEP,900002,ZZB1,ZZ B INC,NASDAQ,Y,Domestic Common Stock,,,,,,,,,,,,USD,,"
    "2010-06-01,,2001-03-01,2010-05-28,,,,\n"
    "SFP,900003,ZZF,ZZ FUND,NYSEARCA,N,ETF,,,,,,,,,,,,USD,,2024-01-04,,"
    "2012-01-03,2024-01-03,,,,\n"
    # The same security in fundamentals: one row per table.
    f"SF1,900001,ZZA,ZZ A CO,NYSE,N,Domestic Common Stock,,3571,,,,,,,,,,USD,,"
    f"2024-01-04,,,,1998-03-31,2023-12-31,{EDGAR}0000900001,\n"
    # An institutional investor: not a security.
    "SF3B,800001,ZZINV,ZZ CAPITAL,,,Institutional Investor,,,,,,,,,,,,USD,,"
    "2024-01-04,,,,,,,\n"
)
ACTIONS = (
    "date,action,ticker,name,value,contraticker,contraname\n"
    "1998-01-02,listed,ZZA,ZZ A CO,,,\n"
    "2020-08-31,split,ZZA,ZZ A CO,4,,\n"
    "2019-11-07,dividend,ZZA,ZZ A CO,0.1925,,\n"
    "2010-05-28,delisted,ZZB1,ZZ B INC,,,\n"
    # One deal, repeated with the acquirer's name spelt two ways.
    "2010-05-28,acquisitionby,ZZB1,ZZ B INC,1250.5,ZZA,ZZ A CORP\n"
    "2010-05-28,acquisitionby,ZZB1,ZZ B INC,1250.5,ZZA,ZZ A CO\n"
    "2010-05-28,acquisitionof,ZZA,ZZ A CO,1250.5,ZZB1,ZZ B INC\n"
    # An acquirer outside the dataset: nothing to key the event on.
    "2015-03-02,acquisitionof,ZZPRIV,ZZ PRIVATE,10,ZZA,ZZ A CO\n"
    # A contra party the master does not know: kept, without a contra key.
    "2004-06-01,tickerchangefrom,ZZA,ZZ A CO,,ZZX,ZZ A CO\n"
)


def _record(db, bulk_dir, table, text):
    bulk_dir.mkdir(exist_ok=True)
    body = text.encode()
    sha = hashlib.sha256(body).hexdigest()
    path = bulk_dir / f"{sha}.csv"
    path.write_bytes(body)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind="full",
        filters={"years": "full"},
        path=path.name,
        member=None,
        sha256=sha,
        nbytes=len(body),
        rows=None,
        columns=None,
        ingestion_run_id=None,
    )
    return entry["file_id"]


@pytest.fixture()
def bulk(db, tmp_path):
    d = tmp_path / "bulk"
    return d, _record(db, d, "tickers", TICKERS), _record(db, d, "actions", ACTIONS)


def _master(db):
    return {
        (r["vendor_table"], r["vendor_key"]): r
        for r in db.fetchall("SELECT * FROM core.vendor_security")
    }


def _events(db):
    return db.fetchall(
        "SELECT vendor_key, event_date, action, ticker, value, contra_ticker, "
        "contra_name, contra_vendor_key FROM core.vendor_security_event "
        "ORDER BY event_date, action"
    )


# ---------------------------------------------------------------------------
# Row mapping (no database)
# ---------------------------------------------------------------------------


def test_parse_cik():
    assert sm.parse_cik(f"{EDGAR}0000320193") == "0000320193"
    assert sm.parse_cik("https://www.sec.gov/cgi-bin/browse-edgar?cik=42&x=1") == "42"
    assert sm.parse_cik("https://zza.example") is None
    assert sm.parse_cik(None) is None


def test_the_investors_table_is_not_a_security():
    assert sm.master_row({"table": "SF3B", "permaticker": 1, "ticker": "X"}) is None
    assert sm.master_row({"table": "holdings_investor"}) is None


def test_event_rows_keep_the_first_name_whatever_the_order():
    rows = [
        {
            "date": date(2010, 5, 28),
            "action": "acquisitionby",
            "ticker": "B",
            "value": None,
            "contraticker": "A",
            "contraname": name,
        }
        for name in ("ZZ A CORP", None, "ZZ A CO")
    ]
    for ordered in (rows, rows[::-1]):
        skipped = Counter()
        out = list(sm.event_rows(ordered, {"A": "1", "B": "2"}, skipped))
        assert [e["contra_name"] for e in out] == ["ZZ A CO"]
        assert skipped == {"duplicate": 2}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_counts_by_table_and_delisted_match_the_file(db, bulk):
    bulk_dir, tickers, actions = bulk
    results = sm.load(db, bulk_dir)
    assert [(r.table, r.file_id, r.error) for r in results] == [
        ("tickers", tickers, None),
        ("actions", actions, None),
    ]
    t, a = results
    assert (t.rows, t.inserted, t.updated, t.unchanged) == (5, 4, 0, 0)
    assert t.skipped == {"not_a_security:holdings_investor": 1}
    assert db.fetchall(
        "SELECT vendor_table, is_delisted, count(*) AS n FROM core.vendor_security "
        "GROUP BY 1, 2 ORDER BY 1, 2"
    ) == [
        {"vendor_table": "fundamentals", "is_delisted": False, "n": 1},
        {"vendor_table": "funds", "is_delisted": False, "n": 1},
        {"vendor_table": "stocks", "is_delisted": False, "n": 1},
        {"vendor_table": "stocks", "is_delisted": True, "n": 1},
    ]
    assert (a.rows, a.inserted) == (9, 5)
    assert a.skipped == {
        "corporate_action": 2,
        "duplicate": 1,
        "unresolved:acquisitionof": 1,
    }


def test_a_tickers_row_maps_every_field(db, bulk):
    bulk_dir, *_ = bulk
    sm.load(db, bulk_dir)
    zza = _master(db)[("stocks", "900001")]
    assert {
        k: zza[k]
        for k in (
            "vendor",
            "ticker",
            "exchange",
            "is_delisted",
            "cusips",
            "figi",
            "cik",
            "sic_code",
            "sector",
            "industry",
            "fama_industry",
            "related_tickers",
            "first_price_date",
            "last_price_date",
            "first_quarter",
            "last_quarter",
            "vendor_last_updated",
        )
    } == {
        "vendor": "sharadar",
        "ticker": "ZZA",
        "exchange": "NYSE",  # as the vendor states it; #56 maps it when linking
        "is_delisted": False,
        "cusips": ["00000A101", "00000A102"],
        "figi": "BBG000000001",
        "cik": "0000900001",
        "sic_code": "3571",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "fama_industry": "Computers",
        "related_tickers": ["ZZOLD"],
        "first_price_date": date(1998, 1, 2),
        "last_price_date": date(2024, 1, 3),
        "first_quarter": date(1998, 3, 31),
        "last_quarter": date(2023, 12, 31),
        "vendor_last_updated": date(2024, 1, 4),
    }
    assert zza["attributes"] == {
        "scalemarketcap": "5 - Large",
        "scalerevenue": "5 - Large",
        "sicsector": "Manufacturing",
        "sicindustry": "Computers",
        "firstadded": "2015-01-02",
        "secfilings": f"{EDGAR}0000900001",
        "companysite": "https://zza.example",
    }
    dead = _master(db)[("stocks", "900002")]
    assert (dead["ticker"], dead["is_delisted"], dead["cusips"]) == ("ZZB1", True, None)
    assert dead["attributes"] == {}


def test_events_resolve_to_permaticker_and_skip_corporate_actions(db, bulk):
    bulk_dir, *_ = bulk
    sm.load(db, bulk_dir)
    assert _events(db) == [
        {
            "vendor_key": "900001",
            "event_date": date(1998, 1, 2),
            "action": "listed",
            "ticker": "ZZA",
            "value": None,
            "contra_ticker": None,
            "contra_name": None,
            "contra_vendor_key": None,
        },
        {
            "vendor_key": "900001",
            "event_date": date(2004, 6, 1),
            "action": "tickerchangefrom",
            "ticker": "ZZA",
            "value": None,
            "contra_ticker": "ZZX",
            "contra_name": "ZZ A CO",
            "contra_vendor_key": None,
        },
        {
            "vendor_key": "900002",
            "event_date": date(2010, 5, 28),
            "action": "acquisitionby",
            "ticker": "ZZB1",
            "value": Decimal("1250.5"),
            "contra_ticker": "ZZA",
            "contra_name": "ZZ A CO",
            "contra_vendor_key": "900001",
        },
        {
            "vendor_key": "900001",
            "event_date": date(2010, 5, 28),
            "action": "acquisitionof",
            "ticker": "ZZA",
            "value": Decimal("1250.5"),
            "contra_ticker": "ZZB1",
            "contra_name": "ZZ B INC",
            "contra_vendor_key": "900002",
        },
        {
            "vendor_key": "900002",
            "event_date": date(2010, 5, 28),
            "action": "delisted",
            "ticker": "ZZB1",
            "value": None,
            "contra_ticker": None,
            "contra_name": None,
            "contra_vendor_key": None,
        },
    ]


def test_a_rerun_is_a_no_op(db, bulk):
    bulk_dir, *_ = bulk
    sm.load(db, bulk_dir)
    master, events = _master(db), _events(db)
    stamps = db.fetchall(
        "SELECT loaded_at, ingestion_run_id FROM core.vendor_security "
        "UNION ALL SELECT loaded_at, ingestion_run_id FROM core.vendor_security_event"
    )
    again = sm.load(db, bulk_dir)
    assert [(r.inserted, r.updated, r.unchanged) for r in again] == [
        (0, 0, 4),
        (0, 0, 5),
    ]
    assert _master(db) == master and _events(db) == events
    assert (
        db.fetchall(
            "SELECT loaded_at, ingestion_run_id FROM core.vendor_security "
            "UNION ALL SELECT loaded_at, ingestion_run_id FROM core.vendor_security_event"
        )
        == stamps
    )


def test_a_changed_row_is_updated_and_nothing_is_deleted(db, bulk, tmp_path):
    bulk_dir, *_ = bulk
    sm.load(db, bulk_dir)
    # A newer TICKERS: ZZA moved exchange; the fund is no longer in the file.
    newer = TICKERS_HEADER + TICKERS.splitlines(keepends=True)[1].replace(
        ",NYSE,", ",NASDAQ,"
    )
    fid = _record(db, bulk_dir, "tickers", newer)
    (r,) = sm.load(db, bulk_dir, file_id=fid)
    assert (r.inserted, r.updated, r.unchanged) == (0, 1, 0)
    master = _master(db)
    assert master[("stocks", "900001")]["exchange"] == "NASDAQ"
    assert master[("stocks", "900001")]["ingestion_run_id"] == r.run_id
    assert ("funds", "900003") in master


def test_actions_need_the_master_first(db, bulk):
    bulk_dir, _, actions = bulk
    (r,) = sm.load(db, bulk_dir, file_id=actions)
    assert "load TICKERS first" in r.error
    run = db.fetchone(
        "SELECT endpoint, status FROM ops.ingestion_run WHERE ingestion_run_id = %s",
        (r.run_id,),
    )
    assert run == {"endpoint": "master:actions", "status": "failed"}
    assert db.fetchval("SELECT count(*) FROM core.vendor_security_event") == 0


def test_a_failed_tickers_load_stops_before_actions(db, bulk):
    bulk_dir, tickers, _ = bulk
    db.execute(
        "UPDATE landing.sharadar_file SET bytes = bytes + 1 WHERE file_id = %s",
        (tickers,),
    )
    results = sm.load(db, bulk_dir)
    assert len(results) == 1 and "bytes" in results[0].error


def test_files_this_does_not_load(db, bulk):
    bulk_dir, *_ = bulk
    stocks = _record(db, bulk_dir, "stocks", "ticker,date\nZZA,2024-01-02\n")
    assert (
        "this loads tickers, actions" in sm.load(db, bulk_dir, file_id=stocks)[0].error
    )
    assert "no file 999" in sm.load(db, bulk_dir, file_id=999)[0].error


def test_no_current_file(db, tmp_path):
    with pytest.raises(FileNotFoundError, match="no current tickers file"):
        sm.load(db, tmp_path)


# ---------------------------------------------------------------------------
# The command and `fafnir status`
# ---------------------------------------------------------------------------


def _rc(tmp_path, bulk_dir):
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{bulk_dir}"\n')
    return str(rc)


def test_the_command(db, bulk, tmp_path, monkeypatch):
    bulk_dir, tickers, actions = bulk
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    out = CliRunner().invoke(
        cli.main, ["--config", _rc(tmp_path, bulk_dir), "ingest", "sharadar-master"]
    )
    assert out.exit_code == 0, out.output
    assert f"tickers  file {tickers}: 5 rows read; 4 new, 0 changed" in out.output
    assert "skipped 1: not_a_security:holdings_investor" in out.output
    assert f"actions  file {actions}: 9 rows read; 5 new" in out.output

    bad = CliRunner().invoke(
        cli.main,
        [
            "--config",
            _rc(tmp_path, bulk_dir),
            "ingest",
            "sharadar-master",
            "--from-file",
            "999",
        ],
    )
    assert bad.exit_code != 0 and "1 file(s) failed" in bad.output


def _status(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    out = CliRunner().invoke(cli.main, ["--config", _rc(tmp_path, tmp_path), "status"])
    assert out.exit_code == 0, out.output
    return out.output.splitlines()


def test_status_keeps_its_lines_and_appends_the_vendor_block(
    db, bulk, tmp_path, monkeypatch
):
    before = _status(db, tmp_path, monkeypatch)
    assert not any(line.startswith("Sharadar") for line in before)
    bulk_dir, *_ = bulk
    sm.load(db, bulk_dir)
    after = _status(db, tmp_path, monkeypatch)
    # The existing lines, byte for byte, then the block.
    assert after[: len(before)] == before
    block = after[len(before) :]
    run = db.fetchone(
        "SELECT ingestion_run_id, started_at FROM ops.ingestion_run "
        "WHERE endpoint = 'master:actions'"
    )
    assert block == [
        "Sharadar   : stocks 1 listed, 1 delisted",
        "             funds 1 listed, 0 delisted",
        "             fundamentals 1 listed, 0 delisted",
        f"             events 5; last load {run['started_at']:%Y-%m-%d %H:%M} "
        f"(master:actions, run {run['ingestion_run_id']}, success)",
    ]
