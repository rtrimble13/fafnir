"""`fafnir security merge --across-vendors` (#125; needs FAFNIR_TEST_DSN).

One company held twice after a rename: the row Sharadar feeds, inactive on the old
ticker, and FMP's re-mint under the new one with the company's history. The two
disagree for reasons that are not identity -- rounding, FMP's adjusted re-serve of
older history, a new CUSIP -- so the merge proves identity from the vendor, keeps
the fed row's bars, and relists it. Every key, ticker and price is synthetic
(ADR 0012 §8).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import cross_vendor_merge as cvm

pytestmark = pytest.mark.integration

KEY = "900601"
RENAMED = date(2026, 3, 2)
FIRST = date(2026, 1, 5)  # the fed row's first bar
LAST = date(2026, 10, 20)  # the vendor's newest price date


def _weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


SHELL_DAYS = _weekdays(FIRST, LAST)
PREFIX_DAYS = _weekdays(date(2025, 10, 1), FIRST - timedelta(days=1))


def _close(d) -> Decimal:
    """The company's raw close: a gentle drift, distinct every day."""
    return Decimal("20") + Decimal((d - date(2025, 1, 1)).days) / 100


def _bars(db, sid, days, *, scale=Decimal(1), nudge=Decimal(0)):
    rows = []
    for d in days:
        c = (_close(d) * scale + nudge).quantize(Decimal("0.0001"))
        rows.append(
            {
                "security_id": sid,
                "trade_date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000,
            }
        )
    repo.upsert_daily_prices(db, rows)


def _security(db, symbol, *, active, cusip, cik="0000123456", delisted=None):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Corp",
        asset_type="equity",
        exchange_code="NYSE",
        is_actively_trading=active,
        cusip=cusip,
        cik=cik,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    if delisted:
        assert repo.mark_delisted(db, security_id=sid, delisted_date=delisted)
    return sid


#: Before this, the re-mint's ``early_scale`` applies (FMP's adjusted re-serve of
#: older history, as ALNT is 2/3 of AMOT before its split).
EARLY_CUT = date(2026, 4, 1)


def _pair(
    db,
    *,
    vendor_ticker="ZZPOS",
    victim_cik="0000123456",
    vendor_cik=None,
    early_scale=Decimal(1),
    recent_scale=Decimal(1),
):
    """The fed shell ZZPRE and FMP's re-mint ZZPOS. The re-mint holds a prefix the
    shell lacks, the shared sessions half a cent apart (at ``recent_scale``), its
    history before :data:`EARLY_CUT` at ``early_scale``, and a dividend of its own
    on each side of the shell's first bar. ``vendor_cik`` is the CIK the vendor's
    master gives the fed key."""
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, category, exchange, is_delisted, first_price_date, last_price_date, "
        "cik) VALUES ('sharadar', 'stocks', %s, %s, 'ZZ CORP', "
        "'Domestic Common Stock', 'NYSE', false, %s, %s, %s)",
        (KEY, vendor_ticker, FIRST, LAST, vendor_cik),
    )
    for action, contra in (("tickerchangefrom", "ZZPRE"), ("tickerchangeto", "ZZPOS")):
        db.execute(
            "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date, "
            "action, ticker, contra_ticker, contra_name) "
            "VALUES ('sharadar', %s, %s, %s, 'ZZPOS', %s, 'N/A')",
            (KEY, RENAMED, action, contra),
        )
    shell = _security(db, "ZZPRE", active=False, cusip="111111111", delisted=RENAMED)
    repo.insert_vendor_link(
        db,
        security_id=shell,
        vendor="sharadar",
        vendor_key=KEY,
        valid_from=FIRST,
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    _bars(db, shell, SHELL_DAYS)
    victim = _security(db, "ZZPOS", active=True, cusip="222222222", cik=victim_cik)
    _bars(db, victim, PREFIX_DAYS, scale=early_scale)
    early = [d for d in SHELL_DAYS if d < EARLY_CUT]
    _bars(db, victim, early, scale=early_scale, nudge=Decimal("0.005"))
    _bars(
        db,
        victim,
        [d for d in SHELL_DAYS if d >= EARLY_CUT],
        scale=recent_scale,
        nudge=Decimal("0.005"),
    )
    for sid, ex in ((shell, date(2026, 6, 15)), (victim, date(2026, 6, 16))):
        db.execute(
            "INSERT INTO core.corporate_action (security_id, action_type, ex_date, "
            "dividend_amount, source) VALUES (%s, 'dividend', %s, 0.25, 'fmp')",
            (sid, ex),
        )
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date, "
        "dividend_amount, source) VALUES (%s, 'dividend', '2025-12-15', 0.25, 'fmp')",
        (victim,),
    )
    repo.add_dq_flag_once(
        db,
        check_name="vendor_xref_unmatched",
        security_id=victim,
        record_key={"symbol": "ZZPOS"},
    )
    repo.add_dq_flag_once(
        db,
        check_name="security_inactive_but_trading",
        security_id=shell,
        record_key={"symbol": "ZZPRE"},
        detail={"source": "sharadar", "next": "held"},
    )
    return shell, victim


@pytest.fixture()
def run_cli(db):
    class _Cfg:
        dsn = db.dsn

    def run(*args):
        return CliRunner().invoke(
            cli.security_merge, list(map(str, args)), obj={"config": _Cfg()}
        )

    return run


def _flat(out) -> str:
    return " ".join(out.output.split())


def _row(db, sid):
    return db.fetchone("SELECT * FROM core.security WHERE security_id = %s", (sid,))


def test_the_plan_reads_identity_from_the_vendor(db):
    shell, victim = _pair(db)
    plan = cvm.plan_cross_vendor_merge(db, victim_id=victim, survivor_id=shell)
    assert plan.blockers == []
    assert plan.base.blockers  # the exact-OHLC merge would refuse this pair
    assert (plan.vendor, plan.vendor_key, plan.vendor_ticker) == (
        "sharadar",
        KEY,
        "ZZPOS",
    )
    assert plan.identifier_notes == ["cusip 111111111 vs 222222222"]
    assert (plan.recent_sessions, plan.recent_agree) == (20, 20)
    assert plan.within_1pct == len(SHELL_DAYS) and plan.same == 0
    assert plan.joined == PREFIX_DAYS and plan.unjoined == []
    assert plan.actions_before == FIRST
    assert (plan.victim_actions_from_first, plan.survivor_actions_from_first) == (1, 1)
    assert plan.victim_splits_before == 0


def test_a_split_inside_the_bars_the_victim_brings_is_named(db, run_cli):
    """Where the histories meet cannot show a scale change wholly inside the
    victim's own earlier history: the dry run says so."""
    shell, victim = _pair(db)
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date, "
        "split_numerator, split_denominator, source) "
        "VALUES (%s, 'split', '2025-11-03', 2, 1, 'fmp')",
        (victim,),
    )
    out = run_cli(victim, shell, "--across-vendors", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "the victim holds 1 split(s) before the survivor's first bar" in _flat(out)


def test_a_dry_run_shows_the_merge_and_changes_nothing(db, run_cli):
    shell, victim = _pair(db)
    out = run_cli(victim, shell, "--across-vendors", "--dry-run")
    assert out.exit_code == 0, out.output
    text = _flat(out)
    assert f"fed by sharadar {KEY}, which it lists as ZZPOS" in text
    assert "Identifier differs (reported, not a blocker): cusip" in text
    assert "20 of the last 20 shared sessions close within 1%" in text
    assert f"{len(PREFIX_DAYS)} ({PREFIX_DAYS[0]}..{PREFIX_DAYS[-1]}) join" in text
    assert f"Relisted: ZZPRE -> ZZPOS on {RENAMED}" in text
    assert "After: ZZPOS, trading, delisted never" in text
    assert "Dry run: nothing changed." in text
    assert _row(db, victim) is not None
    assert _row(db, shell)["primary_symbol"] == "ZZPRE"


def test_the_merge_relists_when_another_row_took_the_old_ticker(db, run_cli):
    # SATS (2026-10): the fed row held SATS when EchoStar became ECHO, and Strive's
    # preferred was listed on SATS since. The relist listed the row on SATS on its way
    # to ECHO, which the listed rows' (source, ticker) index refused.
    shell, victim = _pair(db)
    reuse = repo.upsert_security(
        db,
        primary_symbol="ZZPRE",
        company_name="ZZPRE Preferred",
        asset_type="equity",
        exchange_code="NYSE",
        is_actively_trading=True,
    )
    out = run_cli(victim, shell, "--across-vendors", "-m", "one company", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    row = _row(db, shell)
    assert (row["primary_symbol"], row["delisted_date"]) == ("ZZPOS", None)
    other = _row(db, reuse)
    assert (other["primary_symbol"], other["delisted_date"]) == ("ZZPRE", None)


def test_the_merge_keeps_the_fed_rows_bars_and_relists_it(db, run_cli):
    shell, victim = _pair(db)
    out = run_cli(victim, shell, "--across-vendors", "-m", "one company", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    row = _row(db, shell)
    assert (
        row["primary_symbol"],
        row["is_actively_trading"],
        row["delisted_date"],
    ) == (
        "ZZPOS",
        True,
        None,
    )
    # The instrument as it trades now: the re-mint's name and CUSIP.
    assert (row["company_name"], row["cusip"]) == ("ZZPOS Corp", "222222222")
    # The fed row's bars on the overlap; the re-mint's prefix joins them.
    closes = {
        r["trade_date"]: r["close"]
        for r in db.fetchall(
            "SELECT trade_date, close FROM core.daily_price WHERE security_id = %s",
            (shell,),
        )
    }
    assert len(closes) == len(SHELL_DAYS) + len(PREFIX_DAYS)
    assert closes[LAST] == _close(LAST).quantize(Decimal("0.000001"))
    assert closes[PREFIX_DAYS[0]] == _close(PREFIX_DAYS[0])
    # The fed row's dividend stands; the re-mint's copy of it a day later does not,
    # and its dividend from before the fed row's history moves with its bars.
    assert [
        r["ex_date"]
        for r in db.fetchall(
            "SELECT ex_date FROM core.corporate_action WHERE security_id = %s "
            "ORDER BY ex_date",
            (shell,),
        )
    ] == [date(2025, 12, 15), date(2026, 6, 15)]
    assert [
        (p["symbol"], p["valid_to"])
        for p in db.fetchall(
            "SELECT symbol, valid_to FROM core.symbol_xref WHERE security_id = %s "
            "ORDER BY valid_from",
            (shell,),
        )
    ] == [("ZZPRE", RENAMED - timedelta(days=1)), ("ZZPOS", None)]
    (link,) = repo.vendor_links_of(db, shell, "sharadar")
    assert (link["vendor_key"], link["is_feed"]) == (KEY, True)
    assert not db.fetchall(
        "SELECT check_name FROM ops.data_quality_flag WHERE resolved_at IS NULL"
    )
    run = db.fetchone(
        "SELECT * FROM ops.ingestion_run WHERE endpoint = "
        "'security-merge-across-vendors'"
    )
    assert run["status"] == "success"
    assert run["params"]["relisted"]["new"] == "ZZPOS"
    assert run["params"]["joined"] == len(PREFIX_DAYS)
    assert run["params"]["identity_before"]["cusip"] == "111111111"


@pytest.mark.parametrize(
    "case, blocker",
    [
        ("reversed", "has no feeding vendor link"),
        ("vendor_ticker", "lists 900601 as ZZOTH, not as the victim's ZZPOS"),
        ("cik", "cik differs"),
        ("today", "close within 1%: the rows do not describe one instrument today"),
        ("unjoined", "Re-run with --unjoined drop"),
    ],
)
def test_the_guard_refuses(db, run_cli, case, blocker):
    kwargs = {
        "vendor_ticker": {"vendor_ticker": "ZZOTH"},
        "cik": {"victim_cik": "0000999999"},
        "today": {"recent_scale": Decimal("1.05")},
        "unjoined": {"early_scale": Decimal(2)},
    }.get(case, {})
    shell, victim = _pair(db, **kwargs)
    args = (shell, victim) if case == "reversed" else (victim, shell)
    out = run_cli(*args, "--across-vendors", "--yes")
    assert out.exit_code != 0
    assert blocker in _flat(out), out.output
    assert "Nothing was changed." in out.output
    assert _row(db, victim) is not None and _row(db, shell) is not None


@pytest.mark.parametrize("vendor_cik", ["0000999999", "999999"])
def test_a_new_registrant_the_vendor_carries_merges(db, run_cli, vendor_cik):
    """MTMT to MPU (2026-10): a redomicile gives the company a new registrant. The
    vendor carries it as one security under the new CIK, which FMP's re-mint holds;
    the fed row still holds the old one. The difference is reported, not refused,
    and the survivor takes the new CIK."""
    shell, victim = _pair(db, victim_cik="0000999999", vendor_cik=vendor_cik)
    out = run_cli(victim, shell, "--across-vendors", "--dry-run")
    assert out.exit_code == 0, out.output
    assert (
        "Identifier differs (reported, not a blocker): cik 0000123456 vs 0000999999, "
        f"the CIK sharadar gives {KEY}: a new registrant it carries as one security"
    ) in _flat(out)
    out = run_cli(victim, shell, "--across-vendors", "-m", "redomiciled", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    assert _row(db, shell)["cik"] == "0000999999"
    run = db.fetchone(
        "SELECT params FROM ops.ingestion_run WHERE endpoint = "
        "'security-merge-across-vendors'"
    )
    assert run["params"]["identity_before"]["cik"] == "0000123456"


def test_one_cik_padded_differently_is_no_difference(db, run_cli):
    """0000123456 and 123456 are one registrant: nothing to refuse and nothing to
    report, though the vendor gives no CIK to settle it."""
    shell, victim = _pair(db, victim_cik="123456")
    plan = cvm.plan_cross_vendor_merge(db, victim_id=victim, survivor_id=shell)
    assert plan.blockers == []
    assert not [n for n in plan.identifier_notes if n.startswith("cik")]
    out = run_cli(victim, shell, "--across-vendors", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None


def test_a_cik_the_vendor_gives_the_survivor_still_refuses(db, run_cli):
    """The vendor naming the fed row's own CIK says the re-mint is another
    registrant."""
    shell, victim = _pair(db, victim_cik="0000999999", vendor_cik="0000123456")
    out = run_cli(victim, shell, "--across-vendors", "--yes")
    assert out.exit_code != 0
    assert (
        f"cik differs: survivor '0000123456' vs victim '0000999999' (sharadar gives "
        f"{KEY} '0000123456') -- another registrant is not a rename"
    ) in _flat(out), out.output
    assert _row(db, victim) is not None and _row(db, shell)["cik"] == "0000123456"


@pytest.mark.parametrize("policy, moved", [("drop", False), ("carry", True)])
def test_unjoined_bars_are_dropped_or_carried_on_request(db, run_cli, policy, moved):
    shell, victim = _pair(db, early_scale=Decimal(2))
    out = run_cli(victim, shell, "--across-vendors", "--unjoined", policy, "--yes")
    assert out.exit_code == 0, out.output
    first = db.fetchval(
        "SELECT min(trade_date) FROM core.daily_price WHERE security_id = %s", (shell,)
    )
    assert first == (PREFIX_DAYS[0] if moved else FIRST)
    # With no earlier bar moved, the re-mint's earlier dividend has nothing to stand on.
    early = db.fetchval(
        "SELECT count(*) FROM core.corporate_action WHERE security_id = %s "
        "AND ex_date < %s",
        (shell, FIRST),
    )
    assert early == (1 if moved else 0)


def test_force_and_the_cross_vendor_options_do_not_mix(db, run_cli):
    shell, victim = _pair(db)
    out = run_cli(victim, shell, "--across-vendors", "--force", "--yes")
    assert out.exit_code != 0 and "--force does not apply" in out.output
    out = run_cli(victim, shell, "--unjoined", "drop", "--dry-run")
    assert out.exit_code != 0 and "need --across-vendors" in out.output
