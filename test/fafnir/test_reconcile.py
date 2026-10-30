"""
`fafnir source reconcile` and `scripts/reconcile.sh` (#117).

The rules are unit-tested on `classify`; the reads (a bar's source, the corrections,
the overrides, the Sharadar link's ticker) against the test database with fake
vendor clients; the command through the CLI with its clients replaced.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import reconcile as rc
from fafnir.sources.base import SourceError

D = Decimal
integration = pytest.mark.integration

TODAY = dt.date.today()
DAYS = [TODAY - dt.timedelta(days=n) for n in (8, 7, 6, 5, 4, 3)]


def _kinds(stored, served, *, vendor="fmp", fed=False, corrected=(), operator=()):
    return rc.classify(
        stored,
        served,
        vendor=vendor,
        fed_by_vendor=fed,
        corrected=set(corrected),
        operator=set(operator),
    )


# -- the rules ----------------------------------------------------------------


def test_the_vendors_own_bar_must_match_exactly():
    d = DAYS[0]
    assert _kinds({d: (D("20"), "fmp")}, {d: D("20")}) == {d: "match"}
    assert _kinds({d: (D("20"), "fmp")}, {d: D("20.001")}) == {d: "drift"}


def test_another_vendors_bar_agrees_within_a_cent_or_a_basis_point():
    d = DAYS[0]
    assert _kinds({d: (D("20"), "sharadar")}, {d: D("20.01")}) == {d: "match"}
    assert _kinds({d: (D("20"), "sharadar")}, {d: D("20.02")}) == {d: "differs"}
    # A basis point of 500 is 5 cents.
    assert _kinds({d: (D("500"), "sharadar")}, {d: D("500.05")}) == {d: "match"}


def test_corrected_and_operator_dates_are_expected_whatever_the_values():
    a, b, c = DAYS[:3]
    stored = {a: (D("30"), "sharadar"), c: (D("9"), "operator")}
    served = {a: D("33"), b: D("50"), c: D("10")}
    kinds = _kinds(stored, served, corrected=[a], operator=[b])
    assert kinds == {a: "corrected", b: "operator", c: "operator"}


def test_a_missing_bar_is_drift_only_for_the_vendors_own_feed():
    a, b = DAYS[:2]
    # The warehouse holds FMP's bar and FMP no longer serves it: drift.
    assert _kinds({a: (D("1"), "fmp")}, {}) == {a: "drift"}
    # Another vendor's bar FMP never had: a gap that vendor filled.
    assert _kinds({a: (D("1"), "sharadar")}, {}) == {a: "extra"}
    # FMP serves a bar the warehouse lacks: its feed missed it, or the vendors differ.
    assert _kinds({}, {b: D("2")}, fed=True) == {b: "drift"}
    assert _kinds({}, {b: D("2")}, fed=False) == {b: "differs"}


def test_the_verdict_is_the_worst_kind():
    r = rc.SymbolResult("ZZ", "fmp", counts=Counter(match=3, corrected=1, extra=1))
    assert r.verdict == rc.OK
    r.counts["differs"] = 1
    assert r.verdict == rc.DIFFERS
    r.counts["drift"] = 1
    assert r.verdict == rc.DRIFT
    assert rc.SymbolResult("ZZ", "fmp", error="x").verdict == rc.NOT_COMPARED


def test_the_report_labels_the_warehouse_by_its_feed():
    r = rc.SymbolResult(
        "ZZ",
        "fmp",
        security_id=7,
        feed="sharadar",
        window=(DAYS[0], DAYS[-1]),
        counts=Counter(match=4, corrected=1, differs=1),
        lines=[rc.Line(DAYS[2], "differs", D("40.000000"), "sharadar", D("41"))],
    )
    out = rc.format_report(
        [r, rc.SymbolResult("QQ", "fmp", error="not in the warehouse")]
    )
    assert out[0].startswith("---- ZZ (security 7): warehouse (sharadar) vs FMP, ")
    assert out[1] == "    6 dates: 4 match, 1 corrected (expected), 1 differs"
    assert out[2] == f"    {DAYS[2]}  warehouse 40 (sharadar)  fmp 41  differs"
    assert "DIFFERS: the warehouse (sharadar) and FMP disagree on 1 date(s)" in out[3]
    assert out[4:6] == ["---- QQ ----", "    NOT COMPARED: not in the warehouse"]
    assert out[-1] == "==> 2 symbol(s): 0 OK, 1 DIFFERS, 0 DRIFT, 1 NOT COMPARED"


# -- against the warehouse ----------------------------------------------------


class FakeFMP:
    def __init__(self, bars=None, fail=None):
        self.bars, self.fail, self.calls = bars or {}, fail, []

    def eod_raw(self, symbol, from_date, to_date):
        self.calls.append((symbol, from_date, to_date))
        if self.fail:
            raise SourceError(self.fail)
        return [
            {"date": d.isoformat(), "adjClose": close, "close": close}
            for d, close in sorted(self.bars.get(symbol, {}).items())
            if from_date <= d.isoformat() <= to_date
        ]


class FakeSharadar:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, table, filters):
        self.calls.append((table, dict(filters)))
        return [
            {"ticker": filters["ticker"], "date": d.isoformat(), "closeunadj": c}
            for d, c in sorted(self.rows.get((table, filters["ticker"]), {}).items())
            if filters["from"] <= d.isoformat() <= filters["to"]
        ]


def _bar(sid, day, close):
    c = D(close)
    return {
        "security_id": sid,
        "trade_date": day,
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": 100,
    }


def _setup(db):
    """ZZS: FMP-minted, fed by Sharadar since a link, with each kind of date.
    ZZF: FMP-minted and fed, one bar FMP has since restated."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    zzs = repo.upsert_security(
        db, primary_symbol="ZZS", company_name="ZZS Co", exchange_code="NYSE"
    )
    zzf = repo.upsert_security(
        db, primary_symbol="ZZF", company_name="ZZF Co", exchange_code="NYSE"
    )
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', '900001', 'ZZSV')"
    )
    repo.insert_vendor_link(
        db,
        security_id=zzs,
        vendor="sharadar",
        vendor_key="900001",
        valid_from=DAYS[0],
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    d1, d2, d3, d4, d5, _ = DAYS
    repo.upsert_daily_prices(db, [_bar(zzs, d1, "10"), _bar(zzs, d3, "33")])
    repo.upsert_daily_prices(
        db, [_bar(zzs, d2, "20"), _bar(zzs, d4, "40")], source="sharadar"
    )
    # d3: `vendor correct` replaced FMP's 33 with Sharadar's 30, audited.
    repo.upsert_daily_prices(db, [_bar(zzs, d3, "30")], source="sharadar", correct=True)
    # d5: an operator deleted the bar.
    db.execute(
        "INSERT INTO ops.operator_override (security_id, target, key_date, "
        "operation, note, created_by) VALUES (%s, 'daily_price', %s, 'delete', "
        "'bad print', 't')",
        (zzs, d5),
    )
    repo.upsert_daily_prices(db, [_bar(zzf, d1, "10"), _bar(zzf, d2, "20")])
    return zzs, zzf


def _fmp():
    d1, d2, d3, d4, d5, d6 = DAYS
    return FakeFMP(
        {
            "ZZS": {d1: 10.0, d2: 20.005, d3: 33.0, d4: 41.0, d5: 50.0, d6: 60.0},
            "ZZF": {d1: 10.0, d2: 21.0},
        }
    )


def _run(db, symbols, **kw):
    kw.setdefault("from_date", DAYS[0])
    kw.setdefault("to_date", TODAY)
    return {r.symbol: r for r in rc.reconcile(db, symbols, **kw)}


@integration
def test_each_date_is_judged_by_whose_bar_the_warehouse_holds(db):
    zzs, zzf = _setup(db)
    out = _run(db, ["zzs", "ZZF", "NOPE"], fmp=_fmp())
    s, f = out["ZZS"], out["ZZF"]
    assert (s.security_id, s.feed) == (zzs, "sharadar")
    assert s.counts == Counter(match=2, corrected=1, differs=2, operator=1)
    assert s.verdict == rc.DIFFERS
    # d4: Sharadar's bar against FMP's; d6: a bar FMP serves and Sharadar's feed lacks.
    assert [(x.day, x.kind, x.stored_source) for x in s.lines] == [
        (DAYS[3], "differs", "sharadar"),
        (DAYS[5], "differs", None),
    ]
    assert (f.feed, f.verdict, f.counts) == ("fmp", rc.DRIFT, Counter(match=1, drift=1))
    assert out["NOPE"].verdict == rc.NOT_COMPARED
    assert out["NOPE"].error == "not in the warehouse"


@integration
def test_a_symbol_fmp_serves_nothing_for_is_compared_with_sharadar(db):
    zzs, _ = _setup(db)
    d1, d2, d3, d4, d5, d6 = DAYS
    sharadar = FakeSharadar({("SEP", "ZZSV"): {d2: "20", d3: "30", d4: "40.5"}})
    out = _run(
        db,
        ["ZZS"],
        fmp=FakeFMP(fail="402 Payment Required"),
        sharadar=sharadar,
        sessions=3,
    )
    r = out["ZZS"]
    assert (r.vendor, r.fallback_from) == ("sharadar", "fmp")
    assert r.fallback_reason == "FMP failed: 402 Payment Required"
    # The link's ticker and table, over the last 3 stored sessions (d2..d4).
    assert sharadar.calls == [
        ("SEP", {"ticker": "ZZSV", "from": d2.isoformat(), "to": TODAY.isoformat()})
    ]
    # d2 and d3 are Sharadar's own bars and match; d4 is its own bar, re-served.
    assert r.counts == Counter(match=2, drift=1)
    assert r.verdict == rc.DRIFT
    assert (
        "    FMP not compared (FMP failed: 402 Payment Required); compared with "
        "Sharadar instead." in rc.format_report([r])
    )


@integration
def test_without_a_sharadar_key_an_fmp_failure_is_not_compared(db):
    _setup(db)
    r = _run(db, ["ZZF"], fmp=FakeFMP(fail="timeout"))["ZZF"]
    assert r.verdict == rc.NOT_COMPARED
    assert r.error == "FMP failed: timeout; no Sharadar key for the fallback"


@integration
def test_the_command(db, monkeypatch):
    _setup(db)
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    monkeypatch.setenv("FMP_API_KEY", "dummy")
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_fmp_client", lambda cfg: _fmp())

    def run(*args):
        return CliRunner().invoke(cli.main, ["source", "reconcile", *args])

    out = run("ZZS,ZZF", "--from", DAYS[0].isoformat())
    assert out.exit_code == 0, out.output
    assert out.output.startswith(
        f"==> Reconciling ZZS,ZZF: the warehouse's raw closes against FMP, "
        f"from {DAYS[0]}\n"
    )
    assert "warehouse (sharadar) vs FMP" in out.output
    assert "DRIFT: the warehouse no longer matches FMP, its own feed" in out.output
    assert out.output.rstrip().endswith("0 OK, 1 DIFFERS, 1 DRIFT, 0 NOT COMPARED")
    out = run("NOPE")
    assert out.exit_code == 1
    assert "No symbol could be compared." in out.output
