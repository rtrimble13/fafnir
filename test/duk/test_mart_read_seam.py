"""The mart read seam: duk's db datasource must name mart, and mart must agree
with core (migration 0020, ADR 0008 §4).

Two independent things are asserted here, and they fail for different reasons:

  * `duk.datasource.db` names no `core` or `ops` relation. A read that slips
    through works for whoever writes it -- loaders run as `fafnir_ingest` -- and
    fails for every laptop and MCP client on a mart-only role. That is precisely
    how `ph` was broken for `fafnir_app` before 0020, undetected because the
    developer path never exercised the role that breaks.
  * The mart views return what the core relations return. Renaming the relation
    under a price series is only safe if the series does not move, and
    `scripts/reconcile.sh` compares db output against the live feed on exactly
    that assumption. The same holds for clients other than duk that read mart
    as `fafnir_app`: `mart.v_corporate_action` (#174) is portable's.

The privilege half -- that `fafnir_app` can read these views and still cannot read
the tables under them -- lives in test/fafnir/test_migrations_least_privilege.py,
which can provision its own unprivileged role.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest

from duk.datasource import db as ds_db
from fafnir.db import repository as repo
from fafnir.ingest import adjustments

DSN = os.environ.get("FAFNIR_TEST_DSN", "")

_SOURCE = Path(ds_db.__file__).read_text()
# Comments explain why the seam is mart-only, so they mention core by name. Only
# the code is checked.
_CODE = "\n".join(
    line for line in _SOURCE.splitlines() if not line.lstrip().startswith("#")
)


def test_db_datasource_names_no_core_or_ops_relation():
    """A unit test on purpose: it needs no database, so it guards the rule on
    every run rather than only where FAFNIR_TEST_DSN is set -- which is the
    difference between catching this class of break and shipping it."""
    leaked = sorted(set(re.findall(r"\b(?:core|ops|landing)\.[a-z_]+", _CODE)))
    assert not leaked, (
        "duk.datasource.db must read only mart/ref -- it connects as a mart-only "
        f"role (ADR 0008). Add a mart view instead of reading: {leaked}"
    )


@pytest.mark.integration
def test_mart_symbol_lookup_resolves_like_the_core_ladder(db):
    """Same ticker, same security_id -- through all three rungs of the ladder.

    The third rung is the one worth the setup: a ticker whose xref period is
    CLOSED (a company renamed) resolves to nobody's current primary_symbol, so it
    is reachable only by the historical query.
    """
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db, primary_symbol="NEWCO", company_name="Newco", exchange_code="NASDAQ"
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol="NEWCO")
    # The ticker it traded under before the rename: closed period, not primary.
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from, valid_to, "
        "is_primary) VALUES (%s, 'OLDCO', '1990-01-01', '2001-05-05', false)",
        (sid,),
    )

    with ds_db._connect(DSN) as conn, conn.cursor() as cur:
        assert ds_db._resolve_security_id(cur, "NEWCO") == sid  # live xref
        assert ds_db._resolve_security_id(cur, "OLDCO") == sid  # historical xref
        assert ds_db._resolve_security_id(cur, "NOSUCH") is None

    # And the mart ladder agrees with fafnir's own core ladder, which is the
    # invariant the duplicated SQL exists to preserve.
    for symbol in ("NEWCO", "OLDCO", "NOSUCH"):
        with ds_db._connect(DSN) as conn, conn.cursor() as cur:
            via_mart = ds_db._resolve_security_id(cur, symbol)
        assert via_mart == repo.resolve_security_id(db, symbol), symbol


@pytest.mark.integration
def test_mart_raw_prices_are_identical_to_core(db):
    """The `ph` series must not move by a digit when the relation is renamed."""
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db, primary_symbol="RAWZ", company_name="Raw Inc", exchange_code="NASDAQ"
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol="RAWZ")
    bars = [
        {
            "security_id": sid,
            "trade_date": dt.date(2023, 5, 29) + dt.timedelta(days=n),
            "open": 100 + n,
            "high": 102 + n,
            "low": 99 + n,
            "close": 100.5 + n,
            "volume": 1000 + n,
        }
        for n in range(5)
    ]
    repo.upsert_daily_prices(db, bars)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=2,
        split_denominator=1,
    )
    adjustments.compute_for_security(db, sid)

    expected = db.fetchall(
        "SELECT trade_date, open, high, low, close, volume FROM core.daily_price "
        "WHERE security_id = %s ORDER BY trade_date",
        (sid,),
    )
    through_mart = db.fetchall(
        "SELECT trade_date, open, high, low, close, volume FROM mart.v_daily_price_raw "
        "WHERE security_id = %s ORDER BY trade_date",
        (sid,),
    )
    assert through_mart == expected

    # The same rows once they have been through the datasource's shaping, which is
    # what `duk ph` actually prints.
    frame = ds_db.price_history(
        dsn=DSN,
        symbol="RAWZ",
        start_date=None,
        end_date=None,
        frequency="day",
        limit=None,
        fields=None,
        adjusted=False,
    )
    assert len(frame) == len(expected)
    assert frame.loc["2023-05-29", "close"] == 100.5
    # Raw means raw: the 2:1 split on 2023-06-02 must NOT have moved these.
    assert frame.loc["2023-06-01", "close"] == 103.5
    assert str(frame["volume"].dtype) == "int64"


@pytest.mark.integration
def test_mart_corporate_actions_are_identical_to_core(db):
    """#174: every event, exactly as stored, at the grain of core.corporate_action.

    A client reconciling its own ledger against these (portable's `pt ca sync`)
    compares split ratios and dividend amounts to the cent, so the view must hand
    them back as the NUMERIC they are stored as -- never through a float -- and
    must neither drop nor duplicate a row.
    """
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    sid = repo.upsert_security(
        db, primary_symbol="ACTZ", company_name="Act Inc", exchange_code="NASDAQ"
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=dt.date(2023, 6, 2),
        split_numerator=Decimal("3"),
        split_denominator=Decimal("2"),
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2023, 5, 11),
        dividend_amount=Decimal("0.240001"),
        record_date=dt.date(2023, 5, 12),
        payment_date=dt.date(2023, 5, 18),
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2023, 8, 10),
        dividend_amount=Decimal("0.25"),
        source="operator",
    )

    columns = (
        "corporate_action_id, security_id, action_type, ex_date, record_date, "
        "payment_date, declaration_date, split_numerator, split_denominator, "
        "dividend_amount, currency, source"
    )
    expected = db.fetchall(
        f"SELECT {columns} FROM core.corporate_action ORDER BY corporate_action_id"
    )
    through_mart = db.fetchall(
        f"SELECT {columns} FROM mart.v_corporate_action ORDER BY corporate_action_id"
    )
    assert len(expected) == 3
    assert through_mart == expected

    # The query portable runs, and the types it gets back.
    in_range = db.fetchall(
        "SELECT action_type, ex_date, split_numerator, split_denominator, "
        "dividend_amount FROM mart.v_corporate_action "
        "WHERE security_id = %s AND ex_date BETWEEN %s AND %s ORDER BY ex_date",
        (sid, dt.date(2023, 5, 1), dt.date(2023, 6, 30)),
    )
    assert [(r["action_type"], r["ex_date"]) for r in in_range] == [
        ("dividend", dt.date(2023, 5, 11)),
        ("split", dt.date(2023, 6, 2)),
    ]
    assert in_range[0]["dividend_amount"] == Decimal("0.240001")
    assert isinstance(in_range[1]["split_numerator"], Decimal)

    types = {
        r["column_name"]: (r["data_type"], r["numeric_precision"], r["numeric_scale"])
        for r in db.fetchall(
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_schema = 'mart' AND table_name = 'v_corporate_action'"
        )
    }
    for column in ("split_numerator", "split_denominator", "dividend_amount"):
        assert types[column] == ("numeric", 20, 6), (column, types[column])
    assert not {"ingestion_run_id", "loaded_at"} & set(types), "lineage stays off"


@pytest.mark.integration
def test_no_mart_view_runs_with_the_invokers_rights(db):
    """ADR 0009: definer rights are the mechanism, and `security_invoker` undoes it.

    Set on a mart view, it would fail only for mart-only roles -- the developer
    writing it connects as one that can read core anyway -- so it is checked for
    every view in the schema rather than trusted to review.
    """
    rows = db.fetchall(
        "SELECT c.relname, c.reloptions FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'mart' AND c.relkind IN ('v', 'm')"
    )
    assert "v_corporate_action" in {r["relname"] for r in rows}
    invoker = [
        r["relname"]
        for r in rows
        for option in (r["reloptions"] or ())
        if option.split("=", 1)[0] == "security_invoker"
        and option.split("=", 1)[1].lower() not in ("false", "off", "no", "0")
    ]
    assert not invoker, f"mart views with security_invoker set: {invoker}"


def _lifecycle_security(db, symbol, *, dead=True, source="sharadar", key=None):
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        is_actively_trading=not dead,
        delisted_date=dt.date(2001, 1, 2) if dead else None,
        source=source,
    )
    if key:
        db.execute(
            "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, "
            "ticker) VALUES ('sharadar', 'stocks', %s, %s)",
            (key, symbol),
        )
        repo.insert_vendor_link(
            db,
            security_id=sid,
            vendor="sharadar",
            vendor_key=key,
            valid_from=dt.date(1998, 1, 2),
            match_method="minted" if source == "sharadar" else "manual",
            confidence=None,
            evidence={},
            created_by="tester",
        )
    return sid


def _event(db, key, day, action, contra="N/A", name="N/A", contra_key=None):
    db.execute(
        "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
        "action, contra_ticker, contra_name, contra_vendor_key) "
        "VALUES ('sharadar', %s, %s, %s, %s, %s, %s)",
        (key, day, action, contra, name, contra_key),
    )


@pytest.mark.integration
def test_mart_security_lifecycle_names_why_a_security_stopped(db):
    """#79: the vendor's terminal event, the acquirer, and the trading span.

    An acquisition is recorded as `acquisitionby` and `delisted` on one date, and the
    specific reason wins; `mergerto` is the merged-away side. A reason older than 30
    days before the last terminal event belongs to an earlier listing. A live
    security has no reason, whatever events its key carries. Synthetic keys and
    tickers (ADR 0012 §8).
    """
    D = dt.date
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    acquirer = _lifecycle_security(db, "ZZACQ", dead=False, source="fmp", key="900199")
    acquired = _lifecycle_security(db, "ZZA", key="900101")
    merged = _lifecycle_security(db, "ZZM", key="900102")
    bankrupt = _lifecycle_security(db, "ZZB", key="900103")
    relisted = _lifecycle_security(db, "ZZR", key="900104")
    live = _lifecycle_security(db, "ZZL", dead=False, source="fmp", key="900105")
    closed = _lifecycle_security(db, "ZZC", source="fmp", key="900106")
    unlinked = _lifecycle_security(db, "ZZO", source="fmp")
    db.execute(
        "UPDATE core.security_vendor_xref SET valid_to = '2020-01-01' "
        "WHERE vendor_key = '900106'"
    )

    _event(
        db, "900101", D(2018, 6, 15), "acquisitionby", "ZZACQ", "ZZACQ CORP", "900199"
    )
    _event(db, "900101", D(2018, 6, 15), "acquisitioncash", "ZZACQ", "ZZACQ CORP")
    _event(db, "900101", D(2018, 6, 15), "delisted")
    _event(db, "900102", D(2019, 3, 1), "mergerto", "ZZACQ", "ZZACQ CORP", "900199")
    _event(db, "900102", D(2019, 3, 1), "delisted")
    _event(db, "900103", D(2010, 1, 5), "bankruptcyliquidation")
    _event(db, "900103", D(2010, 1, 6), "delisted")
    _event(db, "900104", D(2005, 3, 1), "regulatorydelisting")
    _event(db, "900104", D(2015, 7, 1), "delisted")
    _event(db, "900105", D(2003, 1, 2), "delisted")
    _event(db, "900106", D(2012, 4, 2), "voluntarydelisting")
    _event(db, "900199", D(2019, 3, 1), "mergerfrom", "ZZM", "ZZM INC", "900102")

    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": acquired,
                "trade_date": day,
                "open": Decimal("10"),
                "high": Decimal("10"),
                "low": Decimal("10"),
                "close": Decimal("10"),
                "volume": 100,
            }
            for day in (D(2018, 6, 13), D(2018, 6, 14))
        ],
    )

    rows = {
        r["security_id"]: r
        for r in db.fetchall("SELECT * FROM mart.v_security_lifecycle")
    }

    def reason(sid):
        r = rows[sid]
        return (r["delist_reason"], r["delist_event_date"], r["acquirer_ticker"])

    assert reason(acquired) == ("acquisitionby", D(2018, 6, 15), "ZZACQ")
    assert rows[acquired]["acquirer_name"] == "ZZACQ CORP"
    assert rows[acquired]["acquirer_security_id"] == acquirer
    assert (rows[acquired]["first_trade_date"], rows[acquired]["last_trade_date"]) == (
        D(2018, 6, 13),
        D(2018, 6, 14),
    )
    assert reason(merged) == ("mergerto", D(2019, 3, 1), "ZZACQ")
    assert reason(bankrupt) == ("bankruptcyliquidation", D(2010, 1, 5), None)
    assert rows[bankrupt]["acquirer_name"] is None  # the vendor's 'N/A'
    assert reason(relisted) == ("delisted", D(2015, 7, 1), None)
    assert reason(live) == (None, None, None)
    assert reason(acquirer) == (None, None, None)  # its mergerfrom is no reason
    assert reason(closed) == ("voluntarydelisting", D(2012, 4, 2), None)
    assert reason(unlinked) == (None, None, None)
    assert rows[unlinked]["first_trade_date"] is None
    assert (rows[acquired]["minted_by"], rows[unlinked]["minted_by"]) == (
        "sharadar",
        "fmp",
    )
    assert {r["survivorship_complete_from"] for r in rows.values()} == {D(1998, 1, 1)}
    assert not {"vendor_key", "contra_vendor_key", "evidence"} & set(
        rows[acquired]
    ), "the vendor's keys stay in core"


@pytest.mark.integration
def test_the_company_marts_carry_a_dead_sharadar_issuer(db):
    """#79: a security Sharadar minted reads through the summary marts like any
    other. They key on security_id and LEFT JOIN the master, so nothing about the
    row's source, missing sector or missing profile drops it."""
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = _lifecycle_security(db, "ZZD", key="900201")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date(2000, 3, d),
                "open": Decimal("4"),
                "high": Decimal("4"),
                "low": Decimal("4"),
                "close": Decimal("4"),
                "volume": 10,
            }
            for d in (1, 2)
        ],
        source="sharadar",
    )
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=dt.date(2000, 3, 2),
        dividend_amount=Decimal("0.1"),
        source="sharadar",
    )

    profile = db.fetchone(
        "SELECT source, is_actively_trading, delisted_date "
        "FROM mart.v_security_profile WHERE security_id = %s",
        (sid,),
    )
    assert (profile["source"], profile["is_actively_trading"]) == ("sharadar", False)
    coverage = db.fetchone(
        "SELECT first_trade_date, last_trade_date, bar_count "
        "FROM mart.v_security_price_coverage WHERE security_id = %s",
        (sid,),
    )
    assert (coverage["first_trade_date"], coverage["bar_count"]) == (
        dt.date(2000, 3, 1),
        2,
    )
    actions = db.fetchone(
        "SELECT dividend_count FROM mart.v_security_action_summary "
        "WHERE security_id = %s",
        (sid,),
    )
    assert actions["dividend_count"] == 1
