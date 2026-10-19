"""
The Sharadar file ledger and the typed-row reader (plan SA-0203, #50).

Every file here is synthetic, written by the test: the shapes are Sharadar's (the
column names and types of its published schemas, a zip holding one CSV), the values
are invented. No vendor row is committed (ADR 0012 §8).

The reader tests are unit tests; the ledger tests need FAFNIR_TEST_DSN.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from fafnir.sources import sharadar_files as sf

PRICES = (
    "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"
    "ZZTEST,2024-01-03,10.125,10.5,9.875,10.25,1500.5,10.031,41,2024-01-04\n"
    "ZZTEST,2024-01-02,10,10.25,9.9,10.1,0,9.884,40.4,\n"
)


def _zip(tmp_path, text, name="stocks.csv", extra=()):
    p = tmp_path / "synthetic.csv.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
        for other, body in extra:
            z.writestr(other, body)
    return p


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("container", ["zip", "gzip", "plain"])
def test_every_container_reads_as_the_same_typed_rows(tmp_path, container):
    if container == "zip":
        path = _zip(tmp_path, PRICES)
    elif container == "gzip":
        path = tmp_path / "slice.csv.gz"
        path.write_bytes(gzip.compress(PRICES.encode()))
    else:
        path = tmp_path / "slice.csv"
        path.write_text(PRICES)

    rows = list(sf.iter_rows(path, "SEP"))

    assert rows[0] == {
        "ticker": "ZZTEST",
        "date": date(2024, 1, 3),
        "open": Decimal("10.125"),
        "high": Decimal("10.5"),
        "low": Decimal("9.875"),
        "close": Decimal("10.25"),
        "volume": Decimal("1500.5"),  # split-adjusted volume is not always whole
        "closeadj": Decimal("10.031"),
        "closeunadj": Decimal("41"),
        "lastupdated": date(2024, 1, 4),
    }
    # An empty field is NULL; a zero stays a zero.
    assert rows[1]["lastupdated"] is None and rows[1]["volume"] == 0
    assert sf.count_rows(path) == 2
    assert sf.header(path)[:3] == ["ticker", "date", "open"]


def test_values_are_exact_decimals_never_floats(tmp_path):
    path = _zip(tmp_path, "ticker,date,close\nZZ,2024-01-02,0.163\n")
    [row] = sf.iter_rows(path, "stocks")
    assert isinstance(row["close"], Decimal) and str(row["close"]) == "0.163"


def test_a_value_that_does_not_parse_names_its_line_and_column(tmp_path):
    bad = PRICES + "ZZTEST,2024-01-0X,1,1,1,1,1,1,1,2024-01-05\n"
    with pytest.raises(
        sf.SharadarFileError, match=r"line 4, column date: '2024-01-0X'"
    ):
        list(sf.iter_rows(_zip(tmp_path, bad), "stocks"))
    nan = "ticker,date,close\nZZ,2024-01-02,NaN\n"
    with pytest.raises(sf.SharadarFileError, match="column close"):
        list(sf.iter_rows(_zip(tmp_path, nan), "stocks"))


def test_a_short_row_is_refused(tmp_path):
    with pytest.raises(
        sf.SharadarFileError, match="line 2: 2 fields, the header has 3"
    ):
        list(
            sf.iter_rows(_zip(tmp_path, "ticker,date,close\nZZ,2024-01-02\n"), "stocks")
        )


def test_tickers_and_fundamentals_types(tmp_path):
    tickers = (
        "table,permaticker,ticker,siccode,isdelisted,firstpricedate,lastquarter,newcol\n"
        "stocks,900001,ZZOLD1,3571.0,Y,1998-01-02,2009-12-31,x\n"
    )
    [t] = sf.iter_rows(_zip(tmp_path, tickers), "tickers")
    assert (t["permaticker"], t["siccode"]) == (900001, 3571)
    assert t["firstpricedate"] == date(1998, 1, 2) and t["lastquarter"] == "2009-12-31"
    assert t["newcol"] == "x"  # unknown, kept as text ...
    assert sf.unknown_columns("tickers", sf.header(_zip(tmp_path, tickers))) == [
        "newcol"
    ]

    fundamentals = (
        "ticker,dimension,calendardate,revenue,eps\nZZ,ARQ,2024-03-31,1000,0.5\n"
    )
    [f] = sf.iter_rows(_zip(tmp_path, fundamentals), "SF1")
    assert (f["revenue"], f["eps"]) == (Decimal(1000), Decimal("0.5"))
    assert sf.unknown_columns("fundamentals", ["revenue", "eps"]) == []  # numeric


def test_a_zip_with_several_csvs_needs_the_member_named(tmp_path):
    path = _zip(tmp_path, PRICES, extra=[("other.csv", "a\n1\n"), ("README.txt", "x")])
    with pytest.raises(sf.SharadarFileError, match="expected one .csv member"):
        sf.header(path)
    assert sf.count_rows(path, member="other.csv") == 1
    readme_only = _zip(tmp_path, PRICES, name="stocks.csv", extra=[("README.txt", "x")])
    assert sf.count_rows(readme_only) == 2  # one csv member: no name needed


def test_a_member_named_for_a_plain_file_is_refused(tmp_path):
    path = tmp_path / "slice.csv"
    path.write_text(PRICES)
    with pytest.raises(sf.SharadarFileError, match="not a zip"):
        sf.header(path, member="stocks.csv")


def test_legacy_names_map_to_the_modern_ones():
    assert [
        sf.canonical_table(t) for t in ("SEP", "sfp", "SF1", "stocks", "tickers")
    ] == [
        "stocks",
        "funds",
        "fundamentals",
        "stocks",
        "tickers",
    ]


# ---------------------------------------------------------------------------
# The ledger (migration 0030)
# ---------------------------------------------------------------------------

SHA = hashlib.sha256(b"synthetic").hexdigest()


def _file(db, **over):
    row = {
        "vendor_table": "stocks",
        "kind": "full",
        "filters": json.dumps({"years": "full"}),
        "path": f"{SHA}.csv.zip",
        "member": "stocks.csv",
        "sha256": SHA,
        "bytes": 1024,
        "rows": 2,
        "columns": ["ticker", "date"],
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return db.fetchone(
        f"INSERT INTO landing.sharadar_file ({cols}) VALUES ({marks}) RETURNING *",
        tuple(row.values()),
    )


@pytest.mark.integration
def test_a_file_is_recorded_once(db):
    first = _file(db)
    assert (first["status"], first["filters"]) == ("downloaded", {"years": "full"})
    assert first["fetched_at"] is not None and first["columns"] == ["ticker", "date"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        _file(db, vendor_table="funds")  # same content, whatever it claims to be


@pytest.mark.integration
@pytest.mark.parametrize(
    "over",
    [
        {"vendor_table": "SEP"},  # the modern name only
        {"kind": "snapshot"},
        {"status": "loaded"},  # the file's lifecycle, not a loader's progress
        {"sha256": "ABC"},
        {"bytes": -1},
        {"rows": -1},
        {"path": "/var/lib/fafnir/x.zip"},  # relative to bulk_dir
        {"path": "../outside.zip"},
        {"path": "sub/../../outside.zip"},
        {"path": ""},
    ],
)
def test_the_ledger_refuses_what_it_cannot_mean(db, over):
    with pytest.raises(psycopg.errors.CheckViolation):
        _file(db, **over)


@pytest.mark.integration
def test_a_delta_and_a_later_full(db):
    delta = _file(
        db,
        kind="delta",
        filters=json.dumps({"lastupdated": "2024-01-04"}),
        sha256=hashlib.sha256(b"delta").hexdigest(),
        path="d.csv.gz",
        member=None,
        rows=None,
    )
    assert delta["member"] is None and delta["rows"] is None
    _file(db)
    db.execute(
        "UPDATE landing.sharadar_file SET status = 'superseded', status_at = now() "
        "WHERE file_id = %s",
        (delta["file_id"],),
    )
    assert (
        db.fetchval(
            "SELECT count(*) FROM landing.sharadar_file WHERE status = 'downloaded'"
        )
        == 1
    )
