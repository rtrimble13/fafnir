"""
`fafnir vendor feed` and the FMP feed rule it changes (plan SA-1005, #98; ADR 0015
§2; the integration tests need FAFNIR_TEST_DSN).

One vendor feeds a security at a time. FMP feeds the securities it minted unless a
feeding link gives them to another vendor; `vendor feed --vendor fmp` takes the
links back. Synthetic keys and tickers (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import vendor_feed as vf

pytestmark = pytest.mark.integration


def _security(db, symbol, *, source="fmp", is_fund=False):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        is_fund=is_fund,
        source=source,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _link(db, sid, vendor, key, *, feed=False):
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor=vendor,
        vendor_key=key,
        valid_from=date(2000, 1, 3),
        match_method="minted" if feed else "manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=feed,
    )


def _fixture(db):
    ids = {
        "a": _security(db, "ZZFA"),
        "b": _security(db, "ZZFB"),
        "dead": _security(db, "ZZFD", source="sharadar"),
        "unlinked": _security(db, "ZZFU"),
        "fund": _security(db, "ZZFN", is_fund=True),
    }
    _link(db, ids["a"], "sharadar", "900501")
    _link(db, ids["b"], "sharadar", "900502")
    _link(db, ids["dead"], "sharadar", "900503", feed=True)
    _link(db, ids["fund"], "sharadar", "900504")
    _link(db, ids["fund"], "tiingo", "ZZFN")
    return ids


def _feeds(db):
    return {
        (r["security_id"], r["vendor"])
        for r in db.fetchall(
            "SELECT security_id, vendor FROM core.security_vendor_xref "
            "WHERE is_feed AND valid_to IS NULL"
        )
    }


def _outcomes(result):
    return {c.security_id: c.outcome for c in result.changes}


def test_fmp_stops_feeding_a_security_another_vendor_feeds(db):
    ids = _fixture(db)
    universe = repo.fmp_price_universe(db)
    assert {"ZZFA", "ZZFB", "ZZFU", "ZZFN"} <= set(universe)
    assert "ZZFD" not in universe  # minted by Sharadar

    vf.feed(db, "sharadar", security_ids=[ids["a"]], by="tester")
    assert "ZZFA" not in repo.fmp_price_universe(db)
    assert not repo.is_fed_by(db, ids["a"], "fmp")
    assert repo.is_fed_by(db, ids["a"], "sharadar")
    assert repo.is_fed_by(db, ids["b"], "fmp")
    # The duplicate-identity check still looks among every row FMP minted.
    assert repo.FMP_MINTED_SECURITY == "source = 'fmp'"


def test_feed_to_sharadar_then_roll_back_to_fmp(db):
    ids = _fixture(db)
    c = Decimal("5")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": ids["a"],
                "trade_date": date(2026, 10, 1),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1,
            }
        ],
        source="sharadar",
    )
    before = _feeds(db)

    plan = vf.feed(db, "sharadar", all_linked=True, by="tester", dry_run=True)
    assert _outcomes(plan) == {
        ids["a"]: "fed",
        ids["b"]: "fed",
        ids["dead"]: "already",
        ids["fund"]: "fed",
    }
    assert _feeds(db) == before  # nothing written
    assert {c.previous for c in plan.changes if c.outcome == "fed"} == {"fmp"}

    done = vf.feed(db, "sharadar", all_linked=True, by="tester")
    assert sorted(done.changed()) == sorted([ids["a"], ids["b"], ids["fund"]])
    assert _feeds(db) == {
        (ids["a"], "sharadar"),
        (ids["b"], "sharadar"),
        (ids["dead"], "sharadar"),
        (ids["fund"], "sharadar"),
    }
    run = db.fetchone(
        "SELECT source, endpoint, params FROM ops.ingestion_run "
        "WHERE ingestion_run_id = %s",
        (done.run_id,),
    )
    assert (run["source"], run["endpoint"], run["params"]["by"]) == (
        "sharadar",
        "feed",
        "tester",
    )
    assert sorted(run["params"]["changed"]) == sorted(done.changed())
    again = vf.feed(db, "sharadar", all_linked=True, by="tester")
    assert set(_outcomes(again).values()) == {"already"}

    # The rollback: FMP feeds what it minted again; the dead issuer keeps its feed.
    back = vf.feed(db, "fmp", all_linked=True, by="tester")
    assert _outcomes(back) == {
        ids["a"]: "restored",
        ids["b"]: "restored",
        ids["dead"]: "not_fmp_minted",
        ids["fund"]: "restored",
    }
    assert _feeds(db) == {(ids["dead"], "sharadar")}
    assert {"ZZFA", "ZZFB", "ZZFN"} <= set(repo.fmp_price_universe(db))
    # No price row moved, whoever wrote it.
    (bar,) = db.fetchall(
        "SELECT source FROM core.daily_price WHERE security_id = %s", (ids["a"],)
    )
    assert bar["source"] == "sharadar"


def test_one_feed_per_security_and_the_scopes(db):
    ids = _fixture(db)
    vf.feed(db, "sharadar", all_linked=True, by="tester")
    # Tiingo takes the fund from Sharadar: one feeding link per security.
    funds = vf.feed(db, "tiingo", funds=True, by="tester")
    assert _outcomes(funds) == {ids["fund"]: "fed"}
    assert funds.changes[0].previous == "sharadar"
    assert (ids["fund"], "tiingo") in _feeds(db)
    assert (ids["fund"], "sharadar") not in _feeds(db)

    some = vf.feed(
        db, "sharadar", security_ids=[ids["a"], ids["unlinked"]], by="tester"
    )
    assert _outcomes(some) == {ids["a"]: "already", ids["unlinked"]: "no_link"}
    back = vf.feed(db, "fmp", security_ids=[ids["unlinked"], ids["dead"]], by="tester")
    assert _outcomes(back) == {
        ids["unlinked"]: "already",
        ids["dead"]: "not_fmp_minted",
    }

    with pytest.raises(vf.FeedError, match="exactly one"):
        vf.feed(db, "sharadar", by="tester")
    with pytest.raises(vf.FeedError, match="exactly one"):
        vf.feed(db, "sharadar", all_linked=True, funds=True, by="tester")
    with pytest.raises(vf.FeedError, match="unknown vendor"):
        vf.feed(db, "polygon", all_linked=True, by="tester")


def test_the_command(db, tmp_path, monkeypatch):
    ids = _fixture(db)
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text("")

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "vendor", "feed", *args]
        )

    out = run("--vendor", "sharadar", "--all-linked", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. Feed to sharadar: 1 already, 3 fed" in out.output
    assert "previously fed by: fmp 3" in out.output
    out = run("--vendor", "sharadar", "--security-id", str(ids["unlinked"]), "--yes")
    assert out.exit_code == 0, out.output
    assert "Nothing to change." in out.output
    assert "no_link" in out.output and "ZZFU" in out.output
    out = run("--vendor", "sharadar", "--all-linked", "--yes", "--by", "operator")
    assert out.exit_code == 0, out.output
    assert "by operator" in out.output
    assert not repo.is_fed_by(db, ids["a"], "fmp")
    out = run("--vendor", "sharadar")
    assert out.exit_code != 0 and "exactly one" in out.output
