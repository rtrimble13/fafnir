"""One absurd number from FMP must not abort a 21k-symbol load.

Regression: FMP returned a per-share `lastDiv` at or above 10^14, psycopg raised
NumericValueOutOfRange, and `ingest securities --enrich` died at 19,199 of 21,031
symbols. 0010 dropped last_dividend outright, but the failure mode is the vendor's
not the column's -- market_cap_usd and beta come from the same feed and can carry
the same garbage, so the guard moved with them onto core.security.
"""

from __future__ import annotations

import types

import pytest

import fafnir.ingest.security_master as sm
from fafnir.ingest.security_master import (
    SECURITY_NUMERIC_LIMITS,
    _bounded_security_numerics,
)


@pytest.fixture()
def harness(monkeypatch):
    flags: list[str] = []
    monkeypatch.setattr(
        sm.repo, "add_dq_flag_once", lambda db, **kw: flags.append(kw["check_name"])
    )
    run = types.SimpleNamespace(rows_quarantined=0, run_id=1)

    def call(row):
        return _bounded_security_numerics(None, row=row, symbol="X", run=run)

    return call, flags, run


def test_an_out_of_range_market_cap_is_dropped_not_fatal(harness):
    call, flags, run = harness
    assert call({"marketCap": 1e22})["market_cap_usd"] is None
    assert flags == ["security_market_cap_usd_out_of_range"]
    assert run.rows_quarantined == 1


def test_a_value_just_inside_the_limit_survives(harness):
    call, flags, run = harness
    # NB 1e22 - 1 == 1e22 in float64, so pick a value actually below the limit.
    assert call({"marketCap": 9.9e21})["market_cap_usd"] == 9.9e21
    assert call({"beta": 1e6 - 1})["beta"] == 1e6 - 1
    assert flags == []
    assert run.rows_quarantined == 0


@pytest.mark.parametrize(
    "field,key", [("market_cap_usd", "marketCap"), ("beta", "beta")]
)
def test_each_column_enforces_its_own_limit(harness, field, key):
    call, flags, _ = harness
    assert call({key: SECURITY_NUMERIC_LIMITS[field]})[field] is None
    assert flags == [f"security_{field}_out_of_range"]


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan"])
def test_non_finite_values_are_dropped(harness, bad):
    # float("inf") parses happily; NUMERIC will not take it.
    call, flags, _ = harness
    assert call({"beta": bad})["beta"] is None
    assert flags == ["security_beta_out_of_range"]


def test_ordinary_screener_rows_pass_through_untouched(harness):
    call, flags, run = harness
    assert call({"marketCap": 3.4e12, "beta": "1.21"}) == {
        "market_cap_usd": 3.4e12,
        "beta": 1.21,
    }
    assert flags == []
    assert run.rows_quarantined == 0


def test_the_profile_spelling_of_market_cap_is_accepted(harness):
    # enrich_profiles feeds the same helper, and the profile payload says mktCap.
    call, _, _ = harness
    assert call({"mktCap": 1.5e9})["market_cap_usd"] == 1.5e9


def test_missing_fields_are_none_without_flagging(harness):
    call, flags, _ = harness
    assert all(v is None for v in call({}).values())
    assert flags == []
