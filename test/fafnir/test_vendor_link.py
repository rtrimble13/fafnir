"""
`fafnir vendor link` (plan SA-0401, #62; the integration tests need FAFNIR_TEST_DSN).

The golden cases are the ones the issue names, rebuilt from synthetic data: a rename
(the FB -> META shape), a venue transfer that left two FMP rows for one issuer, and
a reused ticker (the BID shape: a dead issuer's history filed under ``BID1``).
Sharadar's closes come from a fake source keyed by ticker, so a case can agree or
disagree on purpose. Every key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import vendor_link as vl

D = Decimal


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_identifiers():
    assert vl.normalise_cik("0000320193") == vl.normalise_cik("320193") == "320193"
    assert vl.normalise_cik("000") is None and vl.normalise_cik(None) is None
    assert vl.isin_cusip("us0378331005") == "037833100"
    assert vl.isin_cusip("GB0002634946") is None and vl.isin_cusip(None) is None
    cand = {"cik": "0000000042", "cusips": ["00000A101", "00000A102"]}
    assert vl.identifier_match({"cik": "42"}, cand) == "cik"
    assert vl.identifier_match({"cusip": "00000a102"}, cand) == "cusip"
    assert vl.identifier_match({"isin": "US00000A1014"}, cand) == "cusip"
    assert vl.identifier_match({"cik": "7", "cusip": "ZZZ"}, cand) is None
    assert vl.identifier_match({}, {"cik": None, "cusips": None}) is None


def test_name_similarity():
    assert vl.name_similarity("ZZ Platforms", "ZZ Platforms, Inc.") == 1.0
    assert vl.name_similarity("ZZ Platforms", None) is None
    assert vl.name_similarity("ZZ Platforms", "Unrelated Holdings") < 0.5


def test_overlap_and_sample_windows():
    s = {"first_trade_date": date(2020, 1, 1), "last_trade_date": date(2020, 12, 31)}
    c = {"first_price_date": date(2020, 7, 1), "last_price_date": date(2021, 6, 30)}
    assert vl.overlap(s, c) == (date(2020, 7, 1), date(2020, 12, 31))
    assert vl.overlap(s, dict(c, first_price_date=date(2021, 1, 4))) is None
    assert vl.overlap(dict(s, first_trade_date=None), c) is None
    windows = vl.sample_windows(date(2020, 1, 1), date(2020, 1, 10))
    assert windows[0] == (date(2020, 1, 1), date(2020, 1, 2))
    assert windows[-1] == (date(2020, 1, 9), date(2020, 1, 10))
    assert len(windows) == 5 and all(lo <= hi for lo, hi in windows)
    assert vl.sample_windows(date(2020, 1, 1), date(2020, 1, 3)) == [
        (date(2020, 1, d), date(2020, 1, d)) for d in (1, 2, 3)
    ]


def test_tolerance():
    assert vl.agrees(D("10.005"), D("10.000"))  # within $0.01
    assert not vl.agrees(D("10.02"), D("10.000"))
    assert vl.agrees(D("1000.09"), D("1000.000"))  # within 1 bp
    assert not vl.agrees(D("1000.2"), D("1000.000"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def close_of(d: date, level: float = 50.0) -> Decimal:
    """A price path that differs every day, so a wrong date never agrees."""
    return D(str(round(level + (d.toordinal() % 97) / 10, 3)))


class FakeCloses:
    """Sharadar's closes per ticker: ``prices[ticker](date)``."""

    name = "fake"

    def __init__(self, prices):
        self.prices, self.asked = prices, []

    def closes(self, wanted):
        self.asked.append(wanted)
        return {
            (t, ticker, d): self.prices[ticker](d)
            for (t, ticker), dates in wanted.items()
            if ticker in self.prices
            for d in dates
        }


def _sec(db, symbol, start, end, *, level=50.0, tickers=(), **kw):
    """An FMP-minted security with weekday bars ``start..end`` and ``tickers``
    (``(symbol, valid_from, valid_to)``) in core.symbol_xref."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=kw.pop("name", f"{symbol} Co"),
        exchange_code="NYSE",
        **kw,
    )
    for t, lo, hi in tickers or [(symbol, start, None)]:
        db.execute(
            "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s)",
            (sid, t, lo, hi),
        )
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            c = close_of(d, level)
            rows.append(
                {
                    "security_id": sid,
                    "trade_date": d,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "volume": 100,
                }
            )
        d += timedelta(days=1)
    if rows:
        repo.upsert_daily_prices(db, rows)
    return sid


def _vendor(
    db,
    key,
    ticker,
    first,
    last,
    *,
    table="stocks",
    related=(),
    name=None,
    cik=None,
    cusips=None,
    events=(),
):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, cik, cusips, related_tickers, first_price_date, last_price_date) "
        "VALUES ('sharadar', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            table,
            key,
            ticker,
            name or f"{ticker} Co",
            cik,
            cusips,
            list(related) or None,
            first,
            last,
        ),
    )
    for action, contra in events:
        db.execute(
            "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
            "action, ticker, contra_ticker) VALUES ('sharadar', %s, %s, %s, %s, %s)",
            (key, first, action, ticker, contra),
        )


def _links(db):
    return {
        r["security_id"]: r
        for r in db.fetchall(
            "SELECT * FROM core.security_vendor_xref WHERE valid_to IS NULL"
        )
    }


def _flags(db, state="open"):
    cond = "resolved_at IS NULL" if state == "open" else "resolved_at IS NOT NULL"
    return db.fetchall(
        "SELECT security_id, check_name, severity, detail, resolved_by "
        f"FROM ops.data_quality_flag WHERE check_name LIKE 'vendor_xref_%%' AND {cond} "
        "ORDER BY security_id, check_name"
    )


def _source(prices):
    fake = FakeCloses(prices)
    return fake, (lambda n: fake)


def _by_symbol(result):
    return {o.symbol: o for o in result.outcomes}


Y = date


# ---------------------------------------------------------------------------
# The golden cases
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


@pytestmark_integration
def test_a_rename_links_through_the_old_ticker(db):
    # FMP holds the history under one security that traded as ZZFB, then ZZMETA.
    sid = _sec(
        db,
        "ZZMETA",
        Y(2015, 1, 5),
        Y(2024, 6, 28),
        tickers=[
            ("ZZFB", Y(2015, 1, 5), Y(2022, 6, 8)),
            ("ZZMETA", Y(2022, 6, 9), None),
        ],
    )
    # Sharadar files it under today's ticker; the old one is a ticker change.
    _vendor(
        db,
        "900001",
        "ZZMETA",
        Y(2012, 5, 18),
        Y(2024, 6, 28),
        events=[("tickerchangefrom", "ZZFB")],
    )
    fake, src = _source({"ZZMETA": close_of})
    result = vl.link(db, src)
    (o,) = result.outcomes
    assert (o.outcome, o.vendor_key) == ("linked", "900001")
    link = _links(db)[sid]
    assert (link["match_method"], link["valid_from"], link["is_feed"]) == (
        "ticker_span_price",
        Y(2015, 1, 5),
        False,
    )
    assert link["confidence"] == D("1.000")  # 5/5 and the same name
    ev = link["evidence"]
    assert (ev["agreed"], ev["of"], ev["ticker"]) == (5, 5, "ZZMETA")
    assert ev["matched_on"] == ["ZZFB", "ZZMETA"]
    assert len({s["date"] for s in ev["samples"]}) == 5
    assert link["created_by"] == "fafnir vendor link"


@pytestmark_integration
def test_a_reused_ticker_links_each_issuer_to_its_own_key(db):
    dead = _sec(
        db,
        "ZZBID",
        Y(1995, 1, 2),
        Y(2005, 6, 30),
        level=20.0,
        delisted_date=Y(2005, 7, 1),
        is_actively_trading=False,
        tickers=[("ZZBID", Y(1995, 1, 2), Y(2005, 6, 30))],
    )
    live = _sec(db, "ZZBID", Y(2012, 1, 3), Y(2024, 6, 28), level=80.0)
    # Sharadar: the dead issuer under ZZBID1, listing its old ticker as related.
    _vendor(db, "900010", "ZZBID1", Y(1995, 1, 2), Y(2005, 6, 30), related=["ZZBID"])
    _vendor(db, "900011", "ZZBID", Y(2012, 1, 3), Y(2024, 6, 28))
    _, src = _source(
        {"ZZBID1": lambda d: close_of(d, 20.0), "ZZBID": lambda d: close_of(d, 80.0)}
    )
    vl.link(db, src)
    links = _links(db)
    assert (links[dead]["vendor_key"], links[live]["vendor_key"]) == (
        "900010",
        "900011",
    )


@pytestmark_integration
def test_a_venue_transfer_that_left_two_rows_is_ambiguous_for_both(db):
    # One issuer, two FMP rows (the old venue's, delisted, and the new one's). Both
    # agree with the one permaticker: a duplicate identity for #63 to merge.
    old = _sec(
        db,
        "ZZV",
        Y(2010, 1, 4),
        Y(2018, 3, 30),
        delisted_date=Y(2018, 4, 2),
        is_actively_trading=False,
        tickers=[("ZZV", Y(2010, 1, 4), Y(2018, 3, 30))],
    )
    new = _sec(db, "ZZV", Y(2018, 4, 2), Y(2024, 6, 28))
    _vendor(db, "900020", "ZZV", Y(2010, 1, 4), Y(2024, 6, 28))
    _, src = _source({"ZZV": close_of})
    result = vl.link(db, src)
    assert {(o.outcome, o.reason) for o in result.outcomes} == {
        ("ambiguous", "key_claimed_twice")
    }
    assert _links(db) == {}
    flags = _flags(db)
    assert [(f["security_id"], f["check_name"]) for f in flags] == [
        (old, "vendor_xref_ambiguous"),
        (new, "vendor_xref_ambiguous"),
    ]
    assert flags[0]["detail"]["candidates"] == [
        {"vendor_key": "900020", "also_claimed_by": [new]}
    ]


# ---------------------------------------------------------------------------
# The other outcomes
# ---------------------------------------------------------------------------


@pytestmark_integration
def test_a_share_class_matches_across_spellings(db):
    sid = _sec(db, "ZZK-B", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900100", "ZZK.B", Y(2020, 1, 2), Y(2020, 12, 31))
    _, src = _source({"ZZK.B": close_of})  # asked for by the vendor's spelling
    (o,) = vl.link(db, src).outcomes
    assert (o.outcome, o.vendor_key) == ("linked", "900100")
    assert _links(db)[sid]["evidence"]["matched_on"] == ["ZZK-B"]
    assert vl.ticker_key(" brk.b ") == "BRK-B"


@pytestmark_integration
def test_unmatched_reasons(db):
    _sec(db, "ZZNONE", Y(2020, 1, 2), Y(2020, 12, 31))
    _sec(db, "ZZOFF", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900030", "ZZOFF", Y(2019, 1, 2), Y(2021, 12, 31))
    _sec(db, "ZZSHORT", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900031", "ZZSHORT", Y(2020, 12, 29), Y(2021, 12, 31))  # 3 sessions
    _sec(db, "ZZLATE", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900032", "ZZLATE", Y(2022, 1, 3), Y(2023, 12, 29))  # no overlap
    _sec(db, "ZZMF", Y(2020, 1, 2), Y(2020, 12, 31), asset_type="fund")
    _, src = _source({"ZZOFF": lambda d: close_of(d, 60.0), "ZZSHORT": close_of})
    by = _by_symbol(vl.link(db, src))
    assert {k: (o.outcome, o.reason) for k, o in by.items()} == {
        "ZZNONE": ("unmatched", "no_candidate"),
        "ZZOFF": ("unmatched", "price_disagrees"),
        "ZZSHORT": ("unmatched", "insufficient_overlap"),
        "ZZLATE": ("unmatched", "no_candidate"),
        "ZZMF": ("unmatched", "not_covered_asset_type"),
    }
    assert by["ZZLATE"].candidates == [{"no_overlap_key": "900032"}]
    assert by["ZZOFF"].candidates[0]["agreed"] == 0
    severities = {f["security_id"]: f["severity"] for f in _flags(db)}
    assert severities[by["ZZMF"].security_id] == "info"
    assert severities[by["ZZOFF"].security_id] == "warn"


@pytestmark_integration
def test_several_agree_an_identifier_decides_else_ambiguous(db):
    a = _sec(db, "ZZA", Y(2020, 1, 2), Y(2020, 12, 31), cik="0000000777")
    _vendor(db, "900040", "ZZA", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(
        db, "900041", "ZZA2", Y(2020, 1, 2), Y(2020, 12, 31), related=["ZZA"], cik="777"
    )
    b = _sec(db, "ZZB", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900042", "ZZB", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900043", "ZZB2", Y(2020, 1, 2), Y(2020, 12, 31), related=["ZZB"])
    _, src = _source({t: close_of for t in ("ZZA", "ZZA2", "ZZB", "ZZB2")})
    by = _by_symbol(vl.link(db, src))
    assert (by["ZZA"].outcome, by["ZZA"].vendor_key) == ("linked", "900041")
    assert _links(db)[a]["evidence"]["identifier"] == "cik"
    assert (by["ZZB"].outcome, by["ZZB"].reason) == ("ambiguous", "several_agree")
    assert {c["vendor_key"] for c in by["ZZB"].candidates} == {"900042", "900043"}
    assert b not in _links(db)


@pytestmark_integration
def test_a_link_is_never_moved_and_a_held_key_is_no_candidate(db):
    manual = _sec(db, "ZZM", Y(2020, 1, 2), Y(2020, 12, 31))
    other = _sec(
        db,
        "ZZM2",
        Y(2020, 1, 2),
        Y(2020, 12, 31),
        tickers=[
            ("ZZM2", Y(2020, 1, 2), None),
            ("ZZM", Y(2019, 1, 2), Y(2019, 12, 31)),
        ],
    )
    _vendor(db, "900050", "ZZM", Y(2020, 1, 2), Y(2020, 12, 31))
    repo.insert_vendor_link(
        db,
        security_id=manual,
        vendor="sharadar",
        vendor_key="999999",
        valid_from=Y(2020, 1, 2),
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
    )
    db.execute(
        "INSERT INTO core.security_vendor_xref (security_id, vendor, vendor_key, "
        "valid_from, match_method, created_by) VALUES (%s, 'sharadar', '900050', "
        "'2020-01-02', 'manual', 'tester')",
        (other,),
    )
    _, src = _source({"ZZM": close_of})
    result = vl.link(db, src)
    assert result.outcomes == [] and result.already_linked == 2
    assert {r["vendor_key"] for r in _links(db).values()} == {"999999", "900050"}
    # A third security on the held key's ticker: that key is not a candidate.
    third = _sec(
        db,
        "ZZM3",
        Y(2020, 1, 2),
        Y(2020, 12, 31),
        tickers=[
            ("ZZM3", Y(2020, 1, 2), None),
            ("ZZM", Y(2018, 1, 2), Y(2018, 12, 31)),
        ],
    )
    (o,) = vl.link(db, src).outcomes
    assert (o.security_id, o.reason) == (third, "no_candidate")
    assert o.candidates == [{"taken_key": "900050"}]


# ---------------------------------------------------------------------------
# Runs: dry, repeated, incremental
# ---------------------------------------------------------------------------


@pytestmark_integration
def test_a_dry_run_writes_nothing(db):
    _sec(db, "ZZD", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900060", "ZZD", Y(2020, 1, 2), Y(2020, 12, 31))
    _sec(db, "ZZU", Y(2020, 1, 2), Y(2020, 12, 31))
    _, src = _source({"ZZD": close_of})
    result = vl.link(db, src, dry_run=True)
    assert result.counts() == {("linked", None): 1, ("unmatched", "no_candidate"): 1}
    assert result.run_id is None
    assert _links(db) == {} and _flags(db) == []
    assert db.fetchval("SELECT count(*) FROM ops.ingestion_run") == 0


@pytestmark_integration
def test_a_rerun_converges_and_a_new_outcome_resolves_the_old_flag(db):
    sid = _sec(db, "ZZR", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900070", "ZZR", Y(2020, 1, 2), Y(2020, 12, 31))
    prices = {"ZZR": lambda d: close_of(d, 99.0)}  # disagrees for now
    _, src = _source(prices)
    vl.link(db, src)
    vl.link(db, src)
    assert [(f["check_name"], f["detail"]["reason"]) for f in _flags(db)] == [
        ("vendor_xref_unmatched", "price_disagrees")
    ]  # once, not twice
    # The vendor's prices now agree (a corrected file): the next run links it and
    # closes the flag.
    prices["ZZR"] = close_of
    third = vl.link(db, src)
    assert third.counts() == {("linked", None): 1}
    assert _flags(db) == []
    (closed,) = _flags(db, "resolved")
    assert closed["resolved_by"] == "fafnir vendor link"
    assert _links(db)[sid]["vendor_key"] == "900070"
    assert vl.link(db, src).outcomes == []  # nothing left to evaluate
    runs = db.fetchall(
        "SELECT endpoint, status, rows_inserted, params FROM ops.ingestion_run "
        "ORDER BY ingestion_run_id"
    )
    assert [r["rows_inserted"] for r in runs] == [0, 0, 1, 0]
    assert runs[2]["params"]["outcomes"] == {"linked": 1}


@pytestmark_integration
def test_new_links_a_security_minted_after_the_full_run(db):
    _, src = _source({"ZZN": close_of, "ZZO": close_of})
    note = vl.link(db, src, new=True)
    _vendor(db, "900080", "ZZO", Y(2020, 1, 2), Y(2020, 12, 31))
    _sec(db, "ZZO", Y(2020, 1, 2), Y(2020, 12, 31))
    assert note.note.startswith("no full") and note.outcomes == []
    vl.link(db, src)  # the full run
    db.execute(
        "UPDATE ops.ingestion_run SET started_at = now() - interval '1 hour' "
        "WHERE endpoint = 'link'"
    )
    _vendor(db, "900081", "ZZN", Y(2020, 1, 2), Y(2020, 12, 31))
    sid = _sec(db, "ZZN", Y(2020, 1, 2), Y(2020, 12, 31))
    result = vl.link(db, src, new=True)
    assert [(o.security_id, o.outcome) for o in result.outcomes] == [(sid, "linked")]
    assert (
        db.fetchval(
            "SELECT endpoint FROM ops.ingestion_run WHERE ingestion_run_id = %s",
            (result.run_id,),
        )
        == "link:new"
    )


@pytestmark_integration
def test_no_master_is_an_error(db):
    _sec(db, "ZZX", Y(2020, 1, 2), Y(2020, 12, 31))
    with pytest.raises(vl.LinkError, match="sharadar-master"):
        vl.link(db, _source({})[1])


# ---------------------------------------------------------------------------
# Where Sharadar's closes come from
# ---------------------------------------------------------------------------


@pytestmark_integration
def test_bulk_closes_read_only_the_wanted_tickers(db, tmp_path):
    body = (
        "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"
        "ZZA,2020-01-02,1,1,1,1,1,1,10.5,2020-01-03\n"
        "ZZA,2020-01-03,1,1,1,1,1,1,10.6,2020-01-03\n"
        "ZZA,2020-01-06,1,1,1,1,1,1,,2020-01-06\n"  # no raw close: no sample
        "ZZB,2020-01-02,1,1,1,1,1,1,not-a-number,2020-01-03\n"  # never parsed
    ).encode()
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(body)
    repo.record_sharadar_file(
        db,
        vendor_table="stocks",
        kind="full",
        filters={},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(body),
        rows=3,
        columns=None,
        ingestion_run_id=None,
    )
    src = vl.BulkCloses(db, tmp_path)
    got = src.closes({("stocks", "ZZA"): {Y(2020, 1, 2), Y(2020, 1, 6)}})
    assert got == {("stocks", "ZZA", Y(2020, 1, 2)): D("10.5")}
    with pytest.raises(vl.LinkError, match="no current funds file"):
        src.closes({("funds", "ZZF"): {Y(2020, 1, 2)}})


@pytestmark_integration
def test_select_checks_the_file_and_parses_only_what_it_keeps(db, tmp_path):
    from fafnir.sources import sharadar_files as sf

    def record(body: bytes) -> int:
        sha = hashlib.sha256(body).hexdigest()
        (tmp_path / f"{sha}.csv").write_bytes(body)
        entry, _ = repo.record_sharadar_file(
            db,
            vendor_table="stocks",
            kind="full",
            filters={},
            path=f"{sha}.csv",
            member=None,
            sha256=sha,
            nbytes=len(body),
            rows=None,
            columns=None,
            ingestion_run_id=None,
        )
        return entry["file_id"]

    ok = record(b"ticker,date,closeunadj\nZZA,2020-01-02,1.5\n\nZZA,2020-01-03,x\n")
    assert list(sf.select(db, ok, tmp_path, {"ZZA": {"2020-01-02"}})) == [
        {"ticker": "ZZA", "date": Y(2020, 1, 2), "closeunadj": D("1.5")}
    ]
    with pytest.raises(sf.SharadarFileError, match="line 4, column closeunadj"):
        list(sf.select(db, ok, tmp_path, {"ZZA": {"2020-01-03"}}))
    short = record(b"ticker,date,closeunadj\nZZA,2020-01-02\n")
    with pytest.raises(sf.SharadarFileError, match="line 2: 2 fields"):
        list(sf.select(db, short, tmp_path, {}))
    nodate = record(b"ticker,closeunadj\nZZA,1\n")
    with pytest.raises(sf.SharadarFileError, match="needs ticker and date"):
        list(sf.select(db, nodate, tmp_path, {}))


def test_api_closes_check_what_came_back():
    class Client:
        T_SEP, T_SFP = "SEP", "SFP"

        def __init__(self):
            self.calls = []

        def query(self, table, filters, fields):
            self.calls.append((table, filters, fields))
            return [
                {"ticker": filters["ticker"], "date": "2020-01-02", "closeunadj": 10.5},
                {
                    "ticker": filters["ticker"],
                    "date": "2020-01-06T00:00:00",
                    "closeunadj": 11,
                },
                {"ticker": "OTHER", "date": "2020-01-02", "closeunadj": 99},
                {"ticker": filters["ticker"], "date": "2020-01-03", "closeunadj": None},
            ]

    client = Client()
    got = vl.ApiCloses(client).closes(
        {("funds", "ZZF"): {Y(2020, 1, 2), Y(2020, 1, 3), Y(2020, 1, 6)}}
    )
    assert got == {
        ("funds", "ZZF", Y(2020, 1, 2)): D("10.5"),
        ("funds", "ZZF", Y(2020, 1, 6)): D("11"),
    }
    assert client.calls == [
        (
            "SFP",
            {"ticker": "ZZF", "from": "2020-01-02", "to": "2020-01-06"},
            ["ticker", "date", "closeunadj"],
        )
    ]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytestmark_integration
def test_the_command(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    _sec(db, "ZZC", Y(2020, 1, 2), Y(2020, 12, 31))
    _vendor(db, "900090", "ZZC", Y(2020, 1, 2), Y(2020, 12, 31))
    fake = FakeCloses({"ZZC": close_of})
    monkeypatch.setattr(vl, "BulkCloses", lambda db_, bulk_dir: fake)

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "link", *args]
        )

    dry = run("--dry-run", "--symbols", "ZZC")
    assert dry.exit_code == 0, dry.output
    assert "Dry run: nothing written. 1 securities evaluated (symbols)" in dry.output
    assert "linked" in dry.output and "Sharadar closes from fake" in dry.output
    assert "No security for: ZZNOPE" in run("--symbols", "ZZNOPE").output
    assert "not both" in run("--new", "--symbols", "ZZC").output
    done = run()
    assert done.exit_code == 0 and "Run " in done.output and "linked" in done.output
    assert "no full" not in run("--new").output
