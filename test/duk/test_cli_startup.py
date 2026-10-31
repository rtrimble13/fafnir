"""What starting duk costs: pandas, numpy and the vendor clients load on first use.

duk is a process per question, and portable runs several per ticker. Loading
pandas and numpy took about 0.3s of every start, and the vendor HTTP clients
more, for commands that use none of them (duk._deferred). Each case runs in a
fresh interpreter, since this test process has long since imported them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

#: Modules that mean pandas, numpy or an HTTP client actually executed.
HEAVY = ("pandas.core.frame", "numpy._core.multiarray", "requests", "urllib3")


def _loaded_after(code: str) -> list[str]:
    script = textwrap.dedent(code) + textwrap.dedent(f"""
        import json, sys
        print(json.dumps([m for m in {list(HEAVY)!r} if m in sys.modules]))
        """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_importing_the_cli_loads_none_of_them():
    assert _loaded_after("import duk.cli") == []


def test_importing_duk_loads_none_of_them_and_its_names_still_resolve():
    assert _loaded_after("import duk; assert duk.__version__") == []
    # The library names resolve; pandas loads when one is used.
    assert _loaded_after("from duk import calculate_sma") == []
    used = """
        import pandas
        from duk import calculate_sma
        frame = calculate_sma(pandas.DataFrame({"close": [1.0, 2.0, 3.0]}), "close", 2)
        assert frame["close_sma_2"].tolist()[1:] == [1.5, 2.5]
    """
    assert "pandas.core.frame" in _loaded_after(used)


@pytest.mark.parametrize(
    "args",
    [
        ["th", "ticker:AAPL", "--json"],
        ["ca", "sid:1", "--json", "--exact"],
        ["tc", "NYSE", "--json"],
        ["ph", "sid:1", "--close", "--json", "--exact"],
    ],
)
def test_portables_reads_run_without_them(args):
    """The four commands portable runs, against a stubbed warehouse."""
    code = f"""
        import datetime as dt, os
        from decimal import Decimal
        from click.testing import CliRunner
        from duk.cli import main
        from duk.datasource import db
        db.ticker_history = lambda **kw: []
        db.calendar_days = lambda **kw: [
            {{"date": dt.date(2024, 1, 2), "is_open": True, "session_note": None}}
        ]
        db.corporate_actions = lambda **kw: {{"security_id": 1, "rows": []}}
        db.resolve_identifier = lambda **kw: [{{"security_id": 1, "symbol": "AAPL"}}]
        db.exact_bars = lambda **kw: [
            {{"date": dt.date(2024, 1, 2), "close": Decimal("1.000000")}}
        ]
        os.environ["FAFNIR_DSN"] = "host=/nonexistent"
        result = CliRunner().invoke(main, ["-c", os.devnull, *{args!r}])
        assert result.exit_code == 0, result.output
    """
    assert _loaded_after(code) == []


def test_a_command_that_needs_pandas_still_gets_it():
    code = """
        import os
        import pandas
        from click.testing import CliRunner
        from duk.cli import main
        from duk.datasource import db
        db.price_history = lambda **kw: pandas.DataFrame(
            {"close": [1.5]}, index=pandas.to_datetime(["2024-01-02"]).rename("date"))
        os.environ["FAFNIR_DSN"] = "host=/nonexistent"
        result = CliRunner().invoke(main, ["-c", os.devnull, "ph", "AAPL", "--csv"])
        assert result.exit_code == 0, result.output
        assert "2024-01-02,1.50" in result.output, result.output
    """
    assert "pandas.core.frame" in _loaded_after(code)
