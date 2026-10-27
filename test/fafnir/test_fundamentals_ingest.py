"""
`fafnir ingest fundamentals` against a database (#90): integration tests (needs
FAFNIR_TEST_DSN). Every row is synthetic: no vendor data.

The plan's §10 integration list, as far as the loader goes: idempotency, a
restatement (two versions, chained intervals, a flag), a vendor correction (two rows
of one version), a split after load (no version), a period with no first-reported
row (estimated), quarantine, and the look-ahead and version-chain invariants.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals as fu
from fafnir.ingest import sharadar_bulk as bulk
from fafnir.sources.sharadar import SharadarClient

pytestmark = pytest.mark.integration

D = Decimal
Y = date
BACKFILL_DAY = Y(2026, 1, 5)
NIGHT = Y(2026, 10, 25)
KEY = "900001"

COLUMNS = (
    "ticker,dimension,calendardate,date,reportperiod,fiscalperiod,lastupdated,"
    "revenue,netinc,netinccmn,shareswa,shareswadil,sharesbas,assets,liabilities,"
    "equity,ncfo,capex,eps,pe"
).split(",")
DEFAULTS = {
    "revenue": "1000",
    "netinc": "100",
    "netinccmn": "100",
    "shareswa": "2000000",
    "shareswadil": "2020000",
    "sharesbas": "2000000",
    "assets": "5000",
    "liabilities": "3000",
    "equity": "2000",
    "ncfo": "200",
    "capex": "-50",
    "eps": "0.05",
    "pe": "12.345",
}
ACTIONS = "date,action,ticker,name,value,contraticker,contraname\n"


def _row(dim, period, filed, label, *, ticker="ZZA", stamp="2026-01-02", **values):
    vals = {**DEFAULTS, **{k: str(v) for k, v in values.items()}}
    head = {
        "ticker": ticker,
        "dimension": dim,
        "calendardate": period,
        "date": filed if dim.startswith("AR") else period,
        "reportperiod": period,
        "fiscalperiod": label,
        "lastupdated": stamp,
    }
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


def _security(db, symbol, key, *, valid_from=Y(1990, 1, 2), valid_to=None, ticker=None):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        source="sharadar",
    )
    for table in ("stocks", "fundamentals"):
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
            " currency) VALUES ('sharadar', %s, %s, %s, 'USD') ON CONFLICT DO NOTHING",
            (table, key, ticker or symbol),
        )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=valid_from,
        match_method="minted",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=valid_to is None,
    )
    if valid_to is not None:
        db.execute(
            "UPDATE core.security_vendor_xref SET valid_to = %s WHERE vendor_key = %s",
            (valid_to, key),
        )
    return sid


def _rows(db, table="core.income_statement", sid=None):
    return db.fetchall(
        f"SELECT * FROM {table} WHERE (%s::bigint IS NULL OR security_id = %s)"
        " ORDER BY security_id, period_type, fiscal_date, version_no, loaded_at",
        (sid, sid),
    )


def _flags(db, check=None):
    return db.fetchall(
        "SELECT * FROM ops.data_quality_flag WHERE (%s::text IS NULL OR check_name = %s)"
        " ORDER BY dq_flag_id",
        (check, check),
    )


def _invariants(db):
    """The standing tests: nothing known before its period ended, and every period's
    current versions chain with no gap or overlap, only the last one open."""
    for table in repo.STATEMENT_TABLES:
        assert (
            db.fetchval(f"SELECT count(*) FROM {table} WHERE known_from < fiscal_date")
            == 0
        )
        broken = db.fetchval(f"""
            SELECT count(*) FROM (
                SELECT known_to,
                       lead(known_from) OVER w AS next_from,
                       row_number() OVER w = count(*) OVER (
                           PARTITION BY security_id, period_type, fiscal_date) AS last
                  FROM {table} WHERE superseded_at IS NULL
                WINDOW w AS (PARTITION BY security_id, period_type, fiscal_date
                             ORDER BY version_no)) c
             WHERE (last AND known_to IS NOT NULL)
                OR (NOT last AND known_to IS DISTINCT FROM next_from)
            """)
        assert broken == 0, table


def _history():
    """A company: 2020-Q1 as filed and unrestated; FY2019 restated; 2019-Q4 with no
    first-reported row; a 1995 year (before the floor); and a trailing row."""
    return _sf1(
        _row("ARQ", "2020-03-31", "2020-05-01", "2020-Q1"),
        _row("MRQ", "2020-03-31", "", "2020-Q1"),
        _row("ARY", "2019-12-31", "2020-02-20", "2019-FY", revenue=4000),
        _row("MRY", "2019-12-31", "", "2019-FY", revenue=3900, sharesbas=1),
        _row("MRQ", "2019-12-31", "", "2019-Q4"),
        _row("ARY", "1995-12-31", "1996-03-15", "1995-FY"),
        _row("ART", "2020-03-31", "2020-05-01", "2020-Q1"),
    )


def _backfilled(db, tmp_path):
    sid = _security(db, "ZZA", KEY)
    _record(db, tmp_path, "fundamentals", _history())
    # A 2-for-1 after every filing: SF1's counts are in its terms.
    _record(db, tmp_path, "actions", ACTIONS + "2021-01-04,split,ZZA,ZZA Co,2.0,,\n")
    result = fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    return sid, result


def test_the_backfill_loads_versions_on_two_timelines(db, tmp_path):
    sid, result = _backfilled(db, tmp_path)
    assert (result.keys, result.securities, result.read) == (1, 1, 7)
    assert result.counts["trailing_or_other_dimension"] == 1
    income = {
        (r["period_type"], r["fiscal_date"], r["version_no"]): r
        for r in _rows(db, sid=sid)
    }
    assert len(income) == 5

    q1 = income[("quarter", Y(2020, 3, 31), 1)]
    assert (q1["knowledge_source"], q1["known_from"], q1["known_to"]) == (
        "filed",
        Y(2020, 5, 1),
        None,
    )
    assert (q1["fiscal_year"], q1["fiscal_period"], q1["reported_currency"]) == (
        2020,
        "Q1",
        "USD",
    )
    # Stored in the terms of the filing: SF1's 2,000,000 before the 2-for-1.
    assert q1["weighted_average_shares"] == D("1000000.0000")
    assert q1["extra"] == {"vendor_metric": {"eps": 0.05, "pe": 12.345}}
    assert q1["source"] == "sharadar" and q1["ingestion_run_id"] == result.run_id

    fy1, fy2 = (
        income[("annual", Y(2019, 12, 31), 1)],
        income[("annual", Y(2019, 12, 31), 2)],
    )
    assert (fy1["revenue"], fy1["known_from"], fy1["known_to"]) == (
        D("4000.00"),
        Y(2020, 2, 20),
        BACKFILL_DAY,
    )
    assert (fy2["revenue"], fy2["known_from"], fy2["knowledge_source"]) == (
        D("3900.00"),
        BACKFILL_DAY,
        "observed",
    )
    # Observed today: today's terms, so as given.
    assert fy2["weighted_average_shares"] == D("2000000.0000")
    bal = {
        (r["period_type"], r["fiscal_date"], r["version_no"]): r
        for r in _rows(db, "core.balance_sheet", sid)
    }
    # Only the income statement was restated; the cover count is never compared.
    assert ("annual", Y(2019, 12, 31), 2) not in bal

    q4 = [r for r in _rows(db, sid=sid) if r["fiscal_period"] == "Q4"][0]
    # No first-reported row: known from the next filing, FY2019's 10-K.
    assert (q4["knowledge_source"], q4["known_from"], q4["filing_date"]) == (
        "estimated",
        Y(2020, 2, 20),
        None,
    )
    q4_bal = [
        r for r in _rows(db, "core.balance_sheet", sid) if r["fiscal_period"] == "Q4"
    ]
    assert q4_bal[0]["shares_outstanding"] is None
    # Rows before the 1998 floor load.
    assert income[("annual", Y(1995, 12, 31), 1)]["known_from"] == Y(1996, 3, 15)

    assert result.counts["income:restated"] == 1
    # Rows, one per table: FY2019 filed, its restated income, the estimated Q4.
    assert result.by_decade["2010s"] == {"filed": 3, "observed": 1, "estimated": 3}
    (restated,) = _flags(db, fu.RESTATEMENT)
    assert restated["severity"] == "info"  # a backfill's history, not news
    assert restated["detail"]["largest"][0]["moved"] == {
        "revenue": ["4000.00", "3900.00"]
    }
    assert len(_flags(db, fu.ESTIMATED_FLAG)) == 1
    wm = db.fetchval(
        "SELECT last_loaded_date FROM ops.load_watermark"
        " WHERE source = 'sharadar' AND endpoint = 'SF1:ARY' AND security_id = %s",
        (sid,),
    )
    assert wm == Y(2019, 12, 31)
    run = db.fetchone(
        "SELECT status, endpoint, params FROM ops.ingestion_run"
        " WHERE ingestion_run_id = %s",
        (result.run_id,),
    )
    assert (run["status"], run["endpoint"]) == ("success", "fundamentals")
    assert run["params"]["written"] == result.written == 5 + 4 + 4
    _invariants(db)


def test_a_rerun_of_the_backfill_writes_nothing(db, tmp_path):
    _backfilled(db, tmp_path)
    before = {t: _rows(db, t) for t in repo.STATEMENT_TABLES}
    flags = len(_flags(db))
    again = fu.load(db, tmp_path, backfill=True, as_of=NIGHT)
    assert again.written == 0 and again.securities == 0
    assert {t: _rows(db, t) for t in repo.STATEMENT_TABLES} == before
    assert len(_flags(db)) == flags


def test_the_nightly_restates_corrects_and_ignores_a_split(db, tmp_path):
    sid, _ = _backfilled(db, tmp_path)
    # A 3-for-1 since the backfill: SF1 restates the counts in the new terms.
    _record(
        db,
        tmp_path,
        "actions",
        ACTIONS
        + "2021-01-04,split,ZZA,ZZA Co,2.0,,\n"
        + "2026-06-01,split,ZZA,ZZA Co,3.0,,\n",
    )
    x3 = {"shareswa": 6000000, "shareswadil": 6060000, "sharesbas": 6000000}
    delta = _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            # Re-expressed only.
            _row(
                "ARQ", "2020-03-31", "2020-05-01", "2020-Q1", stamp="2026-10-24", **x3
            ),
            # Restated again.
            _row(
                "MRY",
                "2019-12-31",
                "",
                "2019-FY",
                stamp="2026-10-24",
                revenue=3850,
                **x3,
            ),
            # The vendor fixed its record of the filing.
            _row(
                "ARY",
                "2019-12-31",
                "2020-02-20",
                "2019-FY",
                stamp="2026-10-24",
                revenue=4000,
                netinc=101,
                **x3,
            ),
        ),
        kind="delta",
        since="2026-10-24",
    )
    result = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert result.files == [delta] and result.through == delta

    fy = [
        r
        for r in _rows(db, sid=sid)
        if r["fiscal_period"] == "FY" and r["fiscal_year"] == 2019
    ]
    by = {(r["version_no"], r["superseded_at"] is None): r for r in fy}
    # The correction: two rows of version 1, one current, the same interval.
    old, new = by[(1, False)], by[(1, True)]
    assert (old["net_income"], new["net_income"]) == (D("100.00"), D("101.00"))
    assert (new["known_from"], new["known_to"]) == (old["known_from"], old["known_to"])
    assert new["weighted_average_shares"] == D("1000000.0000")
    # The restatement: version 3 from tonight, version 2 closed on it.
    assert by[(2, True)]["known_to"] == NIGHT
    v3 = by[(3, True)]
    assert (v3["revenue"], v3["known_from"], v3["knowledge_source"]) == (
        D("3850.00"),
        NIGHT,
        "observed",
    )
    assert v3["weighted_average_shares"] == D("6000000.0000")  # tonight's terms
    # The split alone moved nothing.
    q1 = [r for r in _rows(db, sid=sid) if r["fiscal_period"] == "Q1"]
    assert len(q1) == 1
    assert result.counts["income:unchanged"] == 1

    restated = _flags(db, fu.RESTATEMENT)
    assert [f["severity"] for f in restated] == ["info", "warn"]
    (corrected,) = _flags(db, fu.CORRECTION)
    assert corrected["detail"]["largest"][0]["moved"] == {
        "net_income": ["100.00", "101.00"]
    }
    run = db.fetchone(
        "SELECT endpoint, params FROM ops.ingestion_run WHERE ingestion_run_id = %s",
        (result.run_id,),
    )
    assert run["endpoint"] == "fundamentals:nightly"
    assert run["params"]["through"] == {"fundamentals": delta}
    _invariants(db)

    # The next night has nothing pending.
    again = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert (again.files, again.read, again.written) == ([], 0, 0)


def test_a_nightly_for_some_securities_leaves_the_deltas_pending(db, tmp_path):
    sid, _ = _backfilled(db, tmp_path)
    full = repo.current_sharadar_full(db, "fundamentals")["file_id"]
    delta = _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(_row("MRQ", "2020-03-31", "", "2020-Q1", revenue=1)),
        kind="delta",
        since="2026-10-24",
    )
    some = fu.load(db, tmp_path, backfill=False, security_ids=[sid], as_of=NIGHT)
    assert some.through == full and some.written == 1  # and one close
    assert fu.pending_deltas(db) == (
        [repo.sharadar_file(db, delta)],
        full,
    )
    quarters = fu.load(db, tmp_path, backfill=False, period="quarter", as_of=NIGHT)
    assert quarters.through == full


class _FakeSF1(SharadarClient):
    """Answers SF1 as the table API does (``lastupdated.gte``, ``from``/``to`` on
    the filing date, ``sort=date``) from ``rows``, written as :func:`_row` writes
    them."""

    def __init__(self, *rows):
        super().__init__("synthetic-key", rate_per_min=100_000)
        self.rows = [
            {c: v or None for c, v in zip(COLUMNS, r.rstrip("\n").split(","))}
            for r in rows
        ]

    def _call(self, table, params=None):
        assert table == "SF1" and params["sort"] == "date"
        rows = sorted(
            (
                r
                for r in self.rows
                if r["lastupdated"] >= params["lastupdated.gte"]
                and params["from"] <= r["date"] <= params["to"]
            ),
            key=lambda r: r["date"],
        )[: params["limit"]]
        payload = {
            "datatable": {
                "data": [[r[c] for c in COLUMNS] for r in rows],
                "columns": [{"name": c} for c in COLUMNS],
            }
        }
        return payload, 200, 100


def test_the_nights_sf1_delta_is_what_the_nightly_applies(db, tmp_path):
    sid, _ = _backfilled(db, tmp_path)
    today = date.today().isoformat()
    client = _FakeSF1(
        _row("MRY", "2019-12-31", "", "2019-FY", stamp=today, revenue=3850),
        _row("ARQ", "2020-03-31", "2020-05-01", "2020-Q1"),  # an old stamp
    )
    fetched = bulk.fetch_delta(db, client, "SF1", tmp_path)
    assert (fetched.outcome, fetched.rows) == ("recorded", 1)
    assert fu.pending_deltas(db)[0] == [repo.sharadar_file(db, fetched.file_id)]

    result = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert (result.files, result.through) == ([fetched.file_id], fetched.file_id)
    (fy,) = [
        r
        for r in _rows(db, sid=sid)
        if r["fiscal_period"] == "FY"
        and r["fiscal_year"] == 2019
        and r["superseded_at"] is None
        and r["known_to"] is None
    ]
    assert (fy["version_no"], fy["revenue"], fy["known_from"]) == (
        3,
        D("3850.00"),
        NIGHT,
    )
    assert fu.pending_deltas(db) == ([], fetched.file_id)


def test_the_nightly_waits_for_a_backfill(db, tmp_path):
    sid = _security(db, "ZZA", KEY)
    _record(db, tmp_path, "fundamentals", _history())
    _record(db, tmp_path, "actions", ACTIONS)
    delta = _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(_row("ARQ", "2020-06-30", "2020-08-01", "2020-Q2")),
        kind="delta",
        since="2026-10-24",
    )
    runs = "SELECT count(*) FROM ops.ingestion_run"

    def waits():
        before = db.fetchval(runs)
        result = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
        assert (result.waiting, result.read, result.run_id) == (True, 0, None)
        assert db.fetchval(runs) == before  # no run, so no through
        return fu.pending_deltas(db)[0] == [repo.sharadar_file(db, delta)]

    # Before any backfill: its delta waits for one.
    assert waits()
    assert not _rows(db)

    # A backfill for some periods is a backfill, and not a whole one.
    some = fu.load(db, tmp_path, backfill=True, period="annual", as_of=BACKFILL_DAY)
    whole = fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    flags = {
        r["ingestion_run_id"]: r["params"]["whole"]
        for r in db.fetchall(
            "SELECT ingestion_run_id, params FROM ops.ingestion_run "
            "WHERE endpoint = 'fundamentals'"
        )
    }
    assert flags == {some.run_id: False, whole.run_id: True}
    applied = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert not applied.waiting and applied.files == [delta]

    # A reset empties the statements and keeps the runs: it waits again.
    for table in ("cash_flow_statement", "balance_sheet", "income_statement"):
        db.execute(f"DELETE FROM core.{table}")
    result = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert result.waiting and not _rows(db, sid=sid)


def test_a_backfill_or_nightly_that_quarantined_rows_has_finished(db, tmp_path):
    # A run that quarantines a row ends 'partial', and still loaded the rest: the
    # nightly applies its deltas after it. On production the backfill quarantined
    # SF1's duplicates and the next night waited for a backfill (2026-10-27).
    def twins(period, filed, label, **stamp):
        return (
            _row("ARQ", period, filed, label, **stamp),
            _row("ARQ", period, filed, label, revenue=1001, **stamp),
        )

    def status(run_id):
        return db.fetchval(
            "SELECT status FROM ops.ingestion_run WHERE ingestion_run_id = %s",
            (run_id,),
        )

    _security(db, "ZZA", KEY)
    full = _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row("ARY", "2020-12-31", "2021-02-01", "2020-FY"),
            *twins("2020-09-30", "2020-11-02", "2020-Q3"),
        ),
    )
    _record(db, tmp_path, "actions", ACTIONS)
    backfill = fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    assert status(backfill.run_id) == "partial"
    assert fu.backfilled(db)
    assert fu.pending_deltas(db) == ([], full)

    delta = _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row("ARQ", "2021-03-31", "2021-05-03", "2021-Q1", stamp="2026-10-24"),
            *twins("2021-06-30", "2021-08-02", "2021-Q2", stamp="2026-10-24"),
        ),
        kind="delta",
        since="2026-10-24",
    )
    night = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert not night.waiting and night.files == [delta]
    assert status(night.run_id) == "partial"
    assert [r["fiscal_date"] for r in _rows(db) if r["period_type"] == "quarter"] == [
        Y(2021, 3, 31)
    ]
    # Its file is applied: the next night does not read it again.
    assert fu.pending_deltas(db) == ([], delta)
    again = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    assert (again.files, again.read, again.written) == ([], 0, 0)


def test_bad_rows_are_quarantined_once_and_the_rest_loads(db, tmp_path):
    sid = _security(db, "ZZB", "900002")
    dead = _security(
        db, "ZZD", "900003", valid_from=Y(2001, 1, 2), valid_to=Y(2003, 12, 31)
    )
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row("ARQ", "2020-03-31", "2020-05-01", "2020Q1", ticker="ZZB"),
            _row("ARQ", "2020-06-30", "2020-06-29", "2020-Q2", ticker="ZZB"),
            _row("ARQ", "2020-09-30", "2020-11-02", "2020-Q3", ticker="ZZB"),
            _row(
                "ARQ", "2020-09-30", "2020-11-02", "2020-Q3", ticker="ZZB", revenue=1001
            ),
            _row("ARY", "2020-12-31", "2021-02-01", "2020-FY", ticker="ZZB"),
            # Outside its only (closed) link: no security for it.
            _row("ARY", "2010-12-31", "2011-02-01", "2010-FY", ticker="ZZD"),
            _row("ARY", "2002-12-31", "2003-02-01", "2002-FY", ticker="ZZD"),
        ),
    )
    _record(db, tmp_path, "actions", ACTIONS)
    result = fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    assert [r["fiscal_period"] for r in _rows(db, sid=sid)] == ["FY"]
    assert [r["fiscal_period"] for r in _rows(db, sid=dead)] == ["FY"]
    checks = sorted(
        f["check_name"] for f in _flags(db) if f["check_name"] != fu.UNLINKED
    )
    # Two different first reports filed on one day are twins: one condition, one
    # flag, and both rows quarantined.
    assert checks == sorted([fu.MISSING_LABEL, fu.FILED_EARLY, fu.DUPLICATE])
    (unlinked,) = _flags(db, fu.UNLINKED)
    assert (unlinked["severity"], unlinked["detail"]["rows"]) == ("info", 1)
    assert result.counts["unlinked"] == 1
    run = db.fetchone(
        "SELECT status, rows_quarantined FROM ops.ingestion_run"
        " WHERE ingestion_run_id = %s",
        (result.run_id,),
    )
    assert (run["status"], run["rows_quarantined"]) == ("partial", 4)
    n = len(_flags(db))
    fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    assert len(_flags(db)) == n


def test_a_period_listed_once_per_filing_loads_its_first(db, tmp_path):
    # SF1 lists a first-reported period once per filing that reported it: here the
    # 10-K/A after the 10-K, and a quarter listed twice with only its stamp apart.
    sid = _security(db, "ZZA", KEY)
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row("ARY", "2020-12-31", "2021-03-10", "2020-FY", revenue=1200),
            _row("ARY", "2020-12-31", "2021-02-01", "2020-FY"),
            _row("MRY", "2020-12-31", "", "2020-FY", revenue=1200),
            _row("ARQ", "2020-09-30", "2020-11-02", "2020-Q3"),
            _row("ARQ", "2020-09-30", "2020-11-02", "2020-Q3", stamp="2026-01-03"),
        ),
    )
    _record(db, tmp_path, "actions", ACTIONS)
    result = fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY)
    loaded = [
        (
            r["period_type"],
            r["version_no"],
            r["knowledge_source"],
            r["known_from"],
            r["revenue"],
        )
        for r in _rows(db, sid=sid)
    ]
    # The 10-K is the first report, known from its filing; the amendment's figure
    # arrives as the restated row's, a version known from the load day.
    assert loaded == [
        ("annual", 1, "filed", Y(2021, 2, 1), D("1000")),
        ("annual", 2, "observed", BACKFILL_DAY, D("1200")),
        ("quarter", 1, "filed", Y(2020, 11, 2), D("1000")),
    ]
    assert (result.read, result.counts["later_filing"]) == (5, 2)
    assert not _flags(db, fu.DUPLICATE) and not _flags(db, fu.ESTIMATED_FLAG)
    _invariants(db)
    assert fu.load(db, tmp_path, backfill=True, as_of=BACKFILL_DAY).written == 0


def test_the_nightly_loads_the_first_filing_of_a_period_listed_twice(db, tmp_path):
    sid, _ = _backfilled(db, tmp_path)
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(
            _row(
                "ARQ",
                "2020-06-30",
                "2020-08-14",
                "2020-Q2",
                stamp="2026-10-24",
                revenue=1100,
            ),
            _row("ARQ", "2020-06-30", "2020-07-30", "2020-Q2", stamp="2026-10-24"),
        ),
        kind="delta",
        since="2026-10-24",
    )
    result = fu.load(db, tmp_path, backfill=False, as_of=NIGHT)
    (q2,) = [r for r in _rows(db, sid=sid) if r["fiscal_date"] == Y(2020, 6, 30)]
    assert (q2["knowledge_source"], q2["known_from"], q2["revenue"]) == (
        "filed",
        Y(2020, 7, 30),
        D("1000"),
    )
    assert result.counts["later_filing"] == 1 and not _flags(db, fu.DUPLICATE)
    _invariants(db)


def test_a_backfill_needs_a_file_and_rows(db, tmp_path):
    _security(db, "ZZA", KEY)
    with pytest.raises(fu.FundamentalsError, match="no current SF1 file"):
        fu.load(db, tmp_path, backfill=True)
    _record(
        db,
        tmp_path,
        "fundamentals",
        _sf1(_row("ARQ", "2020-03-31", "2020-05-01", "2020-Q1", ticker="ZZQ")),
    )
    with pytest.raises(fu.FundamentalsError, match="read no SF1 row"):
        fu.load(db, tmp_path, backfill=True)
    run = db.fetchone("SELECT status FROM ops.ingestion_run ORDER BY 1 DESC LIMIT 1")
    assert run["status"] == "failed"


def test_a_dry_run_counts_and_writes_nothing(db, tmp_path):
    _security(db, "ZZA", KEY)
    _record(db, tmp_path, "fundamentals", _history())
    result = fu.load(db, tmp_path, backfill=True, dry_run=True, as_of=BACKFILL_DAY)
    assert result.securities == 1 and result.counts["income:inserted"] == 4
    assert not _rows(db) and not _flags(db)
    assert db.fetchval("SELECT count(*) FROM ops.ingestion_run") == 0


def test_a_merge_moves_statements_and_a_fold_refuses_them(db, tmp_path):
    victim, _ = _backfilled(db, tmp_path)
    survivor = repo.upsert_security(
        db,
        primary_symbol="ZZS",
        company_name="ZZS Co",
        exchange_code="NYSE",
        source="fmp",
    )
    assert repo.security_has_history(db, victim)
    assert not repo.fold_empty_security(db, victim_id=victim, survivor_id=survivor)
    report = repo.merge_security(db, victim_id=victim, survivor_id=survivor, force=True)
    assert report.statements_moved == 5 + 4 + 4
    assert not _rows(db, sid=victim) and len(_rows(db, sid=survivor)) == 5


def test_the_command(db, tmp_path, monkeypatch):
    _security(db, "ZZA", KEY)
    _record(db, tmp_path, "fundamentals", _history())
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main, ["--config", str(rc), "ingest", "fundamentals", *args]
        )

    out = run("--backfill", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing written. SF1 file: 1. 1 vendor keys in scope; 7 rows" in (
        out.output
    )
    out = run("--backfill", "--symbols", "ZZA", "--period", "annual")
    assert out.exit_code == 0, out.output
    assert "rows written to 1 securities" in out.output
    assert {r["period_type"] for r in _rows(db)} == {"annual"}
    assert "No security for: NOPE" in run("--backfill", "--symbols", "NOPE").output
    out = run()
    assert out.exit_code == 0, out.output
    assert "SF1 delta file(s): none." in out.output
