"""`fafnir dq compare`: dorq's shadow night against the SQL checks and the labels."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.dq import compare as cmp
from fafnir.dq import dorq


def _row(sid, day, code="DQ201", check="dorq_bad_print", severity="error", end=None):
    return {
        "security_id": sid,
        "table_name": "core.daily_price",
        "record_key": {"trade_date": day},
        "check_name": check,
        "severity": severity,
        "detail": {"code": code, "date": day, "end_date": end},
    }


def _labels(tmp_path, *rows):
    path = tmp_path / "labels.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cmp.read_labels(path)


# ---------------------------------------------------------------------------
# Matching: the same rules as dorq-eval
# ---------------------------------------------------------------------------


def test_a_report_on_a_fault_with_its_code_is_right(tmp_path):
    labels = _labels(
        tmp_path,
        {
            "series": "7",
            "date": "2024-03-04",
            "class": "data_error",
            "expect": "DQ201|DQ204",
        },
    )
    r = cmp.Report.of(_row(7, "2024-03-06"))
    assert cmp.judge(r, labels, 3) == "right"
    assert (
        cmp.judge(cmp.Report.of(_row(7, "2024-03-06", code="DQ202")), labels, 3)
        == "other"
    )
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-12")), labels, 3) == "unlabelled"
    assert cmp.judge(cmp.Report.of(_row(8, "2024-03-04")), labels, 3) == "unlabelled"


def test_a_report_on_a_market_fact_is_false_only_on_its_date_and_codes(tmp_path):
    labels = _labels(
        tmp_path, {"series": "7", "date": "2024-03-04", "class": "market_fact"}
    )
    assert labels[0].codes == "DQ2"
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-04")), labels, 3) == "false"
    assert cmp.judge(cmp.Report.of(_row(7, "2024-03-05")), labels, 3) == "unlabelled"
    gap = cmp.Report.of(_row(7, "2024-03-04", code="DQ301", check="dorq_missing_run"))
    assert cmp.judge(gap, labels, 3) == "unlabelled"


def test_a_cross_sectional_fault_matches_only_the_cohort_row(tmp_path):
    labels = _labels(
        tmp_path,
        {"series": "*", "date": "2024-03-04", "class": "data_error", "expect": "DQ303"},
    )
    cohort = cmp.Report.of(
        _row(None, "2024-03-04", code="DQ303", check="dorq_cohort_gap")
    )
    member = cmp.Report.of(
        _row(7, "2024-03-04", code="DQ301", check="dorq_missing_run")
    )
    assert cmp.judge(cohort, labels, 3) == "right"
    assert cmp.judge(member, labels, 3) == "unlabelled"


def test_a_bad_label_names_its_line(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"series":"1","date":"2024-01-02","class":"data_error"}\n{"x":1}\n'
    )
    with pytest.raises(ValueError, match="line 2"):
        cmp.read_labels(path)


# ---------------------------------------------------------------------------
# Reading dorq's rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "members, ids",
    [
        ("9161,10640,11237", (9161, 10640, 11237)),
        (" 12, 7 ,,x", (7, 12)),
        ("12,12", (12,)),
        ([3, "2"], (2, 3)),
        ("", ()),
        (None, ()),
        ({}, ()),
    ],
)
def test_members_are_read_from_dorqs_string_or_a_list(members, ids):
    assert cmp._ids(members) == ids


#: A DQ601 row as dorq 0.9.0 wrote it, verbatim from the shadow file of
#: 2026-10-13. A hand-built fixture with ``members`` as a list is how v1.4.1 came
#: to read a format dorq never writes.
DORQ_090_COHORT_ROW = (
    '{"check_name": "dorq_cohort_move", "detail": {"check": "cohort-move",'
    ' "classification": "data_error", "code": "DQ601", "date": "2026-10-05",'
    ' "detail": {"factor": 0.6487437185929649, "members":'
    ' "9161,10640,11237,405953,660856", "ratio": "3:2", "series_moving": 5},'
    ' "dorq": {"config_hash": "334f0095e5da4998", "version": "0.9.0"},'
    ' "evidence": [], "hypotheses": {}, "label": null, "line": null, "message":'
    ' "5 series move ×0.6487 (≈ a 3:2 split) on 2026-10-05: 9161,'
    " 10640, 11237, 405953, 660856: a vendor's mass adjustment, or splits the"
    ' actions file is missing", "p_error": 1, "provisional": false, "record_key":'
    ' {"trade_date": "2026-10-05"}, "series": null, "severity": "warn", "source":'
    ' "XNYS, with calendar.csv for 1990-01-02..2028-12-29", "suggested_action":'
    ' null}, "record_key": {"trade_date": "2026-10-05"}, "security_id": null,'
    ' "severity": "warn", "table_name": "core.daily_price"}'
)


def test_a_dorq_090_cohort_row_names_its_members():
    r = cmp.Report.of(json.loads(DORQ_090_COHORT_ROW))
    assert r.members == (9161, 10640, 11237, 405953, 660856)
    assert r.securities == (9161, 10640, 11237, 405953, 660856)


# ---------------------------------------------------------------------------
# Against the queue
# ---------------------------------------------------------------------------


def _mk(db, symbol):
    repo.ensure_exchange(db, "NASDAQ", "Nasdaq", "US")
    return repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code="NASDAQ",
    )


@pytest.mark.integration
def test_overlap_counts_both_directions(db, tmp_path):
    a = _mk(db, "CMPA")
    b = _mk(db, "CMPB")
    for sid, check, key in (
        (a, "outlier", {"trade_date": "2024-03-04"}),  # dorq reports it too
        (b, "outlier", {"trade_date": "2024-03-05"}),  # dorq does not
        (a, "gap", {"trade_date": "2024-03-07"}),  # dorq reports a missing run
        (a, "stale", {"last_date": "2024-01-02"}),  # outside the window
    ):
        repo.add_dq_flag_once(db, check_name=check, security_id=sid, record_key=key)
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [
            _row(a, "2024-03-05"),
            _row(
                a, "2024-03-07", code="DQ301", check="dorq_missing_run", severity="warn"
            ),
            _row(b, "2024-03-01", code="DQ202", check="dorq_scale_shift"),
        ],
        shadow,
    )
    labels = _labels(
        tmp_path,
        {
            "series": str(a),
            "date": "2024-03-05",
            "class": "data_error",
            "expect": "DQ201",
        },
        {"series": str(b), "date": "2024-03-01", "class": "market_fact"},
    )
    c = cmp.compare(db, shadow, labels=labels)
    assert c.window_from == dt.date(2024, 3, 1)
    assert c.window_to == dt.date(2024, 3, 8)
    assert {
        k: {"flags": e["flags"], "also_dorq": e["also_dorq"]}
        for k, e in c.by_sql_check.items()
    } == {
        "outlier": {"flags": 2, "also_dorq": 1},
        "gap": {"flags": 1, "also_dorq": 1},
        "stale": {"flags": 0, "also_dorq": 0},
    }
    # No run recorded the file, so every open flag holds as it stands now.
    assert c.state_at is None
    assert c.by_sql_check["outlier"]["holding"] == 2
    assert c.by_sql_check["outlier"]["unmatched"] == {
        "holding": 1,
        "closed": 0,
        "traded_since": 0,
        "inactive": 0,
    }
    assert c.by_code == {
        "DQ201": {"reports": 1, "also_sql": 1},
        "DQ202": {"reports": 1, "also_sql": 0},
        "DQ301": {"reports": 1, "also_sql": 1},
    }
    # The missing run two days from the bad print is near a fault, with another
    # code: an "other fault", as dorq-eval counts it.
    p = c.precision_warn
    assert (p.right, p.other, p.false, p.unlabelled) == (1, 1, 1, 0)
    assert p.precision == pytest.approx(1 / 3)
    assert p.fault_precision == pytest.approx(2 / 3)
    assert c.precision_error.judged == 2
    assert c.precision_error.precision == 0.5
    text = cmp.format_comparison(c)
    assert "outlier" in text and "0.333" in text


@pytest.mark.integration
def test_cli_compare_reads_the_newest_shadow_file(db, tmp_path, monkeypatch):
    sid = _mk(db, "CMPC")
    shadow_dir = tmp_path / "shadow"
    dorq.write_shadow([_row(sid, "2024-02-01")], shadow_dir / "2024-02-02.jsonl")
    dorq.write_shadow([_row(sid, "2024-03-01")], shadow_dir / "2024-03-04.jsonl")
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{shadow_dir}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "compare", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["shadow_file"].endswith("2024-03-04.jsonl")
    assert payload["dorq_reports"] == 1
    assert payload["precision"] is None

    empty = tmp_path / "none"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{empty}"\n')
    result = CliRunner().invoke(cli.main, ["--config", str(rc), "dq", "compare"])
    assert result.exit_code != 0
    assert "No shadow file" in result.output


# ---------------------------------------------------------------------------
# What `dq compare` got wrong on the 2026-10-12 shadow night
# ---------------------------------------------------------------------------


def _run_row(db, as_of, since, *, started="2024-03-09 04:00+00", params=None):
    """The ops.ingestion_run row of the shadow run that wrote ``as_of``'s file."""
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, params, window_from,"
        " window_to, status, started_at, finished_at)"
        " VALUES ('dorq', 'dq-shadow', %s::jsonb, '2023-06-01', %s, 'success', %s, %s)",
        (
            json.dumps(
                {"since": since, "shadow": True, "full": False, **(params or {})}
            ),
            as_of,
            started,
            started,
        ),
    )


def _flag(
    db,
    sid,
    check,
    key,
    *,
    detected="2024-03-08 23:00+00",
    resolved=None,
    accepted=False,
):
    db.execute(
        "INSERT INTO ops.data_quality_flag (security_id, table_name, record_key,"
        " check_name, severity, detected_at, resolved_at, resolved_by,"
        " resolution_note, accepted_at, accepted_by, accepted_note)"
        " VALUES (%s, 'core.daily_price', %s::jsonb, %s, 'warn', %s, %s, %s, %s, %s,"
        " %s, %s)",
        (
            sid,
            json.dumps(key),
            check,
            detected,
            resolved,
            "t" if resolved else None,
            "n" if resolved else None,
            resolved if accepted else None,
            "t" if accepted else None,
            "n" if accepted else None,
        ),
    )


def _bar(db, sid, day):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": dt.date.fromisoformat(day),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1000,
            }
        ],
    )


def _sql(c, check):
    e = c.by_sql_check[check]
    return (e["flags"], e["also_dorq"], e["holding"], e["holding_also_dorq"])


@pytest.mark.integration
def test_a_stale_flag_keyed_before_the_window_is_matched_within_the_slack(db, tmp_path):
    """SBXD and four others: the SQL flag is keyed on the last bar's date (10-02),
    DQ304 on the first missing session (10-05). The slack paired them, but the
    fetch asked only for flags dated inside the window."""
    sid = _mk(db, "CMPST")
    _run_row(db, "2024-03-08", "2024-03-04")
    _flag(db, sid, "stale", {"last_date": "2024-03-01"})
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [_row(sid, "2024-03-04", code="DQ304", check="dorq_stale_feed")], shadow
    )
    c = cmp.compare(db, shadow)
    assert c.window_from == dt.date(2024, 3, 4)
    assert _sql(c, "stale") == (1, 1, 1, 1)
    assert c.by_code["DQ304"] == {"reports": 1, "also_sql": 1}


@pytest.mark.integration
def test_a_misdated_split_matches_the_move_it_explains(db, tmp_path):
    """ZBAO and WORX: DQ701 is keyed at the split's ex-date; the move the SQL
    outlier sits on is its move_date."""
    sid = _mk(db, "CMPSP")
    _run_row(db, "2024-03-15", "2024-03-01")
    _flag(db, sid, "outlier", {"trade_date": "2024-03-05"})
    row = _row(sid, "2024-03-12", code="DQ701", check="dorq_split_misdated")
    row["detail"]["detail"] = {"move_date": "2024-03-05"}
    shadow = tmp_path / "2024-03-15.jsonl"
    dorq.write_shadow([row], shadow)
    c = cmp.compare(db, shadow)
    assert _sql(c, "outlier") == (1, 1, 1, 1)
    assert c.by_code["DQ701"] == {"reports": 1, "also_sql": 1}

    # A week from the flag, without the move date it would match nothing.
    del row["detail"]["detail"]
    dorq.write_shadow([row], shadow)
    assert _sql(cmp.compare(db, shadow), "outlier") == (1, 0, 1, 0)


@pytest.mark.integration
@pytest.mark.parametrize(
    "members",
    [
        pytest.param(lambda a, b: f"{a},{b}", id="dorq"),  # as dorq 0.9 writes it
        pytest.param(lambda a, b: [str(a), b], id="list"),
    ],
)
def test_a_cohort_row_covers_the_securities_it_names(db, tmp_path, members):
    """XRPN, VCIG and SBFM: dorq named each in a DQ601 cohort row, which has no
    security_id, so it covered no one; and DQ601 was not an outlier successor.
    v1.4.1 read only a list, and dorq writes a comma-separated string."""
    a, b, other = (_mk(db, s) for s in ("CMPCA", "CMPCB", "CMPCC"))
    _run_row(db, "2024-03-08", "2024-03-04")
    for sid in (a, b, other):
        _flag(db, sid, "outlier", {"trade_date": "2024-03-05"})
    row = _row(None, "2024-03-05", code="DQ601", check="dorq_cohort_move")
    row["detail"]["detail"] = {"members": members(a, b)}
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow([row], shadow)
    c = cmp.compare(db, shadow)
    assert _sql(c, "outlier") == (3, 2, 3, 2)
    # One report, however many it names.
    assert c.by_code["DQ601"] == {"reports": 1, "also_sql": 1}

    # dorq up to 0.8.1 names no members: the row covers no one.
    del row["detail"]["detail"]
    dorq.write_shadow([row], shadow)
    c = cmp.compare(db, shadow)
    assert _sql(c, "outlier") == (3, 0, 3, 0)
    assert c.by_code["DQ601"] == {"reports": 1, "also_sql": 0}


@pytest.mark.integration
def test_recall_counts_the_conditions_that_held_when_dorq_ran(db, tmp_path):
    """Of 2026-10-12's 21 unmatched stale flags, 12 were on securities that had
    traded since and 5 were closed. Recall is on the conditions that held -- as
    they stood when dorq ran, so the night's figures do not move as the queue is
    worked."""
    held, traded, traded_late, delisted, closed, closed_late, later = (
        _mk(db, f"CMPS{i}") for i in range(7)
    )
    _run_row(db, "2024-03-08", "2024-03-04", started="2024-03-09 04:00+00")
    for sid in (held, traded, traded_late, delisted):
        _flag(db, sid, "stale", {"last_date": "2024-03-04"})
    for sid, day in ((traded, "2024-03-04"), (traded, "2024-03-06")):
        _bar(db, sid, day)
    # Its next bar came after the night: on the night, it still held.
    for sid, day in ((traded_late, "2024-03-04"), (traded_late, "2024-03-11")):
        _bar(db, sid, day)
    db.execute(
        "UPDATE core.security SET is_actively_trading = false,"
        " delisted_date = '2024-03-07' WHERE security_id = %s",
        (delisted,),
    )
    key = {"trade_date": "2024-03-05"}
    _flag(db, closed, "outlier", key, resolved="2024-03-08 12:00+00")
    _flag(db, closed_late, "outlier", key, resolved="2024-03-10 12:00+00")
    _flag(db, later, "outlier", key, detected="2024-03-10 23:00+00")  # not the night's
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [_row(held, "2024-03-05", code="DQ304", check="dorq_stale_feed")], shadow
    )

    def check(c):
        assert c.state_at == dt.datetime(2024, 3, 9, 4, 0, tzinfo=dt.timezone.utc)
        assert _sql(c, "stale") == (4, 1, 2, 1)
        assert c.by_sql_check["stale"]["unmatched"] == {
            "holding": 1,
            "closed": 0,
            "traded_since": 1,
            "inactive": 1,
        }
        assert _sql(c, "outlier") == (2, 0, 1, 0)
        assert c.by_sql_check["outlier"]["unmatched"] == {
            "holding": 1,
            "closed": 1,
            "traded_since": 0,
            "inactive": 0,
        }

    check(cmp.compare(db, shadow))
    text = cmp.format_comparison(cmp.compare(db, shadow))
    assert "2 holding" not in text and "1 traded since, 1 inactive" in text

    # A week on, the queue worked: the night's figures stand.
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 't',"
        " resolution_note = 'n' WHERE resolved_at IS NULL"
    )
    _bar(db, held, "2024-03-12")
    check(cmp.compare(db, shadow))


@pytest.mark.integration
def test_a_backfilled_securitys_flags_are_compared_at_every_date(db, tmp_path):
    """CELG-RI's ten SQL outliers sit years before the window. Once dorq judges
    its whole history, compare has to fetch them too, or every match is "dorq
    only" and recall never sees them."""
    new = _mk(db, "CMPBN")
    other = _mk(db, "CMPBO")
    _run_row(
        db,
        "2024-03-08",
        "2024-03-04",
        params={"backfill": {"securities": 1, "security_ids": [new], "reports": 1}},
    )
    _flag(db, new, "outlier", {"trade_date": "2023-06-12"})
    _flag(db, other, "outlier", {"trade_date": "2023-06-12"})  # not judged tonight
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow([_row(new, "2023-06-12")], shadow)
    c = cmp.compare(db, shadow)
    assert c.backfill_securities == 1
    assert _sql(c, "outlier") == (1, 1, 1, 1)
    assert c.by_code["DQ201"] == {"reports": 1, "also_sql": 1}
    assert "Backfill    : the whole history of 1 securities" in cmp.format_comparison(c)


@pytest.mark.integration
def test_a_report_on_an_accepted_flag_is_called_out(db, tmp_path):
    """INV's outlier was accepted as a market fact. A report on it counts against
    dorq's precision -- with --labels, when dorq-eval's matching says so."""
    sid = _mk(db, "CMPAC")
    _run_row(db, "2024-03-08", "2024-03-04")
    _flag(
        db,
        sid,
        "outlier",
        {"trade_date": "2024-03-05"},
        resolved="2024-03-06 12:00+00",
        accepted=True,
    )
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow([_row(sid, "2024-03-05")], shadow)
    labels = _labels(
        tmp_path, {"series": str(sid), "date": "2024-03-05", "class": "market_fact"}
    )
    c = cmp.compare(db, shadow, labels=labels)
    assert c.on_accepted == 1
    assert _sql(c, "outlier") == (1, 1, 0, 0)  # closed before the night
    assert c.precision_warn.false == 1
    assert "accepted as a market fact: 1" in cmp.format_comparison(c)


@pytest.mark.integration
def test_compare_json_keeps_its_keys(db, tmp_path, monkeypatch):
    sid = _mk(db, "CMPJS")
    shadow_dir = tmp_path / "shadow"
    _run_row(db, "2024-03-08", "2024-03-04")
    _flag(db, sid, "outlier", {"trade_date": "2024-03-05"})
    dorq.write_shadow([_row(sid, "2024-03-05")], shadow_dir / "2024-03-08.jsonl")
    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{shadow_dir}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    result = CliRunner().invoke(
        cli.main, ["--config", str(rc), "dq", "compare", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # v1.4.2's keys, every one of them, and v1.5.0's beside them.
    assert {
        "shadow_file",
        "window",
        "dorq_reports",
        "by_code",
        "by_sql_check",
        "state_at",
        "backfill_securities",
        "on_accepted",
        "precision",
    } <= set(payload)
    assert {
        "conditions",
        "new_per_night",
        "pre_window",
        "holding_source",
        "dorq_eval",
    } <= set(payload)
    assert payload["window"] == ["2024-03-04", "2024-03-08"]
    assert payload["by_code"] == {"DQ201": {"reports": 1, "also_sql": 1}}
    outlier = payload["by_sql_check"]["outlier"]
    assert (outlier["flags"], outlier["also_dorq"]) == (1, 1)
    assert (outlier["holding"], outlier["holding_also_dorq"]) == (1, 1)
    assert payload["state_at"].startswith("2024-03-09T04:00:00")


# ---------------------------------------------------------------------------
# v1.5.0: the cutover bar, measured
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_new_flags_per_night_against_the_previous_night(db, tmp_path):
    """C1. The conditions this night's file holds that the previous night's did
    not, against the SQL flags written between the two runs' starts."""
    a, b, c, bf = (_mk(db, f"CMPN{i}") for i in range(4))
    shadow = tmp_path / "shadow"
    _run_row(db, "2024-03-07", "2024-03-01", started="2024-03-08 04:00+00")
    _run_row(
        db,
        "2024-03-08",
        "2024-03-04",
        started="2024-03-09 04:00+00",
        params={"backfill": {"security_ids": [bf], "before": "2024-03-04"}},
    )
    dorq.write_shadow(
        [_row(a, "2024-03-05"), _row(b, "2024-03-06")], shadow / "2024-03-07.jsonl"
    )
    dorq.write_shadow(
        [
            _row(b, "2024-03-06"),  # judged again: not new
            _row(c, "2024-03-07"),
            _row(bf, "2023-05-02"),  # the history pass
        ],
        shadow / "2024-03-08.jsonl",
    )
    for day, detected in (
        ("2024-03-04", "2024-03-08 03:00+00"),  # before the previous run: its own
        ("2024-03-05", "2024-03-08 23:00+00"),
        ("2024-03-06", "2024-03-09 03:30+00"),
    ):
        _flag(db, a, "outlier", {"trade_date": day}, detected=detected)
    _flag(db, a, "sparse_coverage", {}, detected="2024-03-08 23:00+00")  # not SQL's
    c_ = cmp.compare(db, shadow / "2024-03-08.jsonl")
    assert c_.new_per_night == {
        "previous_file": str(shadow / "2024-03-07.jsonl"),
        "dorq": 2,
        "dorq_backfill": 1,
        "sql": 2,
        "change": 0.0,
    }
    text = cmp.format_comparison(c_)
    assert (
        "New flags   : dorq 2 (history pass 1) vs SQL 2 (change +0%; the bar is -70%)"
        in text
    )
    first = cmp.compare(db, shadow / "2024-03-07.jsonl")
    assert first.new_per_night is None
    assert "New flags   : n/a" in cmp.format_comparison(first)


@pytest.mark.integration
def test_a_stale_feed_that_started_before_the_window_is_still_measured(db, tmp_path):
    """C2. XJUL/XAPR/XFEB/FPAS stopped on 10-02; once 10-05 left --since, neither
    side fetched their stale flags, and the miss was invisible."""
    reported, missed, closed, traded = (_mk(db, f"CMPP{i}") for i in range(4))
    _run_row(db, "2024-03-08", "2024-03-04", started="2024-03-09 04:00+00")
    for sid in (reported, missed, closed, traded):
        _flag(db, sid, "stale", {"last_date": "2024-02-15"})
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = '2024-03-01', resolved_by = 't'"
        " WHERE security_id = %s",
        (closed,),
    )
    _bar(db, traded, "2024-02-15")
    _bar(db, traded, "2024-02-20")
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [_row(reported, "2024-03-05", code="DQ304", check="dorq_stale_feed")], shadow
    )
    c = cmp.compare(db, shadow)
    assert c.pre_window == {"stale": {"holding": 2, "also_dorq": 1}}
    assert c.by_sql_check["stale"]["flags"] == 0  # the window's own figures stand
    assert "stale before the window, still holding at the run: 1 (50%) of 2" in (
        cmp.format_comparison(c)
    )


@pytest.mark.integration
def test_the_runs_holding_snapshot_freezes_the_night(db, tmp_path):
    """C3. FMP delivered fifteen securities' bars late, in the 10-14 load, and the
    10-12 night's stale recall moved from 23/25 to 8/10 after the fact."""
    late, closed_after, closed_before = (_mk(db, f"CMPH{i}") for i in range(3))
    started = dt.datetime(2024, 3, 9, 4, 0, tzinfo=dt.timezone.utc)
    _run_row(db, "2024-03-08", "2024-03-04", started="2024-03-09 04:00+00")
    for sid in (late, closed_after, closed_before):
        _flag(db, sid, "stale", {"last_date": "2024-03-04"})
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = '2024-03-08 12:00+00',"
        " resolved_by = 't' WHERE security_id = %s",
        (closed_before,),
    )
    snapshot = cmp.holding_snapshot(
        db,
        window_from=dt.date(2024, 3, 4),
        as_of=dt.date(2024, 3, 8),
        started_at=started,
    )
    assert set(snapshot["states"].values()) == {"holding"}
    assert len(snapshot["states"]) == 2  # the one closed before the run is not in it
    db.execute(
        "UPDATE ops.ingestion_run SET params = params || %s::jsonb",
        (json.dumps({"holding": snapshot}),),
    )
    # After the night: the late bar lands, and a flag is closed.
    _bar(db, late, "2024-03-06")
    db.execute(
        "UPDATE ops.data_quality_flag SET resolved_at = now(), resolved_by = 't'"
        " WHERE security_id = %s",
        (closed_after,),
    )
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [_row(late, "2024-03-05", code="DQ304", check="dorq_stale_feed")], shadow
    )
    c = cmp.compare(db, shadow)
    assert c.holding_source == "snapshot"
    assert _sql(c, "stale") == (3, 1, 2, 1)
    assert "the run's own snapshot" in cmp.format_comparison(c)

    db.execute("UPDATE ops.ingestion_run SET params = params - 'holding'")
    c = cmp.compare(db, shadow)
    assert c.holding_source == "recomputed"
    assert _sql(c, "stale") == (3, 1, 1, 0)  # the late bar reads as "traded since"
    assert "may drift" in cmp.format_comparison(c)


@pytest.mark.integration
def test_a_dorq_run_records_the_snapshot_compare_reads(db, tmp_path):
    sid = _mk(db, "CMPHR")
    _flag(db, sid, "stale", {"last_date": "2024-03-04"})
    flag_id = db.fetchval("SELECT dq_flag_id FROM ops.data_quality_flag")
    run_id = dorq.start_run(
        db,
        dorq.Window(as_of=dt.date(2024, 3, 8), since=dt.date(2024, 3, 4)),
        shadow=True,
    )
    snapshot = dorq._holding_snapshot(
        db,
        run_id,
        dorq.Window(as_of=dt.date(2024, 3, 8), since=dt.date(2024, 3, 4)),
        None,
    )
    assert snapshot["states"] == {str(flag_id): "holding"}
    assert snapshot["since"] == "2024-02-02"


@pytest.mark.integration
def test_coverage_and_both_precisions_per_code(db, tmp_path):
    """C4. Of 145 reports, the export judged 12: say so, and say which of them were
    a fault reported under the wrong code (DQ701 on a phantom split row)."""
    a, b, c_, d = (_mk(db, f"CMPQ{i}") for i in range(4))
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [
            _row(a, "2024-03-05", code="DQ701", check="dorq_split_misdated"),
            _row(b, "2024-03-05"),
            _row(c_, "2024-03-05", severity="warn"),
            _row(d, "2024-03-05"),
        ],
        shadow,
    )
    labels = _labels(
        tmp_path,
        {
            "series": str(a),
            "date": "2024-03-05",
            "class": "data_error",
            "expect": "DQ702",
        },
        {
            "series": str(b),
            "date": "2024-03-05",
            "class": "data_error",
            "expect": "DQ201",
        },
        {"series": str(c_), "date": "2024-03-05", "class": "market_fact"},
    )
    c = cmp.compare(db, shadow, labels=labels)
    dq701, dq201 = c.precision_by_code["DQ701"], c.precision_by_code["DQ201"]
    assert (dq701.precision, dq701.fault_precision) == (0.0, 1.0)
    assert (dq201.right, dq201.false, dq201.unlabelled) == (1, 1, 1)
    assert dq201.precision == 0.5 and dq201.fault_precision == 0.5
    payload = c.as_dict()["precision"]
    assert payload["coverage"] == {"judged": 3, "reports": 4, "share": 0.75}
    assert payload["by_code"]["DQ701"]["fault_precision"] == 1.0
    text = cmp.format_comparison(c)
    assert "judged 3 of 4 reports (75%)" in text
    assert "strict = right / judged" in text


@pytest.mark.integration
def test_a_repair_label_does_not_judge_a_night_that_ran_after_it(db, tmp_path):
    """L3. WORX's rescale (10-13 18:52) covered 2016..2026, and the 10-13 night ran
    at 10-14 03:48, on the corrected bars: nine reports scored against a repair dorq
    never saw. A market fact always applies."""
    sid = _mk(db, "CMPT")
    _run_row(db, "2024-03-08", "2024-03-04", started="2024-03-09 04:00+00")
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(
        [_row(sid, "2024-03-05", code="DQ202"), _row(sid, "2024-03-01")], shadow
    )
    labels = _labels(
        tmp_path,
        {
            "series": str(sid),
            "first": "2024-02-01",
            "last": "2024-03-06",
            "class": "data_error",
            "expect": "DQ202",
            "decided_at": "2024-03-08T18:52:00+00:00",
            "applies_until": "2024-03-08T18:52:00+00:00",
        },
        {
            "series": str(sid),
            "date": "2024-03-01",
            "class": "market_fact",
            "verified": True,
        },
    )
    c = cmp.compare(db, shadow, labels=labels)
    assert (c.precision_warn.right, c.precision_warn.false) == (0, 1)
    assert c.labels_used == 1 and c.labels_time_scoped
    c = cmp.compare(db, shadow, labels=labels, labels_ignore_time=True)
    assert c.precision_warn.right == 1
    c = cmp.compare(
        db, shadow, labels=labels, verified_only=True, labels_ignore_time=True
    )
    assert (c.precision_warn.right, c.precision_warn.false) == (0, 1)
    db.execute("UPDATE ops.ingestion_run SET started_at = '2024-03-08 12:00+00'")
    c = cmp.compare(db, shadow, labels=labels)  # the night ran before the repair
    assert c.precision_warn.right == 1


def test_a_cohort_fact_covers_only_the_cohorts_naming_its_members(tmp_path):
    labels = _labels(
        tmp_path,
        {
            "series": "*",
            "date": "2024-03-04",
            "class": "market_fact",
            "codes": "DQ601",
            "members": [3, 4],
        },
    )

    def cohort(members):
        row = _row(None, "2024-03-04", code="DQ601", check="dorq_cohort_move")
        row["detail"]["detail"] = {"members": members, "ratio": "2:1"}
        return cmp.Report.of(row)

    assert cmp.judge(cohort("4,9"), labels, 3) == "false"
    assert cmp.judge(cohort("8,9"), labels, 3) == "unlabelled"
    assert cmp.judge(cmp.Report.of(_row(3, "2024-03-04", code="DQ601")), labels, 3) == (
        "unlabelled"
    )


def _cohort(day, ratio, members=None):
    row = _row(None, day, code="DQ601", check="dorq_cohort_move", severity="warn")
    row["detail"]["detail"] = {"ratio": ratio}
    if members is not None:
        row["detail"]["detail"]["members"] = members
    return row


def test_two_cohorts_on_one_date_are_two_conditions():
    """C5. The 10-14 file: a 2:1 group and a 3:2 group, both on 10-06, both keyed
    {"trade_date": "2026-10-06"} by dorq 0.9."""
    two, three = _cohort("2026-10-06", "2:1", "1,2"), _cohort(
        "2026-10-06", "3:2", "3,4"
    )
    assert dorq.condition_key(two) != dorq.condition_key(three)
    # A cohort's membership changes as it is re-judged, and dorq 0.8.1 writes none:
    # the same group is the same condition.
    assert dorq.condition_key(_cohort("2026-10-06", "3:2")) == dorq.condition_key(
        _cohort("2026-10-06", "3:2", "3,4,5")
    )
    # dorq 0.10 puts the ratio in the key itself; that is taken as it is.
    keyed = _cohort("2026-10-06", "3:2")
    keyed["record_key"] = {"trade_date": "2026-10-06", "ratio": "3:2"}
    assert dorq.condition_key(keyed) == dorq.condition_key(three)
    # Without a ratio, the members tell them apart.
    assert dorq.condition_key(_cohort("2026-10-06", "", "1")) != dorq.condition_key(
        _cohort("2026-10-06", "", "2")
    )
    # A row about one security is keyed as dorq keys it.
    assert dorq.condition_key(_row(5, "2026-10-06"))[2] == '{"trade_date":"2026-10-06"}'


@pytest.mark.integration
def test_two_cohorts_on_one_date_are_two_flags_and_two_compare_conditions(db, tmp_path):
    rows = [_cohort("2024-03-05", "2:1", "1,2"), _cohort("2024-03-05", "3:2", "3,4")]
    assert dorq.ingest(db, rows) == {"dorq_cohort_move": 2}
    assert dorq.ingest(db, rows) == {}  # each still open: written once
    keys = [
        r["record_key"]
        for r in db.fetchall(
            "SELECT record_key FROM ops.data_quality_flag ORDER BY dq_flag_id"
        )
    ]
    assert keys == [
        {"trade_date": "2024-03-05", "ratio": "2:1"},
        {"trade_date": "2024-03-05", "ratio": "3:2"},
    ]
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow(rows, shadow)
    c = cmp.compare(db, shadow)
    assert (c.dorq_reports, c.conditions) == (2, 2)


@pytest.mark.integration
def test_several_nights_count_each_condition_once_as_first_reported(
    db, tmp_path, monkeypatch
):
    """The cutover bar is over the shadow period. A condition re-reported nightly
    counts once, judged as the night the queue would have written it."""
    sid = _mk(db, "CMPS")
    shadow = tmp_path / "shadow"
    dorq.write_shadow(
        [_row(sid, "2024-03-05", severity="warn"), _row(sid, "2024-03-20")],
        shadow / "2024-03-07.jsonl",
    )
    dorq.write_shadow(
        [_row(sid, "2024-03-05", severity="error")], shadow / "2024-03-08.jsonl"
    )
    labels = _labels(
        tmp_path,
        {"series": str(sid), "date": "2024-03-05", "class": "data_error"},
        {"series": str(sid), "date": "2024-03-20", "class": "market_fact"},
    )
    nights = [
        cmp.compare(db, shadow / f, labels=labels)
        for f in ("2024-03-08.jsonl", "2024-03-07.jsonl")
    ]
    s = cmp.summarize(nights)
    assert s.conditions == 2
    assert (s.precision_warn.right, s.precision_warn.false) == (1, 1)
    # First reported at warn: not an error-severity condition.
    assert s.precision_error.judged == 1 and s.precision_error.false == 1
    assert [Path(n.shadow_file).stem for n in s.nights] == ["2024-03-07", "2024-03-08"]

    rc = tmp_path / "fafnirrc"
    rc.write_text(
        f'[database]\ndsn = "{db.dsn}"\n[dq]\ndorq_shadow_dir = "{shadow}"\n'
        f'dorq_path = "{tmp_path / "none"}"\n'
    )
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    files = [str(shadow / "2024-03-07.jsonl"), str(shadow / "2024-03-08.jsonl")]
    label_file = tmp_path / "labels.jsonl"
    result = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(rc),
            "dq",
            "compare",
            *files,
            "--labels",
            str(label_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["nights"]) == 2
    assert payload["all"]["conditions"] == 2
    text = (
        CliRunner()
        .invoke(
            cli.main,
            ["--config", str(rc), "dq", "compare", *files, "--labels", str(label_file)],
        )
        .output
    )
    assert "All 2 nights, as distinct conditions (2)" in text


FAKE_EVAL = """#!/usr/bin/env python3
import sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
rows = open(args["--results"]).read().splitlines()
labels = open(args["--labels"]).read().splitlines()
open(sys.argv[0] + ".seen", "w").write(rows[0] + "\\n" + str(len(labels)))
print("# report\\n\\n## Per check, at warn and above\\n")
print("| Check | Reported | Right | Other fault | False | Unlabelled | Precision |"
      " Fault precision | Faults | Found | Recall | F1 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
print("| DQ201 | 1 | RIGHT | 0 | 0 | 0 | 1.000 | 1.000 | 1 | 1 | 1.000 | 1.000 |")
print("\\n## By severity\\n\\n| Severity | Reported | Precision | Fault precision |")
print("|---|---|---|---|")
print("| warn | 0 | – | – |")
print("| error | 1 | 1.000 | 1.000 |")
"""


@pytest.mark.integration
@pytest.mark.parametrize("right, agrees", [(1, True), (0, False)])
def test_dorq_eval_is_run_on_the_same_night_and_labels(db, tmp_path, right, agrees):
    sid = _mk(db, "CMPEV")
    shadow = tmp_path / "2024-03-08.jsonl"
    dorq.write_shadow([_row(sid, "2024-03-05")], shadow)
    labels = _labels(
        tmp_path,
        {
            "series": str(sid),
            "date": "2024-03-05",
            "class": "data_error",
            "expect": "DQ201",
        },
    )
    fake = tmp_path / "dorq-eval"
    fake.write_text(FAKE_EVAL.replace("RIGHT", str(right)))
    fake.chmod(0o755)
    c = cmp.compare(db, shadow, labels=labels)
    ev = cmp.run_dorq_eval(str(fake), shadow, labels, None)
    assert ev["by_severity"]["error"]["precision"] == 1.0
    c.dorq_eval = {**ev, "disagreements": cmp.eval_disagreements(c, ev)}
    seen = (tmp_path / "dorq-eval.seen").read_text().splitlines()
    assert seen == ["series,date,end_date,code,severity,p_error,message", "1"]
    text = cmp.format_comparison(c)
    if agrees:
        assert "agrees with compare, code by code" in text
    else:
        assert "DISAGREES" in text and "DQ201: compare 1/0/0, dorq-eval 0/0/0" in text
    broken = cmp.run_dorq_eval(str(tmp_path / "missing"), shadow, labels, None)
    assert "error" in broken
