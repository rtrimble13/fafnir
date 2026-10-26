"""
The factor panel (#100, migration 0037): integration tests (FAFNIR_TEST_DSN). Every row
is synthetic.

The plan's §5.3 and ADR 0019: every input known at the as-of date (the standing
invariant), market cap from the raw close and a share count in the same terms (the
split-era trap), rows only while a security traded, the denominator rule, currency
mismatch, the extra lag, and one definition shared by the snapshot and the panel.
"""

from __future__ import annotations

import hashlib
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
        source="sharadar",
    )
    db.execute(
        "UPDATE core.security SET currency = %s WHERE security_id = %s", (currency, sid)
    )
    return sid


def _put(
    db,
    statement,
    sid,
    fiscal_date,
    *,
    label,
    known_from,
    currency="USD",
    version=1,
    known_to=None,
    **items,
):
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900040",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version,
        "known_from": known_from,
        "known_to": known_to,
        # A later version is a restatement: fafnir observed it (ADR 0018).
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": T0,
        "reported_currency": currency,
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _year(db, sid, year, filed, *, currency="USD", **over):
    """A fiscal year ending 31 December, all three statements."""
    end = D(year, 12, 31)
    inc = {
        "revenue": 1000,
        "gross_profit": 400,
        "operating_income": 200,
        "ebit": 200,
        "income_tax_expense": 40,
        "interest_expense": 20,
        "net_income": 120,
        "net_income_common": 120,
        "weighted_average_shares": 100,
    }
    bal = {
        "total_assets": 2000,
        "total_current_assets": 800,
        "total_current_liabilities": 400,
        "inventory": 100,
        "cash_and_equivalents": 300,
        "total_debt": 500,
        "debt_noncurrent": 400,
        "total_liabilities": 1000,
        "total_equity_parent": 1000,
        "retained_earnings": 600,
        "shares_outstanding": 100,
    }
    cfs = {
        "net_cash_operating": 150,
        "capital_expenditure": -50,
        "depreciation_amortization": 30,
        "dividends_paid": -20,
        "net_equity_issuance": -10,
    }
    for statement, items in (
        ("income", inc),
        ("balance_sheet", bal),
        ("cash_flow", cfs),
    ):
        mine = {k: over.get(k, v) for k, v in items.items()}
        _put(
            db,
            statement,
            sid,
            end,
            label=f"{year}-FY",
            known_from=filed,
            currency=currency,
            **mine,
        )


def _price(db, sid, day, close):
    c = Decimal(str(close))
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000,
            }
        ],
        source="sharadar",
    )


def _snapshot(db, as_of, lag=0):
    return {
        r["security_id"]: r
        for r in db.fetchall(
            "SELECT * FROM mart.fn_fundamental_snapshot(%s, %s)", (as_of, lag)
        )
    }


def _refresh(db):
    maintenance.refresh_marts(db, views=["mart.fundamental_panel"])


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


def test_the_panel_is_the_snapshot_and_is_refreshed_with_the_marts(db):
    _refresh(db)
    with db.conn.cursor() as cur:
        cur.execute("SELECT * FROM mart.fundamental_panel LIMIT 0")
        panel = [d.name for d in cur.description]
        cur.execute("SELECT * FROM mart.fn_fundamental_snapshot(current_date) LIMIT 0")
        snap = [d.name for d in cur.description]
    assert panel == snap
    assert "mart.fundamental_panel" in maintenance.MATVIEWS
    assert db.fetchval(
        "SELECT indisunique FROM pg_index WHERE indexrelid = "
        "'mart.ux_fundamental_panel'::regclass"
    )


# ---------------------------------------------------------------------------
# What was known, and when
# ---------------------------------------------------------------------------


def test_a_cross_section_reads_only_what_was_known(db):
    sid = _security(db, "ZZK")
    _year(db, sid, 2009, D(2010, 2, 20))
    _year(
        db,
        sid,
        2010,
        D(2011, 2, 18),
        revenue=1100,
        net_income=150,
        net_income_common=150,
    )
    _price(db, sid, D(2011, 2, 17), 20)
    _price(db, sid, D(2011, 2, 28), 22)

    before = _snapshot(db, D(2011, 2, 17))[sid]
    assert (before["fiscal_year"], before["known_from"]) == (2009, D(2010, 2, 20))
    after = _snapshot(db, D(2011, 2, 28))[sid]
    assert (after["fiscal_year"], after["basis"]) == (2010, "annual")
    assert after["days_since_filing"] == 10
    assert after["revenue_growth_yoy"] == pytest.approx(0.1)
    # The extra lag reads the statements as of 30 days before: FY2010 is not yet known.
    lagged = _snapshot(db, D(2011, 2, 28), lag=30)[sid]
    assert lagged["fiscal_year"] == 2009 and lagged["close_raw"] == 22


def test_the_standing_invariant_holds_on_the_refreshed_panel(db):
    sid = _security(db, "ZZI")
    for year in range(2005, 2011):
        _year(db, sid, year, D(year + 1, 3, 1))
    for year in range(2005, 2012):
        for month in (1, 2, 3, 4, 6, 9, 12):
            _price(db, sid, D(year, month, 28), 10)
    _refresh(db)
    rows = db.fetchall(
        "SELECT as_of_date, known_from FROM mart.fundamental_panel WHERE security_id = %s",
        (sid,),
    )
    assert rows
    assert (
        db.fetchval(
            "SELECT count(*) FROM mart.fundamental_panel WHERE known_from > as_of_date"
        )
        == 0
    )
    # A refresh after the first runs concurrently: the unique index allows it.
    assert maintenance.refresh_marts(db, views=["mart.fundamental_panel"]) == [
        ("mart.fundamental_panel", "concurrently")
    ]


def test_a_security_is_in_the_panel_only_while_it_traded(db):
    sid = _security(db, "ZZDEAD")
    for year in range(2004, 2008):
        _year(db, sid, year, D(year + 1, 3, 1))
    for month in range(1, 13):
        _price(db, sid, D(2006, month, 27), 5)
    _refresh(db)
    months = [
        r["as_of_date"]
        for r in db.fetchall(
            "SELECT as_of_date FROM mart.fundamental_panel WHERE security_id = %s"
            " ORDER BY 1",
            (sid,),
        )
    ]
    # Its last bar is 2006-12-27: no month after that, though FY2007 was filed.
    assert months and months[0].year == 2006 and months[-1] == D(2006, 12, 29)


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_the_factors_from_one_year_and_its_predecessor(db):
    sid = _security(db, "ZZF")
    _year(
        db,
        sid,
        2009,
        D(2010, 2, 20),
        total_assets=1800,
        revenue=900,
        net_income=100,
        net_income_common=100,
        gross_profit=350,
        shares_outstanding=95,
    )
    _year(db, sid, 2010, D(2011, 2, 18))
    _price(db, sid, D(2011, 3, 31), 20)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " dividend_amount, source) VALUES (%s, 'dividend', '2010-11-15', 0.5, 'sharadar')",
        (sid,),
    )
    r = _snapshot(db, D(2011, 3, 31))[sid]
    assert (r["market_cap"], r["enterprise_value"]) == (2000, 2200)
    assert r["earnings_yield"] == pytest.approx(120 / 2000)
    assert r["book_to_price"] == pytest.approx(0.5)
    assert r["fcf_yield"] == pytest.approx(100 / 2000)
    assert r["ev_to_ebitda"] == pytest.approx(2200 / 230)
    assert r["roe"] == pytest.approx(120 / 1000)
    assert r["roa"] == pytest.approx(120 / 1900)
    assert r["gross_profitability"] == pytest.approx(400 / 2000)
    assert r["accruals"] == pytest.approx((120 - 150) / 1900)
    assert r["interest_coverage"] == pytest.approx(10)
    assert r["quick_ratio"] == pytest.approx(700 / 400)
    assert r["revenue_growth_yoy"] == pytest.approx(1000 / 900 - 1)
    assert r["eps_growth_yoy"] == pytest.approx(1.2 - 1)
    assert r["asset_growth_1y"] == pytest.approx(2000 / 1800 - 1)
    assert r["capex_to_sales"] == pytest.approx(0.05)
    assert r["net_share_issuance_1y"] == pytest.approx(0.0512933, rel=1e-5)
    assert r["dividend_yield_ttm"] == pytest.approx(0.5 / 20)
    assert r["buyback_yield_ttm"] == pytest.approx(10 / 2000)
    assert r["shareholder_yield"] == pytest.approx(30 / 2000)
    assert 0 <= r["piotroski_f"] <= 9 and r["altman_z"] > 0
    # Three years back is not on file: no CAGR rather than a guess.
    assert r["revenue_cagr_3y"] is None


def test_the_denominator_rule(db):
    sid = _security(db, "ZZNEG")
    _year(
        db,
        sid,
        2010,
        D(2011, 2, 18),
        total_equity_parent=-50,
        net_income=-30,
        net_income_common=-30,
        interest_expense=0,
    )
    _price(db, sid, D(2011, 3, 31), 20)
    r = _snapshot(db, D(2011, 3, 31))[sid]
    # Negative book equity: no book-to-price, ROE or debt-to-equity.
    assert r["book_to_price"] is None and r["debt_to_equity"] is None
    # A loss is information: a negative yield, kept.
    assert r["earnings_yield"] == pytest.approx(-30 / 2000)
    # No interest expense: no coverage ratio. No prior year: no growth.
    assert r["interest_coverage"] is None and r["revenue_growth_yoy"] is None


def test_statements_in_another_currency_null_the_price_ratios(db):
    sid = _security(db, "ZZEUR", currency="USD")
    _year(db, sid, 2010, D(2011, 2, 18), currency="EUR")
    _price(db, sid, D(2011, 3, 31), 20)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " dividend_amount, source) VALUES (%s, 'dividend', '2010-11-15', 0.5, 'sharadar')",
        (sid,),
    )
    r = _snapshot(db, D(2011, 3, 31))[sid]
    assert r["currency_mismatch"] and r["market_cap"] == 2000
    assert r["earnings_yield"] is None and r["enterprise_value"] is None
    assert r["altman_z"] is None
    # Not a price ratio of a statement figure: these stand.
    assert r["gross_margin"] == pytest.approx(0.4) and r[
        "dividend_yield_ttm"
    ] == pytest.approx(0.025)


def test_ttm_is_the_basis_where_quarters_are_newer(db):
    sid = _security(db, "ZZQ")
    _year(db, sid, 2009, D(2010, 2, 20))
    quarters = [
        (D(2010, 3, 31), "2010-Q1", D(2010, 5, 1)),
        (D(2010, 6, 30), "2010-Q2", D(2010, 8, 1)),
        (D(2010, 9, 30), "2010-Q3", D(2010, 11, 1)),
        (D(2010, 12, 31), "2010-Q4", D(2011, 2, 18)),
    ]
    for end, label, filed in quarters:
        _put(
            db,
            "income",
            sid,
            end,
            label=label,
            known_from=filed,
            revenue=300,
            net_income=40,
            net_income_common=40,
            weighted_average_shares=100,
        )
        _put(
            db,
            "balance_sheet",
            sid,
            end,
            label=label,
            known_from=filed,
            total_assets=2100,
            shares_outstanding=100,
            total_equity_parent=1000,
        )
    _price(db, sid, D(2011, 3, 31), 20)
    r = _snapshot(db, D(2011, 3, 31))[sid]
    assert (r["basis"], r["ttm_quarters_used"], r["fiscal_period"]) == ("ttm", 4, "Q4")
    assert r["sales_to_price"] == pytest.approx(1200 / 2000)
    # Three quarters into 2010 the basis is still the 2009 year.
    early = _snapshot(db, D(2010, 11, 30))
    assert sid not in early  # no price that week


# ---------------------------------------------------------------------------
# One computation for every date
# ---------------------------------------------------------------------------


def _month_ends(first, last):
    """The last weekday of each month from ``first`` to ``last`` (years)."""
    out = []
    for year in range(first, last + 1):
        for month in range(1, 13):
            day = D(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
            while day.weekday() > 4:
                day -= timedelta(days=1)
            out.append(day)
    return out


def _quarter(db, sid, i, end, label, filed, *, version=1, known_to=None, bump=0):
    """Quarter ``i`` of a history in which every figure moves, so a sum that took
    the wrong quarter, or the wrong version of one, shows."""
    items = {
        "income": {
            "revenue": 100 + 7 * i + bump,
            "gross_profit": 40 + 3 * i,
            "net_income": 10 + i,
            "net_income_common": 10 + i,
            "weighted_average_shares": 100,
        },
        "balance_sheet": {
            "total_assets": 2000 + 10 * i,
            "total_equity_parent": 1000,
            "shares_outstanding": 100,
        },
        "cash_flow": {"net_cash_operating": 15 + i, "capital_expenditure": -5},
    }
    for statement, values in items.items():
        if version > 1 and statement != "income":
            continue
        _put(
            db,
            statement,
            sid,
            end,
            label=label,
            known_from=filed,
            version=version,
            known_to=known_to,
            **values,
        )


def _history(db):
    """2004-2008 by quarter and by year, with the three things a date-by-date TTM
    has to get right: a restatement (2006-Q3, from 2007-06-15), a quarter filed
    after the next (2007-Q2), and a quarter never filed (2008-Q1)."""
    sid = _security(db, "ZZT")
    ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    i = 0
    for year in range(2004, 2009):
        for q, (m, d) in enumerate(ends, start=1):
            i += 1
            end, label = D(year, m, d), f"{year}-Q{q}"
            filed = end + timedelta(days=40)
            if label == "2008-Q1":
                continue
            if label == "2007-Q2":
                filed = D(2007, 12, 15)
            if label == "2006-Q3":
                _quarter(db, sid, i, end, label, filed, known_to=D(2007, 6, 15))
                _quarter(db, sid, i, end, label, D(2007, 6, 15), version=2, bump=50)
            else:
                _quarter(db, sid, i, end, label, filed)
        _year(db, sid, year, D(year + 1, 3, 1), revenue=1000 + year)
    for day in _month_ends(2005, 2009):
        _price(db, sid, day, 20)
    return sid


TWO_YEARS = timedelta(days=730)


def _expected(db, sid, as_of):
    """The basis and its year-ago figure, from mart.fn_fundamentals_ttm and the
    versions in force at as_of: the definition the panel must meet."""
    cands = []
    for r in db.fetchall(
        "SELECT * FROM mart.fn_fundamentals_ttm(%s) WHERE security_id = %s"
        " AND ttm_quarters_used = 4",
        (as_of, sid),
    ):
        seq = r["fiscal_year"] * 4 + int(r["fiscal_period"][1])
        cands.append((r["fiscal_date"], 1, "ttm", seq, r))
    in_force = (
        "{t}.superseded_at IS NULL AND {t}.known_from <= %(d)s"
        " AND ({t}.known_to IS NULL OR %(d)s < {t}.known_to)"
    )
    for r in db.fetchall(
        "SELECT i.fiscal_date, i.fiscal_year, i.revenue, i.gross_profit,"
        "       greatest(i.known_from, b.known_from, c.known_from) AS known_from"
        "  FROM mart.v_income_statement_versions i"
        "  LEFT JOIN mart.v_balance_sheet_versions b ON b.security_id = i.security_id"
        "   AND b.period_type = 'annual' AND b.fiscal_date = i.fiscal_date AND "
        + in_force.format(t="b")
        + "  LEFT JOIN mart.v_cash_flow_statement_versions c"
        "    ON c.security_id = i.security_id"
        "   AND c.period_type = 'annual' AND c.fiscal_date = i.fiscal_date AND "
        + in_force.format(t="c")
        + " WHERE i.security_id = %(s)s AND i.period_type = 'annual' AND "
        + in_force.format(t="i"),
        {"d": as_of, "s": sid},
    ):
        cands.append((r["fiscal_date"], 0, "annual", r["fiscal_year"] * 4 + 4, r))
    fresh = [c for c in cands if c[0] >= as_of - TWO_YEARS]
    if not fresh:
        return None
    basis = max(fresh, key=lambda c: (c[0], c[1]))
    prior = [
        c
        for c in cands
        if c[2] == basis[2]
        and c[3] == basis[3] - 4
        and basis[0] - TWO_YEARS <= c[0] < basis[0]
    ]
    return basis, max(prior, key=lambda c: c[0]) if prior else None


def test_the_panel_meets_the_ttm_function_at_every_date(db):
    sid = _history(db)
    days = _month_ends(2005, 2009)
    panel = {
        r["as_of_date"]: r
        for r in db.fetchall(
            "SELECT * FROM mart.fn_fundamental_panel(%s) WHERE security_id = %s",
            (days, sid),
        )
    }
    seen = set()
    for day in days:
        want = _expected(db, sid, day)
        got = panel.get(day)
        # One date or sixty: the same rows.
        assert _snapshot(db, day).get(sid) == got, day
        if want is None:
            assert got is None, day
            continue
        (fd, _, basis, _, row), prior = want
        assert (got["basis"], got["fiscal_date"]) == (basis, fd), day
        assert got["known_from"] == row["known_from"], day
        revenue = got["sales_to_price"] * float(got["market_cap"])
        assert revenue == pytest.approx(float(row["revenue"])), day
        assert got["gross_margin"] == pytest.approx(
            float(row["gross_profit"]) / float(row["revenue"])
        ), day
        if prior is None:
            assert got["revenue_growth_yoy"] is None, day
        else:
            assert got["revenue_growth_yoy"] == pytest.approx(
                float(row["revenue"]) / float(prior[4]["revenue"]) - 1
            ), day
        seen.add((basis, float(row["revenue"])))
    # Not vacuous: both bases, and the restated quarter inside a TTM.
    assert {b for b, _ in seen} == {"ttm", "annual"}
    assert ("ttm", float(sum(100 + 7 * i for i in (9, 10, 11, 12)))) in seen  # 2006-Q4
    # 2007-Q1's TTM before and after 2006-Q3 was restated (+50 from 2007-06-15).
    q1 = sum(100 + 7 * i for i in (10, 11, 12, 13))
    assert ("ttm", float(q1)) in seen and ("ttm", float(q1 + 50)) in seen


def test_a_basis_two_years_stale_gives_no_row(db):
    sid = _security(db, "ZZOLD")
    _year(db, sid, 2005, D(2006, 3, 1))
    for day in _month_ends(2006, 2008):
        _price(db, sid, day, 10)
    days = [
        r["as_of_date"]
        for r in db.fetchall(
            "SELECT as_of_date FROM mart.fn_fundamental_panel(%s) WHERE security_id = %s"
            " ORDER BY 1",
            (_month_ends(2006, 2008), sid),
        )
    ]
    # Known from 2006-03-01; still traded in 2008, but FY2005 is over two years old.
    assert days[0] == D(2006, 3, 31) and days[-1] == D(2007, 12, 31)


def test_a_negative_lag_reads_nothing(db):
    sid = _security(db, "ZZLAG")
    _year(db, sid, 2010, D(2011, 2, 18))
    _price(db, sid, D(2011, 3, 31), 20)
    assert sid in _snapshot(db, D(2011, 3, 31))
    # A negative lag would read statements filed after the as-of date.
    assert _snapshot(db, D(2011, 3, 31), lag=-30) == {}


# ---------------------------------------------------------------------------
# The split-era trap, end to end through the loader
# ---------------------------------------------------------------------------

SF1 = (
    "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
    "revenue,netinc,netinccmn,shareswa,shareswadil,sharesbas,assets,liabilities,equity"
).split(",")
#: Shaped like Apple's split history, the trap ADR 0019 names: 2-for-1 on
#: 2000-06-21 and 2005-02-28, 7-for-1 on 2014-06-09, 4-for-1 on 2020-08-31. The
#: product is 112 from 2000 on. Every figure here is invented.
SPLITS = (("2000-06-21", 2), ("2005-02-28", 2), ("2014-06-09", 7), ("2020-08-31", 4))


def _sf1_row(dim, period, filed, label, **v):
    head = {
        "ticker": "ZZAPL",
        "dimension": dim,
        "calendardate": period,
        "date": filed,
        "reportperiod": period,
        "fiscalperiod": label,
        "lastupdated": "2026-01-02",
    }
    return ",".join(
        {**head, **{k: str(x) for k, x in v.items()}}.get(c, "") for c in SF1
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


def test_market_cap_meets_the_price_in_its_own_share_terms(db, tmp_path):
    sid = _security(db, "ZZAPL")
    for table in ("stocks", "fundamentals"):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
            " currency) VALUES ('sharadar', %s, '900041', 'ZZAPL', 'USD')",
            (table,),
        )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key="900041",
        valid_from=D(1990, 1, 2),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    today_terms = 112  # SF1's counts carry every split since
    _ledger(
        db,
        tmp_path,
        "fundamentals",
        ",".join(SF1)
        + "\n"
        + _sf1_row(
            "ARY",
            "1999-09-25",
            "1999-12-22",
            "1999-FY",
            revenue=6_000_000_000,
            netinc=600_000_000,
            netinccmn=600_000_000,
            shareswa=155_000_000 * today_terms,
            shareswadil=170_000_000 * today_terms,
            sharesbas=161_000_000 * today_terms,
            assets=5_000_000_000,
            liabilities=2_000_000_000,
            equity=3_000_000_000,
        )
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
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="split",
            ex_date=D.fromisoformat(d),
            split_numerator=r,
            split_denominator=1,
            source="sharadar",
        )
    _price(db, sid, D(1999, 12, 31), 100)  # raw, before the 2000 split
    _price(db, sid, D(2000, 7, 31), 50)  # raw, after it
    _refresh(db)
    rows = {
        r["as_of_date"]: r
        for r in db.fetchall(
            "SELECT * FROM mart.fundamental_panel WHERE security_id = %s", (sid,)
        )
    }
    dec, jul = rows[D(1999, 12, 31)], rows[D(2000, 7, 31)]
    # The count as filed in 1999, then in July 2000's terms: twice as many shares.
    assert dec["shares_outstanding_as_of"] == 161_000_000
    assert jul["shares_outstanding_as_of"] == 322_000_000
    assert dec["market_cap"] == 16_100_000_000 == jul["market_cap"]
    # The P/E stays in a plausible band; today's terms against a raw price would put
    # it at 112 times less.
    for r in (dec, jul):
        pe = 1 / r["earnings_yield"]
        assert 5 < pe < 100, pe
