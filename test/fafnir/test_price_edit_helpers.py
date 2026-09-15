"""Pure helpers behind `fafnir prices shift|rescale`, and their argument checks.

No database: the math of a rescale, the date of a shift, and the refusals a
malformed --factor or --from/--to must raise before a connection is opened.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fafnir import cli
from fafnir.ingest import price_edits as pe


def _row(d="2024-06-05", o="10", h="12", lo="9", c="11", volume=1000, vwap="10.5"):
    return {
        "trade_date": date.fromisoformat(d),
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(lo),
        "close": Decimal(c),
        "volume": volume,
        "vwap": Decimal(vwap) if vwap is not None else None,
        "source": "fmp",
    }


# ---------------------------------------------------------------------------
# parse_factor
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("20", Decimal(20)),
        ("0.05", Decimal("0.05")),
        ("1/20", Decimal("0.05")),
        (" 1 / 4 ", Decimal("0.25")),
        ("25000", Decimal(25000)),
    ],
)
def test_a_factor_is_a_number_or_a_fraction(text, expected):
    assert pe.parse_factor(text) == expected


@pytest.mark.parametrize("text", ["0", "-2", "1/0", "abc", "", "1/x", "nan", "inf"])
def test_a_factor_must_be_a_positive_finite_number(text):
    with pytest.raises(ValueError):
        pe.parse_factor(text)


# ---------------------------------------------------------------------------
# rescale_row
# ---------------------------------------------------------------------------
def test_rescale_multiplies_every_price_and_vwap_and_scales_volume_separately():
    new, reason = pe.rescale_row(_row(), Decimal(20), Decimal("0.05"))
    assert reason is None
    assert (new["open"], new["high"], new["low"], new["close"]) == (
        Decimal("200.000000"),
        Decimal("240.000000"),
        Decimal("180.000000"),
        Decimal("220.000000"),
    )
    assert new["vwap"] == Decimal("210.000000")
    assert new["volume"] == 50
    assert new["trade_date"] == date(2024, 6, 5)


def test_rescale_rounds_volume_half_up_to_a_whole_share():
    assert (
        pe.rescale_row(_row(volume=1000), Decimal(1), Decimal(1) / 3)[0]["volume"]
        == 333
    )
    assert (
        pe.rescale_row(_row(volume=1500), Decimal(2), Decimal("0.001"))[0]["volume"]
        == 2
    )


def test_rescale_rounds_prices_to_the_columns_scale():
    new, _ = pe.rescale_row(
        _row(o="1", h="1", lo="1", c="1", vwap=None), Decimal(1) / 3, Decimal(1)
    )
    assert new["close"] == Decimal("0.333333")
    assert new["vwap"] is None


def test_rescale_refuses_a_price_below_the_columns_resolution():
    new, reason = pe.rescale_row(
        _row(o="0.001", h="0.001", lo="0.001", c="0.001"), Decimal("0.0001"), Decimal(1)
    )
    assert new is None
    assert reason == "subresolution_price"


def test_rescale_refuses_a_price_past_the_columns_range():
    new, reason = pe.rescale_row(_row(), Decimal(10) ** 14, Decimal(1))
    assert (new, reason) == (None, "price_out_of_range")


def test_rescale_refuses_a_real_range_the_rounding_would_flatten():
    # 1.0 / 1.4 shrunk a million-fold: both round to 0.000001.
    new, reason = pe.rescale_row(
        _row(o="1.0", h="1.4", lo="1.0", c="1.2"), Decimal("0.000001"), Decimal(1)
    )
    assert (new, reason) == (None, "scale_collapse")


def test_rescale_accepts_a_flat_bar_shrunk_to_the_smallest_price():
    new, reason = pe.rescale_row(
        _row(o="1", h="1", lo="1", c="1", vwap=None), Decimal("0.000001"), Decimal(1)
    )
    assert reason is None
    assert new["close"] == Decimal("0.000001")


# ---------------------------------------------------------------------------
# shift_row, weekday_counts, is_calendar_mlk_gap
# ---------------------------------------------------------------------------
def test_shift_moves_the_date_and_nothing_else():
    old = _row(d="2024-06-02")
    new = pe.shift_row(old, 1)
    assert new["trade_date"] == date(2024, 6, 3)
    assert {k: new[k] for k in ("open", "high", "low", "close", "volume", "vwap")} == {
        k: old[k] for k in ("open", "high", "low", "close", "volume", "vwap")
    }
    assert pe.shift_row(old, -3)["trade_date"] == date(2024, 5, 30)


def test_weekday_counts_shows_the_one_day_early_shape():
    # Sun..Thu is what a history dated one day early looks like: no Fridays.
    early = [date(2024, 6, d) for d in (2, 3, 4, 5, 6)]
    assert pe.weekday_counts(early) == [1, 1, 1, 1, 0, 0, 1]
    assert pe.weekday_counts(d.replace(day=d.day + 1) for d in early) == [
        1,
        1,
        1,
        1,
        1,
        0,
        0,
    ]


@pytest.mark.parametrize(
    "d, expected",
    [
        (date(1994, 1, 17), True),
        (date(1990, 1, 15), True),
        (date(1997, 1, 20), True),
        (date(1998, 1, 19), False),  # NYSE's first MLK closure: the calendar is right
        (date(1994, 1, 18), False),
        (date(1994, 1, 10), False),
    ],
)
def test_the_calendars_mlk_gap_is_recognised(d, expected):
    assert pe.is_calendar_mlk_gap(d) is expected


# ---------------------------------------------------------------------------
# Argument checks before a database is opened
# ---------------------------------------------------------------------------
class _NoDB:
    def __init__(self, dsn):
        raise AssertionError("argument validation must not open a connection")


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(cli, "Database", _NoDB)
    return CliRunner()


def _rescale(runner, *extra):
    return runner.invoke(
        cli.main,
        ["prices", "rescale", "--security-id", "1", "-m", "x"] + list(extra),
        catch_exceptions=False,
    )


@pytest.mark.parametrize("factor", ["0", "-1", "abc", "1/0"])
def test_a_bad_factor_is_a_usage_error(runner, factor):
    out = _rescale(
        runner, "--from", "2024-01-01", "--to", "2024-02-01", "--factor", factor
    )
    assert out.exit_code == 2, out.output
    assert "Invalid value for --factor" in out.output


def test_a_bad_volume_factor_is_a_usage_error(runner):
    out = _rescale(
        runner,
        "--from",
        "2024-01-01",
        "--to",
        "2024-02-01",
        "--factor",
        "2",
        "--volume-factor",
        "0",
    )
    assert out.exit_code == 2, out.output
    assert "Invalid value for --volume-factor" in out.output


def test_a_backwards_range_is_a_usage_error(runner):
    out = runner.invoke(
        cli.main,
        [
            "prices",
            "shift",
            "--security-id",
            "1",
            "--from",
            "2024-02-01",
            "--to",
            "2024-01-01",
            "--days",
            "1",
            "-m",
            "x",
        ],
        catch_exceptions=False,
    )
    assert out.exit_code == 2, out.output
    assert "is after --to" in out.output
