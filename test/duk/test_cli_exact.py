"""`duk ph --exact`: prices as the warehouse stores them, never through a float.

A float prints a stored ``13247980.736555`` as ``13247980.7365550008``, and a
program that must keep money exact (portable, its ADR 0029) has to undo that
by rounding to a scale it has to know. ``--exact`` hands it the stored text.
It is refused wherever it cannot be kept, rather than printing something
rounded under its name.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pandas as pd
import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db
from duk.format_utils import exact_records

#: What ds_db.exact_bars returns: rows as psycopg hands them back.
BARS = [
    {"date": dt.date(2024, 1, 2), "close": Decimal("13247980.736555"), "volume": 100},
    {"date": dt.date(2024, 1, 3), "close": Decimal("189.120000"), "volume": 2000},
]

DB_RC = '[database]\ndsn = "host=x dbname=fafnir"\n'
FMP_RC = '[api]\nfmp_key = "k"\n'


@pytest.fixture()
def calls():
    return {}


@pytest.fixture()
def run(monkeypatch, tmp_path, calls):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    def fake(**kwargs):
        calls["kwargs"] = kwargs
        fields = kwargs.get("fields") or ["close", "volume"]
        return [
            {"date": b["date"], **{f: b[f] for f in fields}}
            for b in calls.get("bars", BARS)
        ]

    monkeypatch.setattr(ds_db, "exact_bars", fake)
    monkeypatch.setattr(
        ds_db, "price_history", lambda **kw: pytest.fail("--exact reads exact_bars")
    )

    def invoke(args, rc=DB_RC):
        cfg = tmp_path / "dukrc.toml"
        cfg.write_text(rc)
        return CliRunner().invoke(main, ["-c", str(cfg)] + args)

    return invoke


def test_json_carries_the_stored_text_of_every_price(run, calls):
    result = run(["-S", "db", "ph", "AAPL", "--exact", "--json", "--close"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"date": "2024-01-02", "close": "13247980.736555"},
        {"date": "2024-01-03", "close": "189.120000"},
    ]
    assert calls["kwargs"]["fields"] == ["close"]
    assert calls["kwargs"]["adjusted"] is False


def test_adjusted_exact_reads_the_adjusted_series(run, calls):
    result = run(["ph", "AAPL", "--exact", "--json", "--adj"])

    assert result.exit_code == 0, result.output
    assert calls["kwargs"]["adjusted"] is True


def test_csv_carries_the_same_text(run):
    result = run(["ph", "AAPL", "--exact", "--csv"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "date,close,volume",
        "2024-01-02,13247980.736555,100",
        "2024-01-03,189.120000,2000",
    ]


def test_an_output_file_is_exact_too(run, tmp_path):
    target = tmp_path / "out.json"

    result = run(
        ["ph", "AAPL", "--exact", "--json", "--close", "-q", "-o", str(target)]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())[0]["close"] == "13247980.736555"


@pytest.mark.parametrize(
    ("args", "rc", "why"),
    [
        (["-S", "fmp", "ph", "AAPL", "--exact"], FMP_RC, "a vendor API sends floats"),
        (["ph", "AAPL", "--exact", "-f", "week"], DB_RC, "would compute new ones"),
        (["ph", "AAPL", "--exact", "--summary"], DB_RC, "not --summary"),
        (["ph", "AAPL", "--exact", "-p", "4"], DB_RC, "--precision does not apply"),
    ],
)
def test_exact_is_refused_where_it_cannot_be_kept(run, calls, args, rc, why):
    result = run(args, rc=rc)

    assert result.exit_code == 2, result.output
    assert why in result.output
    assert "kwargs" not in calls, "refused before anything was fetched"


def test_a_float_is_refused_rather_than_printed_as_exact():
    frame = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "close": [189.12]})

    with pytest.raises(TypeError, match="not an exact decimal"):
        exact_records(frame)
