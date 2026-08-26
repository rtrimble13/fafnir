"""Price display formatting: fixed decimals that never floor a price to zero."""

from __future__ import annotations

import math

import pandas as pd
import pytest
from click.testing import CliRunner

from duk import cli
from duk.format_utils import (
    format_price,
    format_price_columns,
    price_columns_in,
    round_price,
    round_price_columns,
)


class TestFormatPrice:
    def test_ordinary_prices_get_two_decimals(self):
        assert format_price(250.0) == "250.00"
        assert format_price(37.25) == "37.25"
        assert format_price(12345.6789) == "12345.68"

    def test_adjusted_history_rounds_normally(self):
        # AAPL --adj 1990-01-02 -- comfortably above the penny floor.
        assert format_price(0.2602372277) == "0.26"

    def test_sub_penny_keeps_significant_digits(self):
        # These are the values 2dp would report as a zero-priced trade.
        assert format_price(0.004999) == "0.004999"
        assert format_price(0.0003123) == "0.0003123"
        assert format_price(0.0000005) == "0.0000005000"

    def test_nothing_non_zero_ever_renders_as_zero(self):
        value = 1.0
        for _ in range(40):  # 1.0 down to 1e-40
            assert float(format_price(value)) != 0.0, value
            value /= 10

    def test_true_zero_still_renders_as_zero(self):
        assert format_price(0.0) == "0.00"

    def test_rounding_boundary(self):
        # 0.005 survives 2dp rounding; anything below it takes the fallback.
        assert format_price(0.005) == "0.01"
        assert format_price(0.00499) == "0.004990"

    def test_negative_prices(self):
        assert format_price(-37.25) == "-37.25"
        assert format_price(-0.0003123) == "-0.0003123"

    def test_missing_values_render_empty(self):
        assert format_price(float("nan")) == ""
        assert format_price(None) == ""
        assert format_price(math.inf) == ""

    def test_decimals_are_configurable(self):
        assert format_price(0.2602372277, decimals=4) == "0.2602"
        assert format_price(250.0, decimals=0) == "250"

    def test_fallback_significant_digits_are_configurable(self):
        assert format_price(0.0003123456, fallback_sig=2) == "0.00031"
        assert format_price(0.0003123456, fallback_sig=6) == "0.000312346"


class TestRoundPrice:
    """The JSON path: same rule, but the result stays a number."""

    def test_returns_numbers_not_text(self):
        assert round_price(0.2602372277) == 0.26
        assert round_price(250.0) == 250.0

    def test_small_values_are_not_flooded_to_zero(self):
        assert round_price(0.0003123) == 0.0003123
        assert round_price(0.004999) != 0

    def test_missing_values_pass_through(self):
        assert round_price(None) is None
        assert math.isnan(round_price(float("nan")))


def _frame():
    return pd.DataFrame(
        {
            "date": ["1990-01-02", "1990-01-03"],
            "open": [0.2462647591, 0.0003123],
            "high": [0.2619837863, 0.0004],
            "low": [0.2445182005, 0.0002],
            "close": [0.2602372277, 0.0003123],
            "volume": [183198512, 207995312],
        }
    )


class TestPriceColumns:
    def test_only_price_columns_are_selected(self):
        assert price_columns_in(_frame()) == ["open", "high", "low", "close"]

    def test_volume_and_date_are_untouched(self):
        out = format_price_columns(_frame())

        assert out["volume"].tolist() == [183198512, 207995312]
        assert out["date"].tolist() == ["1990-01-02", "1990-01-03"]

    def test_prices_become_fixed_decimal_text(self):
        out = format_price_columns(_frame())

        assert out["close"].tolist() == ["0.26", "0.0003123"]

    def test_the_input_frame_is_not_mutated(self):
        df = _frame()
        format_price_columns(df)

        assert df["close"].iloc[0] == 0.2602372277

    def test_round_keeps_columns_numeric(self):
        out = round_price_columns(_frame())

        assert out["close"].tolist() == [0.26, 0.0003123]


class TestPhOutput:
    """End-to-end: `duk ph` renders prices without reaching a data source."""

    @staticmethod
    def _run(monkeypatch, tmp_path, *args, frame=None):
        data = _frame() if frame is None else frame
        indexed = data.set_index(pd.to_datetime(data["date"])).drop(columns=["date"])
        indexed.index.name = "date"
        monkeypatch.setattr(cli.ds_db, "price_history", lambda **kwargs: indexed.copy())

        dukrc = tmp_path / "dukrc"
        dukrc.write_text(f'[general]\nlog_dir = "{tmp_path / "log"}"\n')
        return CliRunner().invoke(
            cli.main,
            ["--config", str(dukrc), "-S", "db", "ph", "AAPL"] + list(args),
        )

    def test_default_output_is_two_decimals(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0
        assert "0.26" in result.stdout
        assert "0.2602372277" not in result.stdout

    def test_sub_penny_row_is_not_zeroed(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path)

        assert "0.0003123" in result.stdout
        # No row should report a traded price of zero.
        for line in result.stdout.splitlines()[1:]:
            assert ",0.00," not in line

    def test_volume_is_not_reformatted(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path)

        assert "183198512" in result.stdout

    def test_precision_flag_is_honoured(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, "-p", "4")

        assert "0.2602" in result.stdout

    def test_json_output_stays_numeric(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, "--json")

        assert '"close":0.26' in result.stdout.replace(" ", "")
        assert '"0.26"' not in result.stdout

    def test_output_file_keeps_full_precision_by_default(self, monkeypatch, tmp_path):
        out = tmp_path / "prices.csv"
        result = self._run(monkeypatch, tmp_path, "-o", str(out), "-q")

        assert result.exit_code == 0
        # -o feeds the ti/rc compute pipeline, so it must not be quantized.
        assert "0.2602372277" in out.read_text()

    def test_output_file_is_formatted_when_precision_is_explicit(
        self, monkeypatch, tmp_path
    ):
        out = tmp_path / "prices.csv"
        self._run(monkeypatch, tmp_path, "-o", str(out), "-q", "-p", "2")

        written = out.read_text()
        assert "0.26" in written
        assert "0.2602372277" not in written
        # The guard still applies inside files.
        assert "0.0003123" in written


@pytest.mark.parametrize(
    "value",
    [0.0114553617, 0.044457327574, 0.2602372277, 37.25, 250.0],
)
def test_real_market_values_round_trip_without_becoming_zero(value):
    """Values taken from live vendor data for WMT, KO and AAPL."""
    assert float(format_price(value)) > 0
