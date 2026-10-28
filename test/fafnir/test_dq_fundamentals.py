"""
The fundamentals DQ checks and `fafnir status` lines (#102; plan
doc/plans/fundamentals-sf1.md §6.5): integration tests (FAFNIR_TEST_DSN). Every row
is synthetic.

Each check writes its condition once (an open or accepted flag is not written
again), finds what it should, and passes what it should not flag. `run_all` keeps its
five keys and adds one per check. `dq recheck` closes a stale or coverage flag once
the warehouse has caught up. `status` keeps its lines byte for byte and appends the
statements block.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.dq import recheck as rc
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

D = date
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
TODAY = date.today()
OLD_KEYS = ["gaps", "outliers", "stale", "duplicate_identity", "missing_classification"]


def _security(db, symbol, *, currency="USD", active=True):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NASDAQ",
        source="sharadar",
    )
    db.execute(
        "UPDATE core.security SET currency = %s, is_actively_trading = %s"
        " WHERE security_id = %s",
        (currency, active, sid),
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
    version=1,
    known_to=None,
    currency="USD",
    extra=None,
    **items,
):
    year, period = label.split("-")
    row = {
        "security_id": sid,
        "vendor_key": "900050",
        "period_type": "annual" if period == "FY" else "quarter",
        "fiscal_date": fiscal_date,
        "calendar_date": fiscal_date,
        "fiscal_year": int(year),
        "fiscal_period": period,
        "version_no": version,
        "known_from": known_from,
        "known_to": known_to,
        "knowledge_source": "filed" if version == 1 else "observed",
        "filing_date": known_from if version == 1 else None,
        "loaded_at": T0,
        "reported_currency": currency,
        "content_hash": HASH,
        "extra": json.dumps(extra or {}),
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _price(db, sid, day, close=10):
    db.execute(
        "INSERT INTO core.daily_price (security_id, trade_date, open, high, low, close,"
        " volume, source) VALUES (%s, %s, %s, %s, %s, %s, 1000, 'sharadar')",
        (sid, day, close, close, close, close),
    )


def _flags(db, check):
    return db.fetchall(
        "SELECT security_id, table_name, record_key, severity, detail"
        " FROM ops.data_quality_flag WHERE check_name = %s AND resolved_at IS NULL"
        " ORDER BY dq_flag_id",
        (check,),
    )


def _run_twice(db, check):
    """The check's new flags, and its open flags after a second pass over the same
    data, which must write nothing."""
    first = checks.FUNDAMENTALS_CHECKS[check](db)
    assert checks.FUNDAMENTALS_CHECKS[check](db) == 0, "a second pass wrote again"
    flags = _flags(db, check)
    assert len(flags) == first
    return flags


# ---------------------------------------------------------------------------
# run_all's contract
# ---------------------------------------------------------------------------


def test_run_all_keeps_its_keys_and_adds_one_per_check(db):
    out = checks.run_all(db)
    assert list(out) == OLD_KEYS + list(checks.FUNDAMENTALS_CHECKS)
    assert len(checks.FUNDAMENTALS_CHECKS) == 14
    assert all(isinstance(v, int) for v in out.values())


def test_without_fundamentals_or_their_tables_nothing_runs_against_them(
    db, monkeypatch
):
    def refuse(_db):
        raise AssertionError("a fundamentals check ran")

    monkeypatch.setattr(
        checks,
        "FUNDAMENTALS_CHECKS",
        {k: refuse for k in checks.FUNDAMENTALS_CHECKS},
    )
    off = checks.run_all(db, include_fundamentals=False)
    assert list(off)[:5] == OLD_KEYS
    assert all(off[k] == 0 for k in checks.FUNDAMENTALS_CHECKS)
    # A database mid-migration: no statement tables, so nothing to check.
    monkeypatch.setattr(checks, "statements_exist", lambda _db: False)
    assert all(checks.run_all(db)[k] == 0 for k in checks.FUNDAMENTALS_CHECKS)


# ---------------------------------------------------------------------------
# Identities and ties
# ---------------------------------------------------------------------------


def _classify(db, sid, *, sector=None, industry=None):
    """The classification readers see, as core.security's pair (no feeding vendor)."""
    db.execute(
        "UPDATE core.security SET sector_id = %s, industry_id = %s"
        " WHERE security_id = %s",
        (
            repo.get_or_create_sector(db, sector),
            repo.get_or_create_industry(db, industry),
            sid,
        ),
    )


def test_balance_sheet_identity_allows_for_what_sf1_leaves_out(db):
    sid = _security(db, "ZZBAL")
    m = 1_000_000

    def year(y, *, liabilities=1000, equity=900, nci=0, version=1, **kw):
        end = D(y, 12, 31)
        _put(
            db,
            "balance_sheet",
            sid,
            end,
            label=f"{y}-FY",
            known_from=kw.get("known_from", D(y + 1, 2, 18)),
            version=version,
            known_to=kw.get("known_to"),
            total_assets=2000 * m,
            total_liabilities=liabilities * m,
            total_equity_parent=equity * m,
        )
        if version == 1:
            _put(
                db,
                "income",
                sid,
                end,
                label=f"{y}-FY",
                known_from=D(y + 1, 2, 18),
                net_income_noncontrolling=nci,
            )

    year(2007, equity=400)  # before SFAS 160, but 30% of assets: unexplained
    year(2008)  # before SFAS 160, 5%: minority interest outside equity
    year(2010)  # 5% short, no NCI: unexplained
    year(2011, nci=5)  # short by the NCI: not flagged
    year(2012, equity=1100, nci=5)  # liabilities and equity over assets, NCI or not
    year(2013, equity=999.6)  # 0.4M of 2,000M: inside the tolerance
    year(2014, equity=-100)  # a deficit, 55% short: temporary equity
    # Two versions, both short: one condition, listing both.
    year(2015, known_to=D(2016, 6, 1))
    year(2015, equity=800, version=2, known_from=D(2016, 6, 1))
    spac = _security(db, "ZZSPAC")
    _classify(db, spac, sector="Financial Services", industry="Shell Companies")
    _put(
        db,
        "balance_sheet",
        spac,
        D(2020, 12, 31),
        label="2020-FY",
        known_from=D(2021, 3, 1),
        total_assets=200 * m,
        total_liabilities=7 * m,
        total_equity_parent=5 * m,  # the redeemable shares are neither
    )
    flags = _run_twice(db, "balance_sheet_identity")
    got = {
        (f["security_id"], f["record_key"]["fiscal_date"]): (
            f["severity"],
            f["detail"]["reason"],
        )
        for f in flags
    }
    assert got == {
        (sid, "2007-12-31"): ("error", "unexplained"),
        (sid, "2010-12-31"): ("error", "unexplained"),
        (sid, "2012-12-31"): ("error", "liabilities_and_equity_exceed_assets"),
        (sid, "2014-12-31"): ("info", "temporary_equity"),
        (sid, "2015-12-31"): ("error", "unexplained"),
        (spac, "2020-12-31"): ("info", "temporary_equity"),
    }
    # One flag per period, not per version.
    assert all(set(f["record_key"]) == {"period_type", "fiscal_date"} for f in flags)
    (twice,) = [f for f in flags if f["record_key"]["fiscal_date"] == "2015-12-31"]
    assert twice["detail"]["versions"] == [1, 2]
    assert (twice["detail"]["version_no"], twice["detail"]["gap"]) == (
        2,
        pytest.approx(200 * m),
    )


def test_the_cash_ties(db):
    sid = _security(db, "ZZCASH")
    q1, q2 = D(2010, 3, 31), D(2010, 6, 30)
    _put(
        db,
        "balance_sheet",
        sid,
        q1,
        label="2010-Q1",
        known_from=D(2010, 5, 1),
        cash_and_equivalents=500_000_000,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        q2,
        label="2010-Q2",
        known_from=D(2010, 8, 1),
        cash_and_equivalents=800_000_000,
    )
    # Q2: the components sum to 290M against a reported 300M change (cash_flow_tie),
    # and the balance moved 300M against... the reported change: ties.
    _put(
        db,
        "cash_flow",
        sid,
        q2,
        label="2010-Q2",
        known_from=D(2010, 8, 1),
        net_cash_operating=400_000_000,
        net_cash_investing=-60_000_000,
        net_cash_financing=-50_000_000,
        fx_effect_on_cash=0,
        net_change_in_cash=300_000_000,
    )
    tie = _run_twice(db, "cash_flow_tie")
    assert [(f["record_key"]["fiscal_date"], f["severity"]) for f in tie] == [
        ("2010-06-30", "warn")
    ]
    assert tie[0]["record_key"] == {
        "period_type": "quarter",
        "fiscal_date": "2010-06-30",
    }
    assert tie[0]["detail"]["gap"] == pytest.approx(-10_000_000)
    assert tie[0]["detail"]["versions"] == [1]
    assert _run_twice(db, "cash_balance_tie") == []
    # Q1's cash flow says cash fell 200M; the balances say it rose to 500M from
    # nothing on file before: no previous balance, so no tie to judge. Make Q2's
    # balance disagree instead.
    db.execute(
        "UPDATE core.balance_sheet SET cash_and_equivalents = 950000000"
        " WHERE security_id = %s AND fiscal_date = %s",
        (sid, q2),
    )
    flags = _run_twice(db, "cash_balance_tie")
    assert [f["record_key"] for f in flags] == [
        {"period_type": "quarter", "fiscal_date": "2010-06-30"}
    ]
    assert flags[0]["detail"]["gap"] == pytest.approx(150_000_000)


def _cash_quarters(db, sid, year, cash, ncf):
    """Quarters of ``year``: the balance's cash and the cash flow's change, in
    millions; a None change files no cash flow."""
    ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    for q, ((m, d), c, n) in enumerate(zip(ends, cash, ncf), start=1):
        fd = D(year, m, d)
        known = fd + timedelta(days=40)
        _put(
            db,
            "balance_sheet",
            sid,
            fd,
            label=f"{year}-Q{q}",
            known_from=known,
            cash_and_equivalents=c * 1_000_000,
        )
        if n is not None:
            _put(
                db,
                "cash_flow",
                sid,
                fd,
                label=f"{year}-Q{q}",
                known_from=known,
                net_change_in_cash=n * 1_000_000,
            )


def test_the_cash_balance_tie_allows_restricted_cash_from_2018(db):
    # Q2: the balance rose 40M against no change in the cash flow, 3.8% of the
    # balance: restricted cash moving, within 2019's allowance. Q3: 200M against a
    # 200M fall, 19%: flagged. The same 40M in 2016 is beyond 2% of the balance.
    new = _security(db, "ZZRESTR")
    _cash_quarters(db, new, 2019, (1000, 1040, 1040, 1040), (None, 0, -200, 0))
    old = _security(db, "ZZRESTR16")
    _cash_quarters(db, old, 2016, (1000, 1040, 1040, 1040), (None, 0, 0, 0))
    flags = _run_twice(db, "cash_balance_tie")
    assert sorted(
        (f["security_id"], f["record_key"]["fiscal_date"]) for f in flags
    ) == [
        (new, "2019-09-30"),
        (old, "2016-06-30"),
    ]


def test_two_gaps_that_cancel_are_one_balance_sheet(db):
    # Q2's cash is 900M where the cash flows say 510M: Q2's change is 390M over its
    # cash flow and Q3's 390M under. One balance sheet out of line, flagged once.
    sid = _security(db, "ZZPAIR")
    _cash_quarters(db, sid, 2012, (500, 900, 520, 540), (None, 10, 10, 20))
    flags = _run_twice(db, "cash_balance_tie")
    assert [(f["table_name"], f["record_key"]) for f in flags] == [
        (
            "core.balance_sheet",
            {
                "period_type": "quarter",
                "fiscal_date": "2012-06-30",
                "item": "cash_and_equivalents",
            },
        )
    ]
    detail = flags[0]["detail"]
    assert (detail["cash"], detail["from_cash_flow"]) == (
        pytest.approx(900_000_000),
        pytest.approx(510_000_000),
    )
    assert detail["next_fiscal_date"] == "2012-09-30"


def test_a_run_of_alternating_gaps_pairs_from_its_start(db):
    # Q2 and Q4 are each 390M over the cash flows, Q3 is right: gaps +390, -390,
    # +390. Q2's pairs with Q3's; Q3's balance sheet is not flagged, and Q4's gap,
    # with nothing after it to cancel, stands on its cash flow.
    three = _security(db, "ZZRUN3")
    _cash_quarters(db, three, 2012, (500, 900, 520, 930), (None, 10, 10, 20))
    # The same, and Q4's cancelled in the next year's Q1: two pairs, Q2 and Q4.
    four = _security(db, "ZZRUN4")
    _cash_quarters(db, four, 2012, (500, 900, 520, 930), (None, 10, 10, 20))
    _cash_quarters(db, four, 2013, (550, 560, 570, 580), (10, 10, 10, 10))
    flags = _run_twice(db, "cash_balance_tie")
    assert sorted(
        (f["security_id"], f["table_name"], f["record_key"]["fiscal_date"])
        for f in flags
    ) == [
        (three, "core.balance_sheet", "2012-06-30"),
        (three, "core.cash_flow_statement", "2012-12-31"),
        (four, "core.balance_sheet", "2012-06-30"),
        (four, "core.balance_sheet", "2012-12-31"),
    ]
    (alone,) = [f for f in flags if f["table_name"] == "core.cash_flow_statement"]
    assert alone["detail"]["gap"] == pytest.approx(390_000_000)


def _flag(db, sid, check, record_key, severity):
    return db.fetchval(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detail, detected_at)"
        " VALUES (%s, 'core.balance_sheet', %s, %s, %s, '{}', now())"
        " RETURNING dq_flag_id",
        (sid, json.dumps(record_key), check, severity),
    )


def test_recheck_closes_what_the_recalibrated_checks_no_longer_select(db):
    sid = _security(db, "ZZRECAL")
    m = 1_000_000
    for y, equity in ((2010, 900), (2011, 1000)):  # 2010 short; 2011 balances
        _put(
            db,
            "balance_sheet",
            sid,
            D(y, 12, 31),
            label=f"{y}-FY",
            known_from=D(y + 1, 2, 18),
            total_assets=2000 * m,
            total_liabilities=1000 * m,
            total_equity_parent=equity * m,
        )
    period = {"period_type": "annual", "fiscal_date": "2010-12-31"}
    held = _flag(db, sid, "balance_sheet_identity", period, "error")
    # Before the recalibration: one flag per version, and one on a period that
    # balances now (a vendor's fix).
    per_version = _flag(
        db, sid, "balance_sheet_identity", {**period, "version_no": 1}, "error"
    )
    fixed = _flag(
        db,
        sid,
        "balance_sheet_identity",
        {"period_type": "annual", "fiscal_date": "2011-12-31"},
        "error",
    )
    # A bank's negative revenue, written as an error before.
    bank = _security(db, "ZZRBANK")
    _classify(db, bank, sector="Financial Services", industry="Banks - Regional")
    _put(
        db,
        "income",
        bank,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 2, 1),
        revenue=-7,
    )
    rejudged = _flag(
        db,
        bank,
        "fundamentals_negative_impossible",
        {"statement": "income", **period, "version_no": 1},
        "error",
    )
    results = {
        r.check_name: r
        for r in rc.recheck(
            db, checks=["balance_sheet_identity", "fundamentals_negative_impossible"]
        )
    }
    assert sorted(results["balance_sheet_identity"].stale_flag_ids) == sorted(
        [per_version, fixed]
    )
    assert held not in results["balance_sheet_identity"].stale_flag_ids
    assert results["fundamentals_negative_impossible"].stale_flag_ids == [rejudged]
    # Closed, the next run writes the bank's flag again, as info.
    repo.resolve_dq_flags(
        db,
        repo.DqFilter(flag_ids=(rejudged,)),
        note="test",
        resolved_by="tester",
    )
    checks.check_fundamentals_negative_impossible(db)
    (now,) = _flags(db, "fundamentals_negative_impossible")
    assert (now["security_id"], now["severity"]) == (bank, "info")


# ---------------------------------------------------------------------------
# Gaps, staleness, coverage, currency
# ---------------------------------------------------------------------------


def test_a_gap_while_trading_and_a_leading_one(db):
    sid = _security(db, "ZZGAP")
    _put(db, "income", sid, D(2005, 12, 31), label="2005-FY", known_from=D(2006, 3, 1))
    _put(db, "income", sid, D(2008, 12, 31), label="2008-FY", known_from=D(2009, 3, 1))
    _price(db, sid, D(2002, 1, 2))  # traded from 2002: a leading gap to 2005
    _price(db, sid, D(2007, 6, 1))  # and through the 2005-2008 hole
    keys = sorted(
        (f["record_key"]["from"], f["record_key"]["to"])
        for f in _run_twice(db, "fundamentals_gap")
    )
    assert keys == [("2002-01-02", "2005-12-31"), ("2005-12-31", "2008-12-31")]
    # A hole it did not trade through is not a gap.
    quiet = _security(db, "ZZQUIET")
    _put(
        db, "income", quiet, D(2005, 12, 31), label="2005-FY", known_from=D(2006, 3, 1)
    )
    _put(
        db, "income", quiet, D(2008, 12, 31), label="2008-FY", known_from=D(2009, 3, 1)
    )
    _price(db, quiet, D(2005, 6, 1))
    assert checks.check_fundamentals_gap(db) == 0


def _ends(db, sid, ends, period):
    for end in ends:
        label = f"{end.year}-{'FY' if period == 'annual' else 'Q1'}"
        _put(db, "income", sid, end, label=label, known_from=end + timedelta(40))


def test_stale_statements_on_their_own_cycle_and_their_recheck(db):
    ago = [TODAY - timedelta(days=d) for d in (300, 391, 482, 573)]
    quarterly = _security(db, "ZZSTALE")
    _ends(db, quarterly, ago, "quarter")  # every 91 days: stale at 200
    annual = _security(db, "ZZ20F")
    _ends(db, annual, ago[:1] + [ago[0] - timedelta(365)], "annual")  # 500
    old_annual = _security(db, "ZZ20FOLD")
    late = TODAY - timedelta(days=520)
    _ends(db, old_annual, [late, late - timedelta(365)], "annual")
    fresh = _security(db, "ZZFRESH")
    _ends(db, fresh, [TODAY - timedelta(days=90)], "quarter")
    gone = _security(db, "ZZGONE", active=False)
    _ends(db, gone, ago, "quarter")
    flags = _run_twice(db, "fundamentals_stale")
    assert sorted(
        ((f["security_id"], f["record_key"], f["detail"]) for f in flags),
        key=lambda f: f[0],
    ) == [
        (
            quarterly,
            {"latest_fiscal_date": ago[0].isoformat()},
            {"days": 300, "allowed": 200},
        ),
        (
            old_annual,
            {"latest_fiscal_date": late.isoformat()},
            {"days": 520, "allowed": 500},
        ),
    ]
    assert rc.recheck(db, checks=["fundamentals_stale"])[0].stale == 0
    # It files: the recorded condition is gone, and recheck says so.
    _ends(db, quarterly, [TODAY - timedelta(days=20)], "quarter")
    result = rc.recheck(db, checks=["fundamentals_stale"])[0]
    assert result.stale == 1 and result.open_flags == 2


def _sf1_coverage(db, sid, key):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
        " first_quarter) VALUES ('sharadar', 'fundamentals', %s, 'ZZ', '2010-03-31')",
        (key,),
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=D(2000, 1, 3),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )


def test_coverage_and_its_recheck(db):
    missing = _security(db, "ZZMISS")
    _sf1_coverage(db, missing, "900051")
    covered = _security(db, "ZZHAVE")
    _sf1_coverage(db, covered, "900052")
    _put(
        db, "income", covered, D(2010, 3, 31), label="2010-Q1", known_from=D(2010, 5, 1)
    )
    _security(db, "ZZETF")  # SF1 does not cover it: no flag
    flags = _run_twice(db, "fundamentals_coverage")
    assert [f["security_id"] for f in flags] == [missing]
    _put(
        db, "income", missing, D(2010, 3, 31), label="2010-Q1", known_from=D(2010, 5, 1)
    )
    assert rc.recheck(db, checks=["fundamentals_coverage"])[0].stale == 1


def test_statements_in_another_currency(db):
    sid = _security(db, "ZZEUR", currency="USD")
    for q, m in ((1, 3), (2, 6)):
        _put(
            db,
            "income",
            sid,
            D(2010, m, 30 if m == 6 else 31),
            label=f"2010-Q{q}",
            known_from=D(2010, m + 2, 1),
            currency="EUR",
        )
    usd = _security(db, "ZZUSD")
    _put(db, "income", usd, D(2010, 3, 31), label="2010-Q1", known_from=D(2010, 5, 1))
    flags = _run_twice(db, "fundamentals_currency_mismatch")
    assert [(f["security_id"], f["record_key"], f["detail"]) for f in flags] == [
        (sid, {"reported_currency": "EUR"}, {"periods": 2, "trading_currency": "USD"})
    ]


# ---------------------------------------------------------------------------
# Values: impossible, suspicious, unmapped, year to date
# ---------------------------------------------------------------------------


def test_impossible_negatives(db):
    sid = _security(db, "ZZNEG")
    _put(
        db,
        "income",
        sid,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 2, 1),
        revenue=-5,
        net_income=-50,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 2, 1),
        total_assets=100,
        shares_outstanding=-1,
    )
    # A bank's revenue is net of its losses: info. Its share count is not.
    bank = _security(db, "ZZBANK")
    _classify(db, bank, sector="Financial Services", industry="Banks - Regional")
    mreit = _security(db, "ZZMREIT")
    _classify(db, mreit, sector="Real Estate", industry="REIT - Mortgage")
    for who, shares in ((bank, None), (mreit, -3)):
        _put(
            db,
            "income",
            who,
            D(2010, 12, 31),
            label="2010-FY",
            known_from=D(2011, 2, 1),
            revenue=-7,
            weighted_average_shares=shares,
        )
    flags = _run_twice(db, "fundamentals_negative_impossible")
    got = sorted(
        (f["security_id"], f["table_name"], f["severity"], f["detail"]) for f in flags
    )
    # Net income may be negative; revenue and a share count may not.
    assert got == [
        (sid, "core.balance_sheet", "error", {"shares_outstanding": -1}),
        (sid, "core.income_statement", "error", {"revenue": -5}),
        (bank, "core.income_statement", "info", {"revenue": -7}),
        (
            mreit,
            "core.income_statement",
            "error",
            {"revenue": -7, "weighted_average_shares": -3},
        ),
    ]


def test_a_zero_between_two_reported_values(db):
    sid = _security(db, "ZZZERO")
    for q, (m, d), rev in ((1, (3, 31), 100), (2, (6, 30), 0), (3, (9, 30), 120)):
        _put(
            db,
            "income",
            sid,
            D(2010, m, d),
            label=f"2010-Q{q}",
            known_from=D(2010, m, d) + timedelta(days=40),
            revenue=rev,
            net_income=10,
        )
    flags = _run_twice(db, "fundamentals_suspicious_zero")
    assert [(f["record_key"]["fiscal_date"], f["detail"]["items"]) for f in flags] == [
        ("2010-06-30", ["revenue"])
    ]


def test_unmapped_sf1_columns_by_name(db):
    for i, sym in enumerate(("ZZU1", "ZZU2")):
        _put(
            db,
            "income",
            _security(db, sym),
            D(2010, 12, 31),
            label="2010-FY",
            known_from=D(2011, 2, 1),
            extra={"unmapped": {"newfield": str(i)}, "vendor_metric": {"pe": "9"}},
        )
    flags = _run_twice(db, "fundamentals_unmapped_fields")
    assert [(f["security_id"], f["record_key"], f["detail"]) for f in flags] == [
        (None, {"field": "newfield"}, {"rows": 2, "securities": 2})
    ]


def test_quarters_reported_year_to_date(db):
    ytd = _security(db, "ZZYTD")
    discrete = _security(db, "ZZDISC")
    ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    for sid, quarters in ((ytd, (100, 200, 300, 400)), (discrete, (100,) * 4)):
        for q, ((m, d), rev) in enumerate(zip(ends, quarters), start=1):
            _put(
                db,
                "income",
                sid,
                D(2010, m, d),
                label=f"2010-Q{q}",
                known_from=D(2010, m, d) + timedelta(days=40),
                revenue=rev,
            )
        _put(
            db,
            "income",
            sid,
            D(2010, 12, 31),
            label="2010-FY",
            known_from=D(2011, 2, 9),
            revenue=400,
        )
    flags = _run_twice(db, "fundamentals_ytd_suspect")
    assert [(f["security_id"], f["record_key"]) for f in flags] == [
        (ytd, {"fiscal_year": 2010, "item": "revenue"})
    ]


def test_a_fiscal_year_with_two_annual_periods_is_not_judged(db):
    # A change of year end: the June year and the transition to December share the
    # label 2011. Both annual rows fail the ratio; judged, they were two flags on one
    # key, which the open-condition index refused, failing the whole `dq run`.
    sid = _security(db, "ZZFYE")
    ends = (D(2010, 9, 30), D(2010, 12, 31), D(2011, 3, 31), D(2011, 6, 30))
    for q, fd in enumerate(ends, start=1):
        # 100, 200, 300, 400: a year-to-date series, 1,000 against either year's 400.
        _put(
            db,
            "income",
            sid,
            fd,
            label=f"2011-Q{q}",
            known_from=fd + timedelta(days=40),
            revenue=100 * q,
        )
    for fd in (D(2011, 6, 30), D(2011, 12, 31)):
        _put(
            db,
            "income",
            sid,
            fd,
            label="2011-FY",
            known_from=fd + timedelta(days=60),
            revenue=400,
        )
    assert _run_twice(db, "fundamentals_ytd_suspect") == []


# ---------------------------------------------------------------------------
# The timelines: filing lag, TTM series, version chains
# ---------------------------------------------------------------------------


def test_a_filing_days_after_its_period(db):
    sid = _security(db, "ZZLAG")
    _put(db, "income", sid, D(2010, 3, 31), label="2010-Q1", known_from=D(2010, 4, 2))
    _put(db, "income", sid, D(2010, 6, 30), label="2010-Q2", known_from=D(2010, 8, 1))
    flags = _run_twice(db, "fundamentals_implausible_filing_lag")
    assert [(f["record_key"]["fiscal_date"], f["detail"]["days"]) for f in flags] == [
        ("2010-03-31", 2)
    ]


def test_a_missing_quarter_breaks_the_ttm_series(db):
    sid = _security(db, "ZZTTM")
    ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    for year in (2009, 2010):
        for q, (m, d) in enumerate(ends, start=1):
            if (year, q) == (2010, 2):
                continue
            _put(
                db,
                "income",
                sid,
                D(year, m, d),
                label=f"{year}-Q{q}",
                known_from=D(year, m, d) + timedelta(days=40),
            )
    # A half-yearly reporter: Q2 and Q4 each year, no trailing four to break.
    half = _security(db, "ZZHALF")
    for year in (2008, 2009, 2010):
        for q, (m, d) in ((2, (6, 30)), (4, (12, 31))):
            _put(
                db,
                "income",
                half,
                D(year, m, d),
                label=f"{year}-Q{q}",
                known_from=D(year, m, d) + timedelta(days=60),
            )
    flags = _run_twice(db, "fundamentals_ttm_incomplete")
    # 2010-Q3, Q4 and 2011-Q1 would reach back over Q2; 2009's first three are the
    # start of the series, not a break.
    assert [(f["record_key"], f["detail"]) for f in flags] == [
        ({"fiscal_year": 2010}, {"quarters": ["Q3", "Q4"]})
    ]


def test_a_broken_version_chain(db):
    sid = _security(db, "ZZCHAIN")
    end = D(2010, 3, 31)
    # Version 1 closes on 2010-09-01 but version 2 starts on 2010-10-01: a gap.
    _put(
        db,
        "income",
        sid,
        end,
        label="2010-Q1",
        known_from=D(2010, 5, 1),
        known_to=D(2010, 9, 1),
    )
    _put(db, "income", sid, end, label="2010-Q1", known_from=D(2010, 10, 1), version=2)
    # A clean chain beside it.
    _put(
        db,
        "income",
        sid,
        D(2010, 6, 30),
        label="2010-Q2",
        known_from=D(2010, 8, 1),
        known_to=D(2010, 10, 1),
    )
    _put(
        db,
        "income",
        sid,
        D(2010, 6, 30),
        label="2010-Q2",
        known_from=D(2010, 10, 1),
        version=2,
    )
    flags = _run_twice(db, "fundamentals_version_chain")
    assert [(f["record_key"], f["severity"]) for f in flags] == [
        (
            {
                "statement": "income",
                "period_type": "quarter",
                "fiscal_date": "2010-03-31",
            },
            "error",
        )
    ]


# ---------------------------------------------------------------------------
# fafnir status
# ---------------------------------------------------------------------------


def _status(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc_file = tmp_path / "fafnirrc"
    rc_file.write_text("")
    out = CliRunner().invoke(cli.main, ["--config", str(rc_file), "status"])
    assert out.exit_code == 0, out.output
    return out.output.splitlines()


def test_status_keeps_its_lines_and_appends_the_statements(db, tmp_path, monkeypatch):
    sid = _security(db, "ZZSTAT")
    before = _status(db, tmp_path, monkeypatch)
    assert not any(line.startswith(("Statements", "Panel")) for line in before)
    _put(db, "income", sid, D(2009, 12, 31), label="2009-FY", known_from=D(2010, 3, 1))
    _put(
        db,
        "income",
        sid,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 3, 1),
        known_to=D(2011, 6, 1),
    )
    _put(
        db,
        "income",
        sid,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 6, 1),
        version=2,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        D(2010, 12, 31),
        label="2010-FY",
        known_from=D(2011, 3, 1),
    )
    after = _status(db, tmp_path, monkeypatch)
    # The existing lines, byte for byte, then the block.
    assert after[: len(before)] == before
    panel = db.fetchval(
        "SELECT ispopulated FROM pg_matviews WHERE matviewname = 'fundamental_panel'"
    )
    expected = [
        "Statements : income 3, balance 1, cash 0 rows (fiscal 2009-12-31 to 2010-12-31)",
        "             versions 4, in force 3; restated 1, corrected 0",
        "             known filed 3, observed 1",
    ]
    tail = after[len(before) :]
    assert tail[:3] == expected
    if panel:
        assert tail[3].startswith("Panel      : ") and "rows  (latest " in tail[3]
    else:
        assert tail[3] == "Panel      : not refreshed -- fafnir db refresh-marts"
