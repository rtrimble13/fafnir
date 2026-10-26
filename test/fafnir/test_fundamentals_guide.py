"""
doc/fundamentals.md's worked example (#108), run as written.

The guide's value-decile query is the block after its ``worked-example: value-decile``
marker. It is read from the document, not copied here, so the example a researcher
pastes is the one this test runs. On a synthetic universe of twenty issuers, one of
which dies mid-year, every decile's size, yield range, count of dead issuers and mean
return are known in advance. Every row is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals_map as fm

GUIDE = Path(__file__).resolve().parents[2] / "doc" / "fundamentals.md"
MARKER = "<!-- worked-example: value-decile"
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
FORMED = date(2010, 6, 30)  # a Wednesday: June 2010's last weekday
DEAD = 5  # issuer 5 stops trading at the end of 2010


def _example() -> str:
    """The SQL block that follows the marker."""
    text = GUIDE.read_text()
    assert text.count(MARKER) == 1
    after = text[text.index(MARKER) :]
    match = re.search(r"```sql\n(.*?)```", after, re.S)
    assert match, "no ```sql block after the marker"
    return match.group(1)


def test_the_guide_holds_one_worked_example():
    sql = _example()
    assert "mart.fundamental_panel" in sql and "ntile(10)" in sql
    # Nothing in it drops the dead: no filter on today's status.
    assert "is_actively_trading" not in sql and "delisted_date" not in sql


def _security(db, i):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    return repo.upsert_security(
        db,
        primary_symbol=f"ZZV{i:02d}",
        company_name=f"ZZV{i:02d} Inc",
        exchange_code="NASDAQ",
        source="sharadar",
    )


def _put(db, statement, sid, **items):
    row = {
        "security_id": sid,
        "vendor_key": "900108",
        "period_type": "annual",
        "fiscal_date": date(2009, 12, 31),
        "calendar_date": date(2009, 12, 31),
        "fiscal_year": 2009,
        "fiscal_period": "FY",
        "version_no": 1,
        "known_from": date(2010, 2, 1),
        "knowledge_source": "filed",
        "filing_date": date(2010, 2, 1),
        "loaded_at": T0,
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


@pytest.mark.integration
def test_the_worked_example_runs_and_keeps_the_dead(db):
    # Issuer i earns (i - 3) x 10 on 100 shares at 10: an earnings yield of
    # (i - 3) / 100, so decile d holds issuers 2d - 2 and 2d - 1. Twelve months on
    # each closes at 10 x (1 + i / 100), but issuer 5 last trades at 8 in December.
    for i in range(20):
        sid = _security(db, i)
        income = 10 * (i - 3)
        _put(
            db,
            "income",
            sid,
            revenue=1000,
            net_income=income,
            net_income_common=income,
            weighted_average_shares=100,
        )
        _put(
            db,
            "balance_sheet",
            sid,
            total_assets=2000,
            total_equity_parent=1000,
            shares_outstanding=100,
        )
        _price(db, sid, FORMED, 10)
        if i == DEAD:
            _price(db, sid, date(2010, 12, 31), 8)
        else:
            _price(db, sid, date(2011, 6, 30), 10 * (1 + i / 100))
    # Priced but without statements: not in the panel, so not in a decile.
    _price(db, _security(db, 99), FORMED, 10)
    maintenance.refresh_marts(db, views=["mart.fundamental_panel"])

    rows = db.fetchall(_example())
    assert [r["decile"] for r in rows] == list(range(1, 11))
    assert {r["securities"] for r in rows} == {2}
    for r in rows:
        first = 2 * r["decile"] - 2
        assert (r["ey_from"], r["ey_to"]) == (
            Decimal(first - 3) / 100,
            Decimal(first - 2) / 100,
        )
        returns = [
            Decimal("-0.2") if i == DEAD else Decimal(i) / 100
            for i in (first, first + 1)
        ]
        assert r["mean_return_12m"] == round(sum(returns) / 2, 4)
        # The dead issuer is in decile 3, its return run to its last close.
        assert r["stopped_trading"] == (1 if r["decile"] == 3 else 0)
