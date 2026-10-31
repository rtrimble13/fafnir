"""Nothing found, in the form the output takes.

With --json an empty answer is still JSON -- ``[]`` for a list of records,
``null`` for `fa`'s one object -- and the sentence moves to stderr, so a program
parsing stdout never meets prose. As CSV (a pipe's default) or a table, it is
the sentence on stdout it always was. Exit 0 throughout: nothing found is an
answer, not a failure.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from click.testing import CliRunner

from duk.cli import main
from duk.datasource import db as ds_db

EMPTY = pd.DataFrame()


@pytest.fixture()
def run(monkeypatch, tmp_path):
    monkeypatch.setenv("FAFNIR_DSN", "host=/nonexistent dbname=fafnir")
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    monkeypatch.setattr(ds_db, "ticker_periods", lambda **kw: [])
    monkeypatch.setattr(ds_db, "price_history", lambda **kw: EMPTY)
    monkeypatch.setattr(ds_db, "exact_bars", lambda **kw: [])
    monkeypatch.setattr(ds_db, "screen", lambda **kw: EMPTY)
    monkeypatch.setattr(ds_db, "list_actively_trading", lambda **kw: EMPTY)
    monkeypatch.setattr(ds_db, "ticker_history", lambda **kw: [])
    monkeypatch.setattr(ds_db, "calendar_days", lambda **kw: [])
    monkeypatch.setattr(ds_db, "calendars", lambda **kw: [])
    monkeypatch.setattr(
        ds_db, "corporate_actions", lambda **kw: {"security_id": 1, "rows": []}
    )
    monkeypatch.setattr(
        ds_db,
        "statements",
        lambda **kw: {
            "security_id": 1,
            "as_of": dt.date(2024, 6, 30),
            "statements_of": None,
            "rows": [],
        },
    )
    monkeypatch.setattr(
        ds_db,
        "factor_row",
        lambda **kw: {
            "security_id": 1,
            "source": "mart.security_latest",
            "as_of_date": dt.date(2024, 5, 31),
            "statements_of": None,
            "row": None,
        },
    )
    monkeypatch.setattr("duk.fmp_api.get_yield_curve", lambda **kw: EMPTY)
    cfg = tmp_path / "dukrc.toml"
    cfg.write_text("")
    return lambda args: CliRunner().invoke(main, ["-c", str(cfg)] + args)


#: command -> the sentence it says when nothing is found.
LISTS = {
    "ph": (["ph", "ZZZZ"], "No data found for ZZZZ"),
    "ph_exact": (["ph", "ZZZZ", "--exact"], "No data found for ZZZZ"),
    "yc": (["-S", "fmp", "yc"], "No yield curve data found"),
    "ls_list": (["ls"], "No data found"),
    "ls_screen": (["ls", "--exchange", "NYSE"], "No data found"),
    "fs": (
        ["fs", "AAPL"],
        "No annual income statements for AAPL as known on 2024-06-30",
    ),
    "th": (["th", "NOPE"], "No ticker history for NOPE"),
    "ca": (["ca", "AAPL"], "No corporate actions for AAPL"),
    "tc_venue": (["tc", "NYSE"], "No trading calendar for NYSE in that range"),
    "tc_list": (["tc"], "No trading calendars in the warehouse"),
}


@pytest.mark.parametrize("name", sorted(LISTS))
def test_json_is_an_empty_list_and_the_sentence_goes_to_stderr(run, name):
    args, sentence = LISTS[name]
    result = run(args + ["--json"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "[]\n"
    assert sentence in result.stderr


@pytest.mark.parametrize("name", sorted(LISTS))
def test_csv_is_the_sentence_it_always_was(run, name):
    args, sentence = LISTS[name]
    result = run(args)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == sentence


@pytest.mark.parametrize("name", ["ph", "th", "fs"])
def test_quiet_prints_neither(run, name):
    args, _ = LISTS[name]
    result = run(args + ["--json", "-q"])
    assert result.exit_code == 0
    assert result.stdout == "" and result.stderr == ""


def test_fa_has_one_object_so_its_nothing_is_null(run):
    result = run(["fa", "AAPL", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "null\n"
    assert "No factor row for AAPL" in result.stderr


def test_duk_format_json_counts_as_json(run, monkeypatch):
    monkeypatch.setenv("DUK_FORMAT", "json")
    result = run(["ph", "ZZZZ"])
    assert result.stdout == "[]\n"
