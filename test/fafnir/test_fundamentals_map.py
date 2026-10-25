"""
The SF1 statement map (#87): unit tests, no database.

The map is the contract between migration 0035's taxonomy and the loader (#90):
these pin what the plan (doc/plans/fundamentals-sf1.md §4.3) and ADR 0019 decided
about what is typed.
"""

from __future__ import annotations

import re

import pytest

from fafnir.ingest import fundamentals_map as fm
from fafnir.ingest import sf1_probe

ALL = [(s, i) for s, items in fm.STATEMENTS.items() for i in items]

#: SF1 indicators the vendor classifies outside the three statements but that are
#: typed: ``sharesbas`` (an entity field, the cover-page count) and ``ebt`` (tagged a
#: metric, but income before tax is a statement line).
TYPED_OUTSIDE_STATEMENTS = {"sharesbas", "ebt"}


def test_columns_are_unique_and_never_shadow_a_common_column():
    common = set(fm.COMMON_HEAD) | set(fm.COMMON_TAIL)
    for statement, items in fm.STATEMENTS.items():
        names = [i.column for i in items]
        assert len(names) == len(set(names)), statement
        assert not common & set(names), statement
        assert len(fm.columns(statement)) == len(common) + len(names)


@pytest.mark.parametrize("statement", list(fm.STATEMENTS))
def test_vendor_fields_are_unique_sf1_statement_indicators(statement):
    sf1 = set(sf1_probe.INCOME) | set(sf1_probe.BALANCE) | set(sf1_probe.CASH_FLOW)
    fields = [i.vendor_field for i in fm.STATEMENTS[statement]]
    assert len(fields) == len(set(fields))
    assert set(fields) <= sf1 | TYPED_OUTSIDE_STATEMENTS


def test_no_per_share_value_conversion_or_vendor_metric_is_typed():
    typed = {i.vendor_field for _, i in ALL}
    per_share = {"eps", "epsdil", "epsusd", "dps", "bvps", "fcfps", "sps", "tbvps"}
    usd = {c for c in sf1_probe.KNOWN if c.endswith("usd")}
    metrics = set(sf1_probe.METRICS) - TYPED_OUTSIDE_STATEMENTS
    assert not typed & (
        per_share | usd | metrics | set(sf1_probe.AS_OF_DATE) - {"sharesbas"}
    )


def test_every_statement_indicator_is_typed_or_deliberately_left_out():
    """A statement indicator SF1 has that the map does not type must be one the
    plan sends to `extra`: a per-share value or a USD conversion."""
    typed = {i.vendor_field for _, i in ALL}
    statement_fields = (
        set(sf1_probe.INCOME) | set(sf1_probe.BALANCE) | set(sf1_probe.CASH_FLOW)
    )
    left_out = statement_fields - typed
    assert left_out == {
        "dps",
        "eps",
        "epsdil",
        "epsusd",
        "ebitusd",
        "netinccmnusd",
        "revenueusd",
        "cashnequsd",
        "debtusd",
        "equityusd",
    }


def test_classes_signs_and_types():
    for statement, item in ALL:
        assert item.item_class in fm.ITEM_CLASSES
        assert item.sign_convention in fm.SIGN_CONVENTIONS
        if item.vendor_field in ("shareswa", "shareswadil", "sharesbas"):
            assert (item.item_class, item.sql_type) == ("share_count", fm.SHARES)
        else:
            assert item.sql_type == fm.MONEY
            assert item.item_class == (
                "stock" if statement == "balance_sheet" else "flow"
            )
        if statement == "cash_flow":
            # Signed as cash moves (#83): capex and dividends paid are negative.
            assert item.sign_convention == "positive_is_inflow"
        assert item.description


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(p.capitalize() for p in rest)


def test_fmp_field_is_the_probe_maps_counterpart_in_fmp_spelling():
    for statement, item in ALL:
        targets = [
            col
            for st, col in sf1_probe.SF1_TO_BUILD_PROMPT.get(item.vendor_field, ())
            if st == statement
        ]
        expected = _camel(targets[0]) if targets else None
        assert item.fmp_field == expected, (statement, item.column)
        if item.fmp_field:
            assert re.fullmatch(r"[a-z][A-Za-z]+", item.fmp_field)


def test_the_loader_lookup_is_by_vendor_field():
    lookup = fm.by_vendor_field("balance_sheet")
    assert lookup["sharesbas"].column == "shares_outstanding"
    assert lookup["equity"].column == "total_equity_parent"
