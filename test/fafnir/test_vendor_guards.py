"""The loader and ingest guards against what FMP did between 2026-09-16 and 10-14
(v1.5.0, G1-G5).

The shapes are FMP's own, from every payload it served for GCDT, ZBAO, SKYD, OUSL
and QCJL in that period (landing.fmp_raw); the numbers are made up. Runs are
replayed in order through `load_symbol_prices`, as the nightly made them, with the
operator's explicit re-fetches among them.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.db import repository as repo
from fafnir.ingest import adjustments, daily_price, tracked
from fafnir.ingest.daily_price import RESTATEMENT_CHECK, SPLIT_SCALE_CHECK
from fafnir.ingest.runlog import RunLog
from fafnir.sources import vendor_gaps

pytestmark = pytest.mark.integration


class _FMP:
    """Serves one run's payload per symbol: what FMP sent that night."""

    bytes_downloaded = 0

    def __init__(self):
        self.payload: dict[str, list[dict]] = {}
        self.asked: list[tuple] = []

    def eod_raw(self, symbol, from_date=None, to_date=None):
        self.asked.append((symbol, from_date, to_date))
        return self.payload.get(symbol, [])


def _mk(db, symbol, exchange="NASDAQ"):
    repo.ensure_exchange(db, exchange, exchange, "US")
    sid = repo.upsert_security(
        db,
        primary_symbol=symbol,
        company_name=f"{symbol} Inc",
        asset_type="equity",
        exchange_code=exchange,
    )
    repo.upsert_symbol_xref(db, security_id=sid, symbol=symbol)
    return sid


def _sessions(db, n, start=dt.date(2024, 9, 3)):
    return [
        r["trade_date"]
        for r in db.fetchall(
            "SELECT trade_date FROM ref.trading_calendar WHERE exchange_code = 'NASDAQ'"
            " AND is_open AND trade_date >= %s ORDER BY trade_date LIMIT %s",
            (start, n),
        )
    ]


def _bar(day, close, volume):
    return {
        "date": day.isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _night(db, fmp, symbol, bars, *, explicit=False, restated=None):
    """One run of the price load for one symbol. Returns the run id."""
    fmp.payload = {symbol: bars}
    days = [dt.date.fromisoformat(b["date"]) for b in bars]
    window = (
        {"start_date": min(days), "end_date": max(days)} if explicit and days else {}
    )
    with RunLog(db, source="fmp", endpoint=daily_price.ENDPOINT) as run:
        daily_price.load_symbol_prices(
            db, fmp, symbol, run=run, restated=restated, **window
        )
    db.commit()
    return run.run_id


def _stored(db, sid, day):
    row = db.fetchone(
        "SELECT close, volume FROM core.daily_price WHERE security_id = %s"
        " AND trade_date = %s",
        (sid, day),
    )
    return (float(row["close"]), int(row["volume"])) if row else None


def _conflicts(db, sid):
    """{date: (raised by run, open?)} for the security's restatement conflicts."""
    return {
        r["record_key"]["date"]: (r["ingestion_run_id"], r["resolved_at"] is None)
        for r in db.fetchall(
            "SELECT record_key, ingestion_run_id, resolved_at FROM ops.data_quality_flag"
            " WHERE check_name = %s AND security_id = %s",
            (RESTATEMENT_CHECK, sid),
        )
    }


# ---------------------------------------------------------------------------
# G1: the restatement guard
# ---------------------------------------------------------------------------


def test_another_instruments_prices_never_replace_a_first_print(db):
    """OUSL and QCJL: the first print is the real fund; the next night's re-read is
    another instrument's level, every night since 2026-09-16."""
    d = _sessions(db, 3)
    sid = _mk(db, "RSWI")
    fmp = _FMP()
    _night(db, fmp, "RSWI", [_bar(d[0], 11.39, 52150)])
    second = _night(
        db, fmp, "RSWI", [_bar(d[0], 67.03, 52200), _bar(d[1], 12.0, 22777)]
    )
    third = _night(
        db,
        fmp,
        "RSWI",
        [_bar(d[0], 67.03, 52200), _bar(d[1], 68.18, 22800), _bar(d[2], 12.13, 14814)],
    )
    assert [_stored(db, sid, x)[0] for x in d] == [11.39, 12.0, 12.13]
    # Raised the second night of each date, and once: the third night's re-read of
    # d[0] finds the condition open.
    assert _conflicts(db, sid) == {
        d[0].isoformat(): (second, True),
        d[1].isoformat(): (third, True),
    }
    flag = db.fetchone(
        "SELECT detail FROM ops.data_quality_flag WHERE record_key->>'date' = %s",
        (d[0].isoformat(),),
    )["detail"]
    assert flag["over"] == ["close"] and flag["close_ratio"] == pytest.approx(
        5.885, 1e-3
    )
    assert flag["stored"]["close"] == "11.390000"
    assert flag["incoming"]["close"] == "67.030000"
    # The held date is not a hole: the watermark moved past it.
    assert repo.get_watermark(db, "fmp", daily_price.ENDPOINT, sid) == d[2]
    # A conflict is not a quarantine: it spends no watermark budget.
    assert repo.count_price_quarantines(db, sid, d[0].isoformat()) == 0


def test_a_restatement_within_the_guard_is_written(db):
    d = _sessions(db, 2)
    sid = _mk(db, "RSOK")
    fmp = _FMP()
    _night(db, fmp, "RSOK", [_bar(d[0], 0.4465, 927437)])
    _night(db, fmp, "RSOK", [_bar(d[0], 0.44667, 933798), _bar(d[1], 0.40, 567090)])
    assert _stored(db, sid, d[0]) == (0.44667, 933798)
    assert _conflicts(db, sid) == {}


def test_split_scale_restatements_are_held_until_an_operator_refetch(db):
    """GCDT: run 296 re-served four first prints x6, the 1:6 of the next day
    applied backwards. Later nights served the first prints again, except for the
    date that had left the window; an operator re-fetch resolves its conflict."""
    d = _sessions(db, 6)
    sid = _mk(db, "RSGC")
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " split_numerator, split_denominator) VALUES (%s, 'split', %s, 1, 6)",
        (sid, d[4]),
    )
    fmp = _FMP()
    firsts = [(0.4465, 927437), (0.40, 567090), (0.4291, 279547), (0.235, 7929088)]
    for i, (c, v) in enumerate(firsts):
        _night(db, fmp, "RSGC", [_bar(d[j], *firsts[j]) for j in range(i + 1)])
    scaled = _night(
        db,
        fmp,
        "RSGC",
        [_bar(d[j], round(c * 6, 4), v) for j, (c, v) in enumerate(firsts)]
        + [_bar(d[4], 1.40, 900000)],
    )
    assert [_stored(db, sid, d[j])[0] for j in range(4)] == [c for c, _ in firsts]
    assert {k: v[0] for k, v in _conflicts(db, sid).items()} == {
        d[j].isoformat(): scaled for j in range(4)
    }
    flag = db.fetchone(
        "SELECT detail FROM ops.data_quality_flag WHERE record_key->>'date' = %s",
        (d[0].isoformat(),),
    )["detail"]
    assert flag["splits_near"] == [{"ex_date": d[4].isoformat(), "split": "1:6"}]

    restated: list = []
    refetch = _night(
        db, fmp, "RSGC", [_bar(d[0], 0.44667, 933798)], explicit=True, restated=restated
    )
    assert restated == []  # nothing beyond the guard: the stored bar was right
    conflicts = _conflicts(db, sid)
    assert conflicts[d[0].isoformat()] == (scaled, False)
    assert all(open_ for k, (_, open_) in conflicts.items() if k != d[0].isoformat())
    note = db.fetchval(
        "SELECT resolution_note FROM ops.data_quality_flag WHERE record_key->>'date' = %s",
        (d[0].isoformat(),),
    )
    assert note == (
        f"operator re-fetch, run {refetch}: close 0.446500 -> 0.446670, volume "
        "927437 -> 933798"
    )


def test_a_refetch_overwrites_and_shows_what_it_restated(db, tmp_path, monkeypatch):
    """ZBAO: run 322 restated prices x50 (the 1:50 to come, applied early); the
    operator re-fetch put the prices back and returned volume x50 -- which its
    restated table would have shown on the spot."""
    d = _sessions(db, 3)
    sid = _mk(db, "RSZB")
    fmp = _FMP()
    firsts = [(0.0888, 1767746), (0.0866, 1200054), (0.0816, 1782536)]
    _night(db, fmp, "RSZB", [_bar(d[j], *firsts[j]) for j in range(3)])
    held = _night(
        db,
        fmp,
        "RSZB",
        [_bar(d[j], round(c * 50, 4), v) for j, (c, v) in enumerate(firsts)],
    )
    assert [_stored(db, sid, x) for x in d] == firsts
    assert {v for v in _conflicts(db, sid).values()} == {(held, True)}

    restated: list = []
    refetch = _night(
        db,
        fmp,
        "RSZB",
        [_bar(d[j], c, v * 50) for j, (c, v) in enumerate(firsts)],
        explicit=True,
        restated=restated,
    )
    assert [_stored(db, sid, x) for x in d] == [(c, v * 50) for c, v in firsts]
    assert [(r["date"], r["over"]) for r in restated] == [
        (x.isoformat(), ["volume"]) for x in d
    ]
    assert restated[0]["volume"] == [1767746, 88387300]
    assert {v for v in _conflicts(db, sid).values()} == {(held, False)}
    params = db.fetchval(
        "SELECT params FROM ops.ingestion_run WHERE ingestion_run_id = %s", (refetch,)
    )
    assert params["restated_count"] == 3 and len(params["restated"]) == 3

    # The table the operator reads.
    lines = []
    monkeypatch.setattr(cli.click, "echo", lambda s="", **kw: lines.append(str(s)))
    cli._echo_restated(restated, daily_price.RestatementGuard())
    text = "\n".join(lines)
    assert "Restated 3 stored bars by more than x1.25 (close) or x3 (volume)" in text
    assert "1767746 -> 88387300 (x50)" in text


def test_a_vendor_correction_is_held_too_and_waits_for_a_refetch(db):
    """SKYD: the first prints of three dates were at a phantom 2:1 scale (FMP took a
    warrant dividend for a split). The guard keeps them, and blocks the nightly's
    *correct* re-read of the third -- the guard's documented cost. The operator's
    re-fetch of the first two corrects them; the third stays flagged."""
    d = _sessions(db, 4)
    sid = _mk(db, "RSSK")
    fmp = _FMP()
    phantom = [(20.66, 15997250), (18.68, 22134009), (19.0, 18694350)]
    _night(db, fmp, "RSSK", [_bar(d[j], *phantom[j]) for j in range(3)])
    blocked = _night(
        db, fmp, "RSSK", [_bar(d[2], 9.5, 37388700), _bar(d[3], 9.28, 54205403)]
    )
    assert _stored(db, sid, d[2]) == (19.0, 18694350)
    assert _conflicts(db, sid) == {d[2].isoformat(): (blocked, True)}

    restated: list = []
    _night(
        db,
        fmp,
        "RSSK",
        [_bar(d[0], 10.33, 31994500), _bar(d[1], 9.34, 44268018)],
        explicit=True,
        restated=restated,
    )
    assert [_stored(db, sid, x)[0] for x in d[:3]] == [10.33, 9.34, 19.0]
    assert [r["date"] for r in restated] == [d[0].isoformat(), d[1].isoformat()]
    assert _conflicts(db, sid) == {d[2].isoformat(): (blocked, True)}


def test_the_guards_thresholds_come_from_the_config(tmp_path, monkeypatch):
    from fafnir.config import FafnirConfig

    rc = tmp_path / "rc"
    rc.write_text("[prices]\nrestatement_max_ratio = 1.5\n")
    cfg = FafnirConfig(str(rc))
    assert (cfg.restatement_max_ratio, cfg.restatement_max_volume_ratio) == (1.5, 3.0)
    rc.write_text("[prices]\nrestatement_max_volume_ratio = 0.5\n")
    with pytest.raises(ValueError, match="must be above 1"):
        FafnirConfig(str(rc)).restatement_max_volume_ratio


# ---------------------------------------------------------------------------
# G2: a new bar at a split-adjusted scale
# ---------------------------------------------------------------------------


def _history(db, sid, days, close, volume):
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": x,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
            }
            for x in days
        ],
    )


def _split(db, sid, ex, num, den):
    db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date,"
        " split_numerator, split_denominator) VALUES (%s, 'split', %s, %s, %s)",
        (sid, ex, num, den),
    )


def _quarantined(db, sid):
    return [
        r["record_key"]["date"]
        for r in db.fetchall(
            "SELECT record_key FROM ops.data_quality_flag WHERE check_name = %s"
            " AND security_id = %s",
            (SPLIT_SCALE_CHECK, sid),
        )
    ]


@pytest.mark.parametrize("volume", [1_800_000, 36_000], ids=["unchanged", "adjusted"])
def test_a_new_bar_at_a_coming_splits_scale_is_quarantined(db, volume):
    """A 1:50 on file five sessions on; the vendor serves the next bar x50. Its
    volume as FMP served GCDT's and ZBAO's (unchanged), or adjusted with it."""
    d = _sessions(db, 30)
    sid = _mk(db, "SSZB")
    _history(db, sid, d[:22], 0.088, 1_800_000)
    _split(db, sid, d[27], 1, 50)
    fmp = _FMP()
    _night(db, fmp, "SSZB", [_bar(d[22], 4.40, volume)])
    assert _stored(db, sid, d[22]) is None
    assert _quarantined(db, sid) == [d[22].isoformat()]
    detail = db.fetchval(
        "SELECT detail FROM ops.data_quality_flag WHERE check_name = %s",
        (SPLIT_SCALE_CHECK,),
    )
    assert detail["price_factor"] == 50 and detail["split"] == "1:50"
    assert repo.count_price_quarantines(db, sid, d[22].isoformat()) == 1


def test_a_real_move_is_not_mistaken_for_a_split_scale(db):
    """A real x2 squeeze on 50x volume with no split near it; a real reverse split
    on its own ex-date; a 20% drop before a 5:4 split, inside an ordinary day."""
    d = _sessions(db, 30)
    squeeze, reverse, small = _mk(db, "SSQZ"), _mk(db, "SSRV"), _mk(db, "SSSM")
    for sid in (squeeze, reverse, small):
        _history(db, sid, d[:22], 10.0, 100_000)
    _split(db, reverse, d[22], 1, 10)
    _split(db, small, d[25], 5, 4)
    fmp = _FMP()
    _night(db, fmp, "SSQZ", [_bar(d[22], 20.0, 5_000_000)])
    _night(db, fmp, "SSRV", [_bar(d[22], 100.0, 10_000)])
    _night(db, fmp, "SSSM", [_bar(d[22], 8.0, 100_000)])
    for sid in (squeeze, reverse, small):
        assert _stored(db, sid, d[22]) is not None
        assert _quarantined(db, sid) == []


# ---------------------------------------------------------------------------
# G3: one split on file twice
# ---------------------------------------------------------------------------


def test_two_same_ratio_splits_close_together_are_flagged_once(db):
    """GLE: 1:10 at 10-07 and 10-08, the bars moving once, on the later date."""
    d = _sessions(db, 20)
    sid = _mk(db, "SDGL")
    _history(db, sid, d[:6], 0.409, 145_080)
    _history(db, sid, d[6:], 4.29, 14_000)
    _split(db, sid, d[5], 1, 10)  # the stray: 0.409 -> 0.409
    _split(db, sid, d[6], 1, 10)  # the real one: 0.409 -> 4.29
    _split(db, sid, d[15], 2, 1)  # another ratio: not a pair
    adjustments.compute_for_security(db, sid)
    adjustments.compute_for_security(db, sid)  # every night: flagged once
    rows = db.fetchall(
        "SELECT record_key, detail FROM ops.data_quality_flag WHERE check_name = %s",
        (adjustments.DUPLICATE_SPLIT_CHECK,),
    )
    assert len(rows) == 1
    assert rows[0]["record_key"] == {"ex_dates": [d[5].isoformat(), d[6].isoformat()]}
    detail = rows[0]["detail"]
    assert detail["moves_on"] == [d[6].isoformat()]
    assert [s["moves"] for s in detail["splits"]] == [False, True]
    assert detail["splits"][1]["close_ratio"] == pytest.approx(10.489, 1e-3)
    assert detail["sessions_apart"] == 1


def test_same_ratio_splits_far_apart_are_two_splits(db):
    d = _sessions(db, 40)
    sid = _mk(db, "SDFA")
    _history(db, sid, d, 10.0, 1000)
    _split(db, sid, d[2], 1, 10)
    _split(db, sid, d[14], 1, 10)  # twelve sessions on
    adjustments.compute_for_security(db, sid)
    assert not db.fetchval(
        "SELECT count(*) FROM ops.data_quality_flag WHERE check_name = %s",
        (adjustments.DUPLICATE_SPLIT_CHECK,),
    )


# ---------------------------------------------------------------------------
# G4: live securities the vendor stopped serving
# ---------------------------------------------------------------------------


def _land(db, symbol, bars, *, fetched, to=None, frm="2024-09-20"):
    db.execute(
        "INSERT INTO landing.fmp_raw (endpoint, params, symbol, http_status, payload,"
        " fetched_at) VALUES (%s, %s::jsonb, %s, 200, %s::jsonb, %s)",
        (
            daily_price.ENDPOINT,
            json.dumps({"symbol": symbol, "from": frm, "to": to}),
            symbol,
            json.dumps(bars),
            fetched,
        ),
    )


def test_vendor_gaps_lists_what_the_vendor_stopped_serving(db, tmp_path, monkeypatch):
    """FPAS, XJUL, XAPR, XFEB: last bar 10-02 while their venue traded on."""
    d = _sessions(db, 25)
    latest = d[-1]
    tonight = dt.datetime.combine(
        latest + dt.timedelta(days=1), dt.time(3, 30), dt.timezone.utc
    )
    peers = [_mk(db, f"VGP{i}") for i in range(100)]
    repo.upsert_daily_prices(
        db,
        [
            {
                "security_id": sid,
                "trade_date": x,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 1000,
            }
            for sid in peers
            for x in d[15:]
        ],
    )
    gap, explicit, served, delisted_feed, fresh = (
        _mk(db, s) for s in ("VGGAP", "VGEXP", "VGSRV", "VGDEL", "VGNEW")
    )
    for sid in (gap, explicit, served, delisted_feed):
        _history(db, sid, d[:18], 10.0, 1000)
    _history(db, fresh, d[:-1], 10.0, 1000)  # one session behind: the vendor's lag
    _land(db, "VGGAP", [_bar(d[16], 10, 1), _bar(d[17], 10, 1)], fetched=tonight)
    _land(db, "VGEXP", [], fetched=tonight, to=d[17].isoformat())
    _land(db, "VGSRV", [_bar(d[19], 10, 1)], fetched=tonight)  # served, not stored
    _land(db, "VGDEL", [], fetched=tonight)
    _land(db, "VGNEW", [], fetched=tonight)
    db.execute(
        "INSERT INTO landing.fmp_raw (endpoint, params, payload, fetched_at)"
        " VALUES ('delisted-companies', '{}'::jsonb, %s::jsonb, %s)",
        (json.dumps([{"symbol": "VGDEL", "exchange": "NASDAQ"}]), tonight),
    )
    repo.add_dq_flag_once(
        db,
        check_name="stale",
        security_id=gap,
        record_key={"last_date": d[17].isoformat()},
    )
    report = vendor_gaps.vendor_gaps(db)
    assert report.market_latest == latest and report.delisted_payload
    (g,) = report.gaps
    assert (g.symbol, g.last_bar, g.sessions_behind) == ("VGGAP", d[17], 7)
    assert g.payload_max_date == d[17] and g.request_to is None
    assert g.venue_share == pytest.approx(101 / 105)  # the peers and VGNEW
    assert g.open_flags == {"stale": 1}

    rc = tmp_path / "rc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    out = CliRunner().invoke(
        cli.main, ["--config", str(rc), "source", "vendor-gaps", "--json"]
    )
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert [x["symbol"] for x in payload["gaps"]] == ["VGGAP"]
    text = CliRunner().invoke(cli.main, ["--config", str(rc), "source", "vendor-gaps"])
    assert "1 security the vendor stopped serving" in text.output
    assert "stale 1" in text.output


def test_vendor_gaps_needs_the_venue_to_have_moved_on(db):
    """A night the whole venue is behind is a load that did not run, not a gap."""
    d = _sessions(db, 25)
    others = [_mk(db, f"VGV{i}") for i in range(5)]
    for sid in others:
        _history(db, sid, d, 10.0, 1000)
    late = [_mk(db, f"VGL{i}") for i in range(5)]
    tonight = dt.datetime.combine(d[-1] + dt.timedelta(days=1), dt.time(3, 30))
    for sid in late:
        _history(db, sid, d[:18], 10.0, 1000)
    for i in range(5):
        _land(db, f"VGL{i}", [], fetched=tonight.replace(tzinfo=dt.timezone.utc))
    assert vendor_gaps.vendor_gaps(db).gaps == []


# ---------------------------------------------------------------------------
# G5: a declared ticker over a delisted row
# ---------------------------------------------------------------------------


class _Profiles:
    bytes_downloaded = 0

    def __init__(self, profiles):
        self._profiles = profiles

    def profile(self, symbol):
        return self._profiles.get(symbol)


def test_a_declaration_over_a_delisted_identity_mints_nothing(db):
    """BURU: declared on 2026-09-30, its only row (286436) delisted; every night
    `ingest tracked` minted another, which the delisting sweep stamped."""
    dead = repo.upsert_security(
        db,
        primary_symbol="BURX",
        company_name="Nuburu, Inc.",
        asset_type="equity",
        cusip="67021W509",
    )
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2024, 9, 13))
    repo.upsert_tracked_symbol(
        db, symbol="BURX", asset_type="equity", exchange_code=None, note="t"
    )
    db.commit()
    fmp = _Profiles({"BURX": {"companyName": "NUBURU INC", "cusip": "67021W509"}})
    for _ in range(2):
        result = tracked.load_tracked(db, fmp)
        assert result.minted == [] and result.delisted == [("BURX", dead)]
    assert db.fetchval("SELECT count(*) FROM core.security") == 1
    (flag,) = db.fetchall(
        "SELECT record_key, detail FROM ops.data_quality_flag WHERE check_name = %s",
        (tracked.DELISTED_CHECK,),
    )
    assert flag["record_key"] == {"symbol": "BURX"}
    assert flag["detail"]["delisted_security_id"] == dead


def test_a_reused_ticker_over_a_delisted_row_is_still_minted(db):
    dead = repo.upsert_security(
        db,
        primary_symbol="REUS",
        company_name="Old Co",
        asset_type="equity",
        cusip="000000AA1",
    )
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2020, 1, 2))
    repo.upsert_tracked_symbol(
        db, symbol="REUS", asset_type="equity", exchange_code=None, note="t"
    )
    db.commit()
    fmp = _Profiles({"REUS": {"companyName": "New Co", "cusip": "999999ZZ9"}})
    assert tracked.load_tracked(db, fmp).minted == ["REUS"]


def test_track_add_refuses_a_delisted_only_ticker_unless_told(
    db, tmp_path, monkeypatch
):
    dead = repo.upsert_security(
        db, primary_symbol="TADL", company_name="Gone Inc", asset_type="equity"
    )
    repo.mark_delisted(db, security_id=dead, delisted_date=dt.date(2024, 9, 13))
    db.commit()
    rc = tmp_path / "rc"
    rc.write_text(f'[database]\ndsn = "{db.dsn}"\n')
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    refused = CliRunner().invoke(
        cli.main,
        ["--config", str(rc), "track", "add", "TADL", "--asset-type", "equity"],
    )
    assert refused.exit_code != 0
    assert f"security venue-transfer {dead}" in refused.output
    assert not db.fetchval("SELECT count(*) FROM ref.tracked_symbol")
    allowed = CliRunner().invoke(
        cli.main,
        [
            "--config",
            str(rc),
            "track",
            "add",
            "TADL",
            "--asset-type",
            "equity",
            "--allow-delisted",
        ],
    )
    assert allowed.exit_code == 0, allowed.output
    assert db.fetchval("SELECT count(*) FROM ref.tracked_symbol") == 1


def test_a_conflict_is_not_counted_as_a_quarantine():
    assert RESTATEMENT_CHECK in repo.NON_QUARANTINE_PRICE_CHECKS
    assert SPLIT_SCALE_CHECK not in repo.NON_QUARANTINE_PRICE_CHECKS


def test_the_restated_table_caps_its_rows(monkeypatch):
    lines = []
    monkeypatch.setattr(cli.click, "echo", lambda s="", **kw: lines.append(str(s)))
    rows = [
        {
            "symbol": "X",
            "date": f"2024-01-{i:02d}",
            "close": ["1", "2"],
            "volume": [1, 1],
        }
        for i in range(1, 4)
    ]
    cli._echo_restated(rows, daily_price.RestatementGuard(), limit=2)
    assert "... and 1 more" in "\n".join(lines)
