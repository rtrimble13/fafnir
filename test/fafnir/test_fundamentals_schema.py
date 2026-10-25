"""
Migration 0035 (#87): the statement tables and the line-item taxonomy.

Integration tests (FAFNIR_TEST_DSN). Every row is synthetic. They pin the shape the
plan (doc/plans/fundamentals-sf1.md §4.2) and ADR 0018 decided: two timelines, one
version in force per period, one current row per version, and the constraints that
make a look-ahead or an undated restatement unstorable.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from fafnir.db import migrate as m
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

VERSION = "0035"
PREFIX = {"income": "inc", "balance_sheet": "bal", "cash_flow": "cfs"}
HASH = hashlib.sha256(b"synthetic").hexdigest()


@pytest.fixture()
def sid(db):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    return repo.upsert_security(
        db,
        primary_symbol="ZZFUND",
        company_name="ZZ Fundamentals Corp",
        asset_type="equity",
        exchange_code="NYSE",
        is_actively_trading=True,
    )


def _row(sid, **kw):
    """A valid first-reported annual income-statement row; ``kw`` overrides."""
    row = {
        "security_id": sid,
        "vendor_key": "900001",
        "period_type": "annual",
        "fiscal_date": date(2015, 6, 30),
        "calendar_date": date(2015, 12, 31),
        "fiscal_year": 2015,
        "fiscal_period": "FY",
        "version_no": 1,
        "known_from": date(2015, 7, 31),
        "knowledge_source": "filed",
        "filing_date": date(2015, 7, 31),
        "content_hash": HASH,
        "revenue": 1000,
    }
    row.update(kw)
    return row


def _insert(db, row, table="core.income_statement"):
    cols = list(row)
    db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", list(fm.STATEMENTS))
def test_each_table_has_the_maps_columns_in_order(db, statement):
    table = fm.TABLES[statement].split(".")[1]
    cols = db.fetchall(
        "SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = 'core' "
        "AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    assert tuple(c["column_name"] for c in cols) == fm.columns(statement)
    by_name = {c["column_name"]: c for c in cols}
    for item in fm.STATEMENTS[statement]:
        c = by_name[item.column]
        assert c["data_type"] == "numeric"
        assert (
            f"NUMERIC({c['numeric_precision']},{c['numeric_scale']})" == item.sql_type
        )


def test_the_taxonomy_is_the_map(db):
    stored = {
        (r["statement"], r["column_name"]): r
        for r in db.fetchall("SELECT * FROM ref.statement_line_item")
    }
    expected = {(s, i.column): i for s, items in fm.STATEMENTS.items() for i in items}
    assert set(stored) == set(expected)
    for key, item in expected.items():
        r = stored[key]
        assert (
            r["vendor_field"],
            r["fmp_field"],
            r["item_class"],
            r["sign_convention"],
            r["is_subtotal"],
            r["description"],
        ) == (
            item.vendor_field,
            item.fmp_field,
            item.item_class,
            item.sign_convention,
            item.is_subtotal,
            item.description,
        ), key


@pytest.mark.parametrize("statement", list(fm.STATEMENTS))
def test_every_table_carries_the_same_constraints_and_indexes(db, statement):
    table = fm.TABLES[statement]
    checks = db.fetchall(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass "
        "AND contype = 'c' ORDER BY conname",
        (table,),
    )
    names = {c["conname"].split("_", 2)[2] for c in checks}
    assert names == {
        "period_type",
        "fiscal_period",
        "period_label",
        "period_start",
        "version",
        "known_after_period",
        "known_range",
        "knowledge_source",
        "filed",
        "restatement_observed",
        "superseded",
        "content_hash",
        "source",
    }
    assert all(c["conname"].startswith(f"ck_{PREFIX[statement]}_") for c in checks)
    short = table.split(".")[1]
    indexes = {
        r["indexname"]
        for r in db.fetchall(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'core' AND tablename = %s",
            (short,),
        )
    }
    assert {
        f"ux_{short}_current",
        f"ux_{short}_in_force",
        f"ix_{short}_pit",
        f"ix_{short}_known",
        f"ix_{short}_restated",
    } <= indexes


# ---------------------------------------------------------------------------
# What the constraints refuse
# ---------------------------------------------------------------------------


def test_a_valid_first_reported_row_stores(db, sid):
    _insert(db, _row(sid))
    stored = db.fetchone("SELECT * FROM core.income_statement")
    assert stored["known_to"] is None and stored["superseded_at"] is None
    assert stored["extra"] == {} and stored["source"] == "sharadar"


@pytest.mark.parametrize(
    "change, constraint",
    [
        # Known before the period ended: the look-ahead tripwire.
        (
            {"known_from": date(2015, 6, 29), "filing_date": date(2015, 6, 29)},
            "known_after_period",
        ),
        ({"known_to": date(2015, 7, 31)}, "known_range"),
        ({"filing_date": None}, "filed"),
        ({"known_from": date(2015, 8, 3)}, "filed"),
        ({"knowledge_source": "observed"}, "filed"),
        ({"knowledge_source": "accepted", "filing_date": None}, "knowledge_source"),
        ({"version_no": 2}, "restatement_observed"),
        (
            {"version_no": 0, "knowledge_source": "observed", "filing_date": None},
            "version",
        ),
        ({"fiscal_period": "Q4"}, "period_label"),
        ({"period_type": "quarter"}, "period_label"),
        ({"period_type": "monthly", "fiscal_period": "Q1"}, "period_type"),
        ({"period_start_date": date(2015, 6, 30)}, "period_start"),
        ({"content_hash": "not-a-hash"}, "content_hash"),
        ({"source": "fmp"}, "source"),
        (
            {
                "loaded_at": datetime(2026, 10, 25, tzinfo=timezone.utc),
                "superseded_at": datetime(2026, 10, 24, tzinfo=timezone.utc),
            },
            "superseded",
        ),
    ],
)
def test_the_constraints_refuse(db, sid, change, constraint):
    # Each case breaks exactly one constraint: Postgres names only the first it checks.
    with pytest.raises(psycopg.errors.CheckViolation, match=f"ck_inc_{constraint}"):
        _insert(db, _row(sid, **change))


def test_an_estimated_or_observed_first_version_has_no_filing_date(db, sid):
    _insert(
        db,
        _row(
            sid,
            knowledge_source="estimated",
            filing_date=None,
            known_from=date(2016, 7, 28),
        ),
    )
    _insert(
        db,
        _row(
            sid,
            fiscal_date=date(2016, 6, 30),
            calendar_date=date(2016, 12, 31),
            fiscal_year=2016,
            knowledge_source="observed",
            filing_date=None,
            known_from=date(2026, 10, 25),
        ),
    )
    assert db.fetchval("SELECT count(*) FROM core.income_statement") == 2


# ---------------------------------------------------------------------------
# The two timelines
# ---------------------------------------------------------------------------


def test_a_restatement_closes_the_version_before_it(db, sid):
    _insert(db, _row(sid))
    restated = _row(
        sid,
        version_no=2,
        knowledge_source="observed",
        filing_date=None,
        known_from=date(2026, 10, 25),
        revenue=1005,
    )
    # Two versions in force for one period are refused...
    with pytest.raises(
        psycopg.errors.UniqueViolation, match="ux_income_statement_in_force"
    ):
        _insert(db, restated)
    # ...so the restatement closes version 1 on its own known_from.
    db.execute(
        "UPDATE core.income_statement SET known_to = %s WHERE version_no = 1",
        (restated["known_from"],),
    )
    _insert(db, restated)
    rows = db.fetchall(
        "SELECT version_no, known_from, known_to FROM core.income_statement "
        "ORDER BY version_no"
    )
    assert rows[0]["known_to"] == rows[1]["known_from"]
    assert rows[1]["known_to"] is None
    in_force = lambda d: db.fetchval(  # noqa: E731 -- the PIT predicate, once
        "SELECT revenue FROM core.income_statement WHERE superseded_at IS NULL "
        "AND known_from <= %s AND (known_to IS NULL OR %s < known_to)",
        (d, d),
    )
    assert in_force(date(2018, 1, 1)) == 1000
    assert in_force(date(2026, 10, 25)) == 1005
    assert in_force(date(2015, 7, 30)) is None


def test_a_vendor_correction_supersedes_the_row_and_keeps_its_interval(db, sid):
    loaded = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
    _insert(db, _row(sid, loaded_at=loaded))
    corrected = _row(sid, loaded_at=loaded + timedelta(days=1), revenue=999)
    # Two current rows of one version are refused...
    with pytest.raises(
        psycopg.errors.UniqueViolation, match="ux_income_statement_current"
    ):
        _insert(db, corrected)
    # ...so the correction supersedes the row it replaces.
    db.execute(
        "UPDATE core.income_statement SET superseded_at = %s",
        (corrected["loaded_at"],),
    )
    _insert(db, corrected)
    current = db.fetchall(
        "SELECT revenue, known_from FROM core.income_statement WHERE superseded_at IS NULL"
    )
    assert current == [{"revenue": 999, "known_from": date(2015, 7, 31)}]
    assert db.fetchval("SELECT count(*) FROM core.income_statement") == 2


def test_a_security_holding_statements_cannot_be_deleted(db, sid):
    _insert(db, _row(sid))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM core.security WHERE security_id = %s", (sid,))


@pytest.mark.parametrize("statement", ["balance_sheet", "cash_flow"])
def test_the_other_statements_take_the_same_rows(db, sid, statement):
    item = fm.STATEMENTS[statement][0]
    row = _row(sid)
    del row["revenue"]
    row[item.column] = 42
    _insert(db, row, fm.TABLES[statement])
    assert db.fetchval(f"SELECT {item.column} FROM {fm.TABLES[statement]}") == 42


# ---------------------------------------------------------------------------
# The upgrade path: a database before 0035 migrates without touching a row
# ---------------------------------------------------------------------------


def _fingerprint(db) -> dict:
    out = {}
    for table, order in (
        ("core.security", "security_id"),
        ("core.symbol_xref", "security_id, symbol, valid_from"),
        ("core.daily_price", "security_id, trade_date"),
    ):
        out[table] = db.fetchone(
            f"SELECT count(*) AS n, md5(coalesce(string_agg(t::text, '|' ORDER BY {order}), '')) "
            f"AS digest FROM {table} t"
        )
    return out


def test_the_upgrade_leaves_existing_rows_byte_identical(db, migrated_dsn, sid):
    later = [
        v
        for v, _, state in m.status(migrated_dsn)
        if v >= VERSION and state == "applied"
    ]
    m.rollback(migrated_dsn, steps=len(later))
    try:
        assert dict((v, s) for v, _, s in m.status(migrated_dsn))[VERSION] == "pending"
        repo.upsert_symbol_xref(db, security_id=sid, symbol="ZZFUND")
        db.execute(
            "INSERT INTO core.daily_price (security_id, trade_date, open, high, low, close, "
            "volume, source) VALUES (%s, '2024-03-01', 10.5, 11, 10, 10.75, 1000, 'fmp')",
            (sid,),
        )
        before = _fingerprint(db)
        assert before["core.daily_price"]["n"] == 1
    finally:
        m.migrate(migrated_dsn)
    assert _fingerprint(db) == before
    assert db.fetchval("SELECT count(*) FROM ref.statement_line_item") == sum(
        len(v) for v in fm.STATEMENTS.values()
    )
