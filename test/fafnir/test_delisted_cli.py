"""The two commands an operator drives a survivorship backfill with.

`ingest delisted --backfill` mints securities, which is the one destructive thing
in this area, and `source audit-delisted` is what tells them whether it is worth
doing. Both are run by hand against a real warehouse, so what they print is the
whole interface -- a count with no follow-up instruction leaves a warehouse full
of securities with no bars.
"""

from __future__ import annotations

import types

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.ingest import delisted


class _NoDB:
    """Stands in for Database: the command's DB work is monkeypatched out."""

    def __init__(self, dsn):
        self.dsn = dsn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeFMP:
    bytes_downloaded = 1234


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(cli, "Database", _NoDB)
    monkeypatch.setattr(cli, "_fmp_client", lambda cfg: _FakeFMP())
    return CliRunner()


def _invoke(runner, command, args=()):
    return runner.invoke(
        command, list(args), obj={"config": types.SimpleNamespace(dsn="dsn")}
    )


def _result(**kw):
    base = dict(marked=0, seen=0, minted=0, unmatched=0, undated=0, already=0, reused=0)
    base.update(kw)
    return delisted.DelistedSweepResult(**base)


# ---------------------------------------------------------------------------
# ingest delisted
# ---------------------------------------------------------------------------


def test_nightly_sweep_does_not_backfill(runner, monkeypatch):
    seen = {}

    def load(db, fmp, *, max_pages, backfill):
        seen.update(max_pages=max_pages, backfill=backfill)
        return _result(marked=3, seen=10)

    monkeypatch.setattr(delisted, "load_delisted", load)

    result = _invoke(runner, cli.ingest_delisted)

    assert result.exit_code == 0
    assert seen == {"max_pages": 5, "backfill": False}
    assert "Marked 3" in result.output
    assert "Minted" not in result.output


def test_backfill_reports_the_mint_and_the_next_step(runner, monkeypatch):
    """A minted security is inactive from birth, so the ordinary price run skips
    it. Without --include-inactive spelled out, the backfill silently produces a
    universe with no bars behind it."""
    monkeypatch.setattr(
        delisted,
        "load_delisted",
        lambda db, fmp, *, max_pages, backfill: _result(
            marked=1, seen=40, minted=12, unmatched=15
        ),
    )

    result = _invoke(runner, cli.ingest_delisted, ["--full", "--backfill"])

    assert result.exit_code == 0
    assert "Minted 12" in result.output
    assert "--include-inactive" in result.output
    # The 3 it could not mint are named, not swallowed.
    assert "3 feed rows matched no listed security" in result.output


def test_backfill_without_full_warns_it_only_sees_the_tail(runner, monkeypatch):
    monkeypatch.setattr(
        delisted,
        "load_delisted",
        lambda db, fmp, *, max_pages, backfill: _result(),
    )

    result = _invoke(runner, cli.ingest_delisted, ["--backfill"])

    assert "recent tail" in result.output


def test_full_pages_deeper(runner, monkeypatch):
    seen = {}

    def load(db, fmp, *, max_pages, backfill):
        seen.update(max_pages=max_pages)
        return _result()

    monkeypatch.setattr(delisted, "load_delisted", load)
    _invoke(runner, cli.ingest_delisted, ["--full"])

    assert seen == {"max_pages": 500}


def test_nothing_minted_says_so_without_the_price_instruction(runner, monkeypatch):
    monkeypatch.setattr(
        delisted,
        "load_delisted",
        lambda db, fmp, *, max_pages, backfill: _result(seen=4),
    )

    result = _invoke(runner, cli.ingest_delisted, ["--backfill"])

    assert "Minted 0" in result.output
    assert "--include-inactive" not in result.output


# ---------------------------------------------------------------------------
# source audit-delisted
# ---------------------------------------------------------------------------


_REPORT = {
    "feed_rows": 3,
    "bytes_downloaded": 99,
    "max_pages": 500,
    "in_scope": 2,
    "held": 1,
    "reused": 0,
    "mintable": 1,
    "undated": 0,
    "out_of_scope": 1,
    "oldest": "2009-01-16",
    "newest": "2024-05-05",
    "venues": {"NYSE": 2, "HKSE": 1},
    "unmapped_venues": {"HKSE": 1},
    "by_year": {"2009": 1, "2024": 1},
    "master_listed": 5,
    "master_known": 7,
    "rows": [
        {
            "symbol": "GHOST",
            "company": "Ghost Inc",
            "raw_exchange": "NYSE",
            "norm_exchange": "NYSE",
            "delisted_date": "2009-01-16",
            "ipo_date": "1990-01-02",
            "status": "mintable",
        }
    ],
}


def test_audit_prints_the_report(runner, monkeypatch):
    monkeypatch.setattr(
        delisted, "audit_delisted", lambda db, fmp, *, max_pages: dict(_REPORT)
    )

    result = _invoke(runner, cli.source_audit_delisted)

    assert result.exit_code == 0
    assert "Feed depth: 2009-01-16" in result.output
    assert "mintable=1" in result.output


def test_audit_writes_a_csv_when_asked(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        delisted, "audit_delisted", lambda db, fmp, *, max_pages: dict(_REPORT)
    )
    out = tmp_path / "audit.csv"

    result = _invoke(runner, cli.source_audit_delisted, ["-o", str(out)])

    assert result.exit_code == 0
    lines = out.read_text().splitlines()
    assert lines[0] == (
        "symbol,company,raw_exchange,norm_exchange,delisted_date,ipo_date,status"
    )
    assert lines[1].startswith("GHOST,Ghost Inc,NYSE,NYSE,2009-01-16,1990-01-02")
    assert "Wrote 1 rows" in result.output


def test_audit_passes_max_pages_through(runner, monkeypatch):
    seen = {}

    def audit(db, fmp, *, max_pages):
        seen["max_pages"] = max_pages
        return dict(_REPORT)

    monkeypatch.setattr(delisted, "audit_delisted", audit)
    _invoke(runner, cli.source_audit_delisted, ["--max-pages", "3"])

    assert seen == {"max_pages": 3}


def test_refused_reuse_rows_are_named_not_swallowed(runner, monkeypatch):
    """A silent refusal looks identical to "nothing to do". The count is a standing
    survivorship gap the operator has to be able to see."""
    monkeypatch.setattr(
        delisted,
        "load_delisted",
        lambda db, fmp, *, max_pages, backfill: _result(seen=9, reused=2),
    )

    result = _invoke(runner, cli.ingest_delisted, ["--full"])

    assert "Refused 2 rows as ticker reuse" in result.output
    assert "delisted_ticker_reuse" in result.output
