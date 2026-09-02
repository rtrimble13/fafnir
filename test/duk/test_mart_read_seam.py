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
    that assumption.

The privilege half -- that `fafnir_app` can read these views and still cannot read
the tables under them -- lives in test/fafnir/test_migrations_least_privilege.py,
which can provision its own unprivileged role.
"""

from __future__ import annotations

import datetime as dt
import os
import re
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
