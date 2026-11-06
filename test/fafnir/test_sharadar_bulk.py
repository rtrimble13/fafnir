"""
`fafnir source sharadar-bulk` (plan SA-0206, #52; needs FAFNIR_TEST_DSN).

The client is real down to the HTTP: only the redirect and the download are faked,
serving synthetic zips shaped like Sharadar's (one CSV inside a `<table>.csv.zip`).
No vendor row is committed (ADR 0012 §8).
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.ingest import sharadar_bulk as bulk
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.sources import sharadar_files as sf
from fafnir.sources.base import SourceError
from fafnir.sources.sharadar import SharadarClient

pytestmark = pytest.mark.integration

STOCKS = (
    "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"
    "ZZA,2024-01-03,10.125,10.5,9.875,10.25,1500,10.031,41,2024-01-04\n"
    "ZZA,2024-01-02,10,10.25,9.9,10.1,1200,9.884,40.4,2024-01-04\n"
)
TICKERS = (
    "table,permaticker,ticker,name,exchange,isdelisted,category,cusips,siccode,"
    "sicsector,sicindustry,figi,famaindustry,sector,industry,scalemarketcap,"
    "scalerevenue,relatedtickers,currency,location,lastupdated,firstadded,"
    "firstpricedate,lastpricedate,firstquarter,lastquarter,secfilings,companysite\n"
    "stocks,900001,ZZA,ZZ A CO,NYSE,N,Domestic Common Stock,,3571,,,,,,,,,,USD,,"
    "2024-01-04,2020-01-02,2020-01-02,2024-01-03,,,,\n"
)


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return buf.getvalue()


class _FakeBulk(SharadarClient):
    """Serves ``files[table]`` as that table's bulk zip, and TICKERS unless told
    otherwise: a price table is fetched with it (#238)."""

    def __init__(self, files):
        super().__init__("synthetic-key", rate_per_min=100_000)
        self.files = {"tickers": _zip("tickers.csv", TICKERS), **files}
        self.asked: list[tuple[str, str]] = []

    def _bulk_link(self, table, years):
        self.asked.append((table, years))
        if isinstance(self.files.get(table), Exception):
            raise self.files[table]
        return f"https://bulk.example/bulk-sharadar/{table}.csv.zip?X-Amz-Signature=s"

    def _stream(self, url):
        table = url.split("/bulk-sharadar/")[1].split(".csv.zip")[0]
        body = self.files[table]
        self.request_count += 1
        self.bytes_downloaded += len(body)
        yield body


def _runs(db):
    return db.fetchall(
        "SELECT endpoint, status, bytes_downloaded, params, error_message "
        "FROM ops.ingestion_run WHERE source = 'sharadar' ORDER BY ingestion_run_id"
    )


def _files(db):
    return db.fetchall("SELECT * FROM landing.sharadar_file ORDER BY file_id")


def test_a_table_downloads_records_and_reads_back(db, tmp_path):
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS)})
    [t, r] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert (r.table, r.outcome, r.rows) == ("stocks", "recorded", 2)
    assert (t.table, t.outcome) == ("tickers", "recorded")  # fetched with it

    [_, f] = _files(db)
    assert (f["vendor_table"], f["kind"], f["member"], f["rows"]) == (
        "stocks",
        "full",
        "SEP.csv",
        2,
    )
    assert f["filters"] == {"years": "full", "tickers_file_id": t.file_id}
    assert f["path"] == r.path
    assert f["columns"][:2] == ["ticker", "date"] and (tmp_path / f["path"]).is_file()

    [_, run] = _runs(db)
    assert (run["endpoint"], run["status"]) == ("bulk:stocks", "success")
    assert run["bytes_downloaded"] == f["bytes"] > 0
    assert run["params"]["rows"] == 2 and run["params"]["file_id"] == f["file_id"]
    assert "synthetic-key" not in json.dumps(run["params"])

    rows = list(ledger.rows(db, f["file_id"], tmp_path))
    assert rows[0]["date"] == date(2024, 1, 3) and rows[0]["close"] == Decimal("10.25")


def test_the_same_table_twice_records_one_file(db, tmp_path):
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS)})
    bulk.download_tables(db, client, ["SEP"], tmp_path)
    [again] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert again.outcome == "skipped" and len(client.asked) == 2  # no request at all

    [_, forced] = bulk.download_tables(db, client, ["SEP"], tmp_path, force=True)
    assert forced.outcome == "unchanged" and len(client.asked) == 4
    assert len(_files(db)) == 2 and len(list(tmp_path.iterdir())) == 2


def test_new_content_supersedes_the_old_file(db, tmp_path):
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS)})
    [_, first] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    client.files["SEP"] = _zip(
        "SEP.csv", STOCKS + "ZZB,2024-01-03,1,1,1,1,1,1,1,2024-01-04\n"
    )
    [_, second] = bulk.download_tables(db, client, ["SEP"], tmp_path, force=True)
    assert (second.outcome, second.superseded, second.rows) == ("recorded", 1, 3)
    statuses = {
        f["file_id"]: f["status"] for f in _files(db) if f["vendor_table"] == "stocks"
    }
    assert statuses == {first.file_id: "superseded", second.file_id: "downloaded"}


def test_header_drift_fails_loudly_with_the_column_names(db, tmp_path):
    drifted = STOCKS.replace("closeunadj,", "close_unadj,", 1)
    client = _FakeBulk({"SEP": _zip("SEP.csv", drifted)})
    [_, r] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert r.outcome == "failed"
    assert "lacks expected column(s) ['closeunadj']" in r.error
    assert "close_unadj" in r.error  # what it has instead
    assert [f["vendor_table"] for f in _files(db)] == ["tickers"]
    [_, run] = _runs(db)
    assert run["status"] == "failed" and "closeunadj" in run["error_message"]


def test_an_unknown_column_is_a_warning_kept_in_the_run(db, tmp_path):
    extra = STOCKS.replace("lastupdated\n", "lastupdated,newcol\n", 1).replace(
        "2024-01-04\n", "2024-01-04,x\n"
    )
    client = _FakeBulk({"SEP": _zip("SEP.csv", extra)})
    [_, r] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert r.outcome == "recorded" and r.unmapped_columns == ["newcol"]
    assert _runs(db)[1]["params"]["unmapped_columns"] == ["newcol"]


def test_a_failed_table_keeps_the_ones_before_it_and_the_run_resumes(db, tmp_path):
    client = _FakeBulk(
        {
            "tickers": _zip("tickers.csv", TICKERS),
            "SEP": SourceError("GET https://api.sharadar.com/v1.0/data/SEP failed"),
            "actions": _zip(
                "actions.csv", "date,action,ticker,name,value,contraticker,contraname\n"
            ),
        }
    )
    results = bulk.download_tables(db, client, ["tickers", "SEP", "actions"], tmp_path)
    assert [r.outcome for r in results] == ["recorded", "failed", "recorded"]
    assert {f["vendor_table"] for f in _files(db)} == {"tickers", "actions"}
    assert [r["status"] for r in _runs(db)] == ["success", "failed", "success"]

    client.files["SEP"] = _zip("SEP.csv", STOCKS)
    resumed = bulk.download_tables(db, client, ["tickers", "SEP", "actions"], tmp_path)
    # TICKERS is fetched again with SEP, the same file as before (#238).
    assert [r.outcome for r in resumed] == ["unchanged", "recorded", "skipped"]
    assert resumed[1].table == "stocks"


def test_a_table_without_a_schema_is_refused(db, tmp_path):
    [r] = bulk.download_tables(db, _FakeBulk({}), ["holdings"], tmp_path)
    assert r.outcome == "failed" and "no schema" in r.error
    with pytest.raises(ValueError, match="years"):
        bulk.download_tables(db, _FakeBulk({}), ["SEP"], tmp_path, years="3")


def test_the_reader_refuses_a_missing_or_changed_file(db, tmp_path):
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS)})
    [_, r] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    path = tmp_path / r.path
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(sf.SharadarFileError, match="the ledger recorded"):
        ledger.rows(db, r.file_id, tmp_path)
    path.unlink()
    with pytest.raises(sf.SharadarFileError, match="re-download it"):
        ledger.rows(db, r.file_id, tmp_path)
    with pytest.raises(sf.SharadarFileError, match="no file 999"):
        ledger.rows(db, 999, tmp_path)


def _pairing(db, file_id):
    return db.fetchone(
        "SELECT filters->'tickers_file_id' AS t FROM landing.sharadar_file "
        "WHERE file_id = %s",
        (file_id,),
    )["t"]


def test_a_price_file_records_the_tickers_file_fetched_with_it(db, tmp_path):
    # #238: SEP and SFP rows carry only the ticker of the day; the full file names
    # the TICKERS file that says which security each ticker was that day.
    sfp = STOCKS.replace("ZZA", "ZZF")
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS), "SFP": _zip("SFP.csv", sfp)})
    t, sep, fund = bulk.download_tables(db, client, ["SEP", "SFP"], tmp_path)
    assert [r.table for r in (t, sep, fund)] == ["tickers", "stocks", "funds"]
    assert [a for a, _ in client.asked] == ["tickers", "SEP", "SFP"]  # one TICKERS
    assert _pairing(db, sep.file_id) == _pairing(db, fund.file_id) == t.file_id

    # A price table this run skips keeps its pairing, and no TICKERS is fetched.
    [skipped] = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert skipped.outcome == "skipped" and len(client.asked) == 3

    # Forced again on a newer TICKERS: the pairing follows it.
    client.files["tickers"] = _zip(
        "tickers.csv", TICKERS.replace("900001,ZZA", "900001,ZZA1")
    )
    t2, again = bulk.download_tables(db, client, ["SEP"], tmp_path, force=True)
    assert (t2.outcome, again.outcome) == ("recorded", "unchanged")
    assert _pairing(db, sep.file_id) == t2.file_id != t.file_id
    # TICKERS on its own (the nightly) pairs nothing.
    [alone] = bulk.download_tables(db, client, ["tickers"], tmp_path, force=True)
    assert alone.outcome == "unchanged" and _pairing(db, sep.file_id) == t2.file_id


def test_a_price_file_whose_tickers_failed_is_left_unpaired(db, tmp_path, caplog):
    client = _FakeBulk(
        {"tickers": SourceError("HTTP 503"), "SEP": _zip("SEP.csv", STOCKS)}
    )
    t, sep = bulk.download_tables(db, client, ["SEP"], tmp_path)
    assert (t.outcome, sep.outcome) == ("failed", "recorded")
    assert _pairing(db, sep.file_id) is None
    assert "skip the securities renamed since" in caplog.text
    assert "--tables stocks --force" in caplog.text


def test_the_command(db, tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path / "bulk"}"\n')
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    monkeypatch.setenv("SHARADAR_API_KEY", "synthetic-key")
    client = _FakeBulk({"SEP": _zip("SEP.csv", STOCKS), "SF1": SourceError("HTTP 403")})
    monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: client)

    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "source", "sharadar-bulk", "--tables", "SEP,SF1"],
    )
    assert out.exit_code != 0, out.output
    assert "stocks        2 rows" in out.output and "new file" in out.output
    assert "fundamentals  FAILED: HTTP 403" in out.output
    assert "1 table(s) failed: fundamentals" in out.output

    again = CliRunner().invoke(
        cli.main, ["--config", str(rc), "source", "sharadar-bulk", "--tables", "SEP"]
    )
    assert again.exit_code == 0 and "skipped" in again.output
