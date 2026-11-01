"""
Daily market cap (migration 0042; ADR 0019 §5 and its 2026-11 amendment): integration
tests (FAFNIR_TEST_DSN). Every row is synthetic.

mart.fn_daily_market_cap is the raw close x the shares of the latest balance sheet in
force that day (ended within 730 days), in that day's share terms with the
warehouse's splits, x SF1's share factor for the period:

- the count steps on its filing, and a split moves it on the ex-date, so the cap is
  continuous across the split while the raw close is not;
- nothing known after a day reaches it (a restatement, a later filing), nor a
  superseded row;
- a balance sheet ended more than 730 days before the day gives no cap;
- the share factor (an ADR's ratio) multiplies it, as of the day;
- a cross-section in one call is each security's own call, and the span's bounds
  change only which bars come back;
- mart.v_security_attributes meets it on the last bar, and the factor panel meets
  it at month-ends, share factor and all (0043).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals as fu
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

D = date
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def _security(db, symbol, *, currency="USD"):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NASDAQ",
        currency=currency,
        source="sharadar",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _statement(
    db,
    statement,
    sid,
    fiscal,
    known_from,
    *,
    version=1,
    known_to=None,
    superseded=None,
    period="annual",
    **items,
):
    row = {
        "security_id": sid,
        "vendor_key": "900420",
        "period_type": period,
        "fiscal_date": fiscal,
        "calendar_date": fiscal,
        "fiscal_year": fiscal.year,
        "fiscal_period": "FY" if period == "annual" else "Q4",
        "version_no": version,
        "known_from": known_from,
        "known_to": known_to,
        # A later version is a restatement: fafnir observed it (ADR 0018).
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": T0 if superseded is None else T0 - timedelta(days=1),
        "superseded_at": superseded,
        "reported_currency": "USD",
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _balance(db, sid, fiscal, known_from, shares, **kw):
    _statement(
        db, "balance_sheet", sid, fiscal, known_from, shares_outstanding=shares, **kw
    )


def _income(db, sid, fiscal, known_from, *, factor=None, **kw):
    """An income version whose extra carries SF1's sharefactor as the loader writes
    it (vendor_metric), or carries none."""
    extra = {} if factor is None else {"vendor_metric": {"sharefactor": factor}}
    _statement(
        db, "income", sid, fiscal, known_from, revenue=1, extra=json.dumps(extra), **kw
    )


def _bars(db, sid, closes):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": Decimal(str(c)),
                "high": Decimal(str(c)),
                "low": Decimal(str(c)),
                "close": Decimal(str(c)),
                "volume": 1000,
            }
            for day, c in closes
        ],
        source="sharadar",
    )


def _split(db, sid, day, ratio):
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=day,
        split_numerator=ratio,
        split_denominator=1,
        source="sharadar",
    )


def _mcap(db, sids, start=None, end=None):
    return db.fetchall(
        "SELECT * FROM mart.fn_daily_market_cap(%s::bigint[], %s, %s)",
        (list(sids), start, end),
    )


def _by_day(rows):
    return {r["trade_date"]: r for r in rows}


# ---------------------------------------------------------------------------
# The expression
# ---------------------------------------------------------------------------


def test_the_count_steps_on_its_filing_and_moves_with_a_split(db):
    sid = _security(db, "ZZA")
    _balance(db, sid, D(2019, 12, 31), D(2020, 2, 14), 100)
    # Filed after the 2-for-1: SF1's count is already in the new terms.
    _balance(db, sid, D(2020, 12, 31), D(2021, 2, 12), 250)
    _split(db, sid, D(2020, 8, 31), 2)
    _bars(
        db,
        sid,
        [
            (D(2020, 2, 13), 10),  # before the first filing: nothing known
            (D(2020, 2, 14), 10),  # the filing's own day
            (D(2020, 8, 28), 12),
            (D(2020, 8, 31), 6),  # the ex-date: the raw close halves
            (D(2021, 2, 11), 7),
            (D(2021, 2, 12), 7),  # the next filing
        ],
    )
    got = _by_day(_mcap(db, [sid]))

    before = got[D(2020, 2, 13)]
    assert (before["market_cap"], before["shares_outstanding"]) == (None, None)
    assert before["share_factor"] is None and before["fiscal_date"] is None
    assert before["close_raw"] == 10

    expected = {
        D(2020, 2, 14): (100, 1000, D(2019, 12, 31)),
        D(2020, 8, 28): (100, 1200, D(2019, 12, 31)),
        # 100 shares known before the split, in the ex-date's terms: continuous.
        D(2020, 8, 31): (200, 1200, D(2019, 12, 31)),
        D(2021, 2, 11): (200, 1400, D(2019, 12, 31)),
        D(2021, 2, 12): (250, 1750, D(2020, 12, 31)),
    }
    for day, (shares, cap, fiscal) in expected.items():
        r = got[day]
        assert (r["shares_outstanding"], r["market_cap"], r["fiscal_date"]) == (
            shares,
            cap,
            fiscal,
        ), day
        assert r["share_factor"] == 1 and r["currency"] == "USD"
        assert r["known_from"] <= day
    assert list(got) == sorted(got), "oldest first"


def test_nothing_known_after_the_day_reaches_it(db):
    sid = _security(db, "ZZB")
    # Version 1 as filed, then a restatement fafnir observed on 2020-06-01, then a
    # vendor correction's superseded row, which is never read.
    _balance(db, sid, D(2019, 12, 31), D(2020, 2, 14), 100, known_to=D(2020, 6, 1))
    _balance(db, sid, D(2019, 12, 31), D(2020, 6, 1), 110, version=2)
    _balance(db, sid, D(2019, 12, 31), D(2020, 2, 14), 5, superseded=T0)
    # A later period filed after the last bar: not known on any day here.
    _balance(db, sid, D(2020, 12, 31), D(2021, 3, 1), 999)
    _bars(db, sid, [(D(2020, 5, 29), 10), (D(2020, 6, 1), 10), (D(2021, 2, 26), 10)])
    got = _by_day(_mcap(db, [sid]))

    assert got[D(2020, 5, 29)]["shares_outstanding"] == 100
    assert got[D(2020, 6, 1)]["shares_outstanding"] == 110
    assert got[D(2020, 6, 1)]["known_from"] == D(2020, 6, 1)
    assert got[D(2021, 2, 26)]["shares_outstanding"] == 110
    for day, r in got.items():
        assert r["known_from"] <= day, "look-ahead"


def test_a_balance_sheet_ended_730_days_before_is_the_last_one_read(db):
    sid = _security(db, "ZZC")
    fiscal = D(2019, 12, 31)
    _balance(db, sid, fiscal, D(2020, 2, 14), 100)
    last_day = fiscal + timedelta(days=730)
    _bars(db, sid, [(last_day, 10), (last_day + timedelta(days=1), 10)])
    got = _by_day(_mcap(db, [sid]))
    assert got[last_day]["market_cap"] == 1000
    stale = got[last_day + timedelta(days=1)]
    assert (stale["market_cap"], stale["shares_outstanding"]) == (None, None)


def test_a_security_with_no_count_has_its_bars_and_no_cap(db):
    etf = _security(db, "ZZETF")
    _split(db, etf, D(2020, 6, 1), 3)
    _bars(db, etf, [(D(2020, 5, 29), 30), (D(2020, 6, 1), 10)])
    rows = _mcap(db, [etf])
    assert [r["close_raw"] for r in rows] == [30, 10]
    assert all(r["market_cap"] is None and r["share_factor"] is None for r in rows)


# ---------------------------------------------------------------------------
# The share factor
# ---------------------------------------------------------------------------


def test_the_share_factor_turns_the_count_into_the_traded_lines(db):
    adr = _security(db, "ZZTSM")
    fiscal = D(2020, 12, 31)
    _balance(db, adr, fiscal, D(2021, 2, 12), 1000)
    # Five ordinary shares to one ADR, then (an observed restatement) four.
    _income(db, adr, fiscal, D(2021, 2, 12), factor="0.2", known_to=D(2021, 6, 1))
    _income(db, adr, fiscal, D(2021, 6, 1), factor=0.25, version=2)
    _bars(db, adr, [(D(2021, 3, 1), 50), (D(2021, 6, 1), 50)])
    got = _by_day(_mcap(db, [adr]))
    assert (got[D(2021, 3, 1)]["share_factor"], got[D(2021, 3, 1)]["market_cap"]) == (
        Decimal("0.2"),
        10000,
    )
    # The factor as of the day, the count unchanged.
    june = got[D(2021, 6, 1)]
    assert (june["shares_outstanding"], june["share_factor"], june["market_cap"]) == (
        1000,
        Decimal("0.25"),
        12500,
    )


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, None),
        ({"vendor_metric": {"pe": 12}}, None),
        ({"vendor_metric": {"sharefactor": "N/A"}}, None),
        ({"vendor_metric": {"sharefactor": "0"}}, None),
        ({"vendor_metric": {"sharefactor": "-1"}}, None),
        ({"vendor_metric": {"sharefactor": " 1.52 "}}, Decimal("1.52")),
        ({"vendor_metric": {"sharefactor": 1.5e-1}}, Decimal("0.15")),
    ],
)
def test_only_a_positive_number_is_a_factor(db, extra, expected):
    sid = _security(db, "ZZF")
    _statement(
        db, "income", sid, D(2020, 12, 31), D(2021, 2, 12), extra=json.dumps(extra)
    )
    got = db.fetchval(
        "SELECT share_factor FROM mart.v_share_factor_versions WHERE security_id = %s",
        (sid,),
    )
    assert got == expected


def test_no_factor_is_a_factor_of_one(db):
    sid = _security(db, "ZZG")
    fiscal = D(2020, 12, 31)
    _balance(db, sid, fiscal, D(2021, 2, 12), 1000)
    _income(db, sid, fiscal, D(2021, 2, 12), factor="not a number")
    _bars(db, sid, [(D(2021, 3, 1), 50)])
    (r,) = _mcap(db, [sid])
    assert (r["share_factor"], r["market_cap"]) == (1, 50000)


# ---------------------------------------------------------------------------
# Cross-sections and spans
# ---------------------------------------------------------------------------


def _world(db):
    a = _security(db, "ZZW1")
    _balance(db, a, D(2019, 12, 31), D(2020, 2, 14), 100)
    _balance(db, a, D(2020, 12, 31), D(2021, 2, 12), 250)
    _split(db, a, D(2020, 8, 31), 2)
    b = _security(db, "ZZW2")
    _balance(db, b, D(2019, 9, 30), D(2019, 11, 15), 40, period="quarter")
    _income(db, b, D(2019, 9, 30), D(2019, 11, 15), factor="1.5", period="quarter")
    c = _security(db, "ZZW3")  # no count
    days = [D(2019, 11, 14) + timedelta(days=7 * i) for i in range(80)]
    for i, sid in enumerate((a, b, c)):
        _bars(db, sid, [(d, 10 + i + (k % 5)) for k, d in enumerate(days)])
    return (a, b, c), days


def test_a_cross_section_is_each_securitys_own_call(db):
    sids, _ = _world(db)
    together = _mcap(db, sids)
    alone = [r for sid in sids for r in _mcap(db, [sid])]
    assert together == alone
    assert {r["security_id"] for r in together} == set(sids)
    assert any(r["market_cap"] is not None for r in together)


def test_the_span_bounds_only_which_bars_come_back(db):
    sids, days = _world(db)
    full = _mcap(db, sids)
    start, end = days[20], days[60]
    inside = [r for r in full if start <= r["trade_date"] <= end]
    assert _mcap(db, sids, start, end) == inside
    assert _mcap(db, sids, start, None) == [r for r in full if r["trade_date"] >= start]
    assert _mcap(db, sids, None, end) == [r for r in full if r["trade_date"] <= end]
    # A span that starts after every filing still finds the count in force.
    late = _mcap(db, [sids[0]], D(2021, 3, 1), None)
    assert late and all(r["shares_outstanding"] == 250 for r in late)


def test_an_empty_list_or_an_unknown_security_returns_nothing(db):
    assert _mcap(db, []) == []
    assert _mcap(db, [987654321]) == []


# ---------------------------------------------------------------------------
# One expression: the screener and the panel meet it
# ---------------------------------------------------------------------------


def test_the_screener_meets_the_function_on_the_last_bar(db):
    sids, days = _world(db)
    adr = _security(db, "ZZTSM2")
    _balance(db, adr, D(2020, 12, 31), D(2021, 2, 12), 1000)
    _income(db, adr, D(2020, 12, 31), D(2021, 2, 12), factor="0.2")
    _bars(db, adr, [(D(2021, 3, 1), 50)])
    for sid in (*sids, adr):
        rows = _mcap(db, [sid])
        last = rows[-1]
        a = db.fetchone(
            "SELECT * FROM mart.v_security_attributes WHERE security_id = %s", (sid,)
        )
        assert a["last_trade_date"] == last["trade_date"]
        if last["market_cap"] is None:
            assert a["market_cap_source"] != "derived"
        else:
            assert a["market_cap_source"] == "derived"
            assert a["market_cap_usd"] == last["market_cap"], sid
            assert a["market_cap_fiscal_date"] == last["fiscal_date"]
    # The ADR's screener cap is no longer five times too high.
    assert (
        db.fetchval(
            "SELECT market_cap_usd FROM mart.v_security_attributes"
            " WHERE security_id = %s",
            (adr,),
        )
        == 10000
    )


SF1 = (
    "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
    "revenue,netinc,netinccmn,shareswa,shareswadil,sharesbas,sharefactor,assets,"
    "liabilities,equity"
).split(",")
#: Apple's split history, the trap ADR 0019 names (figures invented): the product
#: is 112 from 2000 on.
SPLITS = (("2000-06-21", 2), ("2005-02-28", 2), ("2014-06-09", 7), ("2020-08-31", 4))


def _sf1_row(ticker, filed, sharesbas, sharefactor):
    head = {
        "ticker": ticker,
        "dimension": "ARY",
        "calendardate": "1999-09-25",
        "date": filed,
        "reportperiod": "1999-09-25",
        "fiscalperiod": "1999-FY",
        "lastupdated": "2026-01-02",
    }
    values = {
        "revenue": 6_000_000_000,
        "netinc": 600_000_000,
        "netinccmn": 600_000_000,
        "shareswa": sharesbas,
        "shareswadil": sharesbas,
        "sharesbas": sharesbas,
        "sharefactor": sharefactor,
        "assets": 5_000_000_000,
        "liabilities": 2_000_000_000,
        "equity": 3_000_000_000,
    }
    return ",".join(
        {**head, **{k: str(x) for k, x in values.items()}}.get(c, "") for c in SF1
    )


def _ledger(db, tmp_path, table, body):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind="full",
        filters={"years": "full"},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )


def _linked(db, symbol, key):
    sid = _security(db, symbol)
    for table in ("stocks", "fundamentals"):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
            " currency) VALUES ('sharadar', %s, %s, %s, 'USD')",
            (table, key, symbol),
        )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=D(1990, 1, 2),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    return sid


def test_through_the_loader_the_split_era_and_the_factor(db, tmp_path):
    """SF1's rows, as the loader stores them, give the panel's market cap at its
    month-ends; and the share factor the loader keeps in extra is read."""
    apl = _linked(db, "ZZAPL", "900421")
    adr = _linked(db, "ZZADR", "900422")
    today_terms = 112  # SF1's counts carry every split since
    _ledger(
        db,
        tmp_path,
        "fundamentals",
        ",".join(SF1)
        + "\n"
        + _sf1_row("ZZAPL", "1999-12-22", 161_000_000 * today_terms, 1)
        + "\n"
        + _sf1_row("ZZADR", "1999-12-22", 50_000_000, 0.2)
        + "\n",
    )
    _ledger(
        db,
        tmp_path,
        "actions",
        "date,action,ticker,name,value,contraticker,contraname\n"
        + "".join(f"{d},split,ZZAPL,ZZAPL Inc,{r}.0,,\n" for d, r in SPLITS),
    )
    fu.load(db, tmp_path, backfill=True, as_of=D(2026, 1, 5))
    for d, r in SPLITS:  # the warehouse's own list, the one raw prices follow
        _split(db, apl, D.fromisoformat(d), r)
    for sid in (apl, adr):
        _bars(db, sid, [(D(1999, 12, 31), 100), (D(2000, 7, 31), 50)])

    got = _by_day(_mcap(db, [apl]))
    dec, jul = got[D(1999, 12, 31)], got[D(2000, 7, 31)]
    # As filed in 1999, then in July 2000's terms: twice the shares, half the close.
    assert (dec["shares_outstanding"], jul["shares_outstanding"]) == (
        161_000_000,
        322_000_000,
    )
    assert dec["market_cap"] == 16_100_000_000 == jul["market_cap"]

    adr_rows = _by_day(_mcap(db, [adr]))
    assert adr_rows[D(1999, 12, 31)]["share_factor"] == Decimal("0.2")
    assert adr_rows[D(1999, 12, 31)]["market_cap"] == 100 * 50_000_000 * Decimal("0.2")

    maintenance.refresh_marts(db, views=["mart.fundamental_panel"])
    panel = {
        (r["security_id"], r["as_of_date"]): r
        for r in db.fetchall(
            "SELECT security_id, as_of_date, price_date, market_cap, share_factor"
            "  FROM mart.fundamental_panel WHERE security_id = ANY(%s)",
            ([apl, adr],),
        )
    }
    for day in (D(1999, 12, 31), D(2000, 7, 31)):
        # The panel's month-end cap is the daily one, share factor and all (0043).
        assert panel[(apl, day)]["market_cap"] == got[day]["market_cap"]
        assert panel[(adr, day)]["market_cap"] == adr_rows[day]["market_cap"]
        assert panel[(adr, day)]["share_factor"] == Decimal("0.2")


# ---------------------------------------------------------------------------
# What duk and the MCP tool read (duk.datasource.db, fafnir_mcp.tools)
# ---------------------------------------------------------------------------


def test_duk_reads_the_function_for_mc_and_ph(db, migrated_dsn):
    from duk.datasource import db as ds_db

    sids, days = _world(db)
    a, b, c = sids
    direct = [
        r for sid in (b, a) for r in _mcap(db, [sid]) if r["market_cap"] is not None
    ]
    rows = ds_db.market_cap_rows(dsn=migrated_dsn, security_ids=[b, a, c])
    assert [(r["security_id"], r["date"], r["market_cap"]) for r in rows] == [
        (r["security_id"], r["trade_date"], r["market_cap"]) for r in direct
    ], "every bar with a cap, in the order the securities were named"
    assert {r["security_id"] for r in rows} == {a, b}
    assert ds_db.resolve_symbols(dsn=migrated_dsn, symbols=["ZZW1", "NOPE"]) == {
        "ZZW1": a,
        "NOPE": None,
    }

    bars = ds_db.exact_bars(dsn=migrated_dsn, security_id=a, market_cap=True)
    caps = {r["trade_date"]: r["market_cap"] for r in _mcap(db, [a])}
    assert [r["market_cap"] for r in bars] == [caps[r["date"]] for r in bars]
    frame = ds_db.price_history(
        dsn=migrated_dsn,
        security_id=a,
        start_date=None,
        end_date=None,
        fields=["close"],
        adjusted=True,
        market_cap=True,
    )
    assert list(frame.columns) == ["close", "market_cap"]
    month = ds_db.price_history(
        dsn=migrated_dsn,
        security_id=a,
        start_date=None,
        end_date=None,
        frequency="month",
        market_cap=True,
    )
    last = ds_db.market_cap_history(
        dsn=migrated_dsn, security_ids=[a], frequency="month"
    )
    # `ph -f month --mcap` and `mc -f month` agree on both the labels and the caps.
    joined = month.join(last.set_index("date")["market_cap"], rsuffix="_mc")
    assert (joined["market_cap"].dropna() == joined["market_cap_mc"].dropna()).all()


def test_the_secondary_class_names_the_issuers(db, migrated_dsn):
    from duk.datasource import db as ds_db

    primary = _security(db, "ZZGA")
    secondary = _security(db, "ZZGC")
    db.execute(
        "UPDATE core.security SET cik = '0001652044' WHERE security_id IN (%s, %s)",
        (primary, secondary),
    )
    _balance(db, primary, D(2020, 12, 31), D(2021, 2, 12), 1000)
    _income(db, primary, D(2020, 12, 31), D(2021, 2, 12), factor="1")
    issuer = ds_db.issuer_class(dsn=migrated_dsn, security_id=secondary)
    assert issuer["symbol"] == "ZZGA"
    assert ds_db.issuer_class(dsn=migrated_dsn, security_id=primary) is None


def test_the_mcp_tool_is_ducks_rows_with_symbols(db, migrated_dsn):
    from fafnir_mcp import tools as T

    sids, days = _world(db)
    a, b, c = sids
    out = T.market_cap_history(dsn=migrated_dsn, symbols=["ZZW1", "ZZW2", "ZZW3"])
    assert out["missing"] == ["ZZW3"]
    assert [s["security_id"] for s in out["securities"]] == [a, b, c]
    direct = [r for r in _mcap(db, [a, b]) if r["market_cap"] is not None]
    assert [(r["symbol"], r["date"], r["market_cap"]) for r in out["rows"]] == [
        (
            "ZZW1" if r["security_id"] == a else "ZZW2",
            r["trade_date"].isoformat(),
            str(r["market_cap"]),
        )
        for r in direct
    ]
    # A limit counts back from the last bar the securities hold, not from today.
    tail = T.market_cap_history(
        dsn=migrated_dsn, symbols=["ZZW1", "sid:%d" % a], limit=2
    )
    assert [r["date"] for r in tail["rows"]] == [d.isoformat() for d in days[-2:]]
    assert len(tail["securities"]) == 1, "one security named twice is read once"
