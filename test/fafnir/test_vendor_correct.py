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
def test_only_the_sampled_dates_are_remembered(db, tmp_path, monkeypatch):
    """The first full dry run on the host held every agreeing date of 15,567
    securities and was stopped at 1.6 GB. Only the dates an open flag sampled are
    ever read: 01-05 agreed and 01-06 was corrected, both sampled; 01-08 agreed and
    was not, so it is not kept."""
    sid = _setup(db, tmp_path)
    seen = {}
    real = vc._resolve_history_flags

    def spy(db_, targets, agreed, run):
        seen.update({k: set(v) for k, v in agreed.items()})
        return real(db_, targets, agreed, run)

    monkeypatch.setattr(vc, "_resolve_history_flags", spy)
    result = vc.correct(db, tmp_path, as_of=Y(2026, 1, 2))
    assert seen == {sid: {Y(2010, 1, 5), Y(2010, 1, 6)}}
    assert result.flags_resolved == 1


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


# ---------------------------------------------------------------------------
# --split-bearing
# ---------------------------------------------------------------------------

ACTIONS = "date,action,ticker,name,value,contraticker,contraname\n"


def test_explained_is_fmps_close_times_the_splits_after_it():
    fmp = {"close": D("22.639")}
    assert vc.explained(fmp, {"close": D("45.278")}, D("2"))  # CELG's 2003 bar
    assert not vc.explained(fmp, {"close": D("45.278")}, D("1"))
    assert not vc.explained(fmp, {"close": D("50")}, D("2"))  # 9% off: another cause
    # A reverse split: FMP's pre-adjusted close is the larger one.
    assert vc.explained({"close": D("26440")}, {"close": D("6.61")}, D("0.00025"))


def _sbar(ticker, day, close, unadj, volume=200):
    """A SEP row: OHLC and volume split-adjusted, closeunadj as traded."""
    return (
        f"{ticker},{day},{close},{close},{close},{close},{volume},{close},{unadj},"
        f"{day}\n"
    )


def _split_security(db, symbol, key, fmp_closes, *, split=True):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db, primary_symbol=symbol, company_name=f"{symbol} Co", exchange_code="NYSE"
    )
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker) "
        "VALUES ('sharadar', 'stocks', %s, %s)",
        (key, symbol),
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=Y(2014, 6, 23),
        match_method="ticker_span_price",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,  # the cutover fed every linked security
    )
    repo.upsert_daily_prices(
        db, [_fmp_bar(sid, day, close, volume=400) for day, close in fmp_closes]
    )
    if split:  # the 2:1 FMP never listed, which the gap-fill added
        repo.upsert_corporate_action(
            db,
            security_id=sid,
            action_type="split",
            ex_date=Y(2014, 6, 26),
            split_numerator=D(2),
            split_denominator=D(1),
            source="sharadar",
        )
    return sid


def _split_setup(db, tmp_path):
    days = [Y(2014, 6, 23), Y(2014, 6, 24), Y(2014, 6, 25), Y(2014, 6, 26)]
    # ZZS: FMP's bars before the split are Sharadar's as-traded halved.
    zzs = _split_security(
        db,
        "ZZS",
        "900031",
        [(Y(2014, 6, 20), "49")]  # before the link: FMP's alone
        + list(zip(days, ["50", "50.5", "49", "51"])),
    )
    # ZZT: FMP differs before the split, but not by it.
    zzt = _split_security(
        db, "ZZT", "900032", list(zip(days, ["70", "71", "72", "51"]))
    )
    # ZZU: no Sharadar split, so not a target.
    _split_security(db, "ZZU", "900033", list(zip(days, ["10"] * 4)), split=False)
    sep = PRICES
    for t in ("ZZS", "ZZT"):
        sep += (
            _sbar(t, "2014-06-23", "50", "100")
            + _sbar(t, "2014-06-24", "50.5", "101")
            + _sbar(t, "2014-06-25", "49", "98")
            + _sbar(t, "2014-06-26", "51", "51", volume=100)
        )
    sep += "".join(_sbar("ZZU", d.isoformat(), "10", "10") for d in days)
    _file(db, tmp_path, "stocks", sep)
    _file(
        db,
        tmp_path,
        "actions",
        ACTIONS + "2014-06-26,split,ZZS,ZZS Co,2,,\n2014-06-26,split,ZZT,ZZT Co,2,,\n",
    )
    return zzs, zzt


@integration
def test_split_bearing_corrects_only_the_bars_fmp_pre_adjusted(db, tmp_path):
    zzs, zzt = _split_setup(db, tmp_path)
    plan = vc.correct(
        db, tmp_path, dry_run=True, split_bearing=True, as_of=Y(2026, 1, 2)
    )
    assert plan.securities == 2  # not ZZU, which holds no Sharadar split
    assert {k: v for k, v in plan.counts.items() if k.startswith("split_bearing")} == {
        "split_bearing:compared": 6,
        "split_bearing:stretch_explained": 1,
        "split_bearing:stretch_left": 1,
        "split_bearing:left_differ": 3,  # ZZT: FMP differs, not by the split
        "split_bearing:no_split_after": 2,
        "split_bearing:uncovered_rescaled": 1,  # ZZS's 06-20, before the link
    }
    assert plan.counts["differ"] == 3
    assert dict(plan.split_bearing_by["ZZT"]) == {"left_differ": 3}
    assert _bars(db, zzs)[Y(2014, 6, 23)]["close"] == D("50")  # nothing written

    result = vc.correct(db, tmp_path, split_bearing=True, as_of=Y(2026, 1, 2))
    assert result.corrected == 3
    bars = _bars(db, zzs)
    assert [(bars[d]["close"], bars[d]["source"]) for d in sorted(bars)] == [
        (D("98"), "operator"),  # before the link: rescaled with its stretch
        (D("100"), "sharadar"),
        (D("101"), "sharadar"),
        (D("98"), "sharadar"),
        (D("51"), "fmp"),  # the split's own day: FMP was right
    ]
    assert bars[Y(2014, 6, 23)]["volume"] == 100  # the vendor's, recovered raw
    assert bars[Y(2014, 6, 20)]["volume"] == 200  # FMP's 400, by 1/2
    (edit,) = db.fetchall(
        "SELECT operation, created_by, note FROM ops.operator_override "
        "WHERE security_id = %s AND target = 'daily_price' AND operation = 'add'",
        (zzs,),
    )
    assert edit["created_by"] == vc.CREATED_BY
    assert "rescaled by the factor" in edit["note"]
    assert {r["close"] for r in _bars(db, zzt).values()} == {
        D("70"),
        D("71"),
        D("72"),
        D("51"),
    }
    audit = db.fetchall(
        "SELECT key_date, old_source, new_source FROM ops.vendor_correction "
        "ORDER BY key_date"
    )
    assert [(a["key_date"], a["old_source"], a["new_source"]) for a in audit] == [
        (d, "fmp", "sharadar") for d in (Y(2014, 6, 23), Y(2014, 6, 24), Y(2014, 6, 25))
    ]
    run = repo.last_successful_run(db, "sharadar", "correct")
    assert run["params"]["split_bearing"] is True
    # Corrected, the stretch has nothing left to explain.
    again = vc.correct(db, tmp_path, split_bearing=True, as_of=Y(2026, 1, 2))
    assert again.corrected == 0


@integration
def test_split_bearing_refuses_fmps_volume(db, tmp_path):
    with pytest.raises(ValueError, match="pre-adjusted with its prices"):
        vc.correct(db, tmp_path, split_bearing=True, volume="keep")


@integration
def test_the_split_bearing_command(db, tmp_path, monkeypatch):
    _split_setup(db, tmp_path)
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "correct", *args]
        )

    out = run("--split-bearing", "--volume", "keep", "--dry-run")
    assert out.exit_code != 0 and "pre-adjusted with its prices" in out.output
    out = run("--split-bearing", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. 2 linked securities;" in out.output
    assert "3 bars differ" in out.output
    assert "split_bearing:uncovered_rescaled" in out.output
    assert "Left, differing from the vendor (1 securities" in out.output
    assert "  ZZT " in out.output


@integration
def test_an_explained_stretch_is_corrected_whole(db, tmp_path, monkeypatch):
    """Two of three bars explained (at a lowered threshold): the third is replaced
    too, rather than left at half its neighbours. A stretch where FMP is already as
    traded is left, and counted as agreeing."""
    days = [Y(2014, 6, 23), Y(2014, 6, 24), Y(2014, 6, 25), Y(2014, 6, 26)]
    zzs = _split_security(
        db, "ZZS", "900041", list(zip(days, ["50", "50.5", "60", "51"]))
    )
    _split_security(db, "ZZV", "900042", list(zip(days, ["100", "101", "98", "51"])))
    sep = PRICES
    for t in ("ZZS", "ZZV"):
        sep += (
            _sbar(t, "2014-06-23", "50", "100")
            + _sbar(t, "2014-06-24", "50.5", "101")
            + _sbar(t, "2014-06-25", "49", "98")
            + _sbar(t, "2014-06-26", "51", "51", volume=100)
        )
    _file(db, tmp_path, "stocks", sep)
    _file(
        db,
        tmp_path,
        "actions",
        ACTIONS + "2014-06-26,split,ZZS,ZZS Co,2,,\n2014-06-26,split,ZZV,ZZV Co,2,,\n",
    )
    monkeypatch.setattr(vc, "SEGMENT_SHARE", D("0.6"))
    result = vc.correct(db, tmp_path, split_bearing=True, as_of=Y(2026, 1, 2))
    counts = {k: v for k, v in result.counts.items() if k.startswith("split_bearing")}
    assert counts["split_bearing:bar_replaced_unexplained"] == 1
    assert counts["split_bearing:left_agree"] == 3  # ZZV: as traded already
    assert "split_bearing:left_differ" not in counts
    assert result.corrected == 3
    assert _bars(db, zzs)[Y(2014, 6, 25)]["close"] == D("98")  # FMP's 60 replaced


@integration
def test_a_rescaled_stretch_reverts_with_its_edit(db, tmp_path, monkeypatch):
    zzs, _ = _split_setup(db, tmp_path)
    vc.correct(db, tmp_path, split_bearing=True, as_of=Y(2026, 1, 2))
    edit = db.fetchval(
        "SELECT detail->'transform'->>'edit' FROM ops.operator_override "
        "WHERE security_id = %s AND target = 'daily_price' AND operation = 'add'",
        (zzs,),
    )
    assert _bars(db, zzs)[Y(2014, 6, 20)]["close"] == D("98")
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')
    out = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "override", "revoke", edit, "-m", "test", "--yes"],
    )
    assert out.exit_code == 0, out.output
    bar = _bars(db, zzs)[Y(2014, 6, 20)]
    assert (bar["close"], bar["source"], bar["volume"]) == (D("49"), "fmp", 400)
