"""`dq recheck` closes a flag only when the check that wrote it would not write it.

The value of this command is entirely in what it refuses to close, so most of what
follows is a pair: a flag whose condition has genuinely gone, and one that still
holds, asserted together. A recheck that closed both would look identical on the
count alone.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.dq import recheck as rc

# ---------------------------------------------------------------------------
# Scope. These need no database: they are the guard on what this may ever touch.
# ---------------------------------------------------------------------------


def test_recheck_never_covers_a_never_auto_resolve_check():
    from fafnir_mcp.tools import NEVER_AUTO_RESOLVE

    assert not set(rc.RECHECKABLE) & set(NEVER_AUTO_RESOLVE)


def test_recheck_never_covers_a_price_quarantine():
    """A price_* flag describes a bar that was never stored.

    Re-evaluating one against stored data would find its condition "gone" for
    every row, so a single careless entry here would close the whole quarantine
    record in one command.
    """
    assert not [c for c in rc.RECHECKABLE if c.startswith("price_")]


def test_every_rule_binds_the_settings_its_sql_asks_for():
    """A rule whose params do not match its placeholders binds the wrong value."""
    for name, rule in rc.RECHECKABLE.items():
        assert rule.sql.count("%s") == len(rule.params), name
        assert set(rule.params) <= {"exch", "threshold", "min_density", "min_sessions"}


def test_an_unknown_check_is_refused_by_name():
    with pytest.raises(ValueError, match="not re-evaluable"):
        rc.recheck(None, checks=["security_duplicate_identity"])


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _mk(db, symbol="RCHK"):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _bar(db, sid, day, close=100.0):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            }
        ],
    )


def _stale_ids(db, check):
    (result,) = rc.recheck(db, checks=[check])
    return set(result.stale_flag_ids)


def _flag(db, *, check, sid, record_key, severity="warn"):
    repo.add_dq_flag_once(
        db,
        check_name=check,
        security_id=sid,
        record_key=record_key,
        severity=severity,
    )
    return int(
        db.fetchval(
            "SELECT dq_flag_id FROM ops.data_quality_flag WHERE check_name = %s "
            "AND security_id = %s AND record_key = %s::jsonb",
            (check, sid, json.dumps(record_key)),
        )
    )


@pytest.mark.integration
def test_dividend_flag_clears_only_once_a_prior_close_exists(db):
    repaired = _mk(db, "RDIVA")
    still_broken = _mk(db, "RDIVB")
    _bar(db, repaired, dt.date(2024, 6, 3))
    _bar(db, still_broken, dt.date(2024, 6, 10))

    ex = {"ex_date": "2024-06-05"}
    ok = _flag(db, check="dividend_no_prior_close", sid=repaired, record_key=ex)
    bad = _flag(db, check="dividend_no_prior_close", sid=still_broken, record_key=ex)

    stale = _stale_ids(db, "dividend_no_prior_close")
    assert ok in stale
    assert bad not in stale


@pytest.mark.integration
def test_gap_flag_clears_when_the_bar_arrives(db):
    sid = _mk(db, "RGAPA")
    for d in (dt.date(2024, 6, 3), dt.date(2024, 6, 4), dt.date(2024, 6, 5)):
        _bar(db, sid, d)
    filled = _flag(db, check="gap", sid=sid, record_key={"trade_date": "2024-06-04"})
    missing = _flag(db, check="gap", sid=sid, record_key={"trade_date": "2024-06-06"})
    _bar(db, sid, dt.date(2024, 6, 7))  # widen the window past 06-06

    stale = _stale_ids(db, "gap")
    assert filled in stale
    assert missing not in stale


@pytest.mark.integration
def test_gap_flag_clears_when_the_calendar_closes_the_session(db):
    """Migration 0022's shape: the warehouse was right, the calendar was not."""
    sid = _mk(db, "RGAPB")
    _bar(db, sid, dt.date(2024, 6, 3))
    _bar(db, sid, dt.date(2024, 6, 7))
    flag = _flag(db, check="gap", sid=sid, record_key={"trade_date": "2024-06-05"})

    assert flag not in _stale_ids(db, "gap")

    # ref.trading_calendar is seeded reference data and survives the per-test
    # truncation, so this edit has to be put back or it leaks into every test that
    # runs after it and into every later run against the same database. Scoped to
    # the one exchange for the same reason: unqualified, it shuts that session on
    # all six.
    db.execute(
        "UPDATE ref.trading_calendar SET is_open = FALSE "
        " WHERE exchange_code = %s AND trade_date = %s",
        ("NASDAQ", dt.date(2024, 6, 5)),
    )
    try:
        assert flag in _stale_ids(db, "gap")
    finally:
        db.execute(
            "UPDATE ref.trading_calendar SET is_open = TRUE "
            " WHERE exchange_code = %s AND trade_date = %s",
            ("NASDAQ", dt.date(2024, 6, 5)),
        )


@pytest.mark.integration
def test_stale_flag_clears_once_a_later_bar_lands(db):
    behind = _mk(db, "RSTLA")
    current = _mk(db, "RSTLB")
    _bar(db, behind, dt.date(2024, 6, 3))
    _bar(db, current, dt.date(2024, 6, 3))

    key = {"last_date": "2024-06-03"}
    unchanged = _flag(db, check="stale", sid=behind, record_key=key)
    moved_on = _flag(db, check="stale", sid=current, record_key=key)
    _bar(db, current, dt.date(2024, 6, 4))

    stale = _stale_ids(db, "stale")
    assert moved_on in stale
    assert unchanged not in stale


@pytest.mark.integration
def test_outlier_flag_clears_once_the_split_is_loaded(db):
    sid = _mk(db, "ROUT")
    _bar(db, sid, dt.date(2024, 6, 3), close=100.0)
    _bar(db, sid, dt.date(2024, 6, 4), close=10.0)
    flag = _flag(db, check="outlier", sid=sid, record_key={"trade_date": "2024-06-04"})

    assert flag not in _stale_ids(db, "outlier")

    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2024, 6, 4),
        split_numerator=1,
        split_denominator=10,
    )
    assert flag in _stale_ids(db, "outlier")


@pytest.mark.integration
def test_sparse_coverage_clears_when_the_security_becomes_dense(db):
    """The PRTXX case: density crossed back and the flag kept asserting the old one."""
    sid = _mk(db, "RSPRS")
    days = [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code='NASDAQ' "
            "AND is_open AND trade_date >= %s ORDER BY trade_date LIMIT 200",
            (dt.date(2023, 1, 3),),
        )
    ]
    for d in days[::3]:  # ~33% density
        _bar(db, sid, d)
    # Written by check_gaps itself rather than by hand. Its sparse flag carries an
    # empty record_key as `'{}'::jsonb`, and `add_dq_flag_once` cannot produce that
    # row -- it maps a falsy record_key to NULL -- so a hand-built flag here would
    # be a shape the warehouse never holds.
    checks.check_gaps(db)
    flag = int(
        db.fetchval(
            "SELECT dq_flag_id FROM ops.data_quality_flag "
            "WHERE check_name = 'sparse_coverage' AND security_id = %s",
            (sid,),
        )
    )
    assert flag not in _stale_ids(db, "sparse_coverage")

    for d in days:  # fill it in: now 100%
        _bar(db, sid, d)
    assert flag in _stale_ids(db, "sparse_coverage")


@pytest.mark.integration
def test_classification_flag_clears_once_a_sector_lands(db):
    sid = _mk(db, "RCLS")
    flag = _flag(
        db,
        check="security_missing_classification",
        sid=sid,
        record_key={"symbol": "RCLS"},
        severity="info",
    )
    # Unclassified and listed: the condition holds, so it must not be touched.
    assert flag not in _stale_ids(db, "security_missing_classification")

    sector = db.fetchval(
        "INSERT INTO ref.sector (sector_name) VALUES ('Test') "
        "ON CONFLICT (sector_name) DO UPDATE SET sector_name = EXCLUDED.sector_name "
        "RETURNING sector_id"
    )
    industry = db.fetchval(
        "INSERT INTO ref.industry (industry_name) VALUES ('Test') "
        "ON CONFLICT (industry_name) DO UPDATE SET industry_name = EXCLUDED.industry_name "
        "RETURNING industry_id"
    )
    db.execute(
        "UPDATE core.security SET sector_id = %s, industry_id = %s WHERE security_id = %s",
        (sector, industry, sid),
    )
    assert flag in _stale_ids(db, "security_missing_classification")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, dsn):
        self.dsn = dsn


def _run(db, args, **kwargs):
    return CliRunner().invoke(
        cli.dq_recheck, args, obj={"config": _Cfg(db.dsn)}, **kwargs
    )


def _open(db, flag_id):
    return (
        db.fetchval(
            "SELECT resolved_at IS NULL FROM ops.data_quality_flag WHERE dq_flag_id=%s",
            (flag_id,),
        )
        is True
    )


@pytest.mark.integration
def test_recheck_dry_run_changes_nothing(db):
    sid = _mk(db, "RDRY")
    _bar(db, sid, dt.date(2024, 6, 3))
    flag = _flag(
        db,
        check="dividend_no_prior_close",
        sid=sid,
        record_key={"ex_date": "2024-06-05"},
    )
    result = _run(db, ["--check", "dividend_no_prior_close", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert _open(db, flag)


@pytest.mark.integration
def test_recheck_closes_and_records_the_predicate_it_used(db):
    sid = _mk(db, "RCLOSE")
    _bar(db, sid, dt.date(2024, 6, 3))
    flag = _flag(
        db,
        check="dividend_no_prior_close",
        sid=sid,
        record_key={"ex_date": "2024-06-05"},
    )
    result = _run(db, ["--check", "dividend_no_prior_close", "--by", "tester", "--yes"])
    assert result.exit_code == 0, result.output
    assert not _open(db, flag)

    row = db.fetchone(
        "SELECT resolved_by, resolution_note FROM ops.data_quality_flag "
        "WHERE dq_flag_id = %s",
        (flag,),
    )
    assert row["resolved_by"] == "tester"
    # The note must name the predicate, not merely that something was rechecked:
    # it is the whole evidence the next reader gets.
    assert "prior raw close" in row["resolution_note"]


@pytest.mark.integration
def test_recheck_leaves_a_live_condition_open(db):
    """The one that matters: a still-broken flag survives a full-universe recheck."""
    sid = _mk(db, "RLIVE")
    _bar(db, sid, dt.date(2024, 6, 10))
    flag = _flag(
        db,
        check="dividend_no_prior_close",
        sid=sid,
        record_key={"ex_date": "2024-06-05"},
    )
    result = _run(db, ["--by", "tester", "--yes"])
    assert result.exit_code == 0, result.output
    assert _open(db, flag)


@pytest.mark.integration
def test_recheck_reports_checks_with_nothing_stale(db):
    """A check with no stale flags must still be listed, or a skipped check and a
    clean one are indistinguishable on the output alone."""
    result = _run(db, ["--dry-run"])
    assert result.exit_code == 0, result.output
    for name in sorted(rc.RECHECKABLE):
        assert name in result.output
