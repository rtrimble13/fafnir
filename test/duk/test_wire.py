"""The HTTP API's wire format (ADR 0021): what goes in comes out, type and all.

`duk -S db` must print the same thing whether the warehouse answered over a DSN or
over the API. These assert the half of that which needs no database: every kind of
value a read returns survives a trip through JSON unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal

import pandas as pd
import pytest

from duk import identifiers
from duk.datasource import wire


def _round_trip(value):
    # Through real JSON text, with NaN refused: what the server actually sends.
    return wire.decode(json.loads(json.dumps(wire.encode(value), allow_nan=False)))


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        7,
        2**70,
        1.5,
        "AAPL",
        Decimal("189.8400"),
        Decimal("1E+3"),
        dt.date(2024, 1, 2),
        dt.datetime(2024, 1, 2, 3, 4, 5, 678),
        dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
        [1, "x", None, [Decimal("2.50")]],
        {"security_id": 12, "rows": [{"ex_date": dt.date(2020, 8, 31)}]},
        {"$odd": 1, 3: "non-string key"},
        identifiers.parse("cik:51143"),
        identifiers.parse("CC@2008-06-30"),
    ],
)
def test_a_value_round_trips_with_its_type(value):
    back = _round_trip(value)
    assert back == value
    assert type(back) is type(value)


def test_a_decimal_keeps_its_scale():
    # 189.8400 and 189.84 are equal and print differently; `ph --exact` prints.
    assert str(_round_trip(Decimal("189.8400"))) == "189.8400"


def test_non_finite_floats_survive_strict_json():
    assert math.isnan(_round_trip(math.nan))
    assert _round_trip(math.inf) == math.inf
    assert _round_trip(-math.inf) == -math.inf


def test_a_tuple_arrives_as_a_list():
    assert _round_trip(("a", 1)) == ["a", 1]


def _price_frame(exact=False):
    df = pd.DataFrame(
        [
            {"date": dt.date(2024, 1, 2), "close": Decimal("1.50"), "volume": 10},
            {"date": dt.date(2024, 2, 5), "close": Decimal("2.25"), "volume": 20},
        ]
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    if not exact:
        df["close"] = df["close"].astype(float)
    return df


def _screen_frame():
    df = pd.DataFrame(
        [
            {
                "symbol": "A",
                "companyName": None,
                "marketCap": Decimal("3"),
                "beta": 1.5,
                "feedFrozenOn": dt.date(2024, 1, 1),
                "isEtf": True,
            },
            {
                "symbol": "B",
                "companyName": "B Corp",
                "marketCap": None,
                "beta": None,
                "feedFrozenOn": None,
                "isEtf": False,
            },
        ]
    )
    df.attrs["unclassified"] = 3
    return df


@pytest.mark.parametrize(
    "frame",
    [
        _price_frame(),
        _price_frame(exact=True),
        _price_frame().resample("ME").agg({"close": "last", "volume": "sum"}),
        _screen_frame(),
        pd.DataFrame(),
        pd.DataFrame(columns=["security_id", "date", "market_cap"]),
        # Adjusted volume past int64: exact Python ints in an object column.
        pd.DataFrame({"volume": [2**70, 5]}, dtype=object),
        pd.DataFrame({"x": [1.0, math.nan, math.inf]}),
    ],
    ids=[
        "prices",
        "exact",
        "resampled",
        "screen",
        "empty",
        "columns-only",
        "big-ints",
        "nan-inf",
    ],
)
def test_a_frame_round_trips_with_dtypes_index_and_attrs(frame):
    back = _round_trip(frame)
    pd.testing.assert_frame_equal(back, frame)
    assert back.attrs == frame.attrs


def test_an_exact_frame_still_holds_decimals():
    back = _round_trip(_price_frame(exact=True))
    assert isinstance(back["close"].iloc[0], Decimal)


def test_a_frame_inside_a_dict_round_trips():
    # company_summary returns exactly this shape.
    back = _round_trip({"profile": {"symbol": "A"}, "adjusted_prices": _price_frame()})
    pd.testing.assert_frame_equal(back["adjusted_prices"], _price_frame())


def test_an_uncarried_type_is_refused_not_stringified():
    with pytest.raises(wire.WireError, match="cannot carry"):
        wire.encode({"when": dt.timedelta(days=1)})


@pytest.mark.parametrize(
    "payload",
    [{"$unknown": 1}, {"$dec": "not a number"}, {"$date": "2024-13-40"}],
)
def test_a_payload_not_in_the_format_is_a_wire_error(payload):
    with pytest.raises(wire.WireError):
        wire.decode(payload)
