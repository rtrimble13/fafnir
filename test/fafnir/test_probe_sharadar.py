"""
`fafnir source probe-sharadar` (plan SA-0201, #49): unit tests, no network.

Every row here is synthetic. The fake API reproduces the behaviours the probe
measured on 2026-10-19 (payload shape, newest-first order, the 10,000-row JSON cap,
ignored unknown parameters, modern names in TICKERS' table filter, the bulk
redirect); the securities are invented or carry invented prices. No vendor row is
committed (ADR 0012 §8).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.sources import sharadar as client_mod
from fafnir.sources import sharadar_probe as probe
from fafnir.sources.sharadar import SharadarClient

D = Decimal


# ---------------------------------------------------------------------------
# A fake Sharadar: the measured behaviours over a synthetic world
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _bars(ticker, start, n, raw, *, splits=(), last_event=None, step=D("0.01")):
    """``n`` weekday bars from ``start``, newest last. ``raw`` is the first raw
    close; ``splits`` are (ex_date, value). Split-adjusted fields are the raw
    values divided by the later splits' product, rounded to 3 dp, as SEP stores
    them."""
    out, day, price = [], start, D(raw)
    while len(out) < n:
        if day.weekday() < 5:
            iso = day.isoformat()
            prod = D(1)
            for ex, v in splits:
                if ex > iso:
                    prod *= D(v)
            adj = (price / prod).quantize(D("0.001"))
            out.append(
                {
                    "ticker": ticker,
                    "date": iso,
                    "open": float(adj),
                    "high": float(adj),
                    "low": float(adj),
                    "close": float(adj),
                    "volume": int(1000 * prod),
                    "closeadj": float(adj),
                    "closeunadj": float(price.quantize(D("0.001"))),
                    "lastupdated": last_event or iso,
                }
            )
            price += step
        day += timedelta(days=1)
    return out


NEWEST = "2024-01-02"


def _world():
    aapl_splits = [("2020-08-31", 4)]
    sep = {
        "AAPL": _bars(
            "AAPL",
            date(2019, 11, 1),
            230,
            "250",
            splits=aapl_splits,
            last_event="2020-09-01",
        )
        + _bars("AAPL", date(2023, 12, 27), 5, "190", last_event="2024-01-02"),
        "MSFT": _bars("MSFT", date(2023, 12, 20), 10, "370"),
        "META": _bars("META", date(2012, 5, 18), 3, "38"),
        "OLDCO1": _bars(
            "OLDCO1",
            date(1998, 1, 2),
            40,
            "10",
            splits=[("1998-02-02", 2)],
            last_event="1998-02-02",
        ),
        "NEWCO": _bars("NEWCO", date(2023, 12, 25), 7, "5"),
    }
    sfp = {"FUNDX": _bars("FUNDX", date(2023, 12, 26), 6, "50")}
    actions = {
        "AAPL": [
            {"date": "2020-08-31", "action": "split", "ticker": "AAPL", "value": 4},
            {
                "date": "2019-11-07",
                "action": "dividend",
                "ticker": "AAPL",
                "value": 0.1925,
            },
        ],
        "OLDCO1": [
            {"date": "1998-02-02", "action": "split", "ticker": "OLDCO1", "value": 2}
        ],
    }

    def tick(table, ticker, perma, delisted, first):
        return {
            "table": table,
            "permaticker": perma,
            "ticker": ticker,
            "isdelisted": delisted,
            "firstpricedate": first,
            "lastpricedate": NEWEST,
            "category": "Domestic Common Stock",
            "relatedtickers": None,
        }

    stocks = [
        tick("stocks", "AAPL", "1", "N", "1997-12-31"),
        tick("stocks", "MSFT", "2", "N", "1998-01-02"),
        tick("stocks", "META", "3", "N", "2012-05-18"),
        tick("stocks", "OLDCO1", "4", "Y", "1998-01-02"),
        tick("stocks", "OLDCO", "5", "N", "2015-01-02"),
        tick("stocks", "NEWCO", "6", "N", "2023-12-25"),
    ]
    tickers = (
        stocks
        + [dict(r, table="fundamentals") for r in stocks]
        + [tick("funds", "FUNDX", "7", "N", "2005-01-03")]
    )
    sf1 = [
        {
            "ticker": "AAPL",
            "dimension": "ARQ",
            "calendardate": "2025-12-31",
            "date": "2026-01-30",
            "revenue": 1.0,
            "netinc": None,
            "dps": 0,
        },
        {
            "ticker": "AAPL",
            "dimension": "ARQ",
            "calendardate": "2026-03-31",
            "date": "2026-05-01",
            "revenue": 2.0,
            "netinc": 0.5,
            "dps": 0,
        },
    ]
    return {"SEP": sep, "SFP": sfp, "actions": actions, "tickers": tickers, "SF1": sf1}


class _FakeAPI:
    PAGE_CAP = 10_000
    KNOWN = {
        "ticker",
        "date",
        "from",
        "to",
        "fields",
        "limit",
        "offset",
        "sort",
        "format",
        "table",
        "dimension",
        "years",
        "lastupdated",
        "lastupdated.gte",
    }

    def __init__(self, world, *, paid=True, offset_works=True):
        self.world = world
        self.paid = paid
        self.offset_works = offset_works
        self.calls = 0

    def rows_for(self, table):
        w = self.world
        if table in ("SEP", "stocks"):
            return [r for rows in w["SEP"].values() for r in rows]
        if table in ("SFP", "funds"):
            return [r for rows in w["SFP"].values() for r in rows]
        if table == "actions":
            return [r for rows in w["actions"].values() for r in rows]
        if table == "tickers":
            return list(w["tickers"])
        if table == "SF1":
            return list(w["SF1"])
        return []

    def get(self, table, params, allow_redirects=True):
        self.calls += 1
        p = dict(params)
        if "years" in p:
            return _Resp(
                302,
                {},
                {
                    "Location": f"https://bulk.example/bulk-sharadar/{table}.csv.zip"
                    "?X-Amz-Signature=synthetic"
                },
            )
        if table in ("SEP", "stocks") and p.get("ticker") == "TWO":
            if not self.paid:
                return _Resp(403, {"error": "Exceeds free tier", "description": "x"})
            return _Resp(200, {"count": 1, "data": [{"ticker": "TWO"}]})
        limit = int(p.get("limit", 10_000))
        if limit > self.PAGE_CAP:
            return _Resp(400, {"error": "limit too large", "description": "json cap"})
        rows = self.rows_for(table)
        if "ticker" in p:
            wanted = set(str(p["ticker"]).split(","))
            rows = [r for r in rows if r.get("ticker") in wanted]
        for key in ("table", "dimension", "date"):
            if key in p:
                rows = [r for r in rows if r.get(key) == p[key]]
        if "from" in p:
            rows = [r for r in rows if r.get("date", "") >= p["from"]]
        if "to" in p:
            rows = [r for r in rows if r.get("date", "") <= p["to"]]
        if "lastupdated.gte" in p:
            rows = [
                r for r in rows if (r.get("lastupdated") or "") >= p["lastupdated.gte"]
            ]
        sort = p.get("sort")
        if sort:
            rows = sorted(rows, key=lambda r: str(r.get(sort)))
        else:
            rows = sorted(
                rows,
                key=lambda r: (r.get("date") or "", r.get("ticker") or ""),
                reverse=True,
            )
        offset = int(p.get("offset", 0)) if self.offset_works else 0
        rows = rows[offset : offset + limit]
        if "fields" in p:
            keep = p["fields"].split(",")
            rows = [{k: r.get(k) for k in keep} for r in rows]
        return _Resp(
            200,
            {"count": len(rows), "data": rows},
            {"X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": "4999"},
        )


class _FakeClient(SharadarClient):
    def __init__(self, api):
        super().__init__("synthetic-key", rate_per_min=100_000)
        self.api = api

    def _call(self, table, params=None):
        resp = self.api.get(table, {"format": "json", **(params or {})})
        self.request_count += 1
        if resp.status_code != 200:
            raise client_mod.SourceError(f"HTTP {resp.status_code}")
        return resp.json(), 200, len(resp.content)

    def response(self, table, params=None, *, allow_redirects=True):
        self.request_count += 1
        return self.api.get(table, params or {}, allow_redirects)


def _run(**kw):
    api = _FakeAPI(
        _world(), **{k: v for k, v in kw.items() if k in ("paid", "offset_works")}
    )
    report = probe.probe_sharadar(
        _FakeClient(api), sample_size=kw.get("sample", 10), seed=1
    )
    return {i["number"]: i for i in report["items"]}, report


# ---------------------------------------------------------------------------
# The whole probe over the fake
# ---------------------------------------------------------------------------


def test_every_item_has_a_verdict_and_none_blocks():
    items, report = _run()
    assert sorted(items) == list(range(1, 12))
    assert probe.blocking(report) == []
    assert items[3]["verdict"] in (
        "ratio_matches_actions",
        "ratio_matches_within_rounding",
    )
    assert items[4]["verdict"] == "split_value_new_per_old"
    assert items[5]["verdict"] == "dividend_split_adjusted"
    assert items[6]["verdict"] == "history_under_current_ticker"
    assert items[7]["verdict"] == "permaticker_shared"
    assert items[8]["verdict"] == "same_day_raw_confirmed"
    assert items[9]["verdict"] == "history_restamped_on_adjustment"
    assert items[11]["verdict"] == "as_implemented"
    checks = items[1]["evidence"]["checks"]
    assert "ignored" in checks["unknown_param_ignored"]
    assert "table=SEP -> 0" in checks["tickers_table_filter_modern_names"]
    assert "bulk.example/bulk-sharadar/tickers.csv.zip" in checks["bulk_redirect"]
    assert "synthetic" not in checks["bulk_redirect"]  # the signed query is dropped
    assert report["sample"]["funds"] == 1
    json.dumps(report, default=str)  # the --json report serialises


def test_a_free_key_stops_the_probe_with_one_block():
    items, report = _run(paid=False)
    assert list(items) == [11]
    assert items[11]["level"] == probe.BLOCK and "paid_key" in items[11]["detail"]
    assert report["sample"] == {}


def test_offset_the_client_relies_on_blocks_when_it_is_ignored():
    items, _ = _run(offset_works=False)
    assert items[11]["level"] == probe.BLOCK
    assert "offset_honoured" in items[11]["detail"]


def test_the_report_text_holds_verdicts_not_rows():
    _, report = _run()
    text = probe.format_sharadar_report(report)
    assert "No blocking verdict." in text
    assert " 4. split_direction: PASS split_value_new_per_old" in text
    assert "closeunadj" in text  # in a detail sentence ...
    assert "'close':" not in text  # ... but no evidence row is printed


# ---------------------------------------------------------------------------
# The items, one at a time
# ---------------------------------------------------------------------------


def test_decimals_and_the_split_product():
    assert [probe.decimals(v) for v in (18.19, 0.163, 4, "2.500", None)] == [
        2,
        3,
        0,
        1,
        0,
    ]
    sp = [("2000-06-21", D(2)), ("2014-06-09", D(7)), ("2020-08-31", D(4))]
    assert probe.split_product_after(sp, "1999-01-04") == 56
    assert probe.split_product_after(sp, "2020-08-31") == 1  # trades post-split


def _bar(day, close, unadj, volume=1000):
    return {
        "date": day,
        "close": close,
        "closeunadj": unadj,
        "open": close,
        "high": close,
        "low": close,
        "closeadj": close,
        "volume": volume,
    }


def test_precision_is_ok_at_four_decimals_or_without_splits():
    four = {"A": [_bar("2001-01-02", 1.2345, 2.4691)]}
    assert (
        probe.precision(four, {"A": [("2002-01-02", D(2))]}).verdict == "precision_ok"
    )
    three = {"A": [_bar("2001-01-02", 1.234, 2.468)]}
    assert probe.precision(three, {}).verdict == "precision_ok"  # nothing to recover
    item = probe.precision(three, {"A": [("2002-01-02", D(2))]})
    assert (item.verdict, item.level) == ("precision_3dp", probe.NOTE)


def test_raw_recovery_bands():
    sp = {"A": [("2002-01-02", D(112))]}
    exact = probe.raw_recovery({"A": [_bar("2001-01-02", 0.5, 56)]}, sp)
    assert exact.verdict == "ratio_matches_actions"
    rounded = probe.raw_recovery({"A": [_bar("2001-01-02", 0.163, 18.19)]}, sp)
    assert rounded.verdict == "ratio_matches_within_rounding"  # 0.36% off, inside 3 dp
    missing = {"A": [_bar("2001-01-02", 0.5, 28)] + [_bar("2001-01-03", 0.5, 56)] * 99}
    item = probe.raw_recovery(missing, sp)
    assert (item.verdict, item.level) == ("ratio_mismatch", probe.NOTE)  # 1 in 100
    systematic = {
        "A": [_bar("2001-01-02", 0.5, 28)] * 2 + [_bar("2001-01-03", 0.5, 56)] * 98
    }
    assert probe.raw_recovery(systematic, sp).level == probe.BLOCK  # 2% > 1%


def test_split_direction():
    acts = [{"date": "2020-08-31", "action": "split", "value": 4}]
    assert probe.split_direction(acts, {}, {}).verdict == "split_value_new_per_old"
    inverse = [{"date": "2020-08-31", "action": "split", "value": 0.25}]
    assert probe.split_direction(inverse, {}, {}).verdict == "split_value_old_per_new"
    item = probe.split_direction([], {}, {})
    assert (item.verdict, item.level) == ("unknown", probe.BLOCK)


def test_dividend_value():
    split = {"date": "2020-08-31", "action": "split", "value": 4}

    def div(v):
        return [split, {"date": "2019-11-07", "action": "dividend", "value": v}]

    assert probe.dividend_value(div(0.77)).verdict == "dividend_as_declared"
    assert probe.dividend_value(div(0.1925)).verdict == "dividend_split_adjusted"
    assert probe.dividend_value(div(0.5)).level == probe.BLOCK


def test_ticker_keys_and_permaticker():
    listing = [
        {"ticker": "OLDCO1", "isdelisted": "Y"},
        {"ticker": "OLDCO", "isdelisted": "N"},
        {"ticker": "GONE", "isdelisted": "Y"},
    ]
    item = probe.ticker_keys([{"date": "2012-05-18"}], [], listing)
    assert item.verdict == "history_under_current_ticker"
    assert item.evidence["delisted_with_numeric_suffix"] == 1
    assert item.evidence["suffix_root_live_today"] == 1
    late = probe.ticker_keys(
        [{"date": "2022-06-09"}], [{"date": "2012-05-18"}], listing
    )
    assert late.level == probe.BLOCK

    stocks = [{"ticker": "A", "permaticker": "1"}, {"ticker": "B", "permaticker": "2"}]
    same = probe.permaticker_across_tables(stocks, list(stocks), {"ticker"}, set())
    assert same.verdict == "permaticker_shared"
    other = [{"ticker": "A", "permaticker": "9"}]
    assert (
        probe.permaticker_across_tables(stocks, other, set(), set()).level
        == probe.BLOCK
    )


def test_same_day_and_update_behaviour():
    bars = {"A": [_bar("2024-01-02", 5.0, 5.0), _bar("2023-12-29", 4.0, 4.0)]}
    assert probe.same_day(bars).verdict == "same_day_raw_confirmed"
    bad = {"A": [_bar("2024-01-02", 2.5, 5.0)]}
    assert probe.same_day(bad).level == probe.BLOCK

    rows = [
        dict(_bar(f"2024-01-0{d}", 1.0, 1.0), lastupdated=lu)
        for d, lu in ((2, "2024-01-05"), (3, "2024-01-05"), (5, "2024-01-05"))
    ]
    acts = {"A": [{"date": "2024-01-04", "action": "dividend"}]}
    assert (
        probe.update_behaviour({"A": rows}, acts).verdict
        == "history_restamped_on_adjustment"
    )
    stale = [dict(r, lastupdated=r["date"]) for r in rows]
    assert probe.update_behaviour({"A": stale}, acts).verdict == "history_not_restamped"


def test_the_sample_is_deterministic_and_spans_every_stratum():
    listing = [
        {
            "ticker": f"T{i}",
            "permaticker": str(i),
            "isdelisted": "Y" if (i // 4) % 2 else "N",
            "firstpricedate": f"{1990 + (i % 4) * 10}-01-02",
        }
        for i in range(80)
    ]
    first = probe.draw_sample(listing, 8, seed=3)
    assert first == probe.draw_sample(list(reversed(listing)), 8, seed=3)
    assert first != probe.draw_sample(listing, 8, seed=4)
    strata = {(r["firstpricedate"][:3], r["isdelisted"]) for r in first}
    assert len(strata) == 8  # four decades x listed/delisted, one each


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_cli(tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text("")
    monkeypatch.setenv("SHARADAR_API_KEY", "synthetic-key")

    def run(**kw):
        api = _FakeAPI(_world(), **kw)
        monkeypatch.setattr(cli, "_sharadar_client", lambda cfg: _FakeClient(api))
        return lambda *args: CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "source", "probe-sharadar", "--sample", "10", *args],
        )

    return run


def test_the_command_passes_and_prints_json_on_request(fake_cli):
    ok = fake_cli()()
    assert ok.exit_code == 0, ok.output
    assert "No blocking verdict." in ok.output
    report = json.loads(fake_cli()("--json").output)
    assert len(report["items"]) == 11


def test_the_command_fails_on_a_block(fake_cli):
    out = fake_cli(paid=False)()
    assert out.exit_code != 0
    assert "1 blocking verdict(s)" in out.output
