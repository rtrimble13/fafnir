"""
FMP feeds the securities FMP minted, and only those (ADR 0015 §2; needs FAFNIR_TEST_DSN).

Before this, "vendor-fed" meant "not operator-minted", so a security another vendor
minted -- a Sharadar dead issuer -- would have been fed by FMP. FMP keys history by
ticker and serves a ticker's whole history whoever used it, and the price upsert
overwrites, so an FMP pull would have filled the dead issuer with whoever trades
under its ticker today. These tests pin both halves of the change:

  * for every security the warehouse holds today (FMP-minted listed and delisted,
    operator-minted, a declared fund) every selector returns exactly what it did;
  * a security another vendor minted is reached by no FMP universe, per-symbol
    guard, ticker-grouping check, report or venue transfer.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fafnir.db import repository as repo
from fafnir.dq import checks
from fafnir.ingest import corporate_actions, daily_price, venue_transfer
from fafnir.ingest.runlog import RunLog
from fafnir.sources import vendor_gaps

pytestmark = pytest.mark.integration

# The predicate the selectors used before ADR 0015. The "same as before" tests
# compare against it directly, so they say what the old behaviour was rather than
# restating it as a list of ids.
OLD_RULE = "source <> 'operator'"


class _CountingFMP:
    """An FMP stand-in that records every call. A guarded security makes none."""

    bytes_downloaded = 0

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def eod_raw(self, symbol, from_date=None, to_date=None):
        self.calls.append(("eod_raw", symbol))
        return []

    def splits(self, symbol):
        self.calls.append(("splits", symbol))
        return []

    def dividends(self, symbol):
        self.calls.append(("dividends", symbol))
        return []


def _mint(db, symbol, *, source="fmp", delisted=None, asset_type="equity"):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        asset_type=asset_type,
        exchange_code="NASDAQ",
        is_actively_trading=delisted is None,
        delisted_date=delisted,
        source=source,
    )
    if delisted is None:
        repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol, source=source)
    return sid


@pytest.fixture()
def today(db):
    """The shapes the warehouse holds today: every one of them existed before ADR 0015."""
    ids = {
        "listed": _mint(db, "AAA"),
        "delisted": _mint(db, "BBB", delisted=dt.date(2024, 3, 1)),
        "operator": _mint(db, "CCC", source="operator", delisted=dt.date(2020, 1, 2)),
        "fund": _mint(db, "FUNDX", asset_type="fund"),
    }
    db.commit()
    return ids


@pytest.fixture()
def with_sharadar(db, today):
    """Add what Sharadar will mint: dead issuers, one on a ticker FMP still serves."""
    ids = dict(today)
    # Reused ticker: a company that died in 2008 traded as AAA, as FMP's AAA does now.
    ids["sharadar_reused"] = _mint(
        db, "AAA", source="sharadar", delisted=dt.date(2008, 9, 15)
    )
    ids["sharadar_dead"] = _mint(
        db, "DEAD", source="sharadar", delisted=dt.date(2001, 12, 2)
    )
    db.commit()
    return ids


def _ids(rows):
    return {r["security_id"] for r in rows}


def _old(db, *, extra="", include_inactive=False):
    where = "" if include_inactive else f"AND {repo.nightly_universe()} "
    return {
        r["security_id"]
        for r in db.fetchall(
            f"SELECT security_id FROM core.security WHERE {OLD_RULE} {extra} {where}"
        )
    }


# ---------------------------------------------------------------------------
# Today's shapes: nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("include_inactive", [False, True])
def test_selectors_return_the_same_ids_as_before(db, today, include_inactive):
    kw = {"include_inactive": include_inactive}
    assert _ids(repo.universe_securities(db, **kw)) == _old(db, **kw)
    assert _ids(repo.securities_by_asset_type(db, ["fund"], **kw)) == _old(
        db, extra="AND asset_type = 'fund'", **kw
    )
    assert _ids(
        repo.actions_reconciliation_slice(db, buckets=1, bucket=0, **kw)
    ) == _old(db, **kw)
    assert _ids(
        repo.securities_without_actions_watermark(db, corporate_actions.ENDPOINT, **kw)
    ) == _old(db, **kw)
    expected_symbols = [
        r["primary_symbol"]
        for r in db.fetchall(
            "SELECT primary_symbol FROM core.security WHERE "
            "security_id = ANY(%s) ORDER BY security_id",
            (sorted(_old(db, **kw)),),
        )
    ]
    assert repo.fmp_price_universe(db, **kw) == expected_symbols


def test_the_operator_security_is_still_left_out(db, today):
    reached = _ids(repo.universe_securities(db, include_inactive=True))
    assert today["operator"] not in reached
    assert {today["listed"], today["delisted"], today["fund"]} <= reached


def test_is_fed_by(db, with_sharadar):
    ids = with_sharadar
    for name in ("listed", "delisted", "fund"):
        assert repo.is_fed_by(db, ids[name], "fmp"), name
    for name in ("operator", "sharadar_reused", "sharadar_dead"):
        assert not repo.is_fed_by(db, ids[name], "fmp"), name
    assert not repo.is_fed_by(db, 999_999, "fmp")


def test_another_vendors_feed_is_its_open_is_feed_link(db, with_sharadar):
    # Sharadar's feed is an explicit open is_feed link in core.security_vendor_xref
    # (#55), never a guess from who minted the security.
    dead = with_sharadar["sharadar_dead"]
    assert not repo.is_fed_by(db, dead, "sharadar")  # minted by it, but not linked
    db.execute(
        "INSERT INTO core.security_vendor_xref (security_id, vendor, vendor_key, "
        "valid_from, is_feed, match_method, created_by) "
        "VALUES (%s, 'sharadar', '900001', '2001-01-02', true, 'minted', 'test')",
        (dead,),
    )
    assert repo.is_fed_by(db, dead, "sharadar")
    assert not repo.is_fed_by(db, dead, "fmp")
    with pytest.raises(ValueError, match="unknown vendor"):
        repo.is_fed_by(db, dead, "polygon")


# ---------------------------------------------------------------------------
# A security another vendor minted: no FMP path reaches it
# ---------------------------------------------------------------------------


def test_no_fmp_universe_reaches_a_sharadar_security(db, with_sharadar):
    sharadar = {with_sharadar["sharadar_reused"], with_sharadar["sharadar_dead"]}
    universes = {
        "ingest prices --include-inactive": {
            r["security_id"]
            for r in db.fetchall(
                "SELECT security_id FROM core.security WHERE primary_symbol = ANY(%s)"
                " AND source = 'fmp'",
                (repo.fmp_price_universe(db, include_inactive=True),),
            )
        },
        "universe": _ids(repo.universe_securities(db, include_inactive=True)),
        "actions first load": _ids(
            repo.securities_without_actions_watermark(
                db, corporate_actions.ENDPOINT, include_inactive=True
            )
        ),
        "reconciliation slice": _ids(
            repo.actions_reconciliation_slice(
                db, buckets=1, bucket=0, include_inactive=True
            )
        ),
        "by asset type": _ids(
            repo.securities_by_asset_type(db, ["equity"], include_inactive=True)
        ),
        "vendor gaps": _ids(vendor_gaps._universe(db)),
    }
    for name, reached in universes.items():
        assert not (reached & sharadar), name
    # DEAD is a ticker only Sharadar holds: the price universe must not name it at
    # all, or the per-symbol pull would be the only thing standing in the way.
    assert "DEAD" not in repo.fmp_price_universe(db, include_inactive=True)


def test_the_price_loader_refuses_a_sharadar_security(db, with_sharadar):
    fmp = _CountingFMP()
    stats: dict = {}
    with RunLog(db, source="fmp", endpoint=daily_price.ENDPOINT) as run:
        n = daily_price.load_symbol_prices(db, fmp, "DEAD", run=run, stats=stats)
    assert n == 0
    assert fmp.calls == []
    assert stats == {"not_fmp_fed": 1}
    assert not db.fetchval(
        "SELECT count(*) FROM core.daily_price WHERE security_id = %s",
        (with_sharadar["sharadar_dead"],),
    )


def test_the_actions_loader_refuses_a_sharadar_security(db, with_sharadar):
    fmp = _CountingFMP()
    corporate_actions.load_actions(db, fmp, ["DEAD"], as_of=dt.date(2024, 6, 3))
    assert fmp.calls == []


def test_a_reused_ticker_is_not_a_duplicate_identity(db, with_sharadar):
    # FMP's AAA and Sharadar's dead AAA are different companies. Two vendors holding
    # one ticker for two issuers is what the ticker *means* in a survivorship-free
    # warehouse, not a fork to repair.
    assert repo.duplicate_symbol_groups(db, symbol="AAA") == []
    checks.check_duplicate_identity(db)
    assert not db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag "
        "WHERE check_name = 'security_duplicate_identity'"
    )


def test_two_fmp_rows_on_one_ticker_are_still_flagged(db, with_sharadar):
    # The control: the check still does its job for the rows FMP minted.
    _mint(db, "BBB")  # FMP's BBB again, listed, beside its delisted BBB
    db.commit()
    assert [g.symbol for g in repo.duplicate_symbol_groups(db, symbol="BBB")] == ["BBB"]
    checks.check_duplicate_identity(db)
    assert db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag "
        "WHERE check_name = 'security_duplicate_identity' "
        "AND record_key->>'symbol' = 'BBB'"
    )


def test_a_venue_transfer_refuses_a_sharadar_security(db, with_sharadar):
    plan = venue_transfer.plan_venue_transfer(
        db,
        security_id=with_sharadar["sharadar_dead"],
        venue_code="NYSE",
        moved_on=dt.date(2001, 6, 1),
    )
    assert any("minted by sharadar, not FMP" in r for r in plan.refusals)
