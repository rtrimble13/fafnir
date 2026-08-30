"""The corporate-actions loader stopped re-downloading all of history every night.

These cover the parts of ADR 0007 that are cheap to get wrong and expensive to
notice: the future-ex-date guard (a wrong adjusted series, silently), the sweep's
watermark arithmetic, the first-load rule that keeps a newly minted security from
being skipped forever, and the reconciliation that is the only thing standing
between a vendor coverage gap and a quietly wrong warehouse.

Unit-level: the Database and RunLog are stood in for, so these run with no Postgres.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fafnir.ingest import corporate_actions as ca


class _FakeFMP:
    """Records what was asked for, so a test can assert on request *shape*."""

    def __init__(self, splits=None, dividends=None, cal_splits=None, cal_divs=None):
        self._splits = splits or {}
        self._dividends = dividends or {}
        self._cal_splits = cal_splits or []
        self._cal_divs = cal_divs or []
        self.symbol_calls: list[str] = []
        self.calendar_windows: list[tuple[str, date, date]] = []
        self.bytes_downloaded = 0
        self.request_count = 0

    def splits(self, symbol):
        self.symbol_calls.append(symbol)
        self.request_count += 1
        return list(self._splits.get(symbol, []))

    def dividends(self, symbol):
        self.symbol_calls.append(symbol)
        self.request_count += 1
        return list(self._dividends.get(symbol, []))

    def splits_calendar(self, from_date, to_date):
        self.calendar_windows.append(("splits", from_date, to_date))
        self.request_count += 1
        return list(self._cal_splits)

    def dividends_calendar(self, from_date, to_date):
        self.calendar_windows.append(("dividends", from_date, to_date))
        self.request_count += 1
        return list(self._cal_divs)


class _FakeRun:
    run_id = 7
    rows_quarantined = 0
    rows_inserted = 0
    symbols_requested = 0
    bytes_downloaded = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDB:
    """Just enough Database to run the loader: everything goes through repo, which
    the tests monkeypatch, so this only has to absorb commits."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture()
def stub_repo(monkeypatch):
    """Replace the repository with an in-memory double and hand back its state."""
    state = {
        "actions": {},  # security_id -> {(type, ex_date): dict}
        "watermarks": {},  # (endpoint, security_id) -> date
        "landed": [],  # every land_payload call
        "flags": [],  # every add_dq_flag_once call
        "resolve": {},  # symbol -> security_id
    }

    def upsert(db, *, security_id, action_type, ex_date, **kw):
        rows = state["actions"].setdefault(security_id, {})
        key = (action_type, ex_date)
        values = {
            k: kw.get(k)
            for k in (
                "split_numerator",
                "split_denominator",
                "dividend_amount",
                "record_date",
                "payment_date",
                "declaration_date",
            )
        }
        changed = rows.get(key) != values
        rows[key] = values
        return changed

    monkeypatch.setattr(ca.repo, "upsert_corporate_action", upsert)
    monkeypatch.setattr(
        ca.repo,
        "land_payload",
        lambda db, **kw: state["landed"].append(kw),
    )
    monkeypatch.setattr(
        ca.repo,
        "add_dq_flag_once",
        lambda db, **kw: state["flags"].append(kw) or True,
    )
    monkeypatch.setattr(
        ca.repo,
        "resolve_security_id",
        lambda db, symbol, source="fmp": state["resolve"].get(symbol),
    )
    monkeypatch.setattr(
        ca.repo,
        "get_watermark",
        lambda db, source, endpoint, security_id=0: state["watermarks"].get(
            (endpoint, security_id)
        ),
    )

    def set_wm(db, source, endpoint, last_loaded_date, security_id=0):
        prior = state["watermarks"].get((endpoint, security_id))
        state["watermarks"][(endpoint, security_id)] = (
            last_loaded_date if prior is None else max(prior, last_loaded_date)
        )

    monkeypatch.setattr(ca.repo, "set_watermark", set_wm)
    monkeypatch.setattr(
        ca.repo,
        "corporate_actions_for",
        lambda db, security_id: [
            {"action_type": t, "ex_date": d, **v}
            for (t, d), v in sorted(state["actions"].get(security_id, {}).items())
        ],
    )
    return state


TODAY = date(2026, 8, 30)


# ---------------------------------------------------------------------------
# The future-ex-date guard
# ---------------------------------------------------------------------------


def test_a_declared_but_not_yet_ex_dividend_is_not_stored(stub_repo):
    """The hazard the calendar feed introduces, and the one that corrupts silently.

    A dividend reaches the feed when it is DECLARED. Storing one with a future
    ex_date gives the security an adjustment_factor whose effective_date is in the
    future, and mart.v_daily_price_adjusted applies to a price at date t the factor
    at the smallest effective_date > t -- so today's close would be back-adjusted for
    a dividend that has not happened.
    """
    stub_repo["resolve"]["KO"] = 11
    result = ca.ActionsResult()
    fmp = _FakeFMP(
        dividends={
            "KO": [
                {"date": "2026-08-15", "dividend": 0.48},  # gone ex
                {"date": "2026-09-15", "dividend": 0.48},  # declared, not yet ex
            ]
        }
    )

    ca.load_symbol_actions(
        _FakeDB(), fmp, "KO", 11, run=_FakeRun(), as_of=TODAY, result=result
    )

    stored = sorted(d for _, d in stub_repo["actions"][11])
    assert stored == [date(2026, 8, 15)]
    assert result.future_skipped == 1
    # Not a quarantine: the row is perfectly good data, it has just not happened yet.
    assert stub_repo["flags"] == []


def test_an_event_going_ex_today_is_stored(stub_repo):
    """The boundary is inclusive: an ex-date of today has happened."""
    stub_repo["resolve"]["KO"] = 11
    result = ca.ActionsResult()
    fmp = _FakeFMP(dividends={"KO": [{"date": TODAY.isoformat(), "dividend": 0.48}]})

    ca.load_symbol_actions(
        _FakeDB(), fmp, "KO", 11, run=_FakeRun(), as_of=TODAY, result=result
    )

    assert ("dividend", TODAY) in stub_repo["actions"][11]
    assert result.future_skipped == 0


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_the_sweep_reads_from_the_watermark_back_by_the_overlap(stub_repo):
    stub_repo["watermarks"][(ca.SWEEP_ENDPOINT, 0)] = date(2026, 8, 20)
    fmp = _FakeFMP()

    ca.sweep_calendar(
        _FakeDB(),
        fmp,
        run=_FakeRun(),
        as_of=TODAY,
        overlap_days=7,
        result=ca.ActionsResult(),
    )

    assert [(w[1], w[2]) for w in fmp.calendar_windows] == [
        (date(2026, 8, 13), TODAY),
        (date(2026, 8, 13), TODAY),
    ]


def test_the_sweep_never_asks_past_today(stub_repo):
    """`to` is clamped even though the feed would happily return announced events."""
    stub_repo["watermarks"][(ca.SWEEP_ENDPOINT, 0)] = TODAY
    fmp = _FakeFMP()

    ca.sweep_calendar(
        _FakeDB(),
        fmp,
        run=_FakeRun(),
        as_of=TODAY,
        overlap_days=7,
        result=ca.ActionsResult(),
    )

    assert all(window[2] == TODAY for window in fmp.calendar_windows)


def test_the_first_sweep_starts_at_the_overlap_not_at_the_beginning_of_time(stub_repo):
    """No sweep watermark does NOT mean history is missing.

    Every security without a per-security watermark is getting a full per-symbol pull
    in the same invocation, so the sweep has nothing to catch up on. Starting it in
    1990 would re-download the entire market's history for nothing.
    """
    fmp = _FakeFMP()

    ca.sweep_calendar(
        _FakeDB(),
        fmp,
        run=_FakeRun(),
        as_of=TODAY,
        overlap_days=7,
        result=ca.ActionsResult(),
    )

    assert fmp.calendar_windows[0][1] == TODAY - timedelta(days=7)


def test_the_sweep_keeps_only_symbols_this_warehouse_holds(stub_repo):
    stub_repo["resolve"]["AAPL"] = 1
    fmp = _FakeFMP(
        cal_divs=[
            {"symbol": "AAPL", "date": "2026-08-10", "dividend": 0.25},
            {"symbol": "NOTOURS", "date": "2026-08-10", "dividend": 1.00},
        ],
        cal_splits=[
            {
                "symbol": "ALSONOT",
                "date": "2026-08-11",
                "numerator": 2,
                "denominator": 1,
            }
        ],
    )
    result = ca.ActionsResult()

    ca.sweep_calendar(
        _FakeDB(), fmp, run=_FakeRun(), as_of=TODAY, overlap_days=7, result=result
    )

    assert list(stub_repo["actions"]) == [1]
    assert result.calendar_rows == 3
    assert result.unresolved_rows == 2


def test_the_sweep_lands_one_payload_per_window_not_one_per_symbol(stub_repo):
    """Landing growth tracks how many events happened, not universe size."""
    stub_repo["resolve"].update({"AAPL": 1, "MSFT": 2})
    fmp = _FakeFMP(
        cal_divs=[
            {"symbol": "AAPL", "date": "2026-08-10", "dividend": 0.25},
            {"symbol": "MSFT", "date": "2026-08-11", "dividend": 0.75},
        ]
    )

    ca.sweep_calendar(
        _FakeDB(),
        fmp,
        run=_FakeRun(),
        as_of=TODAY,
        overlap_days=7,
        result=ca.ActionsResult(),
    )

    assert [p["endpoint"] for p in stub_repo["landed"]] == [
        "splits-calendar",
        "dividends-calendar",
    ]
    assert all(p["symbol"] is None for p in stub_repo["landed"])


def test_the_sweep_watermark_advances_only_after_both_feeds(stub_repo):
    """A window is covered when splits AND dividends have been transformed.

    If the dividends call raises, the watermark must not have moved -- otherwise the
    next run starts after a window whose dividends were never read, and those events
    are lost for good.
    """
    fmp = _FakeFMP()

    def boom(from_date, to_date):
        raise RuntimeError("feed down")

    fmp.dividends_calendar = boom

    with pytest.raises(RuntimeError):
        ca.sweep_calendar(
            _FakeDB(),
            fmp,
            run=_FakeRun(),
            as_of=TODAY,
            overlap_days=7,
            result=ca.ActionsResult(),
        )

    assert (ca.SWEEP_ENDPOINT, 0) not in stub_repo["watermarks"]


# ---------------------------------------------------------------------------
# Watermarks on the per-symbol path
# ---------------------------------------------------------------------------


def test_a_security_that_has_never_paid_anything_still_gets_a_watermark(stub_repo):
    """Stamped with as_of, not with the last ex-date.

    Keying the watermark off the last event would mean a security with no events
    never gets one -- and is therefore re-pulled in full every single night, forever.
    """
    db = _FakeDB()
    ca._load_securities(
        db,
        _FakeFMP(),
        [{"security_id": 42, "symbol": "NOPAY"}],
        run=_FakeRun(),
        as_of=TODAY,
        result=ca.ActionsResult(),
    )

    assert stub_repo["watermarks"][(ca.ENDPOINT, 42)] == TODAY


def test_each_security_commits_on_its_own(stub_repo):
    """An interruption keeps every security already processed."""
    db = _FakeDB()
    ca._load_securities(
        db,
        _FakeFMP(),
        [
            {"security_id": 1, "symbol": "A"},
            {"security_id": 2, "symbol": "B"},
            {"security_id": 3, "symbol": "C"},
        ],
        run=_FakeRun(),
        as_of=TODAY,
        result=ca.ActionsResult(),
    )

    assert db.commits == 3


# ---------------------------------------------------------------------------
# The changed-set
# ---------------------------------------------------------------------------


def test_re_loading_an_unchanged_history_reports_nothing_changed(stub_repo):
    """What makes `fafnir adjust --changed` worth having.

    The upsert is idempotent either way; the point is that the second run knows it
    wrote nothing, so the recompute has an empty set to work from instead of the
    whole universe.
    """
    stub_repo["resolve"]["KO"] = 11
    fmp = _FakeFMP(dividends={"KO": [{"date": "2026-08-15", "dividend": 0.48}]})

    first = ca.ActionsResult()
    ca.load_symbol_actions(
        _FakeDB(), fmp, "KO", 11, run=_FakeRun(), as_of=TODAY, result=first
    )
    second = ca.ActionsResult()
    ca.load_symbol_actions(
        _FakeDB(), fmp, "KO", 11, run=_FakeRun(), as_of=TODAY, result=second
    )

    assert first.changed == 1 and first.changed_security_ids == {11}
    assert second.upserted == 1  # still confirmed
    assert second.changed == 0 and second.changed_security_ids == set()


def test_an_amended_dividend_counts_as_changed(stub_repo):
    stub_repo["resolve"]["KO"] = 11
    db, run = _FakeDB(), _FakeRun()
    ca.load_symbol_actions(
        db,
        _FakeFMP(dividends={"KO": [{"date": "2026-08-15", "dividend": 0.48}]}),
        "KO",
        11,
        run=run,
        as_of=TODAY,
        result=ca.ActionsResult(),
    )

    amended = ca.ActionsResult()
    ca.load_symbol_actions(
        db,
        _FakeFMP(dividends={"KO": [{"date": "2026-08-15", "dividend": 0.52}]}),
        "KO",
        11,
        run=run,
        as_of=TODAY,
        result=amended,
    )

    assert amended.changed == 1
    assert amended.changed_security_ids == {11}


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_flags_an_event_the_calendar_missed(stub_repo):
    """The whole reason the rotation exists: a coverage gap is otherwise silent."""
    stub_repo["resolve"]["KO"] = 11
    stub_repo["actions"][11] = {}  # sweep never found anything for KO
    result = ca.ActionsResult()

    ca.reconcile(
        _FakeDB(),
        _FakeFMP(dividends={"KO": [{"date": "2026-06-15", "dividend": 0.48}]}),
        [{"security_id": 11, "symbol": "KO"}],
        run=_FakeRun(),
        as_of=TODAY,
        settle_days=7,
        result=result,
    )

    assert result.reconciled == 1
    assert result.drift == 1
    assert 11 in result.changed_security_ids
    (flag,) = stub_repo["flags"]
    assert flag["check_name"] == "corporate_action_drift"
    assert flag["detail"]["missing_from_calendar"] == ["dividend 2026-06-15"]
    # Repaired as well as reported: leaving known-wrong data in place to make a
    # point would be the worse trade.
    assert ("dividend", date(2026, 6, 15)) in stub_repo["actions"][11]


def test_reconciliation_is_quiet_when_the_sweep_got_it_right(stub_repo):
    stub_repo["resolve"]["KO"] = 11
    fmp = _FakeFMP(dividends={"KO": [{"date": "2026-06-15", "dividend": 0.48}]})
    ca.load_symbol_actions(
        _FakeDB(), fmp, "KO", 11, run=_FakeRun(), as_of=TODAY, result=ca.ActionsResult()
    )

    result = ca.ActionsResult()
    ca.reconcile(
        _FakeDB(),
        fmp,
        [{"security_id": 11, "symbol": "KO"}],
        run=_FakeRun(),
        as_of=TODAY,
        settle_days=7,
        result=result,
    )

    assert result.reconciled == 1
    assert result.drift == 0
    assert stub_repo["flags"] == []


def test_an_event_the_two_feeds_have_not_both_caught_up_on_is_not_drift(stub_repo):
    """The false positive that would turn the DQ queue back into a log.

    The calendar and the per-symbol endpoint do not update in lockstep. A dividend
    that went ex last night can be on one and not the other in either direction, and
    the sweep's overlap window re-reads exactly that period. Flagging inside it would
    file a drift row for every security that just went ex, every night.
    """
    stub_repo["resolve"]["KO"] = 11
    # Swept from the calendar two days ago; the per-symbol feed does not have it yet.
    stub_repo["actions"][11] = {
        ("dividend", TODAY - timedelta(days=2)): {
            "split_numerator": None,
            "split_denominator": None,
            "dividend_amount": 0.48,
            "record_date": None,
            "payment_date": None,
            "declaration_date": None,
        }
    }
    result = ca.ActionsResult()

    ca.reconcile(
        _FakeDB(),
        _FakeFMP(dividends={"KO": []}),
        [{"security_id": 11, "symbol": "KO"}],
        run=_FakeRun(),
        as_of=TODAY,
        settle_days=7,
        result=result,
    )

    assert result.drift == 0
    assert stub_repo["flags"] == []


def test_reconciliation_reports_an_action_the_source_withdrew(stub_repo):
    """An upsert-only loader can never delete one, but it can now see one.

    Detected by comparing the feed against what is stored, not the warehouse before
    against the warehouse after -- the latter can only ever grow.
    """
    stub_repo["resolve"]["KO"] = 11
    stub_repo["actions"][11] = {
        ("dividend", date(2026, 6, 15)): {
            "split_numerator": None,
            "split_denominator": None,
            "dividend_amount": 0.48,
            "record_date": None,
            "payment_date": None,
            "declaration_date": None,
        }
    }
    result = ca.ActionsResult()

    ca.reconcile(
        _FakeDB(),
        _FakeFMP(dividends={"KO": []}),  # the source no longer offers it
        [{"security_id": 11, "symbol": "KO"}],
        run=_FakeRun(),
        as_of=TODAY,
        settle_days=7,
        result=result,
    )

    assert result.drift == 1
    (flag,) = stub_repo["flags"]
    assert flag["detail"]["withdrawn_by_source"] == ["dividend 2026-06-15"]
    # Reported, not acted on: fafnir does not silently discard corporate actions.
    assert ("dividend", date(2026, 6, 15)) in stub_repo["actions"][11]


def test_reconciliation_reports_a_settled_amendment_the_overlap_should_have_caught(
    stub_repo,
):
    stub_repo["resolve"]["KO"] = 11
    stub_repo["actions"][11] = {
        ("dividend", date(2026, 6, 15)): {
            "split_numerator": None,
            "split_denominator": None,
            "dividend_amount": 0.48,
            "record_date": None,
            "payment_date": None,
            "declaration_date": None,
        }
    }
    result = ca.ActionsResult()

    ca.reconcile(
        _FakeDB(),
        _FakeFMP(dividends={"KO": [{"date": "2026-06-15", "dividend": 0.52}]}),
        [{"security_id": 11, "symbol": "KO"}],
        run=_FakeRun(),
        as_of=TODAY,
        settle_days=7,
        result=result,
    )

    assert result.drift == 1
    (flag,) = stub_repo["flags"]
    assert flag["detail"]["amended"] == ["dividend 2026-06-15"]
    assert flag["detail"]["missing_from_calendar"] == []


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        ca.run_actions(_FakeDB(), _FakeFMP(), mode="whatever")


def test_explicit_symbols_always_take_the_per_symbol_path(stub_repo, monkeypatch):
    """`--symbols VFIAX` means that symbol, in full, whatever the configured mode."""
    stub_repo["resolve"]["VFIAX"] = 5
    monkeypatch.setattr(ca, "RunLog", lambda *a, **kw: _FakeRun())
    fmp = _FakeFMP()

    result = ca.run_actions(_FakeDB(), fmp, mode="auto", symbols=["vfiax"], as_of=TODAY)

    assert fmp.calendar_windows == []
    assert fmp.symbol_calls == ["VFIAX", "VFIAX"]
    assert result.symbols_pulled == 1
