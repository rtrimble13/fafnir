"""Labels that can judge dorq (v1.5.0): action repairs, override reasons, when a
label applies, how a flag was judged, and labels a person states (ops.dq_label).

The shadow nights of 2026-10-12..14 are the shapes: the duplicate and phantom split
rows dorq caught (GLE, NXGL, SKYD, WORX) were not labels at all; CELG-RI's 770 bars
deleted for scope were labels of error; and the 23,903 acceptances closed by rule
could not be told from the ones someone checked.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import labels as lb

pytestmark = pytest.mark.integration

D = dt.date


def _mk(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


def _days(db, n, start=D(2024, 3, 1)):
    return [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
            " AND is_open AND trade_date >= %s ORDER BY trade_date LIMIT %s",
            (start, n),
        )
    ]


def _bars(db, sid, days, close=10.0):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": d,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            }
            for d in days
        ],
    )


def _action(db, sid, kind, ex, *, num=None, den=None, amount=None):
    return int(
        db.fetchval(
            "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
            " split_numerator, split_denominator, dividend_amount)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING corporate_action_id",
            (sid, kind, ex, num, den, amount),
        )
    )


def _flag(db, sid, check, key, detail=None):
    repo.add_dq_flag_once(
        db, check_name=check, security_id=sid, record_key=key, detail=detail
    )
    return int(
        db.fetchval(
            "SELECT max(dq_flag_id) FROM ops.data_quality_flag WHERE security_id = %s"
            " AND check_name = %s",
            (sid, check),
        )
    )


def _rc(db, tmp_path, monkeypatch):
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{tmp_path / "sh"}"\n'
        f'dorq_path = "{tmp_path / "no-dorq"}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    return rc


def _cli(rc, *args, input=None):
    return CliRunner().invoke(cli.main, ["--config", str(rc), *args], input=input)


def _by_kind(export):
    out: dict = {}
    for lab in export.labels:
        out.setdefault(lab["kind"], []).append(lab)
    return out


# ---------------------------------------------------------------------------
# L1: corporate-action repairs are labels
# ---------------------------------------------------------------------------


def test_action_repairs_become_labels_with_the_actions_as_they_stood(db):
    days = _days(db, 20)
    gle = _mk(db, "LSGLE")  # a duplicate split: the bars move once
    _bars(db, gle, days)
    stray = _action(db, gle, "split", days[5], num=1, den=10)
    _action(db, gle, "split", days[6], num=1, den=10)
    repo.delete_operator_action(
        db, corporate_action_id=stray, note="duplicate of the 1:10", created_by="t"
    )
    misdated = _mk(db, "LSMIS")
    _bars(db, misdated, days)
    wrong = _action(db, misdated, "split", days[8], num=2, den=1)
    repo.redate_operator_action(
        db,
        corporate_action_id=wrong,
        new_ex_date=days[9],
        note="price halves a day later",
        created_by="t",
    )
    junk = _mk(db, "LSDIV")
    _bars(db, junk, days)
    bad_div = _action(db, junk, "dividend", days[4], amount=Decimal("24"))
    repo.delete_operator_action(
        db, corporate_action_id=bad_div, note="units: cents as dollars", created_by="t"
    )
    repo.add_operator_action(
        db,
        security_id=junk,
        action_type="dividend",
        ex_date=days[12],
        dividend_amount=Decimal("0.5"),
        note="feed missed the distribution",
        created_by="t",
    )

    export = lb.build(db)
    kinds = _by_kind(export)
    (deleted,) = kinds["split_deleted"]
    assert (deleted["series"], deleted["first"]) == (str(gle), days[5].isoformat())
    assert deleted["class"] == "data_error" and deleted["expect"] == "DQ702|DQ704"
    assert deleted["applies_until"] == deleted["decided_at"]
    (redated,) = kinds["split_redated"]
    # On the original ex-date, where the misdated row sat.
    assert redated["first"] == days[8].isoformat() and redated["expect"] == "DQ701"
    assert redated["redated_to"] == days[9].isoformat()
    # The re-date's add half is not a split someone found missing.
    assert "split_added" not in kinds
    (div,) = kinds["dividend_deleted"]
    assert div["expect"] == "DQ705"
    (added,) = kinds["dividend_added"]
    assert added["class"] == "context_gap" and added["expect"] == "DQ201|DQ203"

    actions = {(r["security_id"], r["ex_date"]): r for r in export.restore_actions}
    assert Decimal(str(actions[(gle, days[5].isoformat())]["split_denominator"])) == 10
    assert (misdated, days[8].isoformat()) in actions
    assert Decimal(str(actions[(junk, days[4].isoformat())]["dividend_amount"])) == 24
    assert len(actions) == 3


def test_actions_moved_with_a_history_are_that_repairs_label_not_their_own(db):
    """A history split moves the source's actions to the destination, and a shift
    --with-actions re-dates them: neither says an action was wrong."""
    days = _days(db, 12)
    sid = _mk(db, "LSSHF")
    _bars(db, sid, days[:6])
    _action(db, sid, "split", days[3], num=2, den=1)
    from fafnir.ingest import price_edits

    plan = price_edits.plan_shift(
        db, sid, days[0], days[5], 1, allow_non_session=True, with_actions=True
    )
    price_edits.apply_plan(
        db, plan, note="a day early", created_by="t", with_actions=True
    )
    kinds = _by_kind(lb.build(db))
    assert set(kinds) == {"date_shift"}


def test_the_csv_cli_writes_the_actions_file(db, tmp_path, monkeypatch):
    days = _days(db, 10)
    sid = _mk(db, "LSCSV")
    _bars(db, sid, days)
    aid = _action(db, sid, "split", days[3], num=2, den=1)
    repo.delete_operator_action(db, corporate_action_id=aid, note="x", created_by="t")
    rc = _rc(db, tmp_path, monkeypatch)
    out, acts = tmp_path / "l.jsonl", tmp_path / "actions.csv"
    result = _cli(
        rc, "dq", "export-labels", "--out", str(out), "--restore-actions", str(acts)
    )
    assert result.exit_code == 0, result.output
    assert "1 corporate action as they stood" in result.output
    lines = acts.read_text().splitlines()
    assert lines[0] == (
        "security_id,ex_date,action_type,split_numerator,split_denominator,"
        "dividend_amount"
    )
    assert lines[1].startswith(f"{sid},{days[3].isoformat()},split,2")


# ---------------------------------------------------------------------------
# L2: only a data error is a label
# ---------------------------------------------------------------------------


def test_an_override_out_of_scope_is_no_label_and_no_repair(db):
    """CELG-RI: a contingent value right, removed because it does not belong. Its
    bars' accepted outlier stays a market fact -- the bar was not repaired."""
    days = _days(db, 10)
    sid = _mk(db, "LSCVR")
    _bars(db, sid, days)
    fid = _flag(db, sid, "outlier", {"trade_date": days[4].isoformat()})
    repo.accept_dq_flags(
        db, repo.DqFilter(flag_ids=(fid,)), note="real", accepted_by="t"
    )
    repo.delete_operator_bars(
        db,
        security_id=sid,
        trade_dates=days,
        note="removed for scope",
        created_by="t",
        reason="out_of_scope",
    )
    kinds = _by_kind(lb.build(db))
    assert set(kinds) == {"outlier_accepted"}
    assert db.fetchval(
        "SELECT bool_and(reason = 'out_of_scope') FROM ops.operator_override"
    )


def test_set_reason_changes_the_reason_and_keeps_the_record(db, tmp_path, monkeypatch):
    days = _days(db, 10)
    sid = _mk(db, "LSSET")
    _bars(db, sid, days)
    repo.delete_operator_bars(
        db, security_id=sid, trade_dates=days[:3], note="bad", created_by="t"
    )
    aid = _action(db, sid, "dividend", days[5], amount=Decimal("9"))
    repo.delete_operator_action(
        db, corporate_action_id=aid, note="junk", created_by="t"
    )
    rc = _rc(db, tmp_path, monkeypatch)
    args = [
        "override",
        "set-reason",
        "--security-id",
        str(sid),
        "--target",
        "daily_price",
        "--reason",
        "out_of_scope",
        "-m",
        "removed for scope, not error",
        "--by",
        "claude",
    ]
    dry = _cli(rc, *args, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "3 overrides would become out_of_scope" in dry.output
    assert (
        db.fetchval(
            "SELECT count(*) FROM ops.operator_override WHERE reason <> 'data_error'"
        )
        == 0
    )

    done = _cli(rc, *args, "--yes")
    assert done.exit_code == 0, done.output
    rows = db.fetchall(
        "SELECT target, reason, note, detail FROM ops.operator_override ORDER BY 1"
    )
    bars = [r for r in rows if r["target"] == "daily_price"]
    assert {r["reason"] for r in bars} == {"out_of_scope"}
    assert {r["note"] for r in bars} == {"bad"}  # the original note stands
    (history,) = bars[0]["detail"]["reason_history"]
    assert (history["from"], history["to"], history["by"]) == (
        "data_error",
        "out_of_scope",
        "claude",
    )
    # The dividend delete keeps its reason, and stays a label.
    assert [lab["kind"] for lab in lb.build(db).labels] == ["dividend_deleted"]
    again = _cli(rc, *args, "--yes")
    assert "Nothing to change" in again.output

    refused = _cli(rc, "override", "set-reason", "--reason", "cleanup", "-m", "x")
    assert refused.exit_code != 0 and "Nothing selected" in refused.output


def test_prices_delete_records_its_reason(db, tmp_path, monkeypatch):
    days = _days(db, 5)
    sid = _mk(db, "LSPDR")
    _bars(db, sid, days)
    rc = _rc(db, tmp_path, monkeypatch)
    result = _cli(
        rc,
        "prices",
        "delete",
        "--security-id",
        str(sid),
        "--date",
        days[2].isoformat(),
        "-m",
        "merge leftover",
        "--reason",
        "cleanup",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert db.fetchval("SELECT reason FROM ops.operator_override") == "cleanup"
    listed = _cli(rc, "override", "list")
    assert "delete [cleanup]" in listed.output


# ---------------------------------------------------------------------------
# L3 / L5: when a label was decided, until when it applies, and how it was judged
# ---------------------------------------------------------------------------


def test_judged_flags_carry_when_and_how_they_were_judged(db):
    days = _days(db, 12)
    checked, ruled, unsaid = (_mk(db, f"LSJ{i}") for i in range(3))
    for sid in (checked, ruled, unsaid):
        _bars(db, sid, days)
    ids = {
        sid: _flag(
            db, sid, "outlier", {"trade_date": days[5].isoformat()}, {"close": 10}
        )
        for sid in (checked, ruled, unsaid)
    }
    for sid, basis in ((checked, "per_flag"), (ruled, "by_rule"), (unsaid, None)):
        repo.accept_dq_flags(
            db,
            repo.DqFilter(flag_ids=(ids[sid],)),
            note="real move",
            accepted_by="t",
            judgement_basis=basis,
        )
    labels = {lab["series"]: lab for lab in lb.build(db).labels}
    assert labels[str(checked)]["verified"] is True
    assert labels[str(ruled)]["verified"] is False
    assert "verified" not in labels[str(unsaid)]
    for lab in labels.values():
        assert lab["decided_at"] and lab["applies_until"] is None  # a market fact
    assert [lab["series"] for lab in lb.build(db, verified_only=True).labels] == [
        str(checked)
    ]


def test_a_split_loaded_label_stops_applying_when_the_split_was_loaded(db):
    days = _days(db, 10)
    sid = _mk(db, "LSLOAD")
    _bars(db, sid, days)
    _action(db, sid, "split", days[4], num=2, den=1)
    fid = _flag(db, sid, "outlier", {"trade_date": days[4].isoformat()})
    repo.resolve_dq_flags(
        db,
        repo.DqFilter(flag_ids=(fid,)),
        note="Re-checked: a split explaining it has since been loaded.",
        resolved_by="t",
    )
    (lab,) = lb.build(db).labels
    loaded = db.fetchval("SELECT loaded_at FROM core.corporate_action")
    assert lab["kind"] == "split_loaded"
    assert lab["applies_until"] == loaded.isoformat()


def test_reopen_clears_the_basis_and_revokes_the_closures_label(
    db, tmp_path, monkeypatch
):
    days = _days(db, 10)
    sid = _mk(db, "LSREO")
    _bars(db, sid, days)
    fid = _flag(db, sid, "stale", {"last_date": days[3].isoformat()})
    rc = _rc(db, tmp_path, monkeypatch)
    done = _cli(
        rc,
        "dq",
        "accept",
        str(fid),
        "--per-flag",
        "--label",
        "data_error",
        "--kind",
        "feed_gap",
        "-m",
        "trades on the issuer page; FMP has no bar",
        "--yes",
    )
    assert done.exit_code == 0, done.output
    assert "Stated 1 label (data_error)" in done.output
    row = db.fetchone("SELECT * FROM ops.dq_label")
    assert (row["security_id"], row["first_date"], row["kind"]) == (
        sid,
        days[3],
        "feed_gap",
    )
    assert (row["expect"], row["verified"], row["source"]) == (
        "DQ304",
        True,
        f"dq_flag:{fid}",
    )
    assert (
        db.fetchval(
            "SELECT judgement_basis FROM ops.data_quality_flag WHERE dq_flag_id = %s",
            (fid,),
        )
        == "per_flag"
    )

    reopened = _cli(rc, "dq", "reopen", str(fid))
    assert reopened.exit_code == 0, reopened.output
    assert (
        db.fetchval(
            "SELECT judgement_basis FROM ops.data_quality_flag WHERE dq_flag_id = %s",
            (fid,),
        )
        is None
    )
    assert db.fetchval("SELECT revoked_at IS NOT NULL FROM ops.dq_label")


def test_per_flag_and_by_rule_are_one_or_the_other(db, tmp_path, monkeypatch):
    rc = _rc(db, tmp_path, monkeypatch)
    out = _cli(rc, "dq", "resolve", "1", "--per-flag", "--by-rule", "-m", "x")
    assert out.exit_code != 0 and "one or the other" in out.output
    out = _cli(rc, "dq", "resolve", "1", "--kind", "feed_gap", "-m", "x")
    assert out.exit_code != 0 and "go with --label" in out.output
    out = _cli(
        rc,
        "dq",
        "accept",
        "1",
        "--label",
        "market_fact",
        "--kind",
        "feed_gap",
        "-m",
        "x",
    )
    assert out.exit_code != 0 and "feed_gap label is data_error" in out.output


def test_a_resolve_with_a_label_on_a_dorq_flag_takes_its_code(
    db, tmp_path, monkeypatch
):
    days = _days(db, 10)
    sid = _mk(db, "LSDQR")
    _bars(db, sid, days)
    fid = _flag(
        db,
        sid,
        "dorq_dividend_implausible",
        {"trade_date": days[4].isoformat()},
        {"code": "DQ705"},
    )
    rc = _rc(db, tmp_path, monkeypatch)
    out = _cli(
        rc,
        "dq",
        "resolve",
        str(fid),
        "--by-rule",
        "--label",
        "market_fact",
        "--kind",
        "distribution",
        "-m",
        "ex-date close = prev close - 3.01",
    )
    assert out.exit_code == 0, out.output
    # A distribution is two statements: a false DQ705 and a false price report.
    assert sorted(
        r["codes"] for r in db.fetchall("SELECT codes FROM ops.dq_label")
    ) == [
        "DQ2",
        "DQ705",
    ]
    assert not db.fetchval("SELECT bool_or(verified) FROM ops.dq_label")


# ---------------------------------------------------------------------------
# L6: ops.dq_label and `fafnir dq label`
# ---------------------------------------------------------------------------


def test_label_add_list_revoke(db, tmp_path, monkeypatch):
    sid = _mk(db, "LSADD")
    members = [_mk(db, f"LSM{i}") for i in range(3)]
    rc = _rc(db, tmp_path, monkeypatch)
    dry = _cli(
        rc,
        "dq",
        "label",
        "add",
        "--security-id",
        str(sid),
        "--first",
        "2024-03-04",
        "--last",
        "2024-03-08",
        "--kind",
        "wrong_instrument",
        "--verified",
        "-m",
        "another fund's prices from 03-04",
        "--dry-run",
    )
    assert dry.exit_code == 0, dry.output
    assert "DQ202|DQ205" in dry.output and "would be added" in dry.output
    assert db.fetchval("SELECT count(*) FROM ops.dq_label") == 0
    add = _cli(
        rc,
        "dq",
        "label",
        "add",
        "--security-id",
        str(sid),
        "--first",
        "2024-03-04",
        "--last",
        "2024-03-08",
        "--kind",
        "wrong_instrument",
        "--verified",
        "-m",
        "another fund's prices from 03-04",
        "--yes",
    )
    assert add.exit_code == 0, add.output
    cohort = _cli(
        rc,
        "dq",
        "label",
        "add",
        "--cohort",
        "--members",
        ",".join(map(str, members)),
        "--first",
        "2024-03-05",
        "--kind",
        "cohort_fact",
        "-m",
        "each a real move",
        "--yes",
    )
    assert cohort.exit_code == 0, cohort.output
    no_members = _cli(
        rc,
        "dq",
        "label",
        "add",
        "--cohort",
        "--first",
        "2024-03-05",
        "--kind",
        "cohort_fact",
        "-m",
        "x",
        "--yes",
    )
    assert no_members.exit_code != 0 and "names its members" in no_members.output
    unknown = _cli(
        rc,
        "dq",
        "label",
        "add",
        "--security-id",
        "999999",
        "--first",
        "2024-03-05",
        "--kind",
        "bad_print",
        "-m",
        "x",
        "--yes",
    )
    assert unknown.exit_code != 0 and "Unknown security" in unknown.output

    listed = _cli(rc, "dq", "label", "list", "--json")
    rows = json.loads(listed.output)
    assert [r["kind"] for r in rows] == ["cohort_fact", "wrong_instrument"]
    assert rows[0]["members"] == sorted(members)
    assert _cli(rc, "dq", "label", "list", "--security-id", str(members[1]), "--json")

    export = {lab["kind"]: lab for lab in lb.build(db).labels}
    assert export["cohort_fact"]["series"] == "*"
    assert export["cohort_fact"]["members"] == sorted(members)
    assert export["cohort_fact"]["codes"] == "DQ601"
    assert export["wrong_instrument"]["verified"] is True

    label_id = rows[1]["label_id"]
    gone = _cli(rc, "dq", "label", "revoke", str(label_id), "-m", "it was not", "--yes")
    assert gone.exit_code == 0, gone.output
    assert [lab["kind"] for lab in lb.build(db).labels] == ["cohort_fact"]


def test_a_stated_label_wins_over_the_derived_one(db):
    days = _days(db, 10)
    sid = _mk(db, "LSWIN")
    _bars(db, sid, days)
    aid = _action(db, sid, "split", days[4], num=2, den=1)
    repo.delete_operator_action(db, corporate_action_id=aid, note="dup", created_by="t")
    repo.insert_dq_labels(
        db,
        [
            {
                "security_id": sid,
                "first_date": days[4],
                "class": "data_error",
                "kind": "split_deleted",
                "expect": "DQ702",
                "note": "checked on the split-adjusted feed",
                "verified": True,
                "source": "hand-2026-10-15",
                "decided_by": "t",
            }
        ],
    )
    (lab,) = lb.build(db).labels
    assert (lab["source"], lab["expect"], lab["applies_until"]) == (
        "hand-2026-10-15",
        "DQ702",
        None,
    )


def test_import_counts_refuses_strangers_and_loads(db, tmp_path, monkeypatch):
    sid = _mk(db, "LSIMP")
    other = _mk(db, "LSIMQ")
    rc = _rc(db, tmp_path, monkeypatch)
    good = tmp_path / "hand.jsonl"
    good.write_text(
        "\n".join(
            json.dumps(x)
            for x in (
                {
                    "series": str(sid),
                    "first": "2024-03-04",
                    "last": "2024-03-04",
                    "class": "data_error",
                    "expect": "DQ702",
                    "kind": "split_deleted",
                    "note": "duplicate 1:10",
                    "source": "hand-2026-10-15",
                },
                {
                    "series": str(other),
                    "first": "2024-03-05",
                    "class": "market_fact",
                    "codes": "DQ2",
                    "kind": "news_move",
                    "note": "earnings",
                    "verified": False,
                },
            )
        )
        + "\n"
    )
    dry = _cli(rc, "dq", "label", "import", str(good), "--verified", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "split_deleted" in dry.output and "news_move" in dry.output
    assert "2 labels on 2 securities, 1 verified" in dry.output
    assert db.fetchval("SELECT count(*) FROM ops.dq_label") == 0
    done = _cli(rc, "dq", "label", "import", str(good), "--verified", "--yes")
    assert done.exit_code == 0, done.output
    rows = {r["kind"]: r for r in db.fetchall("SELECT * FROM ops.dq_label")}
    assert rows["split_deleted"]["source"] == "hand-2026-10-15"
    assert rows["news_move"]["source"] == "hand.jsonl"
    assert rows["split_deleted"]["verified"] and not rows["news_move"]["verified"]

    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "series": "424242",
                "first": "2024-03-04",
                "class": "market_fact",
                "note": "x",
            }
        )
        + "\n"
    )
    refused = _cli(rc, "dq", "label", "import", str(bad), "--dry-run")
    assert refused.exit_code != 0 and "Unknown security id(s): 424242" in refused.output
    bad.write_text('{"series": "1", "class": "market_fact"}\n')
    refused = _cli(rc, "dq", "label", "import", str(bad), "--dry-run")
    assert refused.exit_code != 0 and "bad.jsonl line 1" in refused.output


# ---------------------------------------------------------------------------
# L7: pipes
# ---------------------------------------------------------------------------


def test_export_to_stdout_reads_back_through_compare_stdin(db, tmp_path, monkeypatch):
    days = _days(db, 10)
    sid = _mk(db, "LSPIPE")
    _bars(db, sid, days)
    repo.delete_operator_bars(
        db, security_id=sid, trade_dates=[days[3]], note="bad", created_by="t"
    )
    rc = _rc(db, tmp_path, monkeypatch)
    runner = CliRunner()
    exported = runner.invoke(
        cli.main, ["--config", str(rc), "dq", "export-labels", "--out", "-"]
    )
    assert exported.exit_code == 0, exported.output
    jsonl = exported.stdout
    assert json.loads(jsonl.splitlines()[0])["kind"] == "bar_deleted"
    assert "Wrote 1 label to stdout" in exported.stderr

    shadow = tmp_path / "sh" / "2024-03-15.jsonl"
    from fafnir.dq import dorq

    dorq.write_shadow(
        [
            {
                "security_id": sid,
                "record_key": {"trade_date": days[3].isoformat()},
                "check_name": "dorq_bad_print",
                "severity": "error",
                "detail": {"code": "DQ201", "date": days[3].isoformat()},
            }
        ],
        shadow,
    )
    piped = runner.invoke(
        cli.main,
        ["--config", str(rc), "dq", "compare", str(shadow), "--labels", "-", "--json"],
        input=jsonl,
    )
    assert piped.exit_code == 0, piped.output
    assert json.loads(piped.output)["precision"]["warn"]["right"] == 1
    from_db = runner.invoke(
        cli.main,
        [
            "--config",
            str(rc),
            "dq",
            "compare",
            str(shadow),
            "--labels-from-db",
            "--json",
        ],
    )
    assert from_db.exit_code == 0, from_db.output
    assert json.loads(from_db.output)["precision"]["warn"]["right"] == 1
