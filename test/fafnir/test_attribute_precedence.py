"""
Security attributes after the cutover (#109, migration 0039; ADR 0015 §4, ADR 0019 §5):
integration tests (FAFNIR_TEST_DSN). Every row is synthetic.

- **The classification** a reader sees is the feeding vendor's: Sharadar's names for a
  security Sharadar feeds, mapped as ``sharadar_map.map_classification`` maps them,
  else core.security's. No column of core.security is written.
- **Market cap** is derived where SF1 holds the shares (the panel's expression), and
  **beta** from a year of daily returns on SPY's; otherwise the vendor's last values.
- **mart.security_latest** keeps its seventeen columns and reads all this;
  mart.v_security_profile's classification follows; the missing-classification check
  reads what readers see.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.dq import checks, recheck
from fafnir.ingest import fundamentals_map as fm
from fafnir.ingest import sharadar_map

pytestmark = pytest.mark.integration

HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
TODAY = date.today()

SECURITY_LATEST_COLUMNS = [
    "security_id",
    "symbol",
    "company_name",
    "asset_type",
    "exchange_code",
    "sector_name",
    "industry_name",
    "currency",
    "country",
    "is_actively_trading",
    "is_etf",
    "is_fund",
    "market_cap_usd",
    "beta",
    "last_trade_date",
    "last_close",
    "last_volume",
]


def _security(
    db,
    symbol,
    *,
    sector=None,
    industry=None,
    source="fmp",
    currency="USD",
    market_cap=None,
    beta=None,
):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NASDAQ",
        sector_id=repo.get_or_create_sector(db, sector),
        industry_id=repo.get_or_create_industry(db, industry),
        currency=currency,
        market_cap_usd=market_cap,
        beta=beta,
        source=source,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _sharadar(db, sid, key, *, sector, industry, table="stocks", feed=True):
    """Sharadar's TICKERS row for ``key``, and a link to it."""
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
        " sector, industry) VALUES ('sharadar', %s, %s, %s, %s, %s)",
        (table, key, f"T{key}", sector, industry),
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=date(2000, 1, 3),
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=feed,
    )


def _classification(db, sid):
    return db.fetchone(
        "SELECT s.sector_name AS sector, i.industry_name AS industry,"
        "       c.attributes_source AS source"
        "  FROM core.v_security_classification c"
        "  LEFT JOIN ref.sector s ON s.sector_id = c.sector_id"
        "  LEFT JOIN ref.industry i ON i.industry_id = c.industry_id"
        " WHERE c.security_id = %s",
        (sid,),
    )


def _names(db):
    for name in ("Technology", "Healthcare", "Consumer Cyclical"):
        repo.get_or_create_sector(db, name)
    for name in (
        "Software - Application",
        "Drug Manufacturers - General",
        "Furnishings, Fixtures & Appliances",
    ):
        repo.get_or_create_industry(db, name)


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


def test_the_feeding_vendor_classifies(db):
    _names(db)
    fmp_only = _security(
        db, "ZZA", sector="Healthcare", industry="Drug Manufacturers - General"
    )
    fed = _security(
        db, "ZZB", sector="Healthcare", industry="Drug Manufacturers - General"
    )
    _sharadar(
        db, fed, "900201", sector=" technology ", industry="Software - Application"
    )
    alias = _security(db, "ZZC", source="sharadar")
    _sharadar(
        db, alias, "900202", sector="Healthcare", industry="Drug Manufacturers - Major"
    )
    commas = _security(db, "ZZD")
    _sharadar(db, commas, "900203", sector="consumer  cyclical", industry="Furnishings")
    unmapped = _security(
        db, "ZZE", sector="Healthcare", industry="Drug Manufacturers - General"
    )
    _sharadar(db, unmapped, "900204", sector="No Such Sector", industry="Nothing")
    fund = _security(db, "ZZF")
    _sharadar(db, fund, "900205", sector=None, industry=None, table="funds")
    unfed = _security(db, "ZZG", sector="Healthcare", industry=None)
    _sharadar(
        db,
        unfed,
        "900206",
        sector="Technology",
        industry="Software - Application",
        feed=False,
    )

    def seen(sid):
        r = _classification(db, sid)
        return r["sector"], r["industry"], r["source"]

    assert seen(fmp_only) == ("Healthcare", "Drug Manufacturers - General", "fmp")
    assert seen(fed) == ("Technology", "Software - Application", "sharadar")
    assert seen(alias) == ("Healthcare", "Drug Manufacturers - General", "sharadar")
    assert seen(commas) == (
        "Consumer Cyclical",
        "Furnishings, Fixtures & Appliances",
        "sharadar",
    )
    # Sharadar's sector does not map: the pair stays core.security's.
    assert seen(unmapped) == ("Healthcare", "Drug Manufacturers - General", "fmp")
    assert seen(fund) == (None, None, "fmp")
    # Linked but not fed: the feed decides, not the link.
    assert seen(unfed) == ("Healthcare", None, "fmp")
    # Nothing was written to core.security.
    fmp_sector = db.fetchval(
        "SELECT s.sector_name FROM core.security c JOIN ref.sector s"
        " ON s.sector_id = c.sector_id WHERE c.security_id = %s",
        (fed,),
    )
    assert fmp_sector == "Healthcare"


@pytest.mark.parametrize(
    "sector, industry",
    [
        ("Technology", "Software - Application"),
        ("TECHNOLOGY", "software -  application"),
        ("Healthcare", "Drug Manufacturers - Major"),
        ("Consumer Cyclical", "Furnishings,Fixtures & Appliances"),
        ("Consumer Cyclical", "Furnishings"),
        ("Technology", "Not An Industry"),
        ("Not A Sector", "Software - Application"),
        ("", ""),
    ],
)
def test_the_sql_map_is_map_classification(db, sector, industry):
    _names(db)
    sid = _security(db, "ZZM")
    _sharadar(db, sid, "900210", sector=sector, industry=industry)
    sectors, industries = repo.classification_names(db)
    expected = sharadar_map.map_classification(
        sector, industry, sectors=sectors, industries=industries
    )
    got = _classification(db, sid)
    if expected[0] is None:
        # Sharadar's sector does not map: core.security's pair (here none).
        assert (got["sector"], got["industry"], got["source"]) == (None, None, "fmp")
    else:
        assert (got["sector"], got["industry"]) == expected
        assert got["source"] == "sharadar"


def test_the_aliases_are_sharadar_maps():
    """The migration's alias list is sharadar_map.INDUSTRY_ALIASES, normalized."""
    sql = (
        maintenance.__file__.rsplit("src/", 1)[0]
        + "sql/migrations/0039_attribute_precedence.up.sql"
    )
    text = open(sql).read()
    block = text[text.index("LEFT JOIN (VALUES") : text.index(") al (alias_norm")]
    pairs = set()
    for line in block.splitlines()[1:]:
        line = line.strip().rstrip(",")
        if line.startswith("("):
            a, b = line.strip("()").split("', '")
            pairs.add((a.strip("'"), b.strip("'")))
    assert pairs == set(sharadar_map.INDUSTRY_ALIASES.items())


# ---------------------------------------------------------------------------
# Market cap and beta
# ---------------------------------------------------------------------------


def _balance(db, sid, fiscal, known_from, shares, *, known_to=None, superseded=None):
    row = {
        "security_id": sid,
        "vendor_key": "900220",
        "period_type": "annual",
        "fiscal_date": fiscal,
        "calendar_date": fiscal,
        "fiscal_year": fiscal.year,
        "fiscal_period": "FY",
        "version_no": 1,
        "known_from": known_from,
        "known_to": known_to,
        "knowledge_source": "filed",
        "filing_date": known_from,
        "loaded_at": T0 if superseded is None else T0 - timedelta(days=1),
        "superseded_at": superseded,
        "reported_currency": "USD",
        "content_hash": HASH,
        "shares_outstanding": shares,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES['balance_sheet']} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _bars(db, sid, closes):
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
            for day, c in closes
        ],
        source="sharadar",
    )


def _attributes(db, sid):
    return db.fetchone(
        "SELECT * FROM mart.v_security_attributes WHERE security_id = %s", (sid,)
    )


def test_market_cap_is_derived_in_the_close_terms(db):
    last = TODAY - timedelta(days=1)
    split = _security(db, "ZZS", market_cap=999)
    _balance(db, split, TODAY - timedelta(days=150), TODAY - timedelta(days=100), 100)
    # The vendor's first record of the same balance sheet, corrected: not read.
    _balance(
        db,
        split,
        TODAY - timedelta(days=150),
        TODAY - timedelta(days=100),
        5,
        superseded=T0,
    )
    # A later balance sheet, not yet known on the last close's day: not read.
    _balance(db, split, TODAY - timedelta(days=40), TODAY + timedelta(days=5), 7)
    repo.upsert_corporate_action(
        db,
        security_id=split,
        action_type="split",
        ex_date=TODAY - timedelta(days=50),
        split_numerator=2,
        split_denominator=1,
    )
    _bars(db, split, [(last, Decimal("20"))])

    a = _attributes(db, split)
    # 100 shares known before a 2-for-1, in the close's terms: 200 x 20.
    assert a["market_cap_usd"] == 4000
    assert (a["market_cap_source"], a["market_cap_as_of"]) == ("derived", last)
    assert a["market_cap_fiscal_date"] == TODAY - timedelta(days=150)

    # No shares in SF1: the vendor's snapshot, said to be one.
    plain = _security(db, "ZZT", market_cap=555)
    _bars(db, plain, [(last, Decimal("10"))])
    a = _attributes(db, plain)
    assert (a["market_cap_usd"], a["market_cap_source"], a["market_cap_as_of"]) == (
        555,
        "fmp",
        None,
    )
    # Traded in another currency: not a USD market cap.
    cad = _security(db, "ZZU", currency="CAD", market_cap=777)
    _balance(db, cad, TODAY - timedelta(days=150), TODAY - timedelta(days=100), 100)
    _bars(db, cad, [(last, Decimal("10"))])
    assert _attributes(db, cad)["market_cap_usd"] == 777
    # A balance sheet more than two years old: none.
    stale = _security(db, "ZZV")
    _balance(db, stale, TODAY - timedelta(days=900), TODAY - timedelta(days=850), 100)
    _bars(db, stale, [(last, Decimal("10"))])
    a = _attributes(db, stale)
    assert (a["market_cap_usd"], a["market_cap_source"]) == (None, None)


def _weekdays(n):
    """The last ``n`` weekdays before today, oldest first."""
    out, day = [], TODAY
    while len(out) < n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out[::-1]


def test_beta_is_derived_against_spy(db):
    days = _weekdays(150)
    moves = [0.01, -0.006, 0.004, -0.011, 0.008, 0.002, -0.003]
    spy_close, stock_close = 100.0, 50.0
    spy, stock = [], []
    for i, day in enumerate(days):
        if i:
            m = moves[i % len(moves)]
            spy_close *= 1 + m
            stock_close *= 1 + 2 * m + 0.0005
        spy.append((day, Decimal(f"{spy_close:.6f}")))
        stock.append((day, Decimal(f"{stock_close:.6f}")))
    spy_id = _security(db, "SPY", beta=1.0)
    twice = _security(db, "ZZX", beta=0.3)
    thin = _security(db, "ZZY", beta=1.5)
    _bars(db, spy_id, spy)
    _bars(db, twice, stock)
    _bars(db, thin, stock[-50:])

    a = _attributes(db, twice)
    assert a["beta_source"] == "derived"
    assert math.isclose(float(a["beta"]), 2.0, abs_tol=0.01)
    assert (a["beta_as_of"], a["beta_returns"]) == (days[-1], 149)
    assert math.isclose(float(_attributes(db, spy_id)["beta"]), 1.0, abs_tol=1e-9)
    # Fewer than 120 returns: the vendor's, said to be.
    a = _attributes(db, thin)
    assert (a["beta"], a["beta_source"], a["beta_as_of"]) == (1.5, "fmp", None)


# ---------------------------------------------------------------------------
# The two re-defined views, and the check
# ---------------------------------------------------------------------------


def test_security_latest_keeps_its_columns_and_reads_the_attributes(db):
    _names(db)
    fmp_only = _security(
        db,
        "ZZP",
        sector="Healthcare",
        industry="Drug Manufacturers - General",
        market_cap=1234.5,
        beta=0.8,
    )
    fed = _security(db, "ZZQ", sector="Healthcare", market_cap=10)
    _sharadar(db, fed, "900230", sector="Technology", industry="Software - Application")
    _balance(db, fed, TODAY - timedelta(days=150), TODAY - timedelta(days=100), 100)
    _bars(db, fed, [(TODAY - timedelta(days=1), Decimal("3"))])
    maintenance.refresh_marts(db, views=["mart.security_latest"])

    with db.conn.cursor() as cur:
        cur.execute("SELECT * FROM mart.security_latest LIMIT 0")
        assert [d.name for d in cur.description] == SECURITY_LATEST_COLUMNS
    rows = {
        r["security_id"]: r
        for r in db.fetchall("SELECT * FROM mart.security_latest ORDER BY 1")
    }
    # A security FMP feeds reads as it did under 0010.
    old = rows[fmp_only]
    assert (old["sector_name"], old["industry_name"]) == (
        "Healthcare",
        "Drug Manufacturers - General",
    )
    assert (old["market_cap_usd"], old["beta"]) == (Decimal("1234.50"), Decimal("0.8"))
    assert old["last_trade_date"] is None
    # One Sharadar feeds: its classification, and a derived market cap.
    new = rows[fed]
    assert (new["sector_name"], new["industry_name"]) == (
        "Technology",
        "Software - Application",
    )
    assert new["market_cap_usd"] == Decimal("300.00")
    # duk's company summary and the panel read the same classification.
    profile = db.fetchone(
        "SELECT sector_name, industry_name, market_cap_usd FROM mart.v_security_profile"
        " WHERE security_id = %s",
        (fed,),
    )
    assert (profile["sector_name"], profile["industry_name"]) == (
        "Technology",
        "Software - Application",
    )
    assert profile["market_cap_usd"] == Decimal("10.00")  # still the snapshot


def test_the_classification_check_reads_what_readers_see(db):
    _names(db)
    # Unclassified by FMP, classified by Sharadar: nothing to flag.
    covered = _security(db, "ZZH")
    _sharadar(
        db, covered, "900240", sector="Technology", industry="Software - Application"
    )
    # Unclassified by both: flagged.
    bare = _security(db, "ZZI")
    _sharadar(db, bare, "900241", sector="Shiny New Sector", industry="Shiny")
    checks.check_missing_classification(db)
    flagged = {
        r["security_id"]
        for r in db.fetchall(
            "SELECT security_id FROM ops.data_quality_flag"
            " WHERE check_name = 'security_missing_classification'"
            " AND resolved_at IS NULL"
        )
    }
    assert flagged == {bare}

    # fafnir learns the names: the flag no longer holds.
    repo.get_or_create_sector(db, "Shiny New Sector")
    repo.get_or_create_industry(db, "Shiny")
    (result,) = recheck.recheck(db, checks=["security_missing_classification"])
    assert result.stale == 1
