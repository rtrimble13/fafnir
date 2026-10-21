"""
`scripts/survivorship_check.sql` (#79; needs FAFNIR_TEST_DSN).

The script is what the host runs to show the survivorship hole is closed, so its
rules are tested here: who is in scope on the Sharadar side, what counts as held,
and the reason a missing security is given. Every key and ticker is synthetic
(ADR 0012 §8).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fafnir.db import repository as repo

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "survivorship_check.sql"


def _statements() -> list[str]:
    """The script's statements, as psql would send them."""
    out = []
    for chunk in re.split(r";\s*$", SCRIPT.read_text(), flags=re.M):
        code = "\n".join(
            line for line in chunk.splitlines() if not line.lstrip().startswith("--")
        )
        if code.strip():
            out.append(chunk)
    return out


def _vendor(db, key, table, category, exchange, first, last, delisted=True):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "category, exchange, is_delisted, first_price_date, last_price_date) "
        "VALUES ('sharadar', %s, %s, %s, %s, %s, %s, %s, %s)",
        (table, key, f"ZZ{key[-2:]}", category, exchange, delisted, first, last),
    )


def _held(db, key, bars=()):
    sid = repo.upsert_security(
        db,
        primary_symbol=f"ZZ{key[-2:]}",
        company_name="ZZ",
        exchange_code="NYSE",
        source="sharadar",
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=date(1998, 1, 2),
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
    )
    if bars:
        repo.upsert_daily_prices(
            db,
            [
                {
                    "security_id": sid,
                    "trade_date": d,
                    "open": Decimal("5"),
                    "high": Decimal("5"),
                    "low": Decimal("5"),
                    "close": Decimal("5"),
                    "volume": 1,
                }
                for d in bars
            ],
            source="sharadar",
        )


def test_the_survivorship_check(db):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    stock = "Domestic Common Stock"
    # Held 2000-2002; died 2002.
    _vendor(db, "900301", "stocks", stock, "NYSE", date(2000, 1, 3), date(2002, 6, 28))
    _held(db, "900301", bars=(date(2000, 1, 3), date(2002, 6, 28)))
    # Live ETF on NYSEARCA (AMEX), never linked.
    _vendor(
        db, "900302", "funds", "ETF", "NYSEARCA", date(2020, 1, 2), date(2026, 10, 1),
        delisted=False,
    )  # fmt: skip
    # Linked, no bars; died 1999.
    _vendor(
        db, "900303", "stocks", stock, "NASDAQ", date(1999, 3, 1), date(1999, 12, 31)
    )
    _held(db, "900303")
    # Linked, bars only from 2007: 2005 and 2006 are outside its span.
    _vendor(db, "900304", "stocks", stock, "NYSE", date(2005, 1, 3), date(2008, 6, 30))
    _held(db, "900304", bars=(date(2007, 1, 3), date(2008, 6, 30)))
    # Out of scope: a unit, an OTC name, a mutual fund, and one dead before 1998.
    _vendor(db, "900305", "stocks", "UNIT", "NYSE", date(2000, 1, 3), date(2001, 1, 2))
    _vendor(db, "900306", "stocks", stock, "OTC", date(2000, 1, 3), date(2001, 1, 2))
    _vendor(db, "900307", "funds", "MF", "NASDAQ", date(2000, 1, 3), date(2001, 1, 2))
    _vendor(db, "900308", "stocks", stock, "NYSE", date(1995, 1, 3), date(1997, 6, 2))

    *setup, per_year_sql, gaps_sql = _statements()
    for sql in setup:
        db.execute(sql)
    years = {r["year"]: r for r in db.fetchall(per_year_sql)}

    def row(year, *cols):
        return tuple(years[year][c] for c in cols)

    cols = ("sharadar_alive", "fafnir_holds", "no_link", "no_bars", "outside_span")
    assert 1998 not in years  # nothing in scope traded then
    assert row(1999, *cols) == (1, 0, 0, 1, 0)
    assert row(1999, "sharadar_delisted", "delisted_held") == (1, 0)
    assert row(2000, *cols) == (1, 1, 0, 0, 0)
    assert row(2002, "sharadar_delisted", "delisted_held") == (1, 1)
    assert row(2005, *cols) == (1, 0, 0, 0, 1)
    assert row(2007, *cols) == (1, 1, 0, 0, 0)
    assert row(2020, *cols) == (1, 0, 1, 0, 0)
    assert years[2000]["pct_held"] == Decimal("100.00")

    gaps = db.fetchall(gaps_sql)
    assert {(g["venue"], g["kind"], g["reason"]) for g in gaps} == {
        ("AMEX", "etf", "no_link"),
        ("NASDAQ", "stock", "no_bars"),
        ("NYSE", "stock", "outside_span"),
    }
    assert len(gaps) <= 20
    # A second run in the same session replaces the view.
    db.execute(setup[0])
