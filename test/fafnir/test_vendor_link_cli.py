"""
Manual link review: `fafnir vendor link show|accept|reject` (plan SA-0402, #63;
needs FAFNIR_TEST_DSN).

A person decides what the engine (#62) cannot, and the decision must survive the
engine's next run. Every key, ticker and price is synthetic (ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import migrate as m
from fafnir.db import repository as repo
from fafnir.ingest import vendor_link as vl
from fafnir.ingest import vendor_review as vr

pytestmark = pytest.mark.integration

Y = date


def close_of(d: date) -> Decimal:
    return Decimal(str(round(50 + (d.toordinal() % 97) / 10, 3)))


class FakeCloses:
    name = "fake"

    def __init__(self, tickers):
        self.tickers = set(tickers)

    def closes(self, wanted):
        return {
            (t, ticker, d): close_of(d)
            for (t, ticker), dates in wanted.items()
            if ticker in self.tickers
            for d in dates
        }


def _engine(db, *tickers):
    fake = FakeCloses(tickers)
    return vl.link(db, lambda n: fake)


def _sec(db, symbol, start=Y(2020, 1, 2), end=Y(2020, 12, 31), **kw):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Co",
        exchange_code="NYSE",
        **kw,
    )
    db.execute(
        "INSERT INTO core.symbol_xref (security_id, symbol, valid_from) "
        "VALUES (%s, %s, %s)",
        (sid, symbol, start),
    )
    rows, d = [], start
    while d <= end:
        if d.weekday() < 5:
            c = close_of(d)
            rows.append(
                {
                    "security_id": sid,
                    "trade_date": d,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "volume": 100,
                }
            )
        d += timedelta(days=1)
    repo.upsert_daily_prices(db, rows)
    return sid


def _vendor(db, key, ticker, first=Y(2020, 1, 2), last=Y(2020, 12, 31), related=()):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, related_tickers, first_price_date, last_price_date) VALUES "
        "('sharadar', 'stocks', %s, %s, %s, %s, %s, %s)",
        (key, ticker, f"{ticker} Co", list(related) or None, first, last),
    )


def _links(db, sid):
    return repo.vendor_links_of(db, sid, "sharadar")


def _open_flags(db, sid):
    return [(f["check_name"], f["resolved_by"]) for f in vr.open_flags(db, sid)]


@pytest.fixture()
def ambiguous(db):
    """A security two keys both agree with: the engine flags it ambiguous."""
    sid = _sec(db, "ZZA")
    _vendor(db, "900001", "ZZA")
    _vendor(db, "900002", "ZZA2", related=["ZZA"])
    _engine(db, "ZZA", "ZZA2")
    assert _open_flags(db, sid) == [("vendor_xref_ambiguous", None)]
    return sid


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------


def test_accept_links_resolves_and_survives_the_engine(db, ambiguous):
    sid = ambiguous
    plan = vr.plan_accept(db, security_id=sid, vendor_key="900002")
    assert (plan.valid_from, plan.close, plan.noop) == (Y(2020, 1, 2), None, False)
    resolved = vr.apply_accept(db, plan, by="alice", note="share class: 10-K")
    assert len(resolved) == 1
    (link,) = _links(db, sid)
    assert (link["vendor_key"], link["match_method"], link["created_by"]) == (
        "900002",
        "manual",
        "alice",
    )
    assert (link["confidence"], link["note"]) == (None, "share class: 10-K")
    assert link["evidence"]["decision"] == "accepted"
    closed = repo.list_dq_flags(
        db, repo.DqFilter(state="resolved", checks=("vendor_xref_*",)), limit=5
    )
    assert [(f["resolved_by"], f["resolution_note"]) for f in closed] == [
        ("alice", "manual link to sharadar 900002: share class: 10-K")
    ]
    # The engine runs again: the linked security is out of its scope.
    assert _engine(db, "ZZA", "ZZA2").outcomes == []
    assert [r["vendor_key"] for r in _links(db, sid)] == ["900002"]
    again = vr.plan_accept(db, security_id=sid, vendor_key="900002")
    assert again.noop


def test_accept_replaces_an_engine_link_and_refuses_a_held_key(db):
    a, b = _sec(db, "ZZB"), _sec(db, "ZZC")
    _vendor(db, "900010", "ZZB")
    _vendor(db, "900011", "ZZB9", related=["ZZB"])
    _vendor(db, "900012", "ZZC")
    _engine(db, "ZZB", "ZZC")  # ZZB: several agree? no, ZZB9 has no prices
    assert [r["vendor_key"] for r in _links(db, a)] == ["900010"]
    with pytest.raises(vr.ReviewRefused, match="security .*'s .*a key is one security"):
        vr.plan_accept(db, security_id=b, vendor_key="900010")
    with pytest.raises(vr.ReviewRefused, match="pass --from after it"):
        vr.plan_accept(db, security_id=a, vendor_key="900011")  # same start
    plan = vr.plan_accept(
        db, security_id=a, vendor_key="900011", valid_from=Y(2020, 6, 1)
    )
    assert plan.close["vendor_key"] == "900010"
    vr.apply_accept(db, plan, by="bob", note="re-listed")
    rows = {r["vendor_key"]: r for r in _links(db, a)}
    assert rows["900010"]["valid_to"] == Y(2020, 5, 31)
    assert rows["900011"]["valid_to"] is None
    assert rows["900011"]["evidence"]["replaced"]["vendor_key"] == "900010"


def test_accept_refuses_unknown_keys_feeds_and_missing_securities(db):
    sid = _sec(db, "ZZD")
    _vendor(db, "900020", "ZZD")
    _vendor(db, "900021", "ZZD2")
    with pytest.raises(vr.ReviewRefused, match="no sharadar key 999"):
        vr.plan_accept(db, security_id=sid, vendor_key="999")
    with pytest.raises(vr.ReviewRefused, match="no security 999999"):
        vr.plan_accept(db, security_id=999999, vendor_key="900020")
    vr.apply_accept(
        db, vr.plan_accept(db, security_id=sid, vendor_key="900020"), by="t", note="n"
    )
    db.execute("UPDATE core.security_vendor_xref SET is_feed = true")
    with pytest.raises(vr.ReviewRefused, match="feed"):
        vr.plan_accept(
            db, security_id=sid, vendor_key="900021", valid_from=Y(2020, 7, 1)
        )
    with pytest.raises(vr.ReviewRefused, match="feed"):
        vr.plan_reject(db, security_id=sid, vendor_key="900020")


def test_accept_without_price_overlap_starts_at_the_vendor_or_today(db):
    sid = _sec(db, "ZZE")
    _vendor(db, "900030", "ZZE", first=Y(2022, 1, 3), last=Y(2023, 1, 3))
    assert vr.plan_accept(db, security_id=sid, vendor_key="900030").valid_from == Y(
        2022, 1, 3
    )
    db.execute(
        "UPDATE core.vendor_security SET first_price_date = NULL, "
        "last_price_date = NULL"
    )
    assert (
        vr.plan_accept(db, security_id=sid, vendor_key="900030").valid_from
        == date.today()
    )


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


def test_reject_survives_the_engine_and_accept_withdraws_it(db, ambiguous):
    sid = ambiguous
    plan = vr.plan_reject(db, security_id=sid, vendor_key="900002")
    assert (plan.close, plan.restates) == (None, False)
    vr.apply_reject(db, plan, by="carol", note="ZZA2 is the B shares")
    # With 900002 rejected, only 900001 is left: the engine links it, and the
    # ambiguous flag closes.
    (o,) = _engine(db, "ZZA", "ZZA2").outcomes
    assert (o.outcome, o.vendor_key) == ("linked", "900001")
    assert _open_flags(db, sid) == []
    (r,) = repo.vendor_rejections(db, "sharadar", sid)
    assert (r["vendor_key"], r["rejected_by"], r["note"]) == (
        "900002",
        "carol",
        "ZZA2 is the B shares",
    )
    assert vr.plan_reject(db, security_id=sid, vendor_key="900002").restates
    # Changing one's mind: reject the engine's link, accept the other.
    vr.apply_reject(
        db,
        vr.plan_reject(db, security_id=sid, vendor_key="900001"),
        by="carol",
        note="wrong after all",
    )
    assert all(r["valid_to"] is not None for r in _links(db, sid))
    with pytest.raises(vr.ReviewRefused, match="already starts 2020-01-02"):
        vr.plan_accept(db, security_id=sid, vendor_key="900001")
    plan = vr.plan_accept(
        db, security_id=sid, vendor_key="900002", valid_from=Y(2020, 2, 3)
    )
    assert plan.unrejects
    vr.apply_accept(db, plan, by="carol", note="it is the B shares")
    assert [r["vendor_key"] for r in repo.vendor_rejections(db, "sharadar", sid)] == [
        "900001"
    ]
    # Both keys decided: the engine leaves the security alone.
    assert _engine(db, "ZZA", "ZZA2").outcomes == []


def test_a_rejected_key_is_named_when_nothing_else_is_left(db):
    sid = _sec(db, "ZZF")
    _vendor(db, "900040", "ZZF")
    vr.apply_reject(
        db, vr.plan_reject(db, security_id=sid, vendor_key="900040"), by="t", note="n"
    )
    (o,) = _engine(db, "ZZF").outcomes
    assert (o.reason, o.candidates) == ("no_candidate", [{"rejected_key": "900040"}])


def test_a_rejection_goes_with_its_security(db):
    sid = _sec(db, "ZZG")
    _vendor(db, "900050", "ZZG")
    vr.apply_reject(
        db, vr.plan_reject(db, security_id=sid, vendor_key="900050"), by="t", note="n"
    )
    db.execute("DELETE FROM core.daily_price WHERE security_id = %s", (sid,))
    db.execute("DELETE FROM core.symbol_xref WHERE security_id = %s", (sid,))
    db.execute("DELETE FROM core.security WHERE security_id = %s", (sid,))
    assert repo.vendor_rejections(db, "sharadar") == []


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _run(db, monkeypatch, *args):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    return CliRunner().invoke(cli.main, ["vendor", "link", *args])


def test_show(db, ambiguous, monkeypatch):
    out = _run(db, monkeypatch, "show", "ZZA")
    assert out.exit_code == 0, out.output
    assert "ZZA (security" in out.output and "Links: none" in out.output
    assert "vendor_xref_ambiguous (warn), reason several_agree" in out.output
    assert "900001     candidate" in out.output and "900002     candidate" in out.output
    fake = FakeCloses({"ZZA", "ZZA2"})
    monkeypatch.setattr(cli, "_link_closes", lambda cfg, db_, prices: lambda n: fake)
    out = _run(db, monkeypatch, "show", "--security-id", str(ambiguous), "--evaluate")
    assert "Engine today: ambiguous (several_agree)" in out.output
    assert "900001: 5 of 5 agree" in out.output
    vr.apply_accept(
        db,
        vr.plan_accept(db, security_id=ambiguous, vendor_key="900001"),
        by="t",
        note="n",
    )
    out = _run(db, monkeypatch, "show", "ZZA", "--evaluate")
    assert "900001     linked" in out.output and "manual" in out.output
    assert "Engine today: ambiguous" in out.output  # the engine still can't tell
    assert "fmp" not in out.output or "sharadar" in out.output


def test_show_evaluates_a_link_with_its_samples(db, monkeypatch):
    sid = _sec(db, "ZZH")
    _vendor(db, "900060", "ZZH")
    fake = FakeCloses({"ZZH"})
    monkeypatch.setattr(cli, "_link_closes", lambda cfg, db_, prices: lambda n: fake)
    out = _run(db, monkeypatch, "show", "--security-id", str(sid), "--evaluate")
    assert "Engine today: linked (price_history) -> 900060" in out.output
    assert out.output.count(" fmp ") == 5
    monkeypatch.setattr(
        cli,
        "_link_closes",
        lambda cfg, db_, prices: (_ for _ in ()).throw(vl.LinkError("no prices")),
    )
    assert "no prices" in _run(db, monkeypatch, "show", "ZZH", "--evaluate").output


def test_accept_and_reject_commands(db, ambiguous, monkeypatch):
    sid = str(ambiguous)
    dry = _run(
        db,
        monkeypatch,
        "accept",
        "--security-id",
        sid,
        "--vendor-key",
        "900001",
        "-m",
        "why",
        "--by",
        "dana",
        "--dry-run",
    )
    assert dry.exit_code == 0, dry.output
    assert (
        "Would link ZZA" in dry.output
        and "Would resolve 1 vendor_xref flag" in dry.output
    )
    assert _links(db, ambiguous) == []
    done = _run(
        db,
        monkeypatch,
        "accept",
        "--symbol",
        "ZZA",
        "--vendor-key",
        "900001",
        "-m",
        "why",
        "--by",
        "dana",
    )
    assert "Did link ZZA" in done.output and "Did resolve 1" in done.output
    assert (
        "already linked"
        in _run(
            db,
            monkeypatch,
            "accept",
            "--symbol",
            "ZZA",
            "--vendor-key",
            "900001",
            "-m",
            "why",
        ).output
    )
    swap = _run(
        db,
        monkeypatch,
        "accept",
        "--symbol",
        "ZZA",
        "--vendor-key",
        "900002",
        "--from",
        "2020-03-02",
        "-m",
        "why",
        "--by",
        "dana",
        "--dry-run",
    )
    assert (
        "Would close the link to 900001 (from 2020-01-02) on 2020-03-01" in swap.output
    )
    bad = _run(
        db,
        monkeypatch,
        "accept",
        "--symbol",
        "ZZA",
        "--vendor-key",
        "nope",
        "-m",
        "why",
    )
    assert bad.exit_code != 0 and "no sharadar key nope" in bad.output

    rej = _run(
        db,
        monkeypatch,
        "reject",
        "--security-id",
        sid,
        "--vendor-key",
        "900001",
        "-m",
        "no",
        "--by",
        "dana",
        "--dry-run",
    )
    assert "Would close the link to 900001" in rej.output
    assert "Would record a rejection: ZZA" in rej.output
    rej = _run(
        db,
        monkeypatch,
        "reject",
        "--security-id",
        sid,
        "--vendor-key",
        "900001",
        "-m",
        "no",
        "--by",
        "dana",
    )
    assert "Did record a rejection" in rej.output
    again = _run(
        db,
        monkeypatch,
        "reject",
        "--security-id",
        sid,
        "--vendor-key",
        "900001",
        "-m",
        "still no",
    )
    assert "Did restate the rejection" in again.output
    bad = _run(
        db, monkeypatch, "reject", "--security-id", sid, "--vendor-key", "x", "-m", "no"
    )
    assert bad.exit_code != 0


def test_the_engine_still_runs_without_a_subcommand(db, monkeypatch):
    _sec(db, "ZZI")
    _vendor(db, "900070", "ZZI")
    fake = FakeCloses({"ZZI"})
    monkeypatch.setattr(cli, "_link_closes", lambda cfg, db_, prices: lambda n: fake)
    out = _run(db, monkeypatch, "--dry-run")
    assert out.exit_code == 0 and "Dry run: nothing written" in out.output


# ---------------------------------------------------------------------------
# The upgrade path
# ---------------------------------------------------------------------------


def test_0032_leaves_existing_rows_byte_identical(db):
    sid = _sec(db, "ZZJ")
    _vendor(db, "900080", "ZZJ")
    _engine(db, "ZZJ")

    def fingerprint():
        return db.fetchone("""
            SELECT (SELECT md5(string_agg(t::text, '|' ORDER BY security_id))
                      FROM core.security t) AS security,
                   (SELECT md5(string_agg(t::text, '|' ORDER BY vendor_key))
                      FROM core.security_vendor_xref t) AS links
            """)

    before = fingerprint()
    try:
        assert m.rollback(db.dsn, steps=1) == ["0032"]
        assert (
            db.fetchval("SELECT to_regclass('core.security_vendor_rejection')") is None
        )
        assert fingerprint() == before
    finally:
        applied = m.migrate(db.dsn)
    assert applied == ["0032"] and fingerprint() == before
    assert [r["vendor_key"] for r in _links(db, sid)] == ["900080"]


@pytest.mark.parametrize("command", ["accept", "reject"])
def test_the_decision_is_committed_before_it_is_reported(monkeypatch, command):
    """`... | head -1` must not print a decision and then roll it back."""
    import types

    import click

    events = []

    class FakeDatabase:
        def __init__(self, dsn):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")
            return False

        def commit(self):
            events.append("commit")

    plan = types.SimpleNamespace(
        noop=False,
        close=None,
        unrejects=False,
        restates=False,
        flags=[],
        valid_from=Y(2020, 1, 2),
    )
    monkeypatch.setattr(cli, "Database", FakeDatabase)
    monkeypatch.setattr(cli, "_resolve_one_security", lambda db, s, i: (1, "ZZ"))
    monkeypatch.setattr(vr, "plan_accept", lambda *a, **k: plan)
    monkeypatch.setattr(vr, "plan_reject", lambda *a, **k: plan)
    monkeypatch.setattr(
        vr, "apply_accept", lambda *a, **k: events.append("write") or []
    )
    monkeypatch.setattr(vr, "apply_reject", lambda *a, **k: events.append("write"))
    monkeypatch.setattr(
        click, "echo", lambda msg=None, *a, **k: events.append(("echo", msg))
    )
    cli.main.main(
        [
            "vendor",
            "link",
            command,
            "--security-id",
            "1",
            "--vendor-key",
            "K",
            "-m",
            "why",
        ],
        standalone_mode=False,
        obj={"config": types.SimpleNamespace(dsn="unused")},
    )
    first_echo = next(i for i, e in enumerate(events) if isinstance(e, tuple))
    assert events.index("write") < events.index("commit") < first_echo
