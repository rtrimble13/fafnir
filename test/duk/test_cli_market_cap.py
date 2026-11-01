"""`duk mc` and `ph --mcap`: daily market cap (migration 0042; ADR 0019 §5). Unit
tests, no database: the datasources are replaced, so what is pinned is the command's
contract -- which source answers, the columns, several securities in one read, the
refusals, the notes on stderr -- and the shaping (resampling, limit, units).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pandas as pd
import pytest
from click.testing import CliRunner

from duk import sharadar_api
from duk.cli import main
from duk.datasource import db as ds_db
from duk.datasource import sharadar as ds_sharadar
from duk.sharadar_api import SharadarAPIError

DB_RC = '[database]\ndsn = "host=x dbname=fafnir"\n'
SHARADAR_RC = '[api]\nsharadar_key = "k"\n'


def _row(sid, day, close, shares, factor="1"):
    close, shares, factor = Decimal(close), Decimal(shares), Decimal(factor)
    return {
        "security_id": sid,
        "date": day,
        "close_raw": close,
        "shares_outstanding": shares,
        "share_factor": factor,
        "market_cap": close * shares * factor,
        "currency": "USD",
        "fiscal_date": dt.date(2024, 3, 30),
        "known_from": dt.date(2024, 5, 2),
    }


D1, D2 = dt.date(2024, 6, 7), dt.date(2024, 6, 10)
ROWS = [
    _row(7, D1, "100.000000", "1000"),
    _row(7, D2, "25.000000", "4000"),
    _row(9, D1, "50.000000", "5000", "0.2"),
    _row(9, D2, "51.000000", "5000", "0.2"),
]
IDS = {"ZZAPL": 7, "ZZTSM": 9, "ZZETF": 11}


@pytest.fixture()
def calls():
    return {}


@pytest.fixture()
def run(monkeypatch, tmp_path, calls):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    monkeypatch.setattr(ds_db, "issuer_class", lambda **kw: calls.get("issuer"))

    def resolve(*, dsn, symbols):
        calls.setdefault("resolved", []).append(list(symbols))
        return {s: IDS.get(s) for s in symbols}

    def rows(**kw):
        calls["rows"] = kw
        return [r for r in ROWS if r["security_id"] in kw["security_ids"]]

    monkeypatch.setattr(ds_db, "resolve_symbols", resolve)
    monkeypatch.setattr(ds_db, "market_cap_rows", rows)

    def invoke(args, rc=DB_RC, stdin=None):
        cfg = tmp_path / "dukrc.toml"
        cfg.write_text(rc)
        return CliRunner().invoke(main, ["-c", str(cfg)] + args, input=stdin)

    return invoke


def _csv(text):
    lines = text.strip().splitlines()
    head = lines[0].split(",")
    return [dict(zip(head, line.split(","))) for line in lines[1:]]


# ---------------------------------------------------------------------------
# mc, from the warehouse
# ---------------------------------------------------------------------------


def test_one_security_prints_its_count_factor_and_cap(run, calls):
    result = run(["mc", "ZZAPL", "--csv", "-s", "2024-06-01"])
    assert result.exit_code == 0, result.output
    got = _csv(result.stdout)
    assert list(got[0]) == ["date", *ds_db.MARKET_CAP_COLUMNS]
    assert [(r["date"], r["shares_outstanding"], r["market_cap"]) for r in got] == [
        ("2024-06-07", "1000", "100000.0"),
        ("2024-06-10", "4000", "100000.0"),
    ]
    assert got[0]["close_raw"] == "100.00"
    assert calls["rows"]["security_ids"] == [7]
    assert calls["rows"]["start_date"] == "2024-06-01"


def test_several_securities_are_one_read_with_their_symbols(run, calls):
    result = run(["mc", "ZZAPL", "ZZTSM", "--json"])
    assert result.exit_code == 0, result.output
    got = json.loads(result.stdout)
    assert [(r["symbol"], r["security_id"]) for r in got] == [
        ("ZZAPL", 7),
        ("ZZAPL", 7),
        ("ZZTSM", 9),
        ("ZZTSM", 9),
    ]
    assert got[2]["share_factor"] == 0.2 and got[2]["market_cap"] == 50000.0
    assert calls["resolved"] == [["ZZAPL", "ZZTSM"]], "one connection for the list"
    assert calls["rows"]["security_ids"] == [7, 9]


def test_a_symbols_file_adds_to_the_arguments_once_each(run, calls, tmp_path):
    listing = tmp_path / "members.txt"
    listing.write_text("ZZTSM, ZZAPL  # the two\n\n# none here\nZZAPL\n")
    result = run(["mc", "ZZAPL", "--symbols-file", str(listing), "--csv"])
    assert result.exit_code == 0, result.output
    assert calls["resolved"] == [["ZZAPL", "ZZTSM"]]
    result = run(["mc", "--symbols-file", "-", "--csv"], stdin="ZZTSM\n")
    assert result.exit_code == 0, result.output
    assert {r["date"] for r in _csv(result.stdout)} == {"2024-06-07", "2024-06-10"}


def test_a_ticker_matching_nothing_is_named_and_nothing_is_read(run, calls):
    result = run(["mc", "ZZAPL", "NOPE", "ALSONOT", "--csv"])
    assert result.exit_code == 1
    assert "NOPE, ALSONOT" in result.stderr
    assert "rows" not in calls


def test_no_symbol_is_a_usage_error(run):
    result = run(["mc"])
    assert result.exit_code == 2
    assert "SYMBOL" in result.output


def test_a_security_with_no_cap_is_explained_on_stderr(run, calls):
    result = run(["mc", "ZZAPL", "ZZETF", "--csv"])
    assert result.exit_code == 0, result.output
    assert "no market cap for ZZETF" in result.stderr
    assert "ZZETF" not in result.stdout
    calls["issuer"] = {"symbol": "ZZGA", "cik": "0001652044", "security_id": 3}
    result = run(["mc", "ZZETF", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert "under ZZGA" in result.stderr and "every class" in result.stderr


def test_exact_prints_the_computed_decimals_as_text(run, calls):
    result = run(["mc", "ZZTSM", "--exact", "--json"])
    assert result.exit_code == 0, result.output
    first = json.loads(result.stdout)[0]
    assert first == {
        "date": "2024-06-07",
        "close_raw": "50.000000",
        "shares_outstanding": "5000",
        "share_factor": "0.2",
        "market_cap": "50000.0000000",
        "currency": "USD",
        "fiscal_date": "2024-03-30",
        "known_from": "2024-05-02",
    }


@pytest.mark.parametrize(
    "args, phrase",
    [
        (["-S", "fmp", "mc", "ZZAPL"], "FMP's is not read"),
        (["-S", "fmp", "ph", "ZZAPL", "--mcap"], "FMP's is not read"),
        (["mc", "ZZAPL", "--exact", "-f", "month"], "--exact prints daily bars"),
        (["mc", "ZZAPL", "--exact", "-p", "4"], "--precision does not apply"),
    ],
)
def test_refusals(run, args, phrase):
    result = run(args)
    assert result.exit_code == 2
    assert phrase in result.output


def test_the_table_footer_names_the_read(run, calls):
    result = run(["mc", "ZZAPL", "ZZTSM", "--table"])
    assert result.exit_code == 0, result.output
    assert "4 bars · 2 securities · market cap · day" in result.stdout
    assert "5,000" in result.stdout  # a share count, with separators


def test_output_to_a_directory_names_the_file(run, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    assert run(["mc", "ZZAPL", "-o", str(out), "-q", "--csv"]).exit_code == 0
    assert run(["mc", "ZZAPL", "ZZTSM", "-o", str(out), "-q"]).exit_code == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "ZZAPL_mcap_earliest_latest.csv",
        "market_cap_earliest_latest.csv",
    ]


# ---------------------------------------------------------------------------
# The frame: resampling and limit, per security
# ---------------------------------------------------------------------------


def _month_rows():
    out = []
    for sid, base in ((7, 100), (9, 50)):
        for day, bump in ((dt.date(2024, 1, 30), 0), (dt.date(2024, 1, 31), 1)):
            out.append(_row(sid, day, str(base + bump), "10"))
        out.append(_row(sid, dt.date(2024, 2, 1), str(base + 2), "20"))
    return out


def test_a_period_is_its_last_bar_whole_labelled_as_ph_labels_it(monkeypatch):
    monkeypatch.setattr(ds_db, "market_cap_rows", lambda **kw: _month_rows())
    frame = ds_db.market_cap_history(dsn="x", security_ids=[9, 7], frequency="month")
    assert list(frame["security_id"]) == [9, 9, 7, 7], "the order asked for"
    jan = frame.iloc[0]
    assert jan["date"] == pd.Timestamp("2024-01-31")
    assert (jan["close_raw"], jan["market_cap"]) == (51.0, 510.0)
    feb = frame.iloc[1]
    assert (feb["shares_outstanding"], feb["market_cap"]) == (20.0, 1040.0)


def test_limit_is_per_security_by_phs_rule(monkeypatch):
    monkeypatch.setattr(ds_db, "market_cap_rows", lambda **kw: _month_rows())
    last = ds_db.market_cap_history(dsn="x", security_ids=[7, 9], limit=1)
    assert list(zip(last["security_id"], last["date"].dt.day)) == [(7, 1), (9, 1)]
    first = ds_db.market_cap_history(
        dsn="x", security_ids=[7, 9], limit=1, start_date="2024-01-01"
    )
    assert list(first["date"].dt.day) == [30, 30]


def test_no_rows_is_an_empty_frame_with_the_columns(monkeypatch):
    monkeypatch.setattr(ds_db, "market_cap_rows", lambda **kw: [])
    frame = ds_db.market_cap_history(dsn="x", security_ids=[7])
    assert frame.empty
    assert list(frame.columns) == ["security_id", "date", *ds_db.MARKET_CAP_COLUMNS]


# ---------------------------------------------------------------------------
# ph --mcap
# ---------------------------------------------------------------------------


@pytest.fixture()
def ph_run(monkeypatch, tmp_path, calls):
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    monkeypatch.setattr(ds_db, "issuer_class", lambda **kw: None)
    monkeypatch.setattr(ds_db, "resolve_symbols", lambda **kw: {"ZZ": 7})

    def frame(caps):
        def fake(**kw):
            calls["ph"] = kw
            df = pd.DataFrame(
                {
                    "date": pd.to_datetime(["2024-06-07", "2024-06-10"]),
                    "close": [100.0, 25.0],
                    "market_cap": caps,
                }
            ).set_index("date")
            return df

        return fake

    def invoke(args, caps=(100000.004, 100000.0), rc=DB_RC):
        monkeypatch.setattr(ds_db, "price_history", frame(list(caps)))
        monkeypatch.setattr(ds_sharadar, "price_history", frame(list(caps)))
        cfg = tmp_path / "dukrc.toml"
        cfg.write_text(rc)
        return CliRunner().invoke(main, ["-c", str(cfg)] + args)

    return invoke


def test_mcap_is_asked_of_the_warehouse_and_appended(ph_run, calls):
    result = ph_run(["ph", "ZZ", "--adj", "--close", "--mcap", "--csv"])
    assert result.exit_code == 0, result.output
    assert calls["ph"]["market_cap"] is True and calls["ph"]["adjusted"] is True
    assert calls["ph"]["fields"] == ["close"]
    assert _csv(result.stdout)[0] == {
        "date": "2024-06-07",
        "close": "100.00",
        "market_cap": "100000.0",
    }


def test_mcap_beside_adjusted_prices_says_it_is_not_adjusted(ph_run):
    result = ph_run(["ph", "ZZ", "--adj", "--mcap", "--table"])
    assert result.exit_code == 0, result.output
    assert "market_cap unadjusted" in result.stdout
    assert (
        "market_cap unadjusted" not in ph_run(["ph", "ZZ", "--mcap", "--table"]).stdout
    )


def test_mcap_from_sharadar(ph_run, calls):
    result = ph_run(["-S", "sharadar", "ph", "ZZ", "--mcap", "--csv"], rc=SHARADAR_RC)
    assert result.exit_code == 0, result.output
    assert calls["ph"]["market_cap"] is True


def test_a_series_with_no_cap_says_why(ph_run):
    result = ph_run(["ph", "ZZ", "--mcap", "--csv"], caps=(None, None))
    assert result.exit_code == 0, result.output
    assert "no market cap for ZZ" in result.stderr
    assert _csv(result.stdout)[0]["market_cap"] == ""


def test_without_mcap_nothing_is_asked(ph_run, calls):
    assert ph_run(["ph", "ZZ", "--csv"]).exit_code == 0
    assert calls["ph"]["market_cap"] is False


def test_exact_mcap_reads_exact_bars_with_the_cap(monkeypatch, tmp_path):
    seen = {}

    def bars(**kw):
        seen.update(kw)
        return [
            {"date": D1, "close": Decimal("100.000000"), "market_cap": Decimal("1.50")}
        ]

    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    monkeypatch.setattr(ds_db, "exact_bars", bars)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text(DB_RC)
    result = CliRunner().invoke(
        main, ["-c", str(cfg), "ph", "ZZ", "--exact", "--mcap", "--close", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert seen["market_cap"] is True
    assert json.loads(result.stdout) == [
        {"date": "2024-06-07", "close": "100.000000", "market_cap": "1.50"}
    ]


# ---------------------------------------------------------------------------
# -S sharadar: DAILY's figure, for comparison
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def query(self, table, filters=None, fields=None, **kw):
        self.asked.append((table, dict(filters or {}), tuple(fields or ())))
        return self.rows


DAILY = [
    {"ticker": "TSM", "date": "2026-10-30", "marketcap": 2106382.7},
    {"ticker": "TSM", "date": "2026-10-29", "marketcap": 2239571.3},
]


def test_daily_is_read_in_dollars_oldest_first():
    client = FakeClient(DAILY)
    frame = sharadar_api.get_market_cap_history(
        "k", "tsm", start_date="2026-10-29", client=client
    )
    assert list(frame.index.strftime("%Y-%m-%d")) == ["2026-10-29", "2026-10-30"]
    assert frame["market_cap"].tolist() == pytest.approx([2239571.3e6, 2106382.7e6])
    table, filters, fields = client.asked[0]
    assert table == "daily" and filters["ticker"] == "TSM"
    assert filters["from"] == "2026-10-29"
    assert fields == ("ticker", "date", "marketcap")


@pytest.mark.parametrize(
    "row, phrase",
    [
        ({"ticker": "AAPL", "date": "2026-10-30", "marketcap": 1}, "ticker filter"),
        ({"ticker": "TSM", "date": "2026-10-01", "marketcap": 1}, "window"),
    ],
)
def test_rows_the_filters_should_have_excluded_are_an_error(row, phrase):
    with pytest.raises(SharadarAPIError, match=phrase):
        sharadar_api.daily_market_cap_api(
            "TSM",
            from_date=dt.date(2026, 10, 29),
            client=FakeClient([row]),
        )


def test_ph_mcap_joins_daily_onto_the_bars():
    bars = [
        {
            "ticker": "TSM",
            "date": d,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 10,
            "closeadj": 1.0,
            "closeunadj": 1.0,
        }
        for d in ("2026-10-28", "2026-10-29", "2026-10-30")
    ]

    class Both(FakeClient):
        def query(self, table, filters=None, fields=None, **kw):
            self.asked.append(table)
            return DAILY if table == "daily" else bars

    frame = sharadar_api.get_price_history(
        "k",
        "TSM",
        start_date="2026-10-28",
        fields=["close"],
        market_cap=True,
        client=Both([]),
    )
    assert list(frame.columns) == ["close", "market_cap"]
    assert pd.isna(frame["market_cap"].iloc[0]), "a bar DAILY has no row for"
    assert frame["market_cap"].iloc[2] == pytest.approx(2106382.7e6)


def test_sharadar_mc_reads_each_ticker(monkeypatch, tmp_path):
    asked = []

    def fake(**kw):
        asked.append(kw["symbol"])
        if kw["symbol"] == "SPY":
            return pd.DataFrame()
        return pd.DataFrame(
            {"date": pd.to_datetime(["2026-10-30"]), "market_cap": [1.0e12]}
        ).set_index("date")

    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    monkeypatch.setattr(ds_sharadar, "market_cap_history", fake)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text(SHARADAR_RC)
    result = CliRunner().invoke(
        main, ["-c", str(cfg), "-S", "sharadar", "mc", "TSM", "AAPL", "SPY", "--csv"]
    )
    assert result.exit_code == 0, result.output
    assert asked == ["TSM", "AAPL", "SPY"]
    assert [r["symbol"] for r in _csv(result.stdout)] == ["TSM", "AAPL"]
    assert "DAILY table has no market cap for SPY" in result.stderr
    one = CliRunner().invoke(
        main, ["-c", str(cfg), "-S", "sharadar", "mc", "TSM", "--csv"]
    )
    assert list(_csv(one.stdout)[0]) == ["date", "market_cap"]
    refused = CliRunner().invoke(
        main, ["-c", str(cfg), "-S", "sharadar", "mc", "cik:320193"]
    )
    assert refused.exit_code == 1
    assert "cannot be resolved against the Sharadar source" in refused.stderr
