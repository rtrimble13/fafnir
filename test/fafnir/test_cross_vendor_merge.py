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


# ---------------------------------------------------------------------------
# The victim on a former ticker: FMP's row left on the ticker the vendor renamed
# the fed key away from, the fed row already on the new one (MICS and RIME,
# 2026-11; `vendor link`'s duplicate_of_linked).
# ---------------------------------------------------------------------------

FKEY = "900602"
TOOK = date(2025, 6, 2)  # the vendor's rename INTO the victim's ticker
VICTIM_END = date(2026, 4, 30)  # FMP's old-ticker row stops some weeks after


def _former_pair(
    db,
    *,
    events=True,
    took=True,
    related=("ZZANC", "ZZOLD"),
    survivor_symbol="ZZNEW",
    victim_cik="0000123456",
    vendor_cik=None,
    dead=False,
):
    """The fed ZZNEW, on the vendor's ticker since the rename, and FMP's ZZOLD,
    delisted at it. ZZOLD holds a prefix ZZNEW lacks and shares its sessions to
    :data:`VICTIM_END` half a cent apart."""
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker, "
        "name, category, exchange, is_delisted, first_price_date, last_price_date, "
        "cik, related_tickers) VALUES ('sharadar', 'stocks', %s, 'ZZNEW', 'ZZ CORP', "
        "'Domestic Common Stock', 'NYSE', %s, %s, %s, %s, %s)",
        (FKEY, dead, FIRST, LAST, vendor_cik, list(related) if related else None),
    )
    renames = []
    if took:
        renames.append((TOOK, "ZZANC", "ZZOLD"))
    if events:
        renames.append((RENAMED, "ZZOLD", "ZZNEW"))
    for day, old, new in renames:
        for action, contra in (("tickerchangefrom", old), ("tickerchangeto", new)):
            db.execute(
                "INSERT INTO core.vendor_security_event (vendor, vendor_key, "
                "event_date, action, ticker, contra_ticker, contra_name) "
                "VALUES ('sharadar', %s, %s, %s, 'ZZNEW', %s, 'N/A')",
                (FKEY, day, action, contra),
            )
    survivor = _security(
        db,
        survivor_symbol,
        active=not dead,
        cusip="333333333",
        delisted=LAST if dead else None,
    )
    repo.insert_vendor_link(
        db,
        security_id=survivor,
        vendor="sharadar",
        vendor_key=FKEY,
        valid_from=FIRST,
        match_method="manual",
        confidence=None,
        evidence={},
        created_by="tester",
        is_feed=True,
    )
    _bars(db, survivor, SHELL_DAYS)
    victim = _security(
        db,
        "ZZOLD",
        active=False,
        cusip="444444444",
        cik=victim_cik,
        delisted=RENAMED,
    )
    _bars(db, victim, PREFIX_DAYS)
    _bars(
        db,
        victim,
        [d for d in SHELL_DAYS if d <= VICTIM_END],
        nudge=Decimal("0.005"),
    )
    repo.add_dq_flag_once(
        db,
        check_name="vendor_xref_ambiguous",
        security_id=victim,
        record_key={"vendor": "sharadar"},
        detail={
            "reason": "duplicate_of_linked",
            "candidates": [{"vendor_key": FKEY, "linked_security": survivor}],
        },
    )
    return survivor, victim


def _periods(db, sid):
    return [
        (p["symbol"], p["valid_from"], p["valid_to"])
        for p in db.fetchall(
            "SELECT symbol, valid_from, valid_to FROM core.symbol_xref "
            "WHERE security_id = %s ORDER BY valid_from, symbol",
            (sid,),
        )
    ]


def test_a_victim_on_a_former_ticker_is_planned_from_the_vendors_events(db):
    survivor, victim = _former_pair(db)
    plan = cvm.plan_cross_vendor_merge(db, victim_id=victim, survivor_id=survivor)
    assert plan.blockers == []
    assert plan.mode == cvm.MODE_FORMER
    assert plan.former.rename == (RENAMED, "ZZOLD", "ZZNEW")
    assert plan.former.dated_by == "events"
    assert plan.former.period == ("ZZOLD", TOOK, RENAMED - timedelta(days=1))
    assert plan.former.survivor_from == ("ZZNEW", date(1900, 1, 1), RENAMED)
    assert plan.identifier_notes == ["cusip 333333333 vs 444444444"]
    assert plan.joined == PREFIX_DAYS and plan.unjoined == []


def test_the_re_mint_shape_is_planned_as_before(db):
    shell, victim = _pair(db)
    plan = cvm.plan_cross_vendor_merge(db, victim_id=victim, survivor_id=shell)
    assert (plan.mode, plan.former) == (cvm.MODE_REMINT, None)


def test_a_dry_run_of_a_former_ticker_shows_the_periods_and_changes_nothing(
    db, run_cli
):
    survivor, victim = _former_pair(db)
    out = run_cli(victim, survivor, "--across-vendors", "--dry-run")
    assert out.exit_code == 0, out.output
    text = _flat(out)
    assert (
        f"ZZOLD is a former ticker of {FKEY}: renamed ZZOLD -> ZZNEW on {RENAMED} "
        "(sharadar's events)"
    ) in text
    assert (
        f"records ZZOLD {TOOK}..{RENAMED - timedelta(days=1)} on the survivor" in text
    )
    assert f"the survivor's ZZNEW period starts {RENAMED} (was 1900-01-01)" in text
    assert "Dry run: nothing changed." in text
    assert "Relisted" not in text
    assert _row(db, victim) is not None
    assert _periods(db, survivor) == [("ZZNEW", date(1900, 1, 1), None)]


def test_the_merge_keeps_the_survivors_identity_and_records_the_old_ticker(db, run_cli):
    survivor, victim = _former_pair(db)
    out = run_cli(victim, survivor, "--across-vendors", "-m", "one company", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    row = _row(db, survivor)
    # The current identity is the survivor's; the victim's is what it was.
    assert (
        row["primary_symbol"],
        row["company_name"],
        row["cusip"],
        row["is_actively_trading"],
        row["delisted_date"],
    ) == ("ZZNEW", "ZZNEW Corp", "333333333", True, None)
    assert _periods(db, survivor) == [
        ("ZZOLD", TOOK, RENAMED - timedelta(days=1)),
        ("ZZNEW", RENAMED, None),
    ]
    # The survivor's bars on the overlap; the victim's prefix joins them.
    closes = {
        r["trade_date"]: r["close"]
        for r in db.fetchall(
            "SELECT trade_date, close FROM core.daily_price WHERE security_id = %s",
            (survivor,),
        )
    }
    assert len(closes) == len(SHELL_DAYS) + len(PREFIX_DAYS)
    assert closes[FIRST] == _close(FIRST).quantize(Decimal("0.000001"))
    assert not db.fetchall(
        "SELECT check_name FROM ops.data_quality_flag WHERE resolved_at IS NULL "
        "AND check_name = 'vendor_xref_ambiguous'"
    )
    run = db.fetchone(
        "SELECT * FROM ops.ingestion_run WHERE endpoint = "
        "'security-merge-across-vendors'"
    )
    assert run["status"] == "success"
    assert run["params"]["mode"] == cvm.MODE_FORMER
    assert run["params"]["relisted"] is None
    assert run["params"]["identity_after"] is None
    assert run["params"]["former_ticker"]["rename"]["new"] == "ZZNEW"
    assert [p["what"] for p in run["params"]["former_ticker"]["periods"]] == [
        "recorded",
        "moved",
    ]


def test_the_old_ticker_resolves_to_the_survivor_after_the_merge(db, run_cli):
    survivor, victim = _former_pair(db)
    out = run_cli(victim, survivor, "--across-vendors", "--yes")
    assert out.exit_code == 0, out.output
    assert db.fetchval(repo.HISTORICAL_XREF_RESOLVE_SQL, ("ZZOLD",)) == survivor
    assert db.fetchval(repo.XREF_RESOLVE_SQL, ("ZZNEW",)) == survivor


def test_without_a_rename_into_it_the_period_starts_at_the_victims_first_bar(
    db, run_cli
):
    """The victim's own period starts at the 1900 placeholder; recorded from there,
    it would claim every earlier issuer of the ticker's dates (CHK, 2026-11)."""
    survivor, victim = _former_pair(db, took=False)
    out = run_cli(victim, survivor, "--across-vendors", "--yes")
    assert out.exit_code == 0, out.output
    assert _periods(db, survivor)[0] == (
        "ZZOLD",
        PREFIX_DAYS[0],
        RENAMED - timedelta(days=1),
    )


@pytest.mark.parametrize(
    "held_from, held_to, refused",
    [
        # An earlier issuer of the ticker, gone before the victim's first bar.
        (date(2010, 1, 4), date(2015, 6, 30), False),
        # One whose dates the victim's period would overlap.
        (date(2010, 1, 4), date(2025, 12, 31), True),
    ],
)
def test_another_issuers_period_on_the_ticker_refuses_only_where_it_overlaps(
    db, run_cli, held_from, held_to, refused
):
    survivor, victim = _former_pair(db, took=False)
    other = repo.upsert_security(
        db,
        primary_symbol="ZZOLDQ",
        company_name="An earlier ZZOLD",
        asset_type="equity",
        exchange_code="NYSE",
        is_actively_trading=False,
    )
    assert repo.insert_symbol_period(
        db,
        security_id=other,
        symbol="ZZOLD",
        valid_from=held_from,
        valid_to=held_to,
        source="sharadar",
    )
    out = run_cli(victim, survivor, "--across-vendors", "--yes")
    if refused:
        assert out.exit_code != 0
        assert (
            f"security {other} holds a ZZOLD period from {held_from} to {held_to}"
        ) in _flat(out), out.output
        assert _row(db, victim) is not None
    else:
        assert out.exit_code == 0, out.output
        assert _row(db, victim) is None
    # The earlier issuer keeps its own period either way.
    assert ("ZZOLD", held_from, held_to) in _periods(db, other)


def test_a_related_ticker_the_events_do_not_date_needs_a_rename_date(db, run_cli):
    survivor, victim = _former_pair(db, events=False)
    out = run_cli(victim, survivor, "--across-vendors", "--yes")
    assert out.exit_code != 0
    assert "its events date no rename from it: pass --rename-date" in _flat(out)
    assert _row(db, victim) is not None
    out = run_cli(
        victim, survivor, "--across-vendors", "--rename-date", RENAMED, "--yes"
    )
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    assert ("ZZNEW", RENAMED, None) in _periods(db, survivor)


@pytest.mark.parametrize(
    "case, blocker",
    [
        ("unrelated", f"lists {FKEY} as ZZNEW, not as the victim's ZZOLD"),
        ("off_ticker", "is on ZZMID, not the ticker sharadar lists"),
        ("cik", "cik differs"),
        ("contradicted", "contradicts sharadar's own"),
        ("taken", "holds a ZZOLD period from"),
    ],
)
def test_the_former_ticker_guard_refuses(db, run_cli, case, blocker):
    kwargs = {
        "unrelated": {"events": False, "related": None},
        "off_ticker": {"survivor_symbol": "ZZMID"},
        # The vendor naming the victim's CIK proves a re-mint's new registrant,
        # not that an older one was the key's.
        "cik": {"victim_cik": "0000999999", "vendor_cik": "0000999999"},
    }.get(case, {})
    survivor, victim = _former_pair(db, **kwargs)
    args = [victim, survivor, "--across-vendors"]
    if case == "contradicted":
        args += ["--rename-date", RENAMED + timedelta(days=7)]
    if case == "taken":
        other = repo.upsert_security(
            db,
            primary_symbol="ZZOLX",
            company_name="Another ZZOLD",
            asset_type="equity",
            exchange_code="NYSE",
            is_actively_trading=False,
        )
        assert repo.insert_symbol_period(
            db,
            security_id=other,
            symbol="ZZOLD",
            valid_from=TOOK,
            valid_to=None,
            source="fmp",
        )
    out = run_cli(*args, "--yes")
    assert out.exit_code != 0
    assert blocker in _flat(out), out.output
    assert "Nothing was changed." in out.output
    assert _row(db, victim) is not None and _row(db, survivor) is not None


def test_a_dead_company_on_a_former_ticker_merges(db, run_cli):
    """A delisted key refuses a re-mint, which would be relisted on it; a victim on
    a former ticker is folded into the fed row as it stands (VBLT into NTBL)."""
    survivor, victim = _former_pair(db, dead=True)
    out = run_cli(victim, survivor, "--across-vendors", "--dry-run")
    assert out.exit_code == 0, out.output
    assert (
        f"Note : sharadar lists {FKEY} as delisted: the survivor is folded into as "
        "it stands, and nothing is relisted"
    ) in _flat(out)
    out = run_cli(victim, survivor, "--across-vendors", "-m", "dead company", "--yes")
    assert out.exit_code == 0, out.output
    assert _row(db, victim) is None
    row = _row(db, survivor)
    assert (row["is_actively_trading"], row["delisted_date"]) == (False, LAST)
    # The vendor's ticker starts at the rename on a dead company too, though its
    # period was closed at the delisting.
    assert _periods(db, survivor) == [
        ("ZZOLD", TOOK, RENAMED - timedelta(days=1)),
        ("ZZNEW", RENAMED, LAST),
    ]
    run = db.fetchone(
        "SELECT params FROM ops.ingestion_run WHERE endpoint = "
        "'security-merge-across-vendors'"
    )
    assert run["params"]["remarks"]


def test_a_delisted_key_still_refuses_a_re_mint(db, run_cli):
    shell, victim = _pair(db)
    db.execute(
        "UPDATE core.vendor_security SET is_delisted = true WHERE vendor_key = %s",
        (KEY,),
    )
    out = run_cli(victim, shell, "--across-vendors", "--yes")
    assert out.exit_code != 0
    assert f"sharadar lists {KEY} as delisted" in _flat(out), out.output
    assert _row(db, victim) is not None


def test_registrant_changed_lets_an_older_cik_fold_where_the_vendor_gives_the_new(
    db, run_cli
):
    """A SPAC and the company it became (AGC into GRAB): one vendor key, two
    registrants. The operator's --registrant-changed allows the difference only
    where the vendor gives the key the survivor's CIK."""
    survivor, victim = _former_pair(
        db, victim_cik="0000999999", vendor_cik="0000123456"
    )
    out = run_cli(victim, survivor, "--across-vendors", "--yes")
    assert out.exit_code != 0
    text = _flat(out)
    assert "cik differs" in text and "--registrant-changed says so" in text
    assert _row(db, victim) is not None
    out = run_cli(victim, survivor, "--across-vendors", "--registrant-changed", "--yes")
    assert out.exit_code == 0, out.output
    assert "the operator says it carries the victim's older registrant" in _flat(out)
    assert _row(db, victim) is None
    # The survivor keeps its own, current CIK.
    assert _row(db, survivor)["cik"] == "0000123456"
    run = db.fetchone(
        "SELECT params FROM ops.ingestion_run WHERE endpoint = "
        "'security-merge-across-vendors'"
    )
    assert run["params"]["registrant_changed"] is True


def test_registrant_changed_does_not_lift_a_cik_the_vendor_gives_the_victim(
    db, run_cli
):
    survivor, victim = _former_pair(
        db, victim_cik="0000999999", vendor_cik="0000999999"
    )
    out = run_cli(victim, survivor, "--across-vendors", "--registrant-changed", "--yes")
    assert out.exit_code != 0
    assert "cik differs" in _flat(out), out.output
    assert _row(db, victim) is not None


def test_registrant_changed_needs_across_vendors(db, run_cli):
    survivor, victim = _former_pair(db)
    out = run_cli(victim, survivor, "--registrant-changed", "--dry-run")
    assert out.exit_code != 0 and "need --across-vendors" in out.output
