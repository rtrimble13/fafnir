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


def test_balance_sheet_identity_is_an_error_unless_noncontrolling_interest(db):
    sid = _security(db, "ZZBAL")
    filed = D(2011, 2, 18)
    # 2010: 100M short, no NCI -> error. 2009: short, but NCI income -> info.
    # 2008: 0.4M short of 2,000M, inside the tolerance -> nothing.
    for year, eq, nci in ((2010, 900, 0), (2009, 900, 5), (2008, 999.6, 0)):
        end = D(year, 12, 31)
        _put(
            db,
            "balance_sheet",
            sid,
            end,
            label=f"{year}-FY",
            known_from=D(year + 1, 2, 18) if year != 2010 else filed,
            total_assets=2_000_000_000,
            total_liabilities=1_000_000_000,
            total_equity_parent=eq * 1_000_000,
        )
        _put(
            db,
            "income",
            sid,
            end,
            label=f"{year}-FY",
            known_from=D(year + 1, 2, 18) if year != 2010 else filed,
            net_income_noncontrolling=nci,
        )
    flags = _run_twice(db, "balance_sheet_identity")
    got = {f["record_key"]["fiscal_date"]: f["severity"] for f in flags}
    assert got == {"2010-12-31": "error", "2009-12-31": "info"}
    assert flags[0]["detail"]["gap"] == pytest.approx(100_000_000)


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
    assert tie[0]["detail"]["gap"] == pytest.approx(-10_000_000)
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


def test_stale_statements_and_their_recheck(db):
    sid = _security(db, "ZZSTALE")
    end = TODAY - timedelta(days=300)
    _put(db, "income", sid, end, label=f"{end.year}-FY", known_from=end + timedelta(1))
    fresh = _security(db, "ZZFRESH")
    recent = TODAY - timedelta(days=90)
    _put(db, "income", fresh, recent, label=f"{recent.year}-FY", known_from=TODAY)
    gone = _security(db, "ZZGONE", active=False)
    _put(db, "income", gone, end, label=f"{end.year}-FY", known_from=end + timedelta(1))
    flags = _run_twice(db, "fundamentals_stale")
    assert [(f["security_id"], f["record_key"]) for f in flags] == [
        (sid, {"latest_fiscal_date": end.isoformat()})
    ]
    assert rc.recheck(db, checks=["fundamentals_stale"])[0].stale == 0
    # It files: the recorded condition is gone, and recheck says so.
    later = TODAY - timedelta(days=20)
    _put(db, "income", sid, later, label=f"{later.year}-Q1", known_from=TODAY)
    result = rc.recheck(db, checks=["fundamentals_stale"])[0]
    assert result.stale == 1 and result.open_flags == 1


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
    flags = _run_twice(db, "fundamentals_negative_impossible")
    got = sorted((f["table_name"], f["severity"], f["detail"]) for f in flags)
    # Net income may be negative; revenue and a share count may not.
    assert got == [
        ("core.balance_sheet", "error", {"shares_outstanding": -1}),
        ("core.income_statement", "error", {"revenue": -5}),
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
