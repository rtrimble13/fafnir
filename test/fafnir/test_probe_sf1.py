"""
`fafnir source probe-fundamentals` (plan SA-0705, #83): no network.

Every row here is synthetic: an invented SF1 world with the shapes the probe
measured on 2026-10-24 (AR rows dated by their filing, MR rows by their period end,
quarters that sum to the year, share counts in today's terms, zeros for absent
items, ``fxusd`` as foreign units per USD). Only the two filing dates in
``FILING_CASES`` are real, and they are public record. No vendor row is committed.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import sf1_probe as probe
from fafnir.sources import sharadar_files as sf

D = Decimal
COLUMNS = list(
    probe.ENTITY + probe.INCOME + probe.BALANCE + probe.CASH_FLOW + probe.METRICS
)

# ticker -> (fiscal year-end month, first fiscal year, last fiscal year)
COMPANIES = {
    "AAPL": (9, 2012, 2021),
    "MSFT": (6, 2012, 2021),
    "GE": (12, 1996, 2005),
    "KO": (12, 2012, 2021),
    "JPM": (12, 2012, 2021),
    "XOM": (12, 2012, 2021),
    "OLDCO1": (12, 2000, 2005),  # a dead issuer, suffixed as Sharadar suffixes one
    "FORX": (12, 2016, 2021),  # reports in EUR
}
SPLITS = {"AAPL": [(date(2014, 6, 9), D(7)), (date(2020, 8, 31), D(4))]}
FILED = {(t, fp): d for t, fp, d in probe.FILING_CASES}


def _month_end(year: int, month: int) -> date:
    while month <= 0:
        year, month = year - 1, month + 12
    return date(year, month, calendar.monthrange(year, month)[1])


def _product(splits, after: date) -> Decimal:
    p = D(1)
    for ex, value in splits:
        if ex > after:
            p *= value
    return p


def _world(*, lag=None, today_terms=True, ytd=False, drop=()):
    """SF1 rows for COMPANIES. ``lag(seq)`` overrides the filing lag; ``drop`` is a
    set of ``(ticker, fiscalperiod)`` whose ARQ rows are left out."""
    rows, seq = [], 0
    for ticker, (end, first, last) in COMPANIES.items():
        splits = SPLITS.get(ticker, [])
        fx = D("0.9") if ticker == "FORX" else D(1)
        for fy in range(first, last + 1):
            quarters = []
            for k in range(1, 5):
                rp = _month_end(fy, end - 3 * (4 - k))
                revenue = D(100 * (k + fy - 1990))
                as_reported = D(1000) / _product(splits, rp)
                shares = D(1000) if today_terms else as_reported
                quarters.append((k, rp, revenue, shares))
            fy_rp = quarters[-1][1]
            total = sum(q[2] for q in quarters)
            running = D(0)
            for k, rp, revenue, shares in quarters:
                running += revenue
                value = running if ytd else revenue
                seq += 1
                fp = f"{fy}-Q{k}"
                if (ticker, fp) in drop:
                    continue
                filed = rp + timedelta(days=lag(seq) if lag else 30 + seq % 13)
                for dim in ("ARQ", "ART"):
                    rows.append(_row(ticker, dim, rp, rp, filed, fp, value, shares, fx))
                    rows.append(
                        _row(ticker, "M" + dim[1:], rp, rp, rp, fp, value, shares, fx)
                    )
            seq += 1
            fp = f"{fy}-FY"
            filed = FILED.get(
                (ticker, fp),
                fy_rp + timedelta(days=lag(seq) if lag else 60 + seq % 17),
            )
            cal = date(fy_rp.year, 12, 31)
            shares = quarters[-1][3]
            rows.append(_row(ticker, "ARY", cal, fy_rp, filed, fp, total, shares, fx))
            restated = total + 5 if (ticker, fp) == probe.RESTATEMENT_CASE else total
            rows.append(
                _row(ticker, "MRY", cal, fy_rp, fy_rp, fp, restated, shares, fx)
            )
    return rows


def _row(ticker, dim, cal, rp, filed, fp, revenue, shares, fx):
    netinc = revenue / 10
    return {
        "ticker": ticker,
        "dimension": dim,
        "calendardate": cal,
        "date": filed,
        "reportperiod": rp,
        "fiscalperiod": fp,
        "lastupdated": date(2026, 7, 31),
        "revenue": revenue,
        "revenueusd": (revenue / fx).quantize(D("1")),
        "netinc": netinc,
        "ncfo": revenue / 5,
        "capex": -revenue / 20,
        "shareswa": shares,
        "eps": (netinc / shares).quantize(D("0.001")),
        "rnd": D(0) if ticker == "GE" else revenue / 50,
        "intexp": D(3),
        "depamor": D(7),
        "fxusd": fx,
        "price": D(50),
        "marketcap": shares * 50,
    }


LISTING = [
    {"permaticker": "101", "ticker": "AAPL", "currency": "USD"},
    {"permaticker": "102", "ticker": "MSFT", "currency": "USD"},
    {"permaticker": "103", "ticker": "GE", "currency": "USD"},
    {"permaticker": "104", "ticker": "KO", "currency": "USD"},
    {"permaticker": "105", "ticker": "JPM", "currency": "USD"},
    {"permaticker": "106", "ticker": "XOM", "currency": "USD"},
    {"permaticker": "201", "ticker": "OLDCO1", "currency": "USD", "is_delisted": True},
    {"permaticker": "301", "ticker": "FORX", "currency": "EUR", "category": "ADR"},
    {"permaticker": "107", "ticker": "GOOGL", "currency": "USD"},
    {"permaticker": "108", "ticker": "BRK.B", "currency": "USD"},
]


def _listing():
    out = []
    for r in LISTING:
        out.append(
            {
                "permaticker": r["permaticker"],
                "ticker": r["ticker"],
                "category": r.get("category", "Domestic Common Stock Primary Class"),
                "is_delisted": r.get("is_delisted", False),
                "currency": r["currency"],
                "first_quarter": "2000-03-31",
            }
        )
    return out


def _data(rows=None, *, splits=SPLITS, listing=None):
    return probe.Sf1Data(
        origin="test",
        columns=COLUMNS,
        rows=_world() if rows is None else rows,
        listing=listing or _listing(),
        splits=splits,
        sample={
            "probe_set": list(probe.PROBE_SET),
            "dead": ["OLDCO1"],
            "foreign": ["FORX"],
        },
    )


def _items(data):
    return {i["number"]: i for i in probe.probe_fundamentals(data)["items"]}


# ---------------------------------------------------------------------------
# The verdicts on a world shaped as SF1 measured
# ---------------------------------------------------------------------------


def test_the_measured_shape_has_no_block_and_names_each_fact():
    items = _items(_data())
    assert {n: (i["level"], i["verdict"]) for n, i in items.items()} == {
        1: ("NOTE", "history_before_floor"),
        2: ("NOTE", "ar_filed_mr_undated"),
        3: ("NOTE", "mr_carries_restatements"),
        4: ("NOTE", "sf1_taxonomy"),
        5: ("PASS", "discrete_quarters_with_q4"),
        6: ("NOTE", "zero_for_absent"),
        7: ("NOTE", "currency_from_tickers"),
        8: ("NOTE", "primary_class_only"),
        9: ("PASS", "vendor_metrics_identified"),
        10: ("NOTE", "share_terms_today"),
    }
    assert not probe.blocking({"items": list(items.values())})


def test_knowledge_dates_report_the_public_record_and_the_edgar_list():
    item = _items(_data())[2]
    assert "2 of 2 public-record filings agree" in item["detail"]
    assert all(c["sf1"] == c["edgar"] for c in item["evidence"]["public_record"])
    assert len(item["evidence"]["compare_with_edgar"]) == probe.EDGAR_CHECKS
    assert "MR `date` is the period end" in item["detail"]


def test_a_filing_before_its_period_end_blocks():
    item = _items(_data(_world(lag=lambda seq: -2)))[2]
    assert (item["level"], item["verdict"]) == ("BLOCK", "filing_before_period_end")


def test_one_lag_for_every_filing_looks_imputed():
    item = _items(_data(_world(lag=lambda seq: 45)))[2]
    assert (item["level"], item["verdict"]) == ("BLOCK", "filing_dates_look_imputed")


def test_a_filing_date_off_the_public_record_blocks():
    rows = _world()
    for r in rows:
        if (r["ticker"], r["dimension"], r["fiscalperiod"]) == (
            "AAPL",
            "ARY",
            "2020-FY",
        ):
            r["date"] = date(2020, 11, 6)
    item = _items(_data(rows))[2]
    assert item["verdict"] == "filing_date_disagrees_with_public_record"
    assert item["level"] == "BLOCK"


def test_the_restatement_case_names_its_fields_and_dates():
    item = _items(_data())[3]
    # The synthetic world derives net income, cash flows and R&D from revenue.
    assert set(item["evidence"]["case"]["fields"]) == {
        "revenue",
        "revenueusd",
        "netinc",
        "ncfo",
        "capex",
        "rnd",
    }
    assert "dated 2015-07-31, the filing" in item["detail"]
    assert "dated 2015-06-30, the period end" in item["detail"]


def test_year_to_date_quarters_are_named():
    item = _items(_data(_world(ytd=True)))[5]
    assert (item["level"], item["verdict"]) == ("NOTE", "ytd_quarters")


def test_a_missing_q4_is_told_from_a_coverage_edge():
    edges = {("KO", "2012-Q1"), ("KO", "2012-Q2"), ("JPM", "2012-Q1")}
    assert _items(_data(_world(drop=edges)))[5]["verdict"] == (
        "discrete_quarters_with_q4"
    )
    no_q4 = {(t, f"{y}-Q4") for t in ("KO", "JPM") for y in range(2013, 2016)}
    item = _items(_data(_world(drop=no_q4)))[5]
    assert (item["level"], item["verdict"]) == ("NOTE", "q4_missing")


def test_the_null_zero_case_reads_ge():
    item = _items(_data())[6]
    assert item["evidence"]["case"]["rnd"] == {"zero": 8}
    assert item["evidence"]["case"]["intexp"] == {"value": 8}


def test_currency_measures_the_direction_of_fxusd():
    item = _items(_data())[7]
    assert item["evidence"]["non_usd"] == {"FORX": "EUR"}
    assert "revenue x fxusd on 0 and revenue / fxusd on" in item["detail"]


def test_a_secondary_share_class_in_the_listing_changes_the_identity_verdict():
    listing = _listing() + [
        {
            "permaticker": "109",
            "ticker": "GOOG",
            "category": "Domestic Common Stock Secondary Class",
            "is_delisted": False,
            "currency": "USD",
            "first_quarter": "2014-03-31",
        }
    ]
    item = _items(_data(listing=listing))[8]
    assert item["verdict"] == "permaticker_per_ticker"
    assert item["evidence"]["share_classes"]["GOOGL/GOOG"]["secondary_listed"]


def test_as_reported_share_counts_pass():
    item = _items(_data(_world(today_terms=False)))[10]
    assert (item["level"], item["verdict"]) == ("PASS", "share_terms_as_reported")
    assert {s["reads"] for s in item["evidence"]["splits"]} == {"jumps"}


def test_share_terms_need_a_split_between_two_quarters():
    item = _items(_data(splits={}))[10]
    assert item["verdict"] == "no_split_in_sample"


def test_a_file_without_its_key_columns_blocks():
    data = _data()
    data.columns = [c for c in COLUMNS if c != "reportperiod"]
    item = _items(data)[4]
    assert (item["level"], item["verdict"]) == ("BLOCK", "key_columns_missing")


def test_no_rows_blocks():
    item = _items(_data([]))[1]
    assert (item["level"], item["verdict"]) == ("BLOCK", "no_rows")


def test_the_report_prints_verdicts_and_no_evidence():
    report = probe.probe_fundamentals(_data())
    text = probe.format_fundamentals_report(report)
    assert "No blocking verdict." in text
    assert "10. Share terms: NOTE share_terms_today" in text
    assert "compare_with_edgar" not in text


# ---------------------------------------------------------------------------
# The sample
# ---------------------------------------------------------------------------


def test_draw_is_deterministic_and_keeps_the_pools_apart():
    listing = _listing() + [
        {
            "permaticker": str(900 + i),
            "ticker": f"DEAD{i}",
            "category": "Domestic Common Stock",
            "is_delisted": True,
            "currency": "USD",
            "first_quarter": "1999-03-31",
        }
        for i in range(30)
    ]
    one = probe.draw(listing, dead=5, foreign=1, seed=3)
    assert one == probe.draw(list(reversed(listing)), dead=5, foreign=1, seed=3)
    assert one != probe.draw(listing, dead=5, foreign=1, seed=4)
    assert one["probe_set"] == list(probe.PROBE_SET)
    assert len(one["dead"]) == 5 and all(
        t.startswith("DEAD") or t == "OLDCO1" for t in one["dead"]
    )
    assert one["foreign"] == ["FORX"]


def test_draw_skips_tickers_sf1_does_not_cover():
    listing = [dict(r, first_quarter=None) for r in _listing()]
    assert probe.draw(listing, seed=0)["dead"] == []


# ---------------------------------------------------------------------------
# Reading from the API (--from-api)
# ---------------------------------------------------------------------------


def _json(row):
    return {
        k: v.isoformat() if isinstance(v, date) else float(v) if isinstance(v, D) else v
        for k, v in row.items()
    }


class _FakeClient:
    """Serves the world as the API does: a ticker list answers only the last two
    years (here, nothing), and an ignored filter would leak other tickers' rows."""

    T_TICKERS, T_SF1, T_ACTIONS = "tickers", "SF1", "actions"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.request_count = 0
        self.bytes_downloaded = 0

    def query(self, table, filters=None, fields=None, **_):
        self.request_count += 1
        self.calls.append((table, dict(filters or {})))
        if table == "tickers":
            return [
                {
                    "permaticker": int(r["permaticker"]),
                    "ticker": r["ticker"],
                    "category": r["category"],
                    "isdelisted": "Y" if r["is_delisted"] else "N",
                    "currency": r["currency"],
                    "firstquarter": r["first_quarter"],
                }
                for r in _listing()
            ]
        ticker = filters["ticker"]
        if "," in ticker:
            return []
        if table == "SF1":
            mine = [_json(r) for r in self.rows if r["ticker"] == ticker]
            return mine + [_json(dict(self.rows[0], ticker="STRAY"))]
        events = [
            {
                "date": ex.isoformat(),
                "action": "split",
                "ticker": ticker,
                "value": float(v),
            }
            for ex, v in SPLITS.get(ticker, [])
        ]
        return events + [
            {"date": "2019-11-07", "action": "dividend", "ticker": ticker, "value": 0.1}
        ]


def test_from_api_reads_one_ticker_per_request_and_checks_its_rows():
    client = _FakeClient(_world())
    data = probe.from_api(client, dead=1, foreign=1, seed=0)
    sf1_calls = [f for t, f in client.calls if t == "SF1"]
    assert all("," not in f["ticker"] for f in sf1_calls)
    assert {f["ticker"] for f in sf1_calls} == set(probe.PROBE_SET) | {"OLDCO1", "FORX"}
    assert "STRAY" not in {r["ticker"] for r in data.rows}
    assert data.splits["AAPL"] == SPLITS["AAPL"]
    assert isinstance(data.rows[0]["date"], date)
    assert isinstance(data.rows[0]["revenue"], D)
    assert data.requests == client.request_count == 1 + 2 * len(sf1_calls)
    items = _items(data)
    assert items[10]["verdict"] == "share_terms_today"
    assert not [i for i in items.values() if i["level"] == "BLOCK"]


def test_type_row_types_api_values_as_the_file_reader_does():
    typed = sf.type_row(
        "fundamentals",
        {
            "ticker": "AAPL",
            "date": "2020-10-30",
            "eps": 0.52,
            "revenue": 274.0,
            "rnd": None,
        },
    )
    assert typed == {
        "ticker": "AAPL",
        "date": date(2020, 10, 30),
        "eps": D("0.52"),
        "revenue": D("274.0"),
        "rnd": None,
    }
    with pytest.raises(sf.SharadarFileError, match="column date"):
        sf.type_row("fundamentals", {"date": "not a date"})


@pytest.fixture()
def fake_cli(tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text("")
    monkeypatch.setenv("SHARADAR_API_KEY", "synthetic-key")

    def run(rows):
        monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: _FakeClient(rows))
        return lambda *args: CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "source", "probe-fundamentals", "--from-api", *args],
        )

    return run


def test_the_command_passes_and_prints_json_on_request(fake_cli):
    ok = fake_cli(_world())("--dead", "1", "--foreign", "1")
    assert ok.exit_code == 0, ok.output
    assert "No blocking verdict." in ok.output
    report = json.loads(fake_cli(_world())("--json").output)
    assert len(report["items"]) == 10
    assert report["origin"] == "api"


def test_the_command_fails_on_a_block(fake_cli):
    out = fake_cli(_world(lag=lambda seq: 45))()
    assert out.exit_code != 0
    assert "1 blocking verdict(s)" in out.output


# ---------------------------------------------------------------------------
# Reading from the ledger (the default): needs a database
# ---------------------------------------------------------------------------


def _csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if r.get(c) is None else str(r[c]) for c in columns])
    return buf.getvalue()


def _record(db, bulk_dir, table, text):
    bulk_dir.mkdir(exist_ok=True)
    body = text.encode()
    sha = hashlib.sha256(body).hexdigest()
    (bulk_dir / f"{sha}.csv").write_bytes(body)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind="full",
        filters={"years": "full"},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(body),
        rows=None,
        columns=None,
        ingestion_run_id=None,
    )
    return entry["file_id"]


def _master(db):
    for r in _listing():
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
            "category, is_delisted, currency, first_quarter) "
            "VALUES ('sharadar', 'fundamentals', %s, %s, %s, %s, %s, %s)",
            (
                r["permaticker"],
                r["ticker"],
                r["category"],
                r["is_delisted"],
                r["currency"],
                r["first_quarter"],
            ),
        )


@pytest.fixture()
def ledger(db, tmp_path):
    bulk = tmp_path / "bulk"
    _master(db)
    _record(db, bulk, "fundamentals", _csv(_world(), COLUMNS))
    actions = [
        {"date": ex, "action": "split", "ticker": "AAPL", "value": v}
        for ex, v in SPLITS["AAPL"]
    ] + [
        {"date": date(2019, 11, 7), "action": "dividend", "ticker": "AAPL", "value": 1}
    ]
    _record(db, bulk, "actions", _csv(actions, ["date", "action", "ticker", "value"]))
    return bulk


@pytest.mark.integration
def test_from_ledger_reads_the_files_and_agrees_with_the_api(db, ledger):
    data = probe.from_ledger(db, ledger, dead=1, foreign=1, seed=0)
    assert data.origin.startswith("file ")
    assert data.columns == COLUMNS
    assert data.splits == SPLITS
    assert set(data.sample["dead"]) == {"OLDCO1"}
    from_file = {n: (i["level"], i["verdict"]) for n, i in _items(data).items()}
    api = probe.from_api(_FakeClient(_world()), dead=1, foreign=1, seed=0)
    assert from_file == {n: (i["level"], i["verdict"]) for n, i in _items(api).items()}


@pytest.mark.integration
def test_from_ledger_names_what_is_missing(db, tmp_path):
    with pytest.raises(probe.ProbeError, match="no current SF1 file"):
        probe.from_ledger(db, tmp_path)
    _record(db, tmp_path / "bulk", "fundamentals", _csv(_world(), COLUMNS))
    with pytest.raises(probe.ProbeError, match="sharadar-master"):
        probe.from_ledger(db, tmp_path / "bulk")


@pytest.mark.integration
def test_the_command_reads_the_ledger(db, ledger, tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{ledger}"\n')
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    out = CliRunner().invoke(
        cli.main, ["--config", str(rc), "source", "probe-fundamentals", "--dead", "1"]
    )
    assert out.exit_code == 0, out.output
    assert "read from file " in out.output
    assert "No blocking verdict." in out.output


def test_a_row_without_a_key_date_is_left_out_and_counted():
    rows = _world()
    rows[0] = dict(rows[0], date=None)
    rows[1] = dict(rows[1], fxusd=D(0))
    report = probe.probe_fundamentals(_data(rows))
    assert report["rows"] == len(rows) - 1
    assert any("1 rows without one of" in n for n in report["notes"])
    assert not probe.blocking(report)
