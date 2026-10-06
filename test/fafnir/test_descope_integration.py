"""Integration tests for descoping warrants, rights and units (ADR 0012).

Two halves, both SQL-shaped: the security-master load must stop minting these
instruments, and `security descope` must remove the ones already held without
leaving a row anywhere that still points at them -- the FKs on core.security are
NO ACTION and three ops tables carry the id with no FK at all.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import security_master

pytestmark = pytest.mark.integration

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

DAY = dt.date(2024, 6, 3)


class _ScreenerFMP:
    """Screener stub serving one venue's rows."""

    bytes_downloaded = 0
    request_count = 0

    def __init__(self, rows):
        self.rows = rows

    def company_screener(self, *, exchange=None, **_kw):
        return self.rows if exchange == "NASDAQ" else []


def _row(symbol, name):
    return {"symbol": symbol, "exchangeShortName": "NASDAQ", "name": name}


def _held(db, symbol):
    return db.fetchval(
        "SELECT count(*) FROM core.security WHERE primary_symbol = %s", (symbol,)
    )


class _Cfg:
    def __init__(self, dsn, excluded=security_master.INSTRUMENT_KINDS):
        self.dsn = dsn
        self.excluded_instruments = tuple(excluded)


def _descope(db, *args, excluded=security_master.INSTRUMENT_KINDS):
    # Invoked directly, not through cli.main, whose callback would rebuild the
    # config from ~/.fafnirrc and aim this at the machine's own warehouse.
    return CliRunner().invoke(
        cli.security_descope,
        list(args),
        obj={"config": _Cfg(db.dsn, excluded)},
    )


# ---------------------------------------------------------------------------
# The load stops minting them
# ---------------------------------------------------------------------------


SCREENER = [
    _row("ABCD", "Abcd Acquisition Corp."),
    _row("ABCDW", "Abcd Acquisition Corp. Warrant"),
    _row("ABCDR", "Abcd Acquisition Corp. Rights"),
    _row("ABCDU", "Abcd Acquisition Corp. Units"),
    _row("SNOW", "Snowflake Inc."),
]


def test_the_load_skips_warrants_rights_and_units_and_says_so(db):
    result = security_master.load_securities(db, _ScreenerFMP(SCREENER))
    assert sorted(result.new_symbols) == ["ABCD", "SNOW"]
    assert sorted(result.skipped_out_of_scope) == ["ABCDR", "ABCDU", "ABCDW"]
    for symbol in ("ABCDW", "ABCDR", "ABCDU"):
        assert _held(db, symbol) == 0


def test_an_empty_exclusion_admits_them_again(db):
    result = security_master.load_securities(
        db, _ScreenerFMP(SCREENER), excluded_kinds=()
    )
    assert result.skipped_out_of_scope == []
    assert _held(db, "ABCDW") == 1


def test_only_the_excluded_kinds_are_skipped(db):
    result = security_master.load_securities(
        db, _ScreenerFMP(SCREENER), excluded_kinds=("warrant",)
    )
    assert result.skipped_out_of_scope == ["ABCDW"]
    assert _held(db, "ABCDU") == 1


def test_a_declared_symbol_is_exempt(db):
    """`fafnir track add` is how an operator keeps one -- e.g. a REIT's stapled units."""
    repo.upsert_tracked_symbol(db, symbol="ABCDU", asset_type="equity")
    result = security_master.load_securities(db, _ScreenerFMP(SCREENER))
    assert "ABCDU" not in result.skipped_out_of_scope
    assert _held(db, "ABCDU") == 1


def test_a_row_minted_before_the_filter_is_left_untouched_by_the_load(db):
    security_master.load_securities(db, _ScreenerFMP(SCREENER), excluded_kinds=())
    before = db.fetchone(
        "SELECT security_id, company_name FROM core.security"
        " WHERE primary_symbol = 'ABCDW'"
    )
    renamed = [_row("ABCDW", "Something Else Entirely")]
    result = security_master.load_securities(db, _ScreenerFMP(renamed))
    assert result.skipped_out_of_scope == ["ABCDW"]
    after = db.fetchone(
        "SELECT security_id, company_name FROM core.security"
        " WHERE primary_symbol = 'ABCDW'"
    )
    assert after == before


# ---------------------------------------------------------------------------
# `security descope` removes what is already held
# ---------------------------------------------------------------------------


def _mint(db, symbol, name="Abcd Acquisition Corp."):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=name,
        asset_type="equity",
        exchange_code="NASDAQ",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _give_everything(db, sid, symbol):
    """One row in every table that keys to a security."""
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": DAY,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 100,
            }
        ],
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=DAY,
        dividend_amount=0.01,
    )
    repo.replace_adjustment_factors(
        db,
        sid,
        [
            {
                "effective_date": DAY,
                "cumulative_price_factor": 0.99,
                "cumulative_volume_factor": 1,
            }
        ],
    )
    repo.upsert_company_profile(db, security_id=sid, description="A blank check.")
    repo.record_symbol_change(
        db,
        old_symbol=symbol + "X",
        new_symbol=symbol,
        change_date=DAY,
        status="applied",
        security_id=sid,
    )
    repo.set_watermark(db, "fmp", "test-endpoint", DAY, security_id=sid)
    repo._insert_override(
        db,
        security_id=sid,
        target="daily_price",
        action_type=None,
        key_date=DAY + dt.timedelta(days=1),
        operation="delete",
        detail={},
        note="test",
        created_by="test",
    )
    repo.add_dq_flag_once(
        db,
        check_name="stale",
        security_id=sid,
        record_key={"last_date": str(DAY)},
    )


def _rows_for(db, sid):
    return repo.security_footprint(db, [sid])


def test_purge_removes_every_row_and_detaches_the_rename_trail(db):
    sid = _mint(db, "ABCDW", "Abcd Acquisition Corp. Warrant")
    _give_everything(db, sid, "ABCDW")
    assert all(n >= 1 for n in _rows_for(db, sid).values())

    removed = repo.purge_securities(db, [sid])

    assert all(n >= 1 for n in removed.values())
    assert all(n == 0 for n in _rows_for(db, sid).values())
    # The rename record survives, detached, so the sweep does not re-offer it.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.symbol_change"
            " WHERE new_symbol = 'ABCDW' AND security_id IS NULL"
        )
        == 1
    )


def test_purge_leaves_every_other_security_alone(db):
    victim = _mint(db, "ABCDW", "Abcd Acquisition Corp. Warrant")
    keeper = _mint(db, "ABCD")
    _give_everything(db, victim, "ABCDW")
    _give_everything(db, keeper, "ABCD")
    before = _rows_for(db, keeper)

    repo.purge_securities(db, [victim])

    assert _rows_for(db, keeper) == before


def test_descope_dry_run_lists_and_changes_nothing(db):
    sid = _mint(db, "ABCDW", "Abcd Acquisition Corp. Warrant")
    _give_everything(db, sid, "ABCDW")
    _mint(db, "ABCD")

    result = _descope(db, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "ABCDW" in result.output
    assert "ABCD " not in result.output
    assert "Dry run: nothing changed." in result.output
    assert _held(db, "ABCDW") == 1


def test_descope_removes_them_and_leaves_an_audit_row(db):
    w = _mint(db, "ABCDW", "Abcd Acquisition Corp. Warrant")
    u = _mint(db, "ABCDU", "Abcd Acquisition Corp. Units")
    _give_everything(db, w, "ABCDW")
    _mint(db, "ABCD")

    result = _descope(db, "--yes", "-m", "ADR 0012", "--by", "tester")

    assert result.exit_code == 0, result.output
    assert _held(db, "ABCDW") == 0 and _held(db, "ABCDU") == 0
    assert _held(db, "ABCD") == 1
    run = db.fetchone(
        "SELECT source, status, params FROM ops.ingestion_run"
        " WHERE endpoint = 'security-descope' ORDER BY ingestion_run_id DESC LIMIT 1"
    )
    assert run["source"] == "operator" and run["status"] == "success"
    assert sorted(run["params"]["security_ids"]) == sorted([w, u])
    assert run["params"]["note"] == "ADR 0012"


def test_descope_honours_kind_and_the_declared_exemption(db):
    _mint(db, "ABCDW", "Abcd Acquisition Corp. Warrant")
    _mint(db, "ABCDU", "Abcd Acquisition Corp. Units")
    _mint(db, "EFGHU", "Efgh Real Estate Investment Trust")
    repo.upsert_tracked_symbol(db, symbol="EFGHU", asset_type="equity")

    result = _descope(db, "--kind", "unit", "--yes")

    assert result.exit_code == 0, result.output
    assert "Kept, declared in ref.tracked_symbol: EFGHU" in result.output
    assert _held(db, "ABCDU") == 0
    assert _held(db, "EFGHU") == 1
    assert _held(db, "ABCDW") == 1


def test_descope_with_nothing_excluded_refuses(db):
    result = _descope(db, "--dry-run", excluded=())
    assert result.exit_code != 0
    assert "nothing to descope" in result.output
