"""
`fafnir vendor correct` (plan SA-0601, #75; the integration tests need
FAFNIR_TEST_DSN).

An FMP bar that disagrees with Sharadar's is replaced, audited and revertible; one
that agrees, one an operator edited and one before the link are left alone. Every
key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import vendor_correct as vc

D = Decimal
Y = date
integration = pytest.mark.integration

PRICES = "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"


def test_differs_allows_a_cent_a_basis_point_and_the_recoverys_rounding():
    bar = {"open": D("20"), "high": D("20"), "low": D("20"), "close": D("20")}
    assert not vc.differs(bar, dict(bar, close=D("20.01")), D("1"))
    assert vc.differs(bar, dict(bar, close=D("20.02")), D("1"))
    # A 40:1 history recovers open/high/low to +/- 0.02: inside it, not a difference.
    assert not vc.differs(bar, dict(bar, open=D("20.015")), D("40"))
    assert vc.differs(bar, dict(bar, open=D("20.02")), D("1"))


def _file(db, tmp_path, table, body):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind="full",
        filters={},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )


def _bar(ticker, day, close, volume=100):
    return f"{ticker},{day},{close},{close},{close},{close},{volume},{close},{close},{day}\n"


def _fmp_bar(sid, day, close, volume=7):
    c = D(close)
    return {
        "security_id": sid,
        "trade_date": day,
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": volume,
    }


def _setup(db, tmp_path):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db, primary_symbol="ZZK", company_name="ZZK Co", exchange_code="NYSE"
    )
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', '900001', 'ZZK')"
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key="900001",
        valid_from=Y(2010, 1, 4),
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
    )
    repo.upsert_daily_prices(
        db,
        [
            _fmp_bar(sid, Y(2009, 12, 31), "99"),  # before the link: never compared
            _fmp_bar(sid, Y(2010, 1, 5), "20"),  # agrees
            _fmp_bar(sid, Y(2010, 1, 6), "30"),  # differs: corrected
            _fmp_bar(sid, Y(2010, 1, 7), "40"),  # differs, but an operator edited it
            _fmp_bar(sid, Y(2010, 1, 8), "22", volume=14),  # agrees
        ],
    )
    db.execute(
        "INSERT INTO ops.operator_override (security_id, target, key_date, "
        "operation, note, created_by) VALUES (%s, 'daily_price', '2010-01-07', "
        "'delete', 'bad', 't')",
        (sid,),
    )
    repo.add_dq_flag_once(
        db,
        check_name="vendor_history_disagrees",
        security_id=sid,
        table_name="core.daily_price",
        record_key={"vendor": "sharadar", "vendor_key": "900001"},
        detail={"samples": [{"date": "2010-01-05"}, {"date": "2010-01-06"}]},
    )
    _file(
        db,
        tmp_path,
        "stocks",
        PRICES
        + _bar("ZZK", "2009-12-31", "10")
        + _bar("ZZK", "2010-01-05", "20", volume=7)
        + _bar("ZZK", "2010-01-06", "21")
        + _bar("ZZK", "2010-01-07", "21")
        + _bar("ZZK", "2010-01-08", "22", volume=7),
    )
    return sid


def _bars(db, sid):
    return {
        r["trade_date"]: r
        for r in db.fetchall(
            "SELECT * FROM core.daily_price WHERE security_id = %s", (sid,)
        )
    }


@integration
def test_a_differing_bar_is_corrected_audited_and_the_flag_resolved(db, tmp_path):
    sid = _setup(db, tmp_path)
    plan = vc.correct(db, tmp_path, dry_run=True, as_of=Y(2026, 1, 2))
    assert (plan.securities, plan.counts["differ"], plan.run_id) == (1, 2, None)
    decade = plan.by["decade"]["2010s"]
    assert (decade.compared, decade.differ) == (4, 2)
    assert plan.volume_summary() == {
        "bars": 2,
        "median": 1.5,
        "p10": 1.1,
        "p90": 1.9,
        "within_1pct": 0.5,
        "within_5pct": 0.5,
    }
    assert _bars(db, sid)[Y(2010, 1, 6)]["close"] == D("30")  # nothing written

    result = vc.correct(db, tmp_path, as_of=Y(2026, 1, 2))
    assert (result.corrected, result.counts["skipped_suppressed"]) == (1, 1)
    bars = _bars(db, sid)
    fixed = bars[Y(2010, 1, 6)]
    assert (fixed["close"], fixed["source"], fixed["volume"]) == (
        D("21"),
        "sharadar",
        7,
    )
    assert (bars[Y(2010, 1, 7)]["close"], bars[Y(2010, 1, 7)]["source"]) == (
        D("40"),
        "fmp",
    )
    assert bars[Y(2009, 12, 31)]["close"] == D("99")
    (audit,) = db.fetchall(
        "SELECT old_source, new_source, key_date, old_row FROM ops.vendor_correction"
    )
    assert (audit["old_source"], audit["new_source"], audit["key_date"]) == (
        "fmp",
        "sharadar",
        Y(2010, 1, 6),
    )
    assert D(str(audit["old_row"]["close"])) == D("30")
    assert result.flags_resolved == 1
    assert not db.fetchall(
        "SELECT 1 FROM ops.data_quality_flag WHERE check_name = "
        "'vendor_history_disagrees' AND resolved_at IS NULL"
    )
    # A re-run finds nothing: the corrected bar is Sharadar's now.
    again = vc.correct(db, tmp_path, as_of=Y(2026, 1, 2))
    assert (again.counts["differ"], again.corrected) == (1, 0)  # the operator's bar


@integration
def test_volume_sharadar_takes_the_vendors_volume(db, tmp_path):
    sid = _setup(db, tmp_path)
    vc.correct(db, tmp_path, volume="sharadar", as_of=Y(2026, 1, 2))
    assert _bars(db, sid)[Y(2010, 1, 6)]["volume"] == 100
    with pytest.raises(ValueError, match="keep or sharadar"):
        vc.correct(db, tmp_path, volume="both")


@integration
def test_the_command(db, tmp_path, monkeypatch):
    _setup(db, tmp_path)
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "correct", *args]
        )

    out = run("--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. 1 linked securities; 5 vendor rows read; " in (
        out.output
    )
    assert "By decade:" in out.output and "2010s" in out.output
    assert "Volume, FMP / Sharadar, on agreeing-price bars: 2 bars" in out.output
    out = run()
    assert out.exit_code == 0, out.output
    assert "1 corrected, 1 vendor_history_disagrees flags resolved" in out.output
