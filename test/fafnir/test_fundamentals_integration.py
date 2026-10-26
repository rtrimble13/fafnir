"""
Fundamentals end to end (#91): golden values, the two timelines read point in time,
NULL against 0, idempotency, a restatement, and the upgrade path. Integration tests
(FAFNIR_TEST_DSN).

No vendor data. The AAPL and MSFT rows are built from figures in the companies' own
10-K filings, which are public record: AAPL FY2020 (filed 2020-10-30) and FY2015
(filed 2015-10-28), MSFT FY2015 (filed 2015-07-31). Every restated value, and every
row of a ticker starting ZZ, is invented for the test.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from fafnir.db import migrate as m
from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir.ingest import fundamentals as fu

pytestmark = pytest.mark.integration

D = Decimal
Y = date
LOAD_DAY = Y(2026, 1, 5)
NIGHT = Y(2026, 10, 26)

COLUMNS = (
    "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
    "revenue,netinc,netinccmn,netincdis,prefdivis,rnd,shareswa,shareswadil,sharesbas,"
    "assets,ncfo,eps,pe"
).split(",")
ACTIONS = "date,action,ticker,name,value,contraticker,contraname\n"

# In SF1's terms: share counts in today's share terms, so AAPL's FY2015 counts carry
# the 4-for-1 of 2020-08-31 (5,753,421,000 basic shares as filed).
AAPL = (
    dict(
        dim="ARY",
        period="2020-09-26",
        filed="2020-10-30",
        label="2020-FY",
        revenue="274515000000",
        netinc="57411000000",
        shareswa="17352119000",
        shareswadil="17528214000",
    ),
    dict(
        dim="ARY",
        period="2015-09-26",
        filed="2015-10-28",
        label="2015-FY",
        revenue="233715000000",
        netinc="53394000000",
        shareswa="23013684000",
        shareswadil="23172276000",
    ),
)
MSFT = dict(
    dim="ARY",
    period="2015-06-30",
    filed="2015-07-31",
    label="2015-FY",
    revenue="93580000000",
    netinc="12193000000",
    shareswa="8177000000",
    shareswadil="8254000000",
)
#: Invented: what a restatement of MSFT's FY2015 looks like to the loader.
MSFT_RESTATED_REVENUE = "93456000000"


def _row(*, ticker, dim, period, filed, label, stamp="2026-01-02", **values):
    head = {
        "ticker": ticker,
        "dimension": dim,
        "calendardate": period,
        "date": filed if dim.startswith("AR") else period,
        "reportperiod": period,
        "fiscalperiod": label,
        "lastupdated": stamp,
    }
    vals = {"netinccmn": values.get("netinc", ""), **values}
    return ",".join({**head, **vals}.get(c, "") for c in COLUMNS) + "\n"


def _sf1(*rows):
    return ",".join(COLUMNS) + "\n" + "".join(rows)


def _record(db, tmp_path, table, body, *, kind="full", since=None):
    data = body.encode()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / f"{sha}.csv").write_bytes(data)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind=kind,
        filters={"lastupdated.gte": since} if kind == "delta" else {"years": "full"},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(data),
        rows=body.count("\n") - 1,
        columns=None,
        ingestion_run_id=None,
    )
    return entry["file_id"]


def _security(db, symbol, key, *, source="sharadar"):
    repo.ensure_exchange(db, "NASDAQ", "NASDAQ", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NASDAQ",
        source=source,
    )
    for table in ("stocks", "fundamentals"):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
            " currency) VALUES ('sharadar', %s, %s, %s, 'USD')",
            (table, key, symbol),
        )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=Y(1990, 1, 2),
        match_method="minted" if source == "sharadar" else "identifier",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    return sid


def _golden_files(db, tmp_path):
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            *(_row(ticker="AAPL", **r) for r in AAPL),
            _row(ticker="MSFT", **MSFT),
            _row(
                ticker="MSFT",
                **{**MSFT, "dim": "MRY", "revenue": MSFT_RESTATED_REVENUE},
            ),
            # A filing with an item it does not carry (0) and one that does not
            # apply (empty).
            _row(
                ticker="ZZN",
                dim="ARQ",
                period="2021-03-31",
                filed="2021-05-03",
                label="2021-Q1",
                revenue="10",
                netinc="1",
                prefdivis="0",
                netincdis="",
                rnd="0",
            ),
        ),
    )
    _record(
        db,
        tmp_path,
        "actions",
        ACTIONS
        + "2014-06-09,split,AAPL,Apple Inc,7.0,,\n"
        + "2020-08-31,split,AAPL,Apple Inc,4.0,,\n",
    )


def _in_force(db, sid, fiscal_date, as_of):
    """The income statement of a period as known at ``as_of``: ADR 0018's read."""
    return db.fetchone(
        """
        SELECT * FROM core.income_statement
         WHERE security_id = %s AND fiscal_date = %s AND superseded_at IS NULL
           AND known_from <= %s AND (known_to IS NULL OR %s < known_to)
        """,
        (sid, fiscal_date, as_of, as_of),
    )


def _snapshot(db):
    return {
        t: db.fetchall(f"SELECT * FROM {t} ORDER BY 1, 2, 3, 4, 5")
        for t in repo.STATEMENT_TABLES
    }


def test_golden_values_read_point_in_time(db, tmp_path):
    aapl = _security(db, "AAPL", "199059")
    msft = _security(db, "MSFT", "198508")
    _security(db, "ZZN", "900010")
    _golden_files(db, tmp_path)
    fu.load(db, tmp_path, backfill=True, as_of=LOAD_DAY)

    fy2020 = _in_force(db, aapl, Y(2020, 9, 26), Y(2020, 10, 30))
    assert fy2020["revenue"] == D("274515000000.00")
    assert fy2020["net_income"] == D("57411000000.00")
    assert fy2020["weighted_average_shares"] == D("17352119000")
    # Not known the day before the 10-K: the look-ahead tripwire, read.
    assert _in_force(db, aapl, Y(2020, 9, 26), Y(2020, 10, 29)) is None

    # FY2015's counts recovered from today's terms to the filing's.
    fy2015 = _in_force(db, aapl, Y(2015, 9, 26), Y(2016, 1, 4))
    assert fy2015["weighted_average_shares"] == D("5753421000")
    assert fy2015["weighted_average_shares_diluted"] == D("5793069000")
    assert fy2015["revenue"] == D("233715000000.00")

    # MSFT FY2015 in two versions: as filed until the load day, restated from it.
    filed = _in_force(db, msft, Y(2015, 6, 30), Y(2016, 1, 4))
    assert (filed["version_no"], filed["revenue"], filed["known_to"]) == (
        1,
        D("93580000000.00"),
        LOAD_DAY,
    )
    restated = _in_force(db, msft, Y(2015, 6, 30), LOAD_DAY)
    assert (restated["version_no"], restated["revenue"], restated["known_from"]) == (
        2,
        D(MSFT_RESTATED_REVENUE + ".00"),
        LOAD_DAY,
    )
    assert restated["knowledge_source"] == "observed"
    assert restated["filing_date"] is None


def test_null_is_not_zero_on_the_round_trip(db, tmp_path):
    sid = _security(db, "ZZN", "900010")
    _golden_files(db, tmp_path)
    fu.load(db, tmp_path, backfill=True, as_of=LOAD_DAY)
    row = db.fetchone(
        "SELECT preferred_dividends, net_income_discontinued, research_and_development,"
        " interest_expense FROM core.income_statement WHERE security_id = %s",
        (sid,),
    )
    assert row == {
        "preferred_dividends": D("0.00"),
        "net_income_discontinued": None,
        "research_and_development": D("0.00"),
        "interest_expense": None,  # not in the file at all
    }
    # Read back and loaded again, they are the same: no version.
    before = _snapshot(db)
    fu.load(db, tmp_path, backfill=True, as_of=NIGHT)
    assert _snapshot(db) == before


def test_load_twice_is_identical_and_a_mutation_restates(db, tmp_path):
    aapl = _security(db, "AAPL", "199059")
    _security(db, "MSFT", "198508")
    _security(db, "ZZN", "900010")
    _golden_files(db, tmp_path)
    fu.load(db, tmp_path, backfill=True, as_of=LOAD_DAY)
    before, flags = _snapshot(db), db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag"
    )
    again = fu.load(db, tmp_path, backfill=True, as_of=NIGHT)
    assert again.written == 0
    assert _snapshot(db) == before
    assert db.fetchval("SELECT count(*) FROM ops.data_quality_flag") == flags

    # One value moves in a restated row: two versions, one live, and a flag.
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row(
                ticker="AAPL",
                stamp="2026-10-25",
                **{**AAPL[0], "dim": "MRY", "netinc": "57400000000"},
            )
        ),
        kind="delta",
        since="2026-10-25",
    )
    fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    versions = db.fetchall(
        "SELECT version_no, known_from, known_to, net_income FROM core.income_statement"
        " WHERE security_id = %s AND fiscal_date = '2020-09-26'"
        " AND superseded_at IS NULL ORDER BY version_no",
        (aapl,),
    )
    assert versions == [
        {
            "version_no": 1,
            "known_from": Y(2020, 10, 30),
            "known_to": NIGHT,
            "net_income": D("57411000000.00"),
        },
        {
            "version_no": 2,
            "known_from": NIGHT,
            "known_to": None,
            "net_income": D("57400000000.00"),
        },
    ]
    flag = db.fetchone(
        "SELECT severity, detail FROM ops.data_quality_flag"
        " WHERE check_name = %s AND security_id = %s",
        (fu.RESTATEMENT, aapl),
    )
    assert flag["severity"] == "warn"
    assert flag["detail"]["largest"][0]["moved"] == {
        "net_income": ["57411000000.00", "57400000000.00"],
        "net_income_common": ["57411000000.00", "57400000000.00"],
    }


# ---------------------------------------------------------------------------
# The upgrade path: a warehouse before 0035, upgraded and then loaded
# ---------------------------------------------------------------------------

NEW_TABLES = set(repo.STATEMENT_TABLES) | {"ref.statement_line_item"}


def _tables(db) -> list[str]:
    return [f"{r['table_schema']}.{r['table_name']}" for r in db.fetchall("""
            SELECT table_schema, table_name FROM information_schema.tables
             WHERE table_schema IN ('core', 'ref', 'landing', 'ops', 'meta')
               AND table_type = 'BASE TABLE'
             ORDER BY 1, 2
            """) if f"{r['table_schema']}.{r['table_name']}" not in NEW_TABLES]


def _fingerprint(db, tables) -> dict:
    """Rows and an md5 of every row's text, per table, order-free."""
    return {
        t: db.fetchone(
            f"SELECT count(*) AS n, md5(coalesce(string_agg(x.t, '|' ORDER BY x.t), ''))"
            f" AS digest FROM (SELECT t::text AS t FROM {t} t) x"
        )
        for t in tables
    }


def _warehouse(db, tmp_path):
    """A small warehouse shaped like production: an FMP security linked to Sharadar
    and a dead issuer Sharadar minted, with bars across a split, actions, factors,
    profiles, flags, a watermark, a run, and the ledger's files."""
    live = _security(db, "AAPL", "199059", source="fmp")
    dead = _security(db, "ZZN", "900010")
    for sid, symbol in ((live, "AAPL"), (dead, "ZZN")):
        repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
        repo.upsert_company_profile(db, security_id=sid, description=f"{symbol} Inc.")
    bars = [
        (live, Y(2020, 8, 27), "500.04"),
        (live, Y(2020, 8, 28), "499.23"),
        (live, Y(2020, 8, 31), "129.04"),
        (live, Y(2020, 9, 1), "134.18"),
        (dead, Y(2021, 3, 31), "10.00"),
    ]
    for sid, day, close in bars:
        c = D(close)
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
            source="fmp" if sid == live else "sharadar",
        )
    repo.upsert_corporate_action(
        db,
        security_id=live,
        action_type="split",
        ex_date=Y(2020, 8, 31),
        split_numerator=4,
        split_denominator=1,
    )
    repo.upsert_corporate_action(
        db,
        security_id=live,
        action_type="dividend",
        ex_date=Y(2020, 8, 28),
        dividend_amount=0.82,
    )
    adjustments.adjust_all(db, security_ids=[live])
    repo.add_dq_flag_once(
        db, check_name="stale", security_id=dead, record_key={"last_date": "2021-03-31"}
    )
    repo.set_watermark(
        db, "fmp", "historical-price-eod/non-split-adjusted", Y(2020, 9, 1), live
    )
    _golden_files(db, tmp_path)
    return live, dead


def test_the_upgrade_and_the_first_load_leave_every_existing_table_alone(
    db, migrated_dsn, tmp_path
):
    later = [
        v
        for v, _, state in m.status(migrated_dsn)
        if v >= "0035" and state == "applied"
    ]
    m.rollback(migrated_dsn, steps=len(later))
    try:
        assert db.fetchval("SELECT to_regclass('core.income_statement')") is None
        live, _ = _warehouse(db, tmp_path)
        assert db.fetchval(
            "SELECT count(*) FROM core.adjustment_factor WHERE security_id = %s",
            (live,),
        )
        tables = _tables(db)
        before = _fingerprint(db, tables)
    finally:
        m.migrate(migrated_dsn)

    # The upgrade: nothing that existed moves, meta's migration record aside.
    upgraded = _fingerprint(db, tables)
    moved = {t for t in tables if upgraded[t] != before[t]}
    assert moved == {"meta.schema_migration"}

    # The first load writes the statements and its own bookkeeping, nothing else.
    result = fu.load(db, tmp_path, backfill=True, as_of=LOAD_DAY)
    assert result.written > 0
    loaded = _fingerprint(db, tables)
    moved = {t for t in tables if loaded[t] != upgraded[t]}
    assert moved <= {"ops.ingestion_run", "ops.data_quality_flag", "ops.load_watermark"}
    assert db.fetchval(
        "SELECT count(*) FROM core.income_statement WHERE security_id = %s", (live,)
    ) == len(AAPL)
