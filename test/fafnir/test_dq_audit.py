"""
The audit cycles that decide the dorq cutover (DE-03 #268, DE-04 #269): the
sampler's determinism and weights, the statistics, the blind worksheet, the
verdicts, and the gate on a fixed labelled fixture. Every row is synthetic.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import audit

D = dt.date(2024, 3, 8)


# ---------------------------------------------------------------------------
# The design: one hash, the same on any host
# ---------------------------------------------------------------------------


def test_the_unit_hash_is_pinned():
    # MD5 of the unit's text key: these values are the same on every host, and a
    # change to them changes every cycle ever drawn.
    assert audit.unit_text(1124, "bars", (4242, dt.date(2026, 11, 20), None)) == (
        "1124|bars|4242||2026-11-20"
    )
    assert audit.unit_hash(1124, "bars", (4242, dt.date(2026, 11, 20), None)) == (
        0.837339611552256
    )
    assert (
        audit.unit_hash(1124, "statements", (4242, dt.date(2025, 12, 31), "quarter"))
        == 0.36500893287989
    )
    assert (
        audit.unit_hash(1124, "bars", (4242, dt.date(2026, 11, 20), None), "|second")
        == 0.5365338973222138
    )


def test_a_units_probability_is_its_largest_stratum_rate():
    sizes = {"dorq:DQ201": 100, "dorq:error": 10, "sql:gap:new": 0}
    targets = audit.Targets(
        per_code=10, errors=20, withheld=0, per_check=5, random=1, near=0
    )
    rate = audit.rates(sizes, targets)
    assert rate == {"dorq:DQ201": 0.1, "dorq:error": 1.0, "sql:gap:new": 0.0}
    assert audit.inclusion(["dorq:DQ201"], rate) == 0.1
    assert audit.inclusion(["dorq:DQ201", "dorq:error"], rate) == 1.0
    assert audit.inclusion([], rate) == 0.0


def _members(n: int) -> dict:
    return {(i, D, None): {"dorq:DQ201"} for i in range(1, n + 1)}


def test_the_draw_is_reproducible_and_a_larger_target_keeps_the_smaller_ones_units():
    members = _members(2000)
    small = audit.draw_units(members, {"dorq:DQ201": 0.05}, 7, "bars")
    again = audit.draw_units(members, {"dorq:DQ201": 0.05}, 7, "bars")
    large = audit.draw_units(members, {"dorq:DQ201": 0.20}, 7, "bars")
    assert small == again
    assert set(small) <= set(large)
    assert 60 <= len(small) <= 140  # about 100
    assert all(p == 0.05 for p in small.values())
    other_seed = audit.draw_units(members, {"dorq:DQ201": 0.05}, 8, "bars")
    assert set(other_seed) != set(small)


def test_the_second_verification_takes_a_tenth_rounded_up():
    units = [(i, D, None) for i in range(1, 32)]
    second = audit.second_subset(units, 3, "bars")
    assert len(second) == math.ceil(0.1 * 31) == 4
    assert second == audit.second_subset(list(reversed(units)), 3, "bars")
    assert audit.second_subset([], 3, "bars") == set()


def test_targets_name_each_stratum():
    t = audit.DEFAULT_TARGETS["bars"]
    assert t.of("dorq:DQ201") == t.per_code
    assert t.of("dorq:error") == t.errors
    assert t.of("sql:gap:standing") == t.per_check
    assert t.of("none:random") == t.random
    assert t.of("none:near") == t.near
    assert audit.DEFAULT_TARGETS["statements"].of("dorq:withheld") == 10


# ---------------------------------------------------------------------------
# The statistics
# ---------------------------------------------------------------------------


def test_wilson_matches_the_published_interval():
    low, high = audit.wilson(0.5, 100)
    assert round(low, 4) == 0.4038 and round(high, 4) == 0.5962
    assert audit.wilson(0.0, 0) == (0.0, 1.0)


def test_kish_is_n_for_equal_weights():
    assert audit.kish([1.0] * 7) == pytest.approx(7)
    assert audit.kish([2.0, 4.0, 10.0, 2.0]) == pytest.approx(324 / 124)


def test_an_unweighted_estimate_is_the_plain_proportion():
    e = audit.Estimate()
    for hit in (True, True, False, True):
        e.add(hit)
    assert e.value == 0.75 and e.n_eff == pytest.approx(4)
    assert e.interval == audit.wilson(0.75, 4)


def test_the_paired_difference_and_its_bound():
    # a tie on every error: no difference, and a bound below zero, so a tie is
    # never "above"
    delta, lower, upper = audit.newcombe_paired(10, 0, 0, 0, 10)
    assert delta == 0.0
    assert lower == pytest.approx(-0.212942, abs=1e-6)
    assert upper == pytest.approx(0.212942, abs=1e-6)
    # many more found by the first only: the bound clears zero
    delta, lower, _ = audit.newcombe_paired(50, 40, 2, 8, 100)
    assert delta == pytest.approx(0.38)
    assert lower > 0
    assert audit.newcombe_paired(0, 0, 0, 0, 0) is None


def test_mcnemar_exact_one_sided():
    assert audit.mcnemar_one_sided(7, 1) == pytest.approx(9 / 256)
    assert audit.mcnemar_one_sided(1, 2) == pytest.approx(7 / 8)
    assert audit.mcnemar_one_sided(0, 0) == 1.0


def test_ece_is_weighted_by_bin():
    points = [(0.9, True, 2.0), (0.95, False, 4.0), (0.8, True, 10.0)]
    assert audit.ece(points) == pytest.approx(6 / 16 * 0.6 + 10 / 16 * 0.2)
    assert audit.ece([]) is None


# ---------------------------------------------------------------------------
# The gate on a fixed labelled fixture
# ---------------------------------------------------------------------------


def _item(no, sid, p, *, dorq=(), sql=(), status="judged", second=None, check=False):
    return {
        "item_no": no,
        "security_id": sid,
        "unit_date": D,
        "period_type": None,
        "strata": ["x"],
        "inclusion_p": p,
        "data": {},
        "engines": {"dorq": list(dorq), "sql": list(sql)},
        "second_check": check,
        "second": second,
        "status": status,
    }


def _dorq(code, severity="warn", p=None, keyed=True):
    return {
        "code": code,
        "check": "dorq_x",
        "severity": severity,
        "p_error": p,
        "first": D.isoformat(),
        "last": D.isoformat(),
        "keyed": keyed,
    }


def _sql(check, open_=True, keyed=True):
    return {
        "id": 1,
        "check": check,
        "severity": "warn",
        "date": D.isoformat(),
        "keyed": keyed,
        "open": open_,
        "new": True,
    }


def _lab(no, sid, cls, kind, *, expect="", codes=""):
    return {
        "security_id": sid,
        "first_date": D,
        "last_date": D,
        "class": cls,
        "kind": kind,
        "expect": expect,
        "codes": codes,
        "verified": True,
        "statement": None,
        "period_type": None,
        "version_no": None,
        "item": None,
        "sample_item": no,
        "weight": None,
    }


def _fixture(pending: bool = True):
    def fault(no, sid, kind):
        return _lab(
            no, sid, "data_error", kind, expect="|".join(audit.BAR_FAULTS[kind])
        )

    def fact(no, sid):
        return _lab(no, sid, "market_fact", "matches_source", codes="DQ")

    items = [
        # a bad print both engines keyed
        _item(
            1,
            1,
            0.5,
            dorq=[_dorq("DQ201", p=0.9)],
            sql=[_sql("outlier")],
            check=True,
            second={"verdict": "error"},
        ),
        # dorq at error on a correct bar
        _item(2, 2, 0.25, dorq=[_dorq("DQ201", "error", p=0.95)]),
        # a missing bar only the SQL gap check keyed
        _item(3, 3, 1.0, sql=[_sql("gap")]),
        # a missing bar only dorq keyed
        _item(4, 4, 0.1, dorq=[_dorq("DQ301", p=0.8)]),
        # neither engine; correct
        _item(5, 5, 0.5, check=True, second={"verdict": "correct"}),
        # an unrecorded split dorq called a bad print; SQL's outlier names it
        _item(6, 6, 0.5, dorq=[_dorq("DQ201")], sql=[_sql("outlier")]),
        _item(8, 8, 0.5, status="unverifiable"),
    ]
    if pending:
        items.append(_item(7, 7, 0.5, status="pending"))
    labels = {
        1: [fault(1, 1, "bad_price")],
        2: [fact(2, 2)],
        3: [fault(3, 3, "missing_bar")],
        4: [fault(4, 4, "missing_bar")],
        5: [fact(5, 5)],
        6: [fault(6, 6, "unreported_split")],
    }
    cycle = {
        "cycle_id": "t-bars",
        "domain": "bars",
        "night": D,
        "dorq_version": "0.14.0",
        "design": {
            "volume": {
                "standing": {"dorq": 20, "sql": 100, "ratio": 0.2},
                "new": {"dorq": 5, "sql": 10, "ratio": 0.5},
            }
        },
    }
    return cycle, items, labels


def test_the_gate_pins_weighted_precision_recall_and_the_paired_test():
    cycle, items, labels = _fixture()
    g = audit.gate(cycle, items, labels)
    assert (g.drawn, g.judged, g.unverifiable, g.pending) == (8, 6, 1, 1)
    assert g.confirmed_errors == 4

    warn = g.precision["dorq"]["warn"]
    # right on 1 (w 2) and 4 (w 10), false on 2 (w 4), another fault on 6 (w 2)
    assert warn.strict.value == pytest.approx(12 / 18)
    assert warn.fault.value == pytest.approx(14 / 18)
    assert warn.strict.n_eff == pytest.approx(324 / 124)
    assert warn.strict.interval == audit.wilson(12 / 18, 324 / 124)
    assert g.precision["dorq"]["error"].strict.value == 0.0
    # SQL: right on 1, 3 and 6 (its outlier's successors name the split)
    assert g.precision["sql"]["warn"].strict.value == 1.0

    # recall: errors 1 (w 2), 3 (w 1), 4 (w 10), 6 (w 2)
    assert g.recall["dorq"].value == pytest.approx(12 / 15)
    assert g.recall["sql"].value == pytest.approx(5 / 15)
    assert g.recall_any["dorq"].value == pytest.approx(14 / 15)
    assert g.paired["cells_weighted"] == {"a": 2.0, "b": 10.0, "c": 3.0, "d": 0.0}
    assert g.paired["discordant"] == {"dorq_only": 1, "sql_only": 2}
    assert g.paired["difference"] == pytest.approx(7 / 15)
    assert g.paired["lower_one_sided_95"] == pytest.approx(-0.437228, abs=1e-6)
    assert g.paired["mcnemar_p"] == pytest.approx(7 / 8)

    assert g.ece == pytest.approx(6 / 16 * 0.6 + 10 / 16 * 0.2)
    assert g.second["agreement"] == 1.0 and g.second["checked"] == 2
    assert g.verdict == "incomplete: 1 of 8 units not yet verified"


def test_unit_weights_give_the_unweighted_numbers():
    cycle, items, labels = _fixture(pending=False)
    for it in items:
        it["inclusion_p"] = 1.0
    g = audit.gate(cycle, items, labels)
    assert g.precision["dorq"]["warn"].strict.value == pytest.approx(2 / 4)
    assert g.recall["dorq"].value == pytest.approx(2 / 4)
    assert g.recall["sql"].value == pytest.approx(3 / 4)


def test_the_verdict_waits_for_enough_errors_and_names_what_fails():
    cycle, items, labels = _fixture(pending=False)
    g = audit.gate(cycle, items, labels)
    assert g.verdict == "not enough errors yet: 4 confirmed of about 100"
    lines = {m: ok for m, ok, _ in g.lines}
    assert lines["precision, warn and above"] is False
    assert lines["volume standing, as written"] is True
    assert lines["volume new per night, as written"] is False
    text = audit.format_gate(g)
    assert "verdict: not enough errors yet" in text
    assert "exact McNemar p 0.8750" in text


def test_a_second_verification_below_the_bar_makes_the_cycle_unusable():
    cycle, items, labels = _fixture(pending=False)
    items[0]["second"] = {"verdict": "correct"}  # disagrees with the label
    g = audit.gate(cycle, items, labels)
    assert g.second["agreement"] == 0.5
    assert g.verdict.startswith("not usable: second verification agreed on 50%")
    assert "re-verify items 1" in g.verdict


# ---------------------------------------------------------------------------
# The worksheet and the verdicts
# ---------------------------------------------------------------------------


def _statements_item(no=1):
    return {
        "item_no": no,
        "security_id": 9,
        "unit_date": dt.date(2023, 12, 31),
        "period_type": "quarter",
        "strata": ["dorq:DQ903"],
        "inclusion_p": 0.25,
        "data": {
            "items": {
                "income": ["net_income", "revenue"],
                "balance_sheet": ["total_assets"],
            },
            "statements": {
                "income": [{"version_no": 1}, {"version_no": 2}],
                "balance_sheet": [{"version_no": 1}],
            },
        },
        "engines": {"dorq": [{"code": "DQ903"}], "sql": []},
        "second_check": False,
        "status": "pending",
        "second": None,
    }


def test_the_worksheet_is_blind():
    cycle = {"cycle_id": "c", "domain": "statements"}
    it = _statements_item()
    rows = audit.worksheet(cycle, [it, {**_statements_item(2), "second_check": True}])
    assert [r["item"] for r in rows] == [1, 2]
    for row in rows:
        assert set(row) == {
            "cycle",
            "domain",
            "item",
            "security_id",
            "date",
            "period_type",
            "data",
        }
    assert [r["item"] for r in audit.worksheet(cycle, [it], second=True)] == []


def _verdicts(domain, items, *lines):
    text = "".join(json.dumps(x) + "\n" for x in lines)
    return audit.parse_verdicts(io.StringIO(text), domain, items)


def test_a_verdict_cites_its_source_and_a_known_kind():
    items = {1: _statements_item()}
    with pytest.raises(audit.VerdictError, match="cites its source"):
        _verdicts(
            "statements",
            items,
            {"item": 1, "verdict": "correct", "kind": "matches_filing"},
        )
    with pytest.raises(audit.VerdictError, match="kind 'bad_price'"):
        _verdicts(
            "statements",
            items,
            {
                "item": 1,
                "verdict": "error",
                "kind": "bad_price",
                "source": "s",
                "value": "v",
            },
        )
    with pytest.raises(audit.VerdictError, match="not in the cycle"):
        _verdicts(
            "statements", items, {"item": 2, "verdict": "unverifiable", "note": "n"}
        )
    with pytest.raises(audit.VerdictError, match="not among the unit's items"):
        _verdicts(
            "statements",
            items,
            {
                "item": 1,
                "verdict": "error",
                "kind": "wrong_value",
                "source": "s",
                "value": "v",
                "line_items": ["capex"],
            },
        )
    with pytest.raises(audit.VerdictError, match="needs a note"):
        _verdicts("statements", items, {"item": 1, "verdict": "unverifiable"})


def test_a_statements_error_on_one_item_labels_it_and_clears_the_rest():
    cycle = {"cycle_id": "c", "domain": "statements"}
    item = _statements_item()
    (v,) = _verdicts(
        "statements",
        {1: item},
        {
            "item": 1,
            "verdict": "error",
            "kind": "wrong_value",
            "source": "EDGAR 0000000000-24-000001",
            "value": "NetIncomeLoss 1,234",
            "line_items": ["net_income"],
        },
    )
    rows = audit.verdict_labels(cycle, item, v, by="claude")
    faults = [r for r in rows if r["class"] == "data_error"]
    facts = [r for r in rows if r["class"] == "market_fact"]
    assert [(r["statement"], r["item"], r["version_no"]) for r in faults] == [
        ("income", "net_income", 2)
    ]
    assert sorted((r["statement"], r["version_no"]) for r in facts) == [
        ("balance_sheet", 1),
        ("income", 2),
    ]
    for r in rows:
        assert r["weight"] == 4.0 and r["sample_id"] == "c" and r["sample_item"] == 1
        assert r["verified"] is True and "EDGAR 0000000000-24-000001" in r["note"]
        assert r["period_type"] == "quarter"


def test_a_bars_verdict_states_the_codes_its_fault_names():
    cycle = {"cycle_id": "b", "domain": "bars"}
    item = {**_item(3, 3, 0.2, status="pending"), "data": {}}
    (v,) = _verdicts(
        "bars",
        {3: item},
        {
            "item": 3,
            "verdict": "error",
            "kind": "missing_bar",
            "source": "https://example.org/quote",
            "value": "traded 1,200 shares",
        },
    )
    (row,) = audit.verdict_labels(cycle, item, v, by="claude")
    assert row["class"] == "data_error" and row["expect"] == "DQ301|DQ302|DQ303|DQ305"
    assert row["weight"] == pytest.approx(5.0)
    (v,) = _verdicts(
        "bars",
        {3: item},
        {
            "item": 3,
            "verdict": "correct",
            "kind": "no_trading",
            "source": "https://example.org/quote",
            "value": "no trade that day",
        },
    )
    (row,) = audit.verdict_labels(cycle, item, v, by="claude")
    assert row["class"] == "market_fact" and row["codes"] == "DQ"


def test_items_to_verify_add_the_named_and_two_drawn():
    unit = (9, dt.date(2023, 12, 31), "quarter")
    all_items = {
        "income": [
            "a1",
            "a2",
            "a3",
            "net_income",
            "revenue",
            "weighted_average_shares",
        ],
        "balance_sheet": [
            "b1",
            "b2",
            "cash_and_equivalents",
            "shares_outstanding",
            "total_assets",
            "total_equity_parent",
            "total_liabilities",
        ],
        "cash_flow": ["c1", "c2", "c3", "net_cash_operating"],
    }
    out = audit.items_to_verify(unit, [("income", "a3")], all_items, 5)
    assert out == audit.items_to_verify(unit, [("income", "a3")], all_items, 5)
    assert {"revenue", "net_income", "a3"} <= set(out["income"])
    assert len(out["income"]) == 3 + 1 + 2
    assert len(out["cash_flow"]) == 1 + 2
    assert audit.items_to_verify((9, D, "-"), [], all_items, 5) == {}


# ---------------------------------------------------------------------------
# On the database
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_sql_hash_is_the_python_hash(db):
    units = [
        (1, D, None),
        (987654, dt.date(1999, 1, 4), None),
        (5, dt.date(2023, 12, 31), "quarter"),
    ]
    for unit in units:
        sql = audit._sql_hash("%(sid)s::bigint", "%(d)s::date", "%(pt)s::text")
        got = db.fetchval(
            f"SELECT {sql}",
            {"seed": 77, "domain": "bars", "sid": unit[0], "d": unit[1], "pt": unit[2]},
        )
        assert got == audit.unit_hash(77, "bars", unit)


def _security(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


def _sessions(db, before: dt.date, n: int) -> list[dt.date]:
    rows = db.fetchall(
        "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
        " AND is_open AND trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
        (before, n),
    )
    return sorted(r["trade_date"] for r in rows)


def _bars_night(db, tmp_path):
    """Two securities on 30 sessions to the night; dorq reports a bad print on
    the first's last bar, and the SQL gap check flagged the session the second
    missed."""
    days = _sessions(db, D, 30)
    a, b = _security(db, "AUDA"), _security(db, "AUDB")
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1000,
            }
            for sid in (a, b)
            for d in days
            if not (sid == b and d == days[-2])
        ],
    )
    started = "2024-03-09 04:00+00"
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, params, window_from,"
        " window_to, status, started_at, finished_at)"
        " VALUES ('dorq', 'dq-shadow', %s::jsonb, %s, %s, 'success', %s, %s)",
        (
            json.dumps({"since": days[-5].isoformat(), "dorq_version": "0.14.0"}),
            days[0],
            D,
            started,
            started,
        ),
    )
    shadow = tmp_path / "sh" / f"{D.isoformat()}.jsonl"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_text(
        json.dumps(
            {
                "security_id": a,
                "table_name": "core.daily_price",
                "record_key": {"trade_date": days[-1].isoformat()},
                "check_name": "dorq_bad_print",
                "severity": "error",
                "detail": {
                    "code": "DQ201",
                    "date": days[-1].isoformat(),
                    "p_error": 0.97,
                },
            }
        )
        + "\n"
    )
    db.execute(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detected_at) VALUES (%s, 'core.daily_price',"
        " %s::jsonb, 'gap', 'warn', '2024-03-08 23:00+00')",
        (b, json.dumps({"trade_date": days[-2].isoformat()})),
    )
    return a, b, days, shadow


@pytest.mark.integration
def test_a_bars_cycle_is_drawn_with_its_strata_and_snapshot(db, tmp_path):
    a, b, days, shadow = _bars_night(db, tmp_path)
    targets = audit.Targets(
        per_code=1, errors=1, withheld=0, per_check=1, random=1000, near=1000
    )
    cycle = audit.draw_cycle(db, "bars", shadow, cycle_id="t1", seed=5, targets=targets)
    units = {it["unit"]: it for it in cycle.items}
    # the frame is small and the targets large: every unit is drawn, at p 1
    assert units[(a, days[-1], None)]["strata"] == ["dorq:DQ201", "dorq:error"]
    assert units[(b, days[-2], None)]["strata"] == ["sql:gap:standing"]
    assert all(it["inclusion_p"] == 1.0 for it in cycle.items)
    # the window: 5 sessions of a's bars and b's, b's missing one a unit too
    assert len(cycle.items) == 10
    assert units[(a, days[-1], None)]["data"]["bar"]["close"] == 10
    assert units[(b, days[-2], None)]["data"]["bar"] is None
    assert units[(b, days[-2], None)]["data"]["session"] is True
    assert [x["code"] for x in units[(a, days[-1], None)]["engines"]["dorq"]] == [
        "DQ201"
    ]
    assert [x["check"] for x in units[(b, days[-2], None)]["engines"]["sql"]] == ["gap"]
    assert cycle.design()["volume"]["standing"] == {"dorq": 1, "sql": 1, "ratio": 1.0}
    again = audit.draw_cycle(db, "bars", shadow, cycle_id="t1", seed=5, targets=targets)
    assert [it["unit"] for it in again.items] == [it["unit"] for it in cycle.items]
    small = audit.Targets(
        per_code=1, errors=1, withheld=0, per_check=1, random=2, near=0
    )
    fewer = audit.draw_cycle(db, "bars", shadow, cycle_id="t1", seed=5, targets=small)
    neither = [it for it in fewer.items if it["strata"] == ["none:random"]]
    assert all(it["inclusion_p"] == pytest.approx(2 / 8) for it in neither)


@pytest.mark.integration
def test_cli_draws_verifies_and_gates_a_cycle(db, tmp_path):
    from test.fafnir import test_dq_dorq_statements as base

    a, b, days, shadow = _bars_night(db, tmp_path)
    rc = base._rc(tmp_path, db, tmp_path / "dorq")

    def run(*args, stdin=None):
        return CliRunner().invoke(cli.main, ["--config", str(rc), *args], input=stdin)

    sample = [
        "dq",
        "sample",
        "--domain",
        "bars",
        "--cycle",
        "t2",
        "--seed",
        "5",
        "--random",
        "1000",
        "--near",
        "1000",
    ]
    result = run(*sample, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run: nothing written." in result.output
    assert db.fetchval("SELECT count(*) FROM ops.dq_audit_cycle") == 0
    result = run(*sample, "--by", "test")
    assert result.exit_code == 0, result.output
    assert "Wrote cycle t2: 10 units." in result.output
    assert run(*sample).exit_code != 0  # the cycle exists

    ws = tmp_path / "ws.jsonl"
    result = run("dq", "audit", "worksheet", "t2", "--out", str(ws))
    assert result.exit_code == 0, result.output
    sheet = [json.loads(x) for x in ws.read_text().splitlines()]
    assert len(sheet) == 10
    assert not {"strata", "inclusion_p", "engines"} & set(json.dumps(sheet).split('"'))

    by_unit = {(r["security_id"], r["date"]): r["item"] for r in sheet}
    verdicts = []
    for (sid, day), item in by_unit.items():
        if (sid, day) == (b, days[-2].isoformat()):
            verdicts.append(
                {
                    "item": item,
                    "verdict": "error",
                    "kind": "missing_bar",
                    "source": "https://example.org/b",
                    "value": "900 shares",
                }
            )
        else:
            verdicts.append(
                {
                    "item": item,
                    "verdict": "correct",
                    "kind": "matches_source",
                    "source": "https://example.org/q",
                    "value": "close 10",
                }
            )
    vf = tmp_path / "v.jsonl"
    vf.write_text("".join(json.dumps(v) + "\n" for v in verdicts))
    result = run(
        "dq",
        "audit",
        "verdicts",
        "t2",
        "-",
        "--by",
        "claude",
        "--dry-run",
        stdin=vf.read_text(),
    )
    assert result.exit_code == 0, result.output
    assert "Dry run: would store 10 verdicts" in result.output
    assert db.fetchval("SELECT count(*) FROM ops.dq_label") == 0
    result = run("dq", "audit", "verdicts", "t2", str(vf), "--by", "claude")
    assert result.exit_code == 0, result.output
    assert "Stored 10 verdicts: 10 labels, 0 unverifiable." in result.output
    labels = db.fetchall("SELECT sample_id, weight, verified FROM ops.dq_label")
    assert len(labels) == 10
    assert all(
        r["sample_id"] == "t2" and r["weight"] == 1.0 and r["verified"] for r in labels
    )

    second = [
        v
        for v in verdicts
        if v["item"]
        in {i["item_no"] for i in audit.load_items(db, "t2") if i["second_check"]}
    ]
    sf = tmp_path / "s.jsonl"
    sf.write_text("".join(json.dumps(v) + "\n" for v in second))
    result = run("dq", "audit", "verdicts", "t2", str(sf), "--second", "--by", "other")
    assert result.exit_code == 0, result.output
    assert db.fetchval("SELECT count(*) FROM ops.dq_label") == 10  # no new labels

    result = run("dq", "compare", "--audit", "t2")
    assert result.exit_code == 0, result.output
    assert "1 confirmed errors" in result.output
    assert "verdict: not enough errors yet: 1 confirmed of about 100" in result.output
    result = run("dq", "compare", "--audit", "t2", "--json")
    payload = json.loads(result.output)
    gate = payload["cycles"][0]
    assert gate["recall"]["sql"]["value"] == 1.0
    assert gate["recall"]["dorq"]["value"] == 0.0
    assert gate["precision"]["dorq"]["warn"]["strict"]["value"] == 0.0
    assert gate["second"]["agreement"] == 1.0

    out = tmp_path / "labels.jsonl"
    result = run("dq", "export-labels", "--out", str(out))
    assert result.exit_code == 0, result.output
    exported = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    audit_labels = [x for x in exported if x.get("sample") == "t2"]
    assert len(audit_labels) == 10
    assert all(x["weight"] == 1.0 and "sample_item" in x for x in audit_labels)

    result = run("dq", "audit", "list")
    assert result.exit_code == 0, result.output
    assert "t2" in result.output


@pytest.mark.integration
def test_cli_sample_refuses_audit_options_without_a_domain(db, tmp_path):
    from test.fafnir import test_dq_dorq_statements as base

    rc = base._rc(tmp_path, db, tmp_path / "dorq")
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "sample", "--cycle", "x"]
    )
    assert result.exit_code != 0
    assert "only with --domain" in result.output
