"""
The wire format of the fafnir HTTP API (ADR 0021): the warehouse reads' arguments
and results as JSON, and back, with nothing changed on the way.

``duk.datasource.db`` returns what psycopg and pandas hand it: ``Decimal`` prices,
``date`` and ``datetime`` values, frames with a ``DatetimeIndex`` and typed
columns. Plain JSON has none of those, and a client that read a price through a
float, or a date through a string, would print something other than what the same
command prints over a DSN. So every value JSON cannot carry is tagged -- an object
with one ``$``-prefixed key -- and :func:`decode` puts back the value
:func:`encode` was given:

    ``{"$dec": "189.8400"}``         ``Decimal``, digits and scale as stored
    ``{"$date": "2024-01-02"}``      ``datetime.date``
    ``{"$dt": "..."}``               ``datetime.datetime`` (ISO 8601, offset kept)
    ``{"$ts": "..."}``               ``pandas.Timestamp``
    ``{"$nat": true}``               ``pandas.NaT``
    ``{"$f": "nan"|"inf"|"-inf"}``   a float JSON has no literal for
    ``{"$map": [[key, value], ...]}``  a dict whose keys are not plain strings
    ``{"$identifier": {...}}``       :class:`duk.identifiers.Identifier`
    ``{"$frame": {...}}``            ``pandas.DataFrame``: columns, dtypes, index,
                                     ``attrs``

A tuple arrives as a list, and a timezone as its UTC offset; nothing the CLI
prints depends on either. A value of any other type is refused
(:class:`WireError`) rather than passed through ``str``: a read that starts
returning one should fail in a test, not reach a client as text.

Both ends import this module, the server from the same ``duk`` the client runs,
so there is one definition of the format. It loads neither pandas nor numpy
unless a frame is actually on the wire (``duk._deferred``).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from duk import identifiers

_NONFINITE = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}


class WireError(TypeError):
    """A value the wire format does not carry, or a payload that is not in it."""


def _from_pandas(value: Any) -> bool:
    return type(value).__module__.partition(".")[0] == "pandas"


def _from_numpy(value: Any) -> bool:
    return type(value).__module__.partition(".")[0] == "numpy"


def encode(value: Any) -> Any:
    """``value`` as JSON-ready data (``json.dumps(..., allow_nan=False)`` accepts
    it). Raises :class:`WireError` for a type the format does not carry."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$f": "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if _from_pandas(value):
        return _encode_pandas(value)
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, identifiers.Identifier):
        return {
            "$identifier": {
                "scheme": value.scheme,
                "value": value.value,
                "raw": value.raw,
                "explicit": value.explicit,
                "as_of": encode(value.as_of),
            }
        }
    if isinstance(value, dict):
        if all(isinstance(k, str) and not k.startswith("$") for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {"$map": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if _from_numpy(value) and getattr(value, "shape", None) == ():
        return encode(value.item())
    raise WireError(f"the fafnir API cannot carry a {type(value).__name__}")


def decode(value: Any) -> Any:
    """The value :func:`encode` was given. Raises :class:`WireError` for a payload
    that is not in the format (an unknown tag: a newer server, or not this API)."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        (tag, body), *_ = value.items()
        if tag.startswith("$"):
            return _decode_tag(tag, body)
    return {k: decode(v) for k, v in value.items()}


def _decode_tag(tag: str, body: Any) -> Any:
    try:
        if tag == "$dec":
            return Decimal(body)
        if tag == "$date":
            return date.fromisoformat(body)
        if tag == "$dt":
            return datetime.fromisoformat(body)
        if tag == "$f":
            return _NONFINITE[body]
        if tag == "$map":
            return {decode(k): decode(v) for k, v in body}
        if tag == "$identifier":
            return identifiers.Identifier(
                scheme=body["scheme"],
                value=body["value"],
                raw=body["raw"],
                explicit=bool(body["explicit"]),
                as_of=decode(body["as_of"]),
            )
        if tag == "$ts":
            from duk._deferred import pandas as pd

            return pd.Timestamp(body)
        if tag == "$nat":
            from duk._deferred import pandas as pd

            return pd.NaT
        if tag == "$frame":
            return _decode_frame(body)
    except WireError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any malformed body is one error
        raise WireError(f"malformed {tag} value in a fafnir API payload") from exc
    raise WireError(f"unknown tag {tag!r} in a fafnir API payload")


# ---------------------------------------------------------------------------
# pandas
# ---------------------------------------------------------------------------


def _encode_pandas(value: Any) -> Any:
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        return {"$frame": _encode_frame(value)}
    if value is pd.NaT:
        return {"$nat": True}
    if isinstance(value, pd.Timestamp):
        return {"$ts": value.isoformat()}
    raise WireError(f"the fafnir API cannot carry a pandas {type(value).__name__}")


def _encode_array(values: Any) -> dict:
    """One column, or the index: its dtype by name, and its values as a list."""
    dtype = values.dtype
    kind = getattr(dtype, "kind", "O")
    if kind == "M":
        cells = [None if v is None else v.isoformat() for v in _stamps(values)]
    elif kind == "f":
        cells = [None if v != v else encode(v) for v in values.tolist()]
    elif kind in "iub":
        cells = values.tolist()
    else:
        cells = [encode(v) for v in values.tolist()]
    return {"dtype": str(dtype), "values": cells}


def _stamps(values: Any) -> list:
    import pandas as pd

    return [None if v is pd.NaT else v for v in pd.Index(values).tolist()]


def _encode_frame(df: Any) -> dict:
    import pandas as pd

    if isinstance(df.index, pd.MultiIndex) or isinstance(df.columns, pd.MultiIndex):
        raise WireError("the fafnir API cannot carry a frame with a MultiIndex")
    index = df.index
    plain = (
        isinstance(index, pd.RangeIndex)
        and index.start == 0
        and index.step == 1
        and index.name is None
    )
    encoded_index = None
    if not plain:
        encoded_index = _encode_array(index)
        encoded_index["name"] = encode(index.name)
        encoded_index["freq"] = getattr(index, "freqstr", None)
    return {
        "rows": len(df),
        "columns": [
            {"name": encode(name), **_encode_array(df.iloc[:, i])}
            for i, name in enumerate(df.columns.tolist())
        ],
        "index": encoded_index,
        "attrs": encode(dict(df.attrs)),
    }


def _decode_array(spec: dict) -> Any:
    """The array :func:`_encode_array` described, at the dtype it had where this
    pandas has that dtype, else as objects."""
    import numpy as np
    import pandas as pd

    name, cells = spec["dtype"], spec["values"]
    if name == "object":
        return pd.array([decode(v) for v in cells], dtype=object)
    if name.startswith("datetime64["):
        unit, _, zone = name[len("datetime64[") : -1].partition(", ")
        stamps = pd.to_datetime(cells, utc=bool(zone), format="ISO8601")
        if zone:
            stamps = stamps.tz_convert(zone)
        return stamps.as_unit(unit)
    try:
        dtype = np.dtype(name)
    except TypeError:
        dtype = None
    if dtype is not None and dtype.kind == "f":
        return np.array([math.nan if v is None else decode(v) for v in cells], dtype)
    if dtype is not None and dtype.kind in "iub":
        return np.array(cells, dtype)
    decoded = [decode(v) for v in cells]
    try:
        return pd.array(decoded, dtype=name)
    except (TypeError, ValueError):
        # A dtype this pandas does not have (the server's is newer): the values
        # themselves are intact, as objects.
        return pd.array(decoded, dtype=object)


def _decode_frame(body: dict) -> Any:
    import pandas as pd

    columns = body["columns"]
    index = None
    if body["index"] is not None:
        spec = body["index"]
        index = pd.Index(_decode_array(spec), name=decode(spec["name"]))
        if spec.get("freq"):
            try:
                index.freq = spec["freq"]
            except (ValueError, AttributeError):
                pass  # the labels are right; only the frequency hint is lost
    if not columns:
        if index is not None:
            return _with_attrs(pd.DataFrame(index=index), body)
        if body["rows"]:
            return _with_attrs(pd.DataFrame(index=pd.RangeIndex(body["rows"])), body)
        return _with_attrs(pd.DataFrame(), body)
    # Built by position and named afterwards, so no column name (a duplicate, a
    # non-string) can change which values land where.
    frame = pd.DataFrame(
        {i: pd.Series(_decode_array(col)) for i, col in enumerate(columns)}
    )
    frame.columns = pd.Index([decode(col["name"]) for col in columns])
    if index is not None:
        frame.index = index
    return _with_attrs(frame, body)


def _with_attrs(frame: Any, body: dict) -> Any:
    frame.attrs.update(decode(body.get("attrs") or {}))
    return frame
