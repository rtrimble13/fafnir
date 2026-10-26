"""
`fafnir source sharadar-prune`: the Sharadar files nothing reads any more are
deleted, a short history kept (needs FAFNIR_TEST_DSN).

Each file is a real file in a temporary ``bulk_dir`` with its ledger row, aged by
setting ``fetched_at`` (and the file's mtime, for what the ledger does not hold).
Synthetic content only (ADR 0012 §8).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import fundamentals as fu
from fafnir.ingest import sharadar_prices as sp
from fafnir.ingest import sharadar_prune as pr

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
HEADER = "ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated\n"


def _file(db, bulk, table, kind, *, days_ago, tag):
    """A recorded file fetched ``days_ago`` before NOW; its ledger row."""
    body = (HEADER + f"ZZ{tag},2026-01-02,1,1,1,1,1,1,1,2026-01-02\n").encode()
    sha = hashlib.sha256(body).hexdigest()
    (bulk / f"{sha}.csv").write_bytes(body)
    entry, _ = repo.record_sharadar_file(
        db,
        vendor_table=table,
        kind=kind,
        filters={"lastupdated.gte": "2026-01-01"} if kind == "delta" else {},
        path=f"{sha}.csv",
        member=None,
        sha256=sha,
        nbytes=len(body),
        rows=1,
        columns=None,
        ingestion_run_id=None,
    )
    if kind == "full":
        repo.supersede_sharadar_fulls(db, table, entry["file_id"])
    db.execute(
        "UPDATE landing.sharadar_file SET fetched_at = %s WHERE file_id = %s",
        (NOW - timedelta(days=days_ago), entry["file_id"]),
    )
    return entry


def _aged(path, *, hours):
    stamp = (NOW - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _status(db, entry):
    return db.fetchval(
        "SELECT status FROM landing.sharadar_file WHERE file_id = %s",
        (entry["file_id"],),
    )


def _nightly_through(db, **through):
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, status, params) "
        "VALUES ('sharadar', %s, 'success', %s::jsonb)",
        (sp.NIGHTLY_ENDPOINT, json.dumps({"through": through})),
    )


def test_what_is_kept_and_what_goes(db, tmp_path):
    bulk = tmp_path
    # TICKERS: re-downloaded nightly. Current, two within a week, two older.
    t_old = _file(db, bulk, "tickers", "full", days_ago=30, tag="T1")
    t_older = _file(db, bulk, "tickers", "full", days_ago=20, tag="T2")
    t_week = [
        _file(db, bulk, "tickers", "full", days_ago=d, tag=f"W{d}") for d in (5, 2)
    ]
    t_now = _file(db, bulk, "tickers", "full", days_ago=0, tag="T0")
    # SEP: the previous full is kept whatever its age.
    s_prev = _file(db, bulk, "stocks", "full", days_ago=200, tag="S1")
    s_full = _file(db, bulk, "stocks", "full", days_ago=100, tag="S2")
    # Deltas: applied and old, applied in the window, pending.
    d_old = _file(db, bulk, "stocks", "delta", days_ago=60, tag="D1")
    d_window = _file(db, bulk, "stocks", "delta", days_ago=20, tag="D2")
    d_pending = _file(db, bulk, "stocks", "delta", days_ago=1, tag="D3")
    _nightly_through(db, stocks=d_window["file_id"])

    plan = pr.plan(db, bulk, keep_days=7, now=NOW)
    assert {r.file_id: r.reason for r in plan.removals} == {
        t_old["file_id"]: "superseded",
        t_older["file_id"]: "superseded",
        d_old["file_id"]: "delta_applied",
    }
    # current: TICKERS and SEP; previous: the newest superseded of each, whatever
    # its age (SEP's is 200 days old); recent: TICKERS' other one within the week.
    assert plan.kept == {
        "current": 2,
        "previous": 2,
        "recent": 1,
        "pending": 1,
        "window": 1,
    }
    assert (bulk / t_old["path"]).exists()  # a plan deletes nothing

    done = pr.prune(db, bulk, keep_days=7, now=NOW)
    assert done.counts() == {"superseded": 2, "delta_applied": 1}
    assert done.bytes_freed() == t_old["bytes"] + t_older["bytes"] + d_old["bytes"]
    for gone in (t_old, t_older, d_old):
        assert not (bulk / gone["path"]).exists()
        assert _status(db, gone) == "deleted"  # the row stays, for lineage
    for kept in [*t_week, t_now, s_prev, s_full, d_window, d_pending]:
        assert (bulk / kept["path"]).exists()
    assert _status(db, s_prev) == "superseded"
    run = db.fetchone(
        "SELECT endpoint, status, params FROM ops.ingestion_run "
        "WHERE ingestion_run_id = %s",
        (done.run_id,),
    )
    assert (run["endpoint"], run["status"]) == ("prune", "success")
    assert run["params"]["deleted"] == {"delta_applied": 1, "superseded": 2}

    # A deleted delta is no longer a file the nightly reads.
    assert d_old["file_id"] not in [
        f["file_id"] for f in repo.sharadar_deltas_after(db, "stocks", 0)
    ]
    assert pr.prune(db, bulk, keep_days=7, now=NOW).removals == []


def test_without_a_nightly_the_deltas_since_the_full_are_kept(db, tmp_path):
    # Recorded in the order they were fetched, as the ledger's ids are.
    before = _file(db, tmp_path, "stocks", "delta", days_ago=120, tag="D0")
    _file(db, tmp_path, "stocks", "full", days_ago=100, tag="S")
    after = _file(db, tmp_path, "stocks", "delta", days_ago=90, tag="D1")
    plan = pr.plan(db, tmp_path, keep_days=7, now=NOW)
    # Before the first nightly, every delta after the full is pending.
    assert [r.file_id for r in plan.removals] == [before["file_id"]]
    assert plan.kept["pending"] == 1 and after["file_id"] > before["file_id"]


def _fundamentals_run(db, endpoint, params):
    db.execute(
        "INSERT INTO ops.ingestion_run (source, endpoint, status, params) "
        "VALUES ('sharadar', %s, 'success', %s::jsonb)",
        (endpoint, json.dumps(params)),
    )


def test_an_applied_sf1_delta_ages_out_and_an_unapplied_one_is_kept(db, tmp_path):
    _file(db, tmp_path, "fundamentals", "full", days_ago=100, tag="F")
    d1 = _file(db, tmp_path, "fundamentals", "delta", days_ago=70, tag="D1")
    d2 = _file(db, tmp_path, "fundamentals", "delta", days_ago=30, tag="D2")
    d3 = _file(db, tmp_path, "fundamentals", "delta", days_ago=10, tag="D3")
    # The price nightly's applied state says nothing about SF1.
    _nightly_through(db, stocks=d3["file_id"], funds=d3["file_id"])

    # Before the fundamentals nightly, every SF1 delta after the full is pending,
    # however old.
    plan = pr.plan(db, tmp_path, keep_days=7, now=NOW)
    assert plan.removals == [] and plan.kept["pending"] == 3

    _fundamentals_run(
        db, fu.NIGHTLY_ENDPOINT, {"through": {"fundamentals": d2["file_id"]}}
    )
    done = pr.prune(db, tmp_path, keep_days=7, now=NOW)
    assert {r.file_id: r.reason for r in done.removals} == {
        d1["file_id"]: "delta_applied"
    }
    assert (done.kept["window"], done.kept["pending"]) == (1, 1)
    assert not (tmp_path / d1["path"]).exists() and _status(db, d1) == "deleted"

    # Forty days on, the applied one has aged out too; the pending one stays.
    later = pr.prune(db, tmp_path, keep_days=7, now=NOW + timedelta(days=40))
    assert [r.file_id for r in later.removals] == [d2["file_id"]]
    assert (tmp_path / d3["path"]).exists() and _status(db, d3) == "downloaded"


def test_a_whole_sf1_backfill_takes_over_from_the_deltas_before_its_file(db, tmp_path):
    _file(db, tmp_path, "fundamentals", "full", days_ago=100, tag="F1")
    d1 = _file(db, tmp_path, "fundamentals", "delta", days_ago=90, tag="D1")
    d2 = _file(db, tmp_path, "fundamentals", "delta", days_ago=80, tag="D2")
    _fundamentals_run(
        db, fu.NIGHTLY_ENDPOINT, {"through": {"fundamentals": d1["file_id"]}}
    )
    # The nightly failed after d2; then a re-run from a fresh file.
    f2 = _file(db, tmp_path, "fundamentals", "full", days_ago=60, tag="F2")
    d3 = _file(db, tmp_path, "fundamentals", "delta", days_ago=59, tag="D3")

    # A run for some securities is no takeover: d2 waits for the nightly.
    some = {"backfill": True, "whole": False, "files": [f2["file_id"]]}
    _fundamentals_run(db, fu.ENDPOINT, some)
    assert fu.pending_deltas(db) == (
        [repo.sharadar_file(db, d2["file_id"]), repo.sharadar_file(db, d3["file_id"])],
        d1["file_id"],
    )

    # A whole one is: f2 holds d2, so d2 is applied, and only d3 is pending.
    _fundamentals_run(db, fu.ENDPOINT, {**some, "whole": True})
    assert fu.pending_deltas(db) == (
        [repo.sharadar_file(db, d3["file_id"])],
        f2["file_id"],
    )
    plan = pr.plan(db, tmp_path, keep_days=7, now=NOW)
    assert {r.file_id: r.reason for r in plan.removals} == {
        d1["file_id"]: "delta_applied",
        d2["file_id"]: "delta_applied",
    }
    assert (plan.kept["current"], plan.kept["previous"], plan.kept["pending"]) == (
        1,
        1,
        1,
    )


def test_what_a_killed_run_left(db, tmp_path):
    _file(db, tmp_path, "tickers", "full", days_ago=0, tag="T")
    old_part = tmp_path / ".SEP-abc.part"
    old_part.write_bytes(b"x" * 10)
    _aged(old_part, hours=30)
    new_part = tmp_path / ".SEP-def.part"
    new_part.write_bytes(b"y")
    sort_dir = tmp_path / ".sharadar-sort-abc"
    sort_dir.mkdir()
    (sort_dir / "0001.csv").write_bytes(b"z" * 5)
    _aged(sort_dir, hours=48)
    unrecorded_old = tmp_path / (hashlib.sha256(b"drift").hexdigest() + ".csv.zip")
    unrecorded_old.write_bytes(b"drift")
    _aged(unrecorded_old, hours=24 * 20)
    unrecorded_new = tmp_path / (hashlib.sha256(b"new").hexdigest() + ".csv.zip")
    unrecorded_new.write_bytes(b"new")
    foreign = tmp_path / "notes.txt"
    foreign.write_text("mine")
    _aged(foreign, hours=24 * 400)

    done = pr.prune(db, tmp_path, keep_days=7, now=NOW)
    assert done.counts() == {
        "partial_download": 1,
        "sort_scratch": 1,
        "unrecorded": 1,
    }
    assert not old_part.exists() and not sort_dir.exists()
    assert not unrecorded_old.exists()
    assert new_part.exists() and unrecorded_new.exists() and foreign.exists()
    assert done.foreign == ["notes.txt"]
    assert done.kept["download_in_use"] == 1 and done.kept["unrecorded_recent"] == 1


def test_a_file_marked_deleted_that_survived_is_removed(db, tmp_path):
    current = _file(db, tmp_path, "actions", "full", days_ago=0, tag="A")
    old = _file(db, tmp_path, "actions", "full", days_ago=30, tag="A0")
    repo.mark_sharadar_file_deleted(db, old["file_id"])  # the unlink had failed
    done = pr.prune(db, tmp_path, keep_days=7, now=NOW)
    assert done.counts() == {"marked_deleted": 1}
    assert not (tmp_path / old["path"]).exists()
    assert (tmp_path / current["path"]).exists()


def test_a_missing_current_file_stops_it(db, tmp_path):
    entry = _file(db, tmp_path, "stocks", "full", days_ago=1, tag="S")
    _file(db, tmp_path, "stocks", "delta", days_ago=90, tag="D")
    (tmp_path / entry["path"]).unlink()
    with pytest.raises(pr.PruneError, match="is not in"):
        pr.prune(db, tmp_path, keep_days=7, now=NOW)
    assert (
        db.fetchval(
            "SELECT count(*) FROM landing.sharadar_file WHERE status = 'deleted'"
        )
        == 0
    )


def test_the_command(db, tmp_path):
    # Oldest first: the last recorded is the current file.
    old = _file(db, tmp_path, "tickers", "full", days_ago=60, tag="T2")
    _file(db, tmp_path, "tickers", "full", days_ago=30, tag="T1")
    _file(db, tmp_path, "tickers", "full", days_ago=0, tag="T")
    # The command prunes as of today: age the file against the real clock.
    db.execute(
        "UPDATE landing.sharadar_file SET fetched_at = now() - interval '60 days' "
        "WHERE file_id = %s",
        (old["file_id"],),
    )
    db.execute(
        "UPDATE landing.sharadar_file SET fetched_at = now() WHERE file_id <> %s",
        (old["file_id"],),
    )
    rc = tmp_path / "fafnirrc"
    rc.write_text(f'[sharadar]\nbulk_dir = "{tmp_path}"\nkeep_days = 3\n')

    def run(*args):
        return CliRunner().invoke(
            cli.main,
            ["--config", str(rc), "source", "sharadar-prune", *args],
            env={"FAFNIR_DSN": db.dsn},
        )

    out = run("--dry-run")
    assert out.exit_code == 0, out.output
    assert "Dry run: nothing deleted. Would remove 1 file(s)" in out.output
    assert "superseded 1" in out.output and "not fafnir's" in out.output  # fafnirrc
    assert (tmp_path / old["path"]).exists()
    out = run()
    assert out.exit_code == 0, out.output
    assert "Removed 1 file(s)" in out.output
    assert not (tmp_path / old["path"]).exists()
