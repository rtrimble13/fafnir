"""duk.table: the console table, and when duk prints one.

The rule that matters most is the one about *when*: a table only where a person is
reading (a terminal, or an explicit ``--table``), and CSV in a pipe, so every script
and agent reading duk's output today reads exactly what it did.
"""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest

from duk import table as t


class _Stream(io.StringIO):
    def __init__(self, tty: bool, encoding: str = "utf-8"):
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self):
        return self._tty

    @property
    def encoding(self):
        return self._encoding


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("DUK_FORMAT", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)


class TestResolveFormat:
    def test_auto_is_a_table_on_a_terminal(self):
        assert t.resolve_format(stream=_Stream(tty=True)) == "table"

    def test_auto_is_csv_in_a_pipe_so_scripts_are_unaffected(self):
        assert t.resolve_format(stream=_Stream(tty=False)) == "csv"

    @pytest.mark.parametrize("flag", ["table", "csv", "json"])
    def test_a_flag_wins_over_everything(self, monkeypatch, flag):
        monkeypatch.setenv("DUK_FORMAT", "json" if flag != "json" else "csv")
        chosen = t.resolve_format(
            **{flag: True}, configured="table", stream=_Stream(tty=False)
        )
        assert chosen == flag

    def test_two_flags_are_refused(self):
        with pytest.raises(t.FormatError, match="Only one of --table, --csv or --json"):
            t.resolve_format(table=True, csv=True)

    def test_env_beats_config(self, monkeypatch):
        monkeypatch.setenv("DUK_FORMAT", "csv")
        assert t.resolve_format(configured="table", stream=_Stream(tty=True)) == "csv"

    def test_config_applies_without_env(self):
        assert t.resolve_format(configured="json", stream=_Stream(tty=True)) == "json"

    def test_configured_auto_is_auto(self):
        assert t.resolve_format(configured="auto", stream=_Stream(tty=False)) == "csv"

    def test_an_unknown_configured_format_is_an_error_not_a_guess(self, monkeypatch):
        monkeypatch.setenv("DUK_FORMAT", "tabel")
        with pytest.raises(t.FormatError, match="DUK_FORMAT is 'tabel'"):
            t.resolve_format()

    def test_requested_format_is_none_for_auto(self):
        assert t.requested_format() is None
        assert t.requested_format(configured="auto") is None
        assert t.requested_format(csv=True) == "csv"


class TestCells:
    def test_prices_take_separators_and_the_requested_decimals(self):
        assert t.format_price(1234567.891, 2) == "1,234,567.89"
        assert t.format_price(250.0, 2) == "250.00"

    def test_a_sub_penny_price_is_never_floored_to_zero(self):
        assert t.format_price(0.0003123, 2) == "0.0003123"

    def test_a_decimal_is_formatted_without_a_float(self):
        # Through a float this would print ...736555 noise-free only by luck.
        assert t.format_price(Decimal("98765432109876.543210"), 2) == (
            "98,765,432,109,876.54"
        )

    def test_exact_keeps_every_digit(self):
        assert t.format_exact(Decimal("13247980.736555")) == "13,247,980.736555"
        assert t.format_exact(Decimal("189.120000")) == "189.120000"
        assert t.format_exact("-1234.5") == "-1,234.5"

    def test_an_amount_keeps_its_own_digits_up_to_six(self):
        assert t.format_amount(Decimal("0.242500")) == "0.2425"
        assert t.format_amount(Decimal("0.240000")) == "0.24"
        assert t.format_amount(0.95) == "0.95"

    def test_missing_is_a_dash_never_blank(self):
        column = t.Column("close", kind="price")
        assert t.format_cell(None, column, t.UNICODE) == "—"
        assert t.format_cell(float("nan"), column, t.UNICODE) == "—"
        assert t.format_cell(None, column, t.ASCII) == "-"
        # ...and zero is zero.
        assert t.format_cell(0.0, column, t.UNICODE) == "0.00"

    def test_no_negative_zero(self):
        assert (
            t.format_cell(-0.0001, t.Column("x", kind="number", decimals=2), t.ASCII)
            == "0.00"
        )

    @pytest.mark.parametrize(
        ("name", "values", "kind"),
        [
            ("date", [dt.date(2024, 1, 2)], "date"),
            ("ex_date", ["2024-01-02"], "date"),
            ("close", [1.5], "price"),
            ("marketCap", [Decimal("1.00")], "money"),
            ("volume", [100], "count"),
            ("security_count", [3], "count"),
            ("security_id", [601133], "id"),
            ("fiscal_year", [2024], "id"),
            ("dividend_amount", [Decimal("0.24")], "amount"),
            ("is_etf", [False], "bool"),
            ("beta", [1.24], "number"),
            ("sector_name", ["Energy"], "text"),
            ("sector_name", [None], "text"),
        ],
    )
    def test_kinds_are_inferred_from_name_then_value(self, name, values, kind):
        assert t.infer_kind(name, values) == kind

    def test_an_id_is_not_grouped_but_a_count_is(self):
        built = t.table_from_rows(
            [{"security_id": 601133, "volume": 82488700}], style=t.ASCII
        )
        assert built.rows == [["601133", "82,488,700"]]


class TestRender:
    def test_a_bordered_aligned_table_with_a_footer(self):
        built = t.table_from_rows(
            [
                {"date": dt.date(2024, 1, 2), "close": 185.64, "volume": 82488700},
                {"date": dt.date(2024, 1, 3), "close": 1234.5, "volume": 1},
            ],
            price_decimals=2,
            footer="2 bars",
            style=t.UNICODE,
        )
        assert t.render(built, style=t.UNICODE).splitlines() == [
            "╭────────────┬──────────┬────────────╮",
            "│ DATE       │    CLOSE │     VOLUME │",
            "├────────────┼──────────┼────────────┤",
            "│ 2024-01-02 │   185.64 │ 82,488,700 │",
            "│ 2024-01-03 │ 1,234.50 │          1 │",
            "╰────────────┴──────────┴────────────╯",
            "2 bars",
        ]

    def test_a_code_page_that_cannot_draw_boxes_gets_ascii(self):
        assert t.style_for_stream(_Stream(tty=True, encoding="cp1252")) is t.ASCII
        assert t.style_for_stream(_Stream(tty=True, encoding="utf-8")) is t.UNICODE

    def test_fitting_shortens_text_and_never_a_number(self):
        built = t.table_from_rows(
            [
                {
                    "name": "A very long company name indeed",
                    "cap": Decimal("123456789012"),
                }
            ],
            columns=[t.Column("name"), t.Column("cap", kind="money")],
            style=t.ASCII,
        )
        text = t.render(built, style=t.ASCII, max_width=30)
        assert "123,456,789,012" in text
        assert "A very long company name indeed" not in text
        assert "~" in text  # the ASCII ellipsis

    def test_color_only_when_asked(self):
        built = t.table_from_rows([{"a": 1}], style=t.ASCII)
        assert "\x1b[" not in t.render(built, style=t.ASCII)
        assert "\x1b[" in t.render(built, style=t.ASCII, color=True)

    def test_wide_characters_are_counted_as_two_columns(self):
        assert t.display_width("株式") == 4

    def test_key_values_format_each_row_by_its_own_field(self):
        text = t.render_key_values(
            [("security_id", 601133), ("earnings_yield", 0.034512), ("pe", None)],
            decimals=4,
            style=t.ASCII,
        )
        assert "| 601133 |" in text
        assert "0.0345" in text
        assert "601,133" not in text
        assert "| pe" in text and text.rstrip().splitlines()[-2].endswith("- |")


def test_plural():
    assert t.plural(1, "bar") == "1 bar"
    assert t.plural(2046, "bar") == "2,046 bars"
    assert t.plural(2, "security", "securities") == "2 securities"


def test_humanize():
    assert t.humanize("market_cap_usd") == "MARKET CAP USD"
    assert t.humanize("companyName") == "COMPANY NAME"
