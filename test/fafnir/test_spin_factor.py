"""
Spin factors (#283, migration 0048): integration tests (FAFNIR_TEST_DSN). Every row
is synthetic; the figures are VHI's 2007-03-27 TIMET distribution as #283 measured
it.

A spin-off is one split row whose ratio is the inverse of its total-return factor
(spin-as-split, #282). Marked ``split_kind = 'spin_factor'``:

- the market cap, the screener and the factor panel no longer read it as a share
  change, so the cap across the ex-date is the raw close x the count;
- adjusted prices are unchanged, and adjusted volume before the ex-date unscaled;
- a dividend on its ex-date is valued against the raw prior close;
- the kind survives a vendor's revision of the row, a re-date and a history split's
  undo, and is recorded in ops.split_kind_change;
- `actions mark-spins` marks what an operator's note or Sharadar's spin-off event
  names, and lists the rest.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import maintenance
from fafnir.db import repository as repo
from fafnir.ingest import adjustments
from fafnir.ingest import fundamentals_map as fm

pytestmark = pytest.mark.integration

D = date
HASH = hashlib.sha256(b"synthetic").hexdigest()
T0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)

EX = D(2007, 3, 27)
SPIN = (Decimal(224278), Decimal(100000))  # 1 / 0.44587
COUNT = 114_100_000  # FY2006's balance sheet, known before the ex-date
Q1_COUNT = 114_200_000  # Q1 2007's, known 2007-05-08
# Raw closes and volumes: the ex-date close is the parent alone.
BARS = [
    (D(2007, 3, 26), "29.45", 5000),
    (EX, "13.95", 9000),
    (D(2007, 3, 30), "14.20", 4000),
    (D(2007, 4, 30), "15.00", 3000),
    (D(2007, 5, 7), "16.00", 3000),
    (D(2007, 5, 8), "16.90", 3000),
]
NOTE = (
    "TIMET spin-off: 0.4776 TIMET per Valhi share (Valhi 8-K); "
    "13.95 / (13.95 + 0.4776 x 36.30)"
)


def _security(db, symbol):
    repo.ensure_exchange(db, "NYSE", "NYSE", "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        exchange_code="NYSE",
        currency="USD",
        source="sharadar",
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _put(db, statement, sid, fiscal, known_from, *, period, label, **items):
    row = {
        "security_id": sid,
        "vendor_key": "900283",
        "period_type": period,
        "fiscal_date": fiscal,
        "calendar_date": fiscal,
        "fiscal_year": fiscal.year,
        "fiscal_period": label,
        "version_no": 1,
        "known_from": known_from,
        "knowledge_source": "filed",
        "filing_date": known_from,
        "loaded_at": T0,
        "reported_currency": "USD",
        "content_hash": HASH,
        **items,
    }
    cols = list(row)
    db.execute(
        f"INSERT INTO {fm.TABLES[statement]} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )


def _statements(db, sid):
    fy = D(2006, 12, 31)
    _put(
        db,
        "income",
        sid,
        fy,
        D(2007, 3, 1),
        period="annual",
        label="FY",
        revenue=1000,
        net_income=100,
        net_income_common=100,
        weighted_average_shares=COUNT,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        fy,
        D(2007, 3, 1),
        period="annual",
        label="FY",
        total_assets=2000,
        total_equity_parent=1000,
        shares_outstanding=COUNT,
    )
    _put(
        db,
        "balance_sheet",
        sid,
        D(2007, 3, 31),
        D(2007, 5, 8),
        period="quarter",
        label="Q1",
        total_assets=2000,
        total_equity_parent=1000,
        shares_outstanding=Q1_COUNT,
    )


def _bars(db, sid, bars=BARS):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": day,
                "open": Decimal(c),
                "high": Decimal(c),
                "low": Decimal(c),
                "close": Decimal(c),
                "volume": v,
            }
            for day, c, v in bars
        ],
        source="sharadar",
    )


def _vhi(db, *, kind=repo.SPLIT_KIND_SPIN, symbol="ZZVHI", bars=BARS):
    """VHI with its statements and bars, and the operator's spin-as-split row."""
    sid = _security(db, symbol)
    _statements(db, sid)
    _bars(db, sid, bars)
    action_id, _ = repo.add_operator_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=EX,
        split_numerator=SPIN[0],
        split_denominator=SPIN[1],
        note=NOTE,
        created_by="tester",
        split_kind=kind,
    )
    adjustments.compute_for_security(db, sid)
    return sid, action_id


def _mcap(db, sid):
    return {
        r["trade_date"]: r
        for r in db.fetchall(
            "SELECT * FROM mart.fn_daily_market_cap(%s::bigint[], NULL, NULL)", ([sid],)
        )
    }


def _adjusted(db, sid):
    return {
        r["trade_date"]: r
        for r in db.fetchall(
            "SELECT trade_date, close, volume, price_factor, volume_factor"
            "  FROM mart.v_daily_price_adjusted WHERE security_id = %s",
            (sid,),
        )
    }


def _kind(db, action_id):
    return db.fetchval(
        "SELECT split_kind FROM core.corporate_action WHERE corporate_action_id = %s",
        (action_id,),
    )


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def test_a_share_split_kind_overstates_the_cap_across_the_spin_off(db):
    """#283's measurement: read as a share change, the ratio multiplies the count
    from the ex-date until the next balance sheet is known."""
    sid, _ = _vhi(db, kind=repo.SPLIT_KIND_SHARES)
    got = _mcap(db, sid)
    assert got[D(2007, 3, 26)]["shares_outstanding"] == COUNT
    for day in (EX, D(2007, 4, 30), D(2007, 5, 7)):
        assert got[day]["shares_outstanding"] == round(COUNT * SPIN[0] / SPIN[1])
    assert got[D(2007, 5, 8)]["shares_outstanding"] == Q1_COUNT


def test_the_cap_across_a_spin_factor_is_the_close_times_the_count(db):
    sid, _ = _vhi(db)
    got = _mcap(db, sid)
    for day, close, _ in BARS:
        shares = Q1_COUNT if day >= D(2007, 5, 8) else COUNT
        r = got[day]
        assert r["shares_outstanding"] == shares, day
        assert r["market_cap"] == Decimal(close) * shares, day


def test_the_screener_and_the_panel_agree(db):
    # A last bar inside the window, so the screener's cap is the spin's.
    sid, _ = _vhi(db, bars=BARS[:4])
    attrs = db.fetchone(
        "SELECT market_cap_usd, market_cap_source FROM mart.v_security_attributes"
        " WHERE security_id = %s",
        (sid,),
    )
    assert attrs["market_cap_source"] == "derived"
    assert attrs["market_cap_usd"] == Decimal("15.00") * COUNT

    days = [D(2007, 3, 30), D(2007, 4, 30)]
    panel = {
        r["as_of_date"]: r
        for r in db.fetchall(
            "SELECT * FROM mart.fn_fundamental_panel(%s::date[]) WHERE security_id = %s",
            (days, sid),
        )
    }
    caps = _mcap(db, sid)
    for day in days:
        assert panel[day]["shares_outstanding_as_of"] == COUNT, day
        assert panel[day]["market_cap"] == caps[day]["market_cap"], day


def test_adjusted_prices_are_unchanged_and_volume_is_unscaled(db):
    sid, action_id = _vhi(db, kind=repo.SPLIT_KIND_SHARES)
    as_split = _adjusted(db, sid)
    repo.set_split_kind(
        db,
        corporate_action_id=action_id,
        kind=repo.SPLIT_KIND_SPIN,
        basis="operator",
        note=NOTE,
        created_by="tester",
    )
    adjustments.compute_for_security(db, sid)
    as_spin = _adjusted(db, sid)

    assert {d: r["close"] for d, r in as_spin.items()} == {
        d: r["close"] for d, r in as_split.items()
    }
    before = as_spin[D(2007, 3, 26)]
    assert before["close"] < Decimal("29.45")  # still back-adjusted by the spin
    # The count did not change, so neither does the volume: not x2.24278.
    assert before["volume"] == 5000 and before["volume_factor"] == 1
    assert as_split[D(2007, 3, 26)]["volume"] != 5000
    # The ex-date is still a factor boundary.
    assert (
        db.fetchval(
            "SELECT count(*) FROM core.adjustment_factor WHERE security_id = %s", (sid,)
        )
        == 1
    )


def test_a_dividend_on_a_spin_factors_ex_date_is_valued_on_the_raw_close(db):
    sid, _ = _vhi(db)
    repo.add_operator_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=EX,
        dividend_amount=Decimal("0.05"),
        note="regular dividend",
        created_by="tester",
    )
    factors = adjustments.compute_for_security(db, sid)
    (f,) = factors
    # (P - D) / P on the prior raw close, times the spin's den/num; not re-based.
    want = (Decimal("29.45") - Decimal("0.05")) / Decimal("29.45") * SPIN[1] / SPIN[0]
    assert abs(f["cumulative_price_factor"] - want) < Decimal("1e-20")
    assert f["cumulative_volume_factor"] == 1
    assert not db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag WHERE security_id = %s"
        " AND check_name = %s",
        (sid, adjustments.SPLIT_BASIS_CHECK),
    )


def test_the_mart_views_say_which_splits_are_spin_factors(db):
    sid, action_id = _vhi(db)
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=D(2005, 6, 1),
        split_numerator=2,
        split_denominator=1,
        source="sharadar",
    )
    kinds = {
        r["ex_date"]: r["split_kind"]
        for r in db.fetchall(
            "SELECT ex_date, split_kind FROM mart.v_corporate_action"
            " WHERE security_id = %s",
            (sid,),
        )
    }
    assert kinds == {D(2005, 6, 1): "shares", EX: "spin_factor"}
    s = db.fetchone(
        "SELECT * FROM mart.v_security_action_summary WHERE security_id = %s", (sid,)
    )
    assert (s["split_count"], s["last_split_date"], s["last_split_numerator"]) == (
        1,
        D(2005, 6, 1),
        2,
    )
    assert (s["spin_factor_count"], s["last_spin_factor_date"]) == (1, EX)


# ---------------------------------------------------------------------------
# The kind, its record and its durability
# ---------------------------------------------------------------------------


def test_setting_a_kind_is_recorded_and_only_a_split_has_one(db):
    sid, action_id = _vhi(db, kind=repo.SPLIT_KIND_SHARES)
    change = repo.set_split_kind(
        db,
        corporate_action_id=action_id,
        kind=repo.SPLIT_KIND_SPIN,
        basis="operator",
        note=NOTE,
        created_by="tester",
        evidence={"filing": "8-K"},
    )
    assert _kind(db, action_id) == "spin_factor"
    (c,) = repo.split_kind_changes(db, security_id=sid)
    assert (c["change_id"], c["old_kind"], c["new_kind"], c["basis"]) == (
        change,
        "shares",
        "spin_factor",
        "operator",
    )
    assert (c["split_numerator"], c["source"], c["evidence"]) == (
        SPIN[0],
        "operator",
        {"filing": "8-K"},
    )
    # The same kind again changes and records nothing.
    again = repo.set_split_kind(
        db,
        corporate_action_id=action_id,
        kind=repo.SPLIT_KIND_SPIN,
        basis="operator",
        note=NOTE,
        created_by="tester",
    )
    assert again is None and len(repo.split_kind_changes(db, security_id=sid)) == 1

    div_id, _ = repo.add_operator_action(
        db,
        security_id=sid,
        action_type="dividend",
        ex_date=D(2007, 4, 2),
        dividend_amount=Decimal("0.05"),
        note="x",
        created_by="tester",
    )
    for kw in (
        {"corporate_action_id": div_id, "kind": "spin_factor", "basis": "operator"},
        {"corporate_action_id": action_id, "kind": "bogus", "basis": "operator"},
        {"corporate_action_id": action_id, "kind": "shares", "basis": "bogus"},
    ):
        with pytest.raises(repo.OverrideRefused):
            repo.set_split_kind(db, note="x", created_by="tester", **kw)
    with pytest.raises(repo.OverrideRefused):
        repo.add_operator_action(
            db,
            security_id=sid,
            action_type="dividend",
            ex_date=D(2007, 4, 3),
            dividend_amount=Decimal("0.05"),
            note="x",
            created_by="tester",
            split_kind=repo.SPLIT_KIND_SPIN,
        )


def test_a_vendor_revision_and_a_redate_keep_the_kind(db):
    sid = _security(db, "ZZFMP")
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=EX,
        split_numerator=10000,
        split_denominator=4109,
        source="fmp",
    )
    action_id = repo.corporate_action_at(
        db, security_id=sid, action_type="split", ex_date=EX
    )["corporate_action_id"]
    repo.set_split_kind(
        db,
        corporate_action_id=action_id,
        kind=repo.SPLIT_KIND_SPIN,
        basis="operator",
        note="x",
        created_by="tester",
    )
    # FMP revises its ratio: the row is still the spin-off's.
    assert repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=EX,
        split_numerator=10000,
        split_denominator=4200,
        source="fmp",
    )
    assert _kind(db, action_id) == "spin_factor"
    new_id, delete_id, _ = repo.redate_operator_action(
        db,
        corporate_action_id=action_id,
        new_ex_date=D(2007, 3, 28),
        note="misdated",
        created_by="tester",
    )
    assert _kind(db, new_id) == "spin_factor"
    deleted = db.fetchval(
        "SELECT detail FROM ops.operator_override WHERE override_id = %s",
        (delete_id,),
    )
    assert deleted["row"]["split_kind"] == "spin_factor"


# ---------------------------------------------------------------------------
# The marking
# ---------------------------------------------------------------------------


def _link(db, sid, key):
    db.execute(
        "INSERT INTO core.vendor_security (vendor, vendor_table, vendor_key, ticker,"
        " currency) VALUES ('sharadar', 'stocks', %s, %s, 'USD')",
        (key, f"T{key}"),
    )
    repo.insert_vendor_link(
        db,
        security_id=sid,
        vendor="sharadar",
        vendor_key=key,
        valid_from=D(2020, 1, 2),  # the event belongs to the key, whenever linked
        match_method="identifier",
        confidence=None,
        evidence={},
        created_by="tester",
    )


def _event(db, key, day, action="spinoff", contra="ZZKID"):
    db.execute(
        "INSERT INTO core.vendor_security_event (vendor, vendor_key, event_date,"
        " action, ticker, contra_ticker, contra_name)"
        " VALUES ('sharadar', %s, %s, %s, %s, %s, %s)",
        (key, day, action, f"T{key}", contra, f"{contra} Corp"),
    )


def _split(db, sid, day, num, den, source="fmp"):
    repo.upsert_corporate_action(
        db,
        security_id=sid,
        action_type="split",
        ex_date=day,
        split_numerator=num,
        split_denominator=den,
        source=source,
    )
    return repo.corporate_action_at(
        db, security_id=sid, action_type="split", ex_date=day
    )["corporate_action_id"]


def _world(db):
    """One candidate of each kind."""
    vhi, noted = _vhi(db, kind=repo.SPLIT_KIND_SHARES)  # the operator names a spin
    fmp = _security(db, "ZZPAR")
    _link(db, fmp, "900901")
    on_day = _split(db, fmp, D(2015, 10, 1), 10000, 7431)
    _event(db, "900901", D(2015, 10, 1))
    near = _split(db, fmp, D(2018, 6, 4), 10000, 8800)
    _event(db, "900901", D(2018, 6, 1))  # three days off: shown, not marked
    shared = _split(db, fmp, D(2019, 2, 1), 21, 20)  # a stock dividend, no event
    _event(db, "900901", D(2012, 1, 3), action="spunofffrom")  # the child's, ignored
    odd = _split(db, fmp, D(2020, 3, 2), 1000, 999)  # the shape, no evidence
    sh = _security(db, "ZZSHR")
    _link(db, sh, "900902")
    sharadar = _split(db, sh, D(2016, 5, 2), 3, 1, source="sharadar")
    _event(db, "900902", D(2016, 5, 2))  # not FMP's row: listed, not marked
    real = _split(db, fmp, D(2021, 7, 1), 2, 1)  # an ordinary split: not a candidate
    return {
        "noted": noted,
        "on_day": on_day,
        "near": near,
        "shared": shared,
        "odd": odd,
        "sharadar": sharadar,
        "real": real,
        "vhi": vhi,
        "fmp": fmp,
    }


def test_the_rules_mark_what_the_evidence_names(db):
    w = _world(db)
    found = {c["corporate_action_id"]: c for c in repo.spin_factor_candidates(db)}
    assert found[w["noted"]]["basis"] == "operator_note"
    assert found[w["noted"]]["noted"]["note"] == NOTE
    assert found[w["on_day"]]["basis"] == "sharadar_spinoff"
    assert found[w["on_day"]]["event"]["contra_ticker"] == "ZZKID"
    assert not found[w["on_day"]]["dividend_same_day"]
    assert found[w["near"]]["basis"] is None
    assert found[w["near"]]["event"]["event_date"] == D(2018, 6, 1)
    assert found[w["odd"]]["basis"] is None and found[w["odd"]]["event"] is None
    assert found[w["sharadar"]]["basis"] is None
    assert w["shared"] not in found and w["real"] not in found
    assert set(
        c["security_id"] for c in repo.spin_factor_candidates(db, security_id=w["fmp"])
    ) == {w["fmp"]}


def _run(*args):
    return CliRunner().invoke(cli.main, list(args), catch_exceptions=False)


@pytest.fixture()
def cli_db(db, monkeypatch):
    monkeypatch.setenv("FAFNIR_DSN", db.dsn)
    return db


def test_mark_spins_dry_runs_marks_and_marks_nothing_twice(cli_db):
    db = cli_db
    w = _world(db)
    repo.upsert_corporate_action(
        db,
        security_id=w["fmp"],
        action_type="dividend",
        ex_date=D(2015, 10, 1),
        dividend_amount=Decimal("0.20"),
    )
    out = _run("actions", "mark-spins", "--dry-run", "--all")
    assert out.exit_code == 0, out.output
    assert "2 splits a rule marks" in out.output
    assert "1 of them shares an ex-date with a cash dividend" in out.output
    assert (
        "3 other candidates" in out.output and "spinoff ZZKID 2018-06-01" in out.output
    )
    assert "Dry run" in out.output
    assert _kind(db, w["on_day"]) == "shares"

    out = _run("actions", "mark-spins", "--yes", "--by", "tester")
    assert out.exit_code == 0, out.output
    assert "Marked 2 splits spin_factor as tester, on 2 securities" in out.output
    assert _kind(db, w["on_day"]) == _kind(db, w["noted"]) == "spin_factor"
    assert _kind(db, w["near"]) == _kind(db, w["odd"]) == "shares"
    changes = {c["corporate_action_id"]: c for c in repo.split_kind_changes(db)}
    assert changes[w["on_day"]]["basis"] == "sharadar_spinoff"
    assert "Sharadar ACTIONS spinoff ZZKID 2015-10-01" in changes[w["on_day"]]["note"]
    assert changes[w["noted"]]["evidence"]["noted"]["note"] == NOTE
    # The factors were recomputed: VHI's volume is unscaled.
    assert _adjusted(db, w["vhi"])[D(2007, 3, 26)]["volume"] == 5000

    out = _run("actions", "mark-spins", "--yes")
    assert "0 splits a rule marks" in out.output
    assert len(repo.split_kind_changes(db)) == 2


def test_actions_kind_add_and_list_show_the_kind(cli_db):
    db = cli_db
    sid = _security(db, "ZZKND")
    _bars(db, sid)
    action_id = _split(db, sid, EX, 224278, 100000)
    out = _run(
        "actions", "kind", str(action_id), "--spin-factor", "-m", NOTE, "--dry-run"
    )
    assert out.exit_code == 0 and "Dry run" in out.output, out.output
    assert _kind(db, action_id) == "shares"
    out = _run("actions", "kind", str(action_id), "--spin-factor", "-m", NOTE, "--yes")
    assert out.exit_code == 0, out.output
    assert _kind(db, action_id) == "spin_factor"
    out = _run("actions", "list", "--security-id", str(sid))
    assert "224278:100000 spin factor" in out.output
    out = _run("actions", "kind", str(action_id), "--spin-factor", "-m", NOTE, "--yes")
    assert "Nothing to change" in out.output
    out = _run("actions", "kind", str(action_id), "-m", NOTE, "--yes")
    assert out.exit_code != 0

    out = _run(
        "actions",
        "add",
        "--security-id",
        str(sid),
        "--ex-date",
        "2007-04-30",
        "--split",
        "11:10",
        "--spin-factor",
        "-m",
        NOTE,
        "--yes",
    )
    assert out.exit_code == 0, out.output
    assert "add split 11:10 spin factor" in out.output
    added = repo.corporate_action_at(
        db, security_id=sid, action_type="split", ex_date=D(2007, 4, 30)
    )
    assert added["split_kind"] == "spin_factor"
    out = _run(
        "actions",
        "add",
        "--security-id",
        str(sid),
        "--ex-date",
        "2007-05-01",
        "--dividend",
        "0.1",
        "--spin-factor",
        "-m",
        NOTE,
        "--yes",
    )
    assert out.exit_code != 0 and "--spin-factor marks a --split" in out.output


def test_the_marts_refresh_with_the_kind(db):
    """mart.fundamental_panel and mart.security_latest stand through 0048 and take
    the kind on their refresh."""
    sid, _ = _vhi(db, bars=BARS[:4])
    maintenance.refresh_marts(
        db, views=["mart.fundamental_panel", "mart.security_latest"]
    )
    row = db.fetchone(
        "SELECT shares_outstanding_as_of FROM mart.fundamental_panel"
        " WHERE security_id = %s AND as_of_date = %s",
        (sid, D(2007, 3, 30)),
    )
    assert row["shares_outstanding_as_of"] == COUNT
    assert (
        db.fetchval(
            "SELECT market_cap_usd FROM mart.security_latest WHERE security_id = %s",
            (sid,),
        )
        == Decimal("15.00") * COUNT
    )


# ---------------------------------------------------------------------------
# 0048's readers are their predecessors, one line apart
# ---------------------------------------------------------------------------

MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"


def _block(name, start, end="$fn$;"):
    text = (MIGRATIONS / name).read_text()
    i = text.index(start)
    return text[i : text.index(end, i) + len(end)].splitlines()


@pytest.mark.parametrize(
    "before, start, end",
    [
        (
            "0042_daily_market_cap.up.sql",
            "CREATE OR REPLACE FUNCTION mart.fn_daily_market_cap(",
            "$fn$;",
        ),
        (
            "0042_daily_market_cap.up.sql",
            "CREATE OR REPLACE VIEW mart.v_security_attributes AS",
            ") bt ON bt.security_id = s.security_id;",
        ),
        (
            "0043_panel_share_factor.up.sql",
            "CREATE OR REPLACE FUNCTION mart.fn_fundamental_panel(",
            "$fn$;",
        ),
    ],
)
def test_0048_re_creates_each_reader_with_one_line_added(before, start, end):
    """The up is the last definition with the spin-factor filter added, marked
    `-- 0048`; the down is the last definition exactly."""
    old = _block(before, start, end)
    new = _block("0048_split_kind.up.sql", start, end)
    added = [line for line in new if line not in old]
    assert [line for line in new if "-- 0048" not in line] == old
    assert len(added) == 1 and "split_kind = 'shares'" in added[0]
    # The down drops and re-creates a view (CREATE VIEW); a function is replaced.
    plain = start.replace("CREATE OR REPLACE VIEW", "CREATE VIEW")
    restored = _block("0048_split_kind.down.sql", plain, end)
    assert restored[1:] == old[1:]
