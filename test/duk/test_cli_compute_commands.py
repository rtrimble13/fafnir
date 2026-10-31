"""`duk rc` and `duk ti sma|ema|rsi|macd`, end to end through the CLI, pinned.

These five commands share their input handling and their output. Each case below
runs one invocation and compares everything it produced -- exit code, stdout,
stderr and any `-o` file -- with the record in
``golden/compute_commands.json``, so a change to the shared code that changes a
single byte of any command's output fails here.

The record was taken from the commands as they stood before their shared code
was factored out, and is the contract that refactor kept. To re-record after a
deliberate change, run with ``DUK_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from duk.cli import main

GOLDEN = Path(__file__).parent / "golden" / "compute_commands.json"
UPDATE = os.environ.get("DUK_UPDATE_GOLDEN") == "1"


def _prices_csv(rows: int = 40) -> str:
    """Deterministic prices: a wave on a trend, two price columns and a label."""
    lines = ["date,close,high,label"]
    for i in range(rows):
        day = f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}"
        close = 100 + i * 0.75 + (5 if i % 4 == 0 else -3 if i % 4 == 2 else 0)
        lines.append(f"{day},{close:.2f},{close + 1.5:.2f},x{i % 3}")
    return "\n".join(lines) + "\n"


#: name -> (argv after `duk`, input files to create). `{in}` is the CSV of 40
#: rows, `{short}` 3 rows, `{json}` the 40 rows as JSON records, `{out}` an
#: output path under the temporary directory.
CASES: dict[str, list[str]] = {
    # rc
    "rc_simple_csv": ["rc", "-i", "{in}", "--simple"],
    "rc_all_kinds_json_lookback": [
        "rc",
        "-i",
        "{in}",
        "--simple",
        "--log",
        "--diff",
        "--cum-simple",
        "--cum-log",
        "-l",
        "2",
        "--append",
        "--json",
    ],
    "rc_annual_table": [
        "rc",
        "-i",
        "{in}",
        "--annual-simple",
        "--annual-log",
        "--table",
    ],
    "rc_precision": ["rc", "-i", "{in}", "--log", "-p", "6"],
    "rc_summary_text": ["rc", "-i", "{in}", "--simple", "--summary"],
    "rc_summary_table": ["rc", "-i", "{in}", "--simple", "--summary", "--table"],
    "rc_output_csv": ["rc", "-i", "{in}", "--simple", "-o", "{out}.csv"],
    "rc_output_json_quiet": [
        "rc",
        "-i",
        "{in}",
        "--diff",
        "--json",
        "-q",
        "-o",
        "{out}.json",
    ],
    "rc_json_input": ["rc", "-i", "{json}", "--simple"],
    "rc_no_kind": ["rc", "-i", "{in}"],
    "rc_two_formats": ["rc", "-i", "{in}", "--simple", "--csv", "--json"],
    "rc_annual_one_row": ["rc", "-i", "{one}", "--annual-simple"],
    "rc_no_date_column": ["rc", "-i", "{nodate}", "--simple"],
    "rc_no_numeric_column": ["rc", "-i", "{nonumeric}", "--simple"],
    "rc_empty_input": ["rc", "-i", "{empty}", "--simple"],
    "rc_unreadable_input": ["rc", "-i", "{garbage}", "--simple"],
}
for _kind in ("sma", "ema", "rsi"):
    CASES |= {
        f"{_kind}_csv": ["ti", _kind, "-i", "{in}", "-c", "close", "-w", "3"],
        f"{_kind}_json_column_case": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "CLOSE",
            "-w",
            "3",
            "--json",
        ],
        f"{_kind}_table": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "high",
            "-w",
            "5",
            "--table",
            "-p",
            "2",
        ],
        f"{_kind}_summary_text": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "close",
            "-w",
            "3",
            "--summary",
        ],
        f"{_kind}_summary_table": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "close",
            "-w",
            "3",
            "--summary",
            "--table",
        ],
        f"{_kind}_output_csv": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "close",
            "-w",
            "3",
            "-o",
            "{out}.csv",
        ],
        f"{_kind}_output_json": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "close",
            "-w",
            "3",
            "--json",
            "-o",
            "{out}.json",
        ],
        f"{_kind}_json_input": ["ti", _kind, "-i", "{json}", "-c", "close", "-w", "3"],
        f"{_kind}_window_zero": ["ti", _kind, "-i", "{in}", "-c", "close", "-w", "0"],
        f"{_kind}_missing_column": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "volume",
            "-w",
            "3",
        ],
        f"{_kind}_text_column": ["ti", _kind, "-i", "{in}", "-c", "label", "-w", "3"],
        f"{_kind}_too_short": ["ti", _kind, "-i", "{short}", "-c", "close", "-w", "5"],
        f"{_kind}_empty_input": [
            "ti",
            _kind,
            "-i",
            "{empty}",
            "-c",
            "close",
            "-w",
            "3",
        ],
        f"{_kind}_two_formats": [
            "ti",
            _kind,
            "-i",
            "{in}",
            "-c",
            "close",
            "-w",
            "3",
            "--table",
            "--csv",
        ],
    }
CASES |= {
    "rsi_default_window": ["ti", "rsi", "-i", "{in}", "-c", "close"],
    "macd_defaults_csv": ["ti", "macd", "-i", "{in}", "-c", "close"],
    "macd_windows_json": [
        "ti", "macd", "-i", "{in}", "-c", "Close", "--fast-window", "3",
        "--slow-window", "6", "--signal-window", "2", "--json",
    ],
    "macd_table": [
        "ti", "macd", "-i", "{in}", "-c", "close", "--fast-window", "3",
        "--slow-window", "6", "--signal-window", "2", "--table",
    ],
    "macd_summary_text": ["ti", "macd", "-i", "{in}", "-c", "close", "--summary"],
    "macd_summary_table": ["ti", "macd", "-i", "{in}", "-c", "close", "--summary", "--table"],
    "macd_output_csv": ["ti", "macd", "-i", "{in}", "-c", "close", "-o", "{out}.csv"],
    "macd_fast_not_below_slow": [
        "ti", "macd", "-i", "{in}", "-c", "close", "--fast-window", "6", "--slow-window", "6",
    ],
    "macd_fast_zero": ["ti", "macd", "-i", "{in}", "-c", "close", "--fast-window", "0"],
    "macd_slow_zero": ["ti", "macd", "-i", "{in}", "-c", "close", "--slow-window", "0"],
    "macd_signal_zero": ["ti", "macd", "-i", "{in}", "-c", "close", "--signal-window", "0"],
    "macd_too_short": ["ti", "macd", "-i", "{short}", "-c", "close"],
    "macd_missing_column": ["ti", "macd", "-i", "{in}", "-c", "volume"],
    "macd_text_column": ["ti", "macd", "-i", "{in}", "-c", "label"],
    "macd_empty_input": ["ti", "macd", "-i", "{empty}", "-c", "close"],
}  # fmt: skip


def _inputs(folder: Path) -> dict[str, str]:
    full = _prices_csv()
    files = {
        "in": ("prices.csv", full),
        "short": ("short.csv", "".join(full.splitlines(keepends=True)[:4])),
        "one": ("one.csv", "".join(full.splitlines(keepends=True)[:2])),
        "nodate": ("nodate.csv", "when,close\n2024-01-01,1\n2024-01-02,2\n"),
        "nonumeric": ("nonumeric.csv", "date,label\n2024-01-01,a\n2024-01-02,b\n"),
        "empty": ("empty.csv", "date,close\n"),
        "garbage": ("garbage.json", "{not json"),
    }
    paths = {}
    for key, (name, text) in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8")
        paths[key] = str(path)
    records = []
    for line in full.splitlines()[1:]:
        day, close, high, label = line.split(",")
        records.append(
            {"date": day, "close": float(close), "high": float(high), "label": label}
        )
    path = folder / "prices.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    paths["json"] = str(path)
    paths["out"] = str(folder / "out")
    return paths


def _run(name: str, folder: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    paths = _inputs(folder)
    rc = folder / "dukrc.toml"
    rc.write_text(
        f'[general]\nlog_dir = "{(folder / "log").as_posix()}"\n', encoding="utf-8"
    )
    argv = [arg.format(**paths) for arg in CASES[name]]
    result = CliRunner().invoke(main, ["-c", str(rc), *argv])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    produced = {}
    for path in sorted(folder.glob("out.*")):
        produced[path.name] = path.read_text(encoding="utf-8")

    def scrub(text: str) -> str:
        # pandas words its own parse errors, and rewords them between releases:
        # that the file was refused is duk's contract, the wording is not.
        text = re.sub(r"(Failed to read input file: ).*", r"\1<reason>", text)
        return text.replace(str(folder), "<tmp>")

    return {
        "exit_code": result.exit_code,
        "stdout": scrub(result.stdout),
        "stderr": scrub(result.stderr),
        "files": {k: scrub(v) for k, v in produced.items()},
    }


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


@pytest.mark.parametrize("name", sorted(CASES))
def test_output_is_what_it_was(name, tmp_path, monkeypatch):
    got = _run(name, tmp_path, monkeypatch)
    if UPDATE:
        record = _golden()
        record[name] = got
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    assert name in _golden(), f"no record for {name}: run with DUK_UPDATE_GOLDEN=1"
    assert got == _golden()[name]


def test_every_record_is_a_case():
    """A case renamed or dropped leaves a stale record; it should go too."""
    assert set(_golden()) == set(CASES)


def test_the_record_covers_success_and_failure():
    record = _golden()
    assert {r["exit_code"] for r in record.values()} == {0, 1}
    assert any(r["files"] for r in record.values())
    assert any("Warning:" in r["stderr"] for r in record.values())
