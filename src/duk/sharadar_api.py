"""
Sharadar API integration for ``duk -S sharadar``.

Reads Sharadar's equity-price table (SEP) directly, the way ``duk.fmp_api`` reads
FMP. It exists so a series can be checked against the warehouse from an
independent vendor, and so a symbol fafnir does not hold yet can be read before
it is loaded.

**The request follows Sharadar's documentation and is not yet measured.** The
vendor's agent guide documents ``GET https://api.sharadar.com/v1.0/data/{table}``,
the key as an ``api_key`` query parameter, and ``ticker`` / ``from`` / ``to``
filters with ``format=json``; ``fafnir.sources.sharadar`` records the evidence item
by item. The payload *shape* is undocumented, so the parser (shared with that
module) accepts a list of row objects and the datatable shape. The base URL stays
configurable (``[api].sharadar_base_url`` / ``SHARADAR_BASE_URL``), and the
warehouse-side probe (``fafnir source probe-sharadar``, plan issue SA-0201)
measures the rest; correct this module from its findings rather than from memory.

Field semantics (Sharadar's documented SEP contract):

- ``open``/``high``/``low``/``close``/``volume`` are **split-adjusted**;
- ``closeadj`` is adjusted for splits and dividends;
- ``closeunadj`` is the close as traded, the one unadjusted field.

``duk ph`` without ``--adj`` means *raw*, so the raw series is recovered with the
vendor's own identities ``OpenUnadj = Open × CloseUnadj / Close`` and
``VolumeUnadj = Volume × Close / CloseUnadj``. On the capture day the ratio is
exactly 1; it departs from 1 only for bars followed by a later split, where the
recovered values carry the vendor's rounding (ADR 0004's objection, confined to
history). With ``--adj`` every price field is scaled by ``closeadj / close`` so the
series is fully adjusted; volume stays as delivered (split-adjusted), as on FMP's
dividend-adjusted feed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from duk.date_utils import get_api_date_range
from duk.fmp_api import redact_secrets
from fafnir.sources.base import SourceError
from fafnir.sources.sharadar import (  # noqa: F401 -- re-exported
    DEFAULT_BASE_URL,
    PRICE_TABLES,
    rows_from_payload,
)

logger = logging.getLogger(__name__)

# The base URL, the price tables and the payload parser are the warehouse
# client's (fafnir.sources.sharadar), so the probe (#49) corrects them once for
# both. SEP holds equities, SFP funds (ETFs, CEFs): `duk` tries SEP first and
# falls back to SFP when SEP returns no rows, so one spelling of a ticker works
# for both.

#: Columns the price tables carry; the parser requires `date` and `close`.
PRICE_COLUMNS = (
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "closeadj",
    "closeunadj",
    "lastupdated",
)


class SharadarAPIError(Exception):
    """Raised when a Sharadar request fails or returns an unusable payload."""


def _rows_from_payload(data: Any) -> List[Dict[str, Any]]:
    """Normalise either payload shape to a list of row dicts."""
    try:
        return rows_from_payload(data)
    except SourceError as exc:
        raise SharadarAPIError(str(exc)) from None


def price_table_api(
    table: str,
    symbol: str,
    api_key: str,
    *,
    base_url: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Request one ticker's rows from a Sharadar price table (SEP or SFP).

    Returns the vendor's rows as dicts, unmodified. Raises ``SharadarAPIError``
    on transport failure, a non-2xx status or an error payload. The API key is
    redacted from every message that can leave this function.
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    if not api_key:
        raise ValueError("API key cannot be empty")
    if table not in PRICE_TABLES:
        raise ValueError(f"Unknown Sharadar price table {table!r}")

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    endpoint = f"{base}/{table}"
    params: Dict[str, str] = {
        "ticker": symbol.upper(),
        "format": "json",
        "api_key": api_key,
    }
    if from_date:
        params["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        params["to"] = to_date.strftime("%Y-%m-%d")

    logger.debug(f"Requesting {table} rows for {symbol} from Sharadar")
    try:
        response = requests.get(endpoint, params=params, timeout=60)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        detail = redact_secrets(str(e))
        logger.error(f"Failed to fetch {table} for {symbol}: {detail}")
        raise SharadarAPIError(
            f"Failed to fetch Sharadar {table} for {symbol}: {detail}. "
            "The endpoint shape is unverified; see doc/duk.md › Sources and "
            "[api].sharadar_base_url."
        ) from None

    try:
        data = response.json()
    except ValueError as e:
        raise SharadarAPIError(
            f"Sharadar {table} for {symbol}: response is not JSON ({e})"
        ) from None

    rows = _rows_from_payload(data)
    logger.info(f"Retrieved {len(rows)} {table} rows for {symbol}")
    return rows


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def frame_from_rows(rows: List[Dict[str, Any]], *, adjusted: bool) -> pd.DataFrame:
    """Turn SEP/SFP rows into duk's price frame (date index; open/high/low/close/
    volume), raw or fully adjusted per the module docstring."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.columns = [str(c).lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError("Sharadar price rows lack a 'date' or 'close' column")

    for col in ("open", "high", "low", "close", "volume", "closeadj", "closeunadj"):
        if col in df.columns:
            df[col] = df[col].map(_as_float)

    close = df["close"]
    if adjusted:
        if "closeadj" not in df.columns:
            raise ValueError("Sharadar rows lack 'closeadj'; cannot build --adj")
        factor = df["closeadj"] / close
        volume = df["volume"] if "volume" in df.columns else None
    else:
        if "closeunadj" not in df.columns:
            raise ValueError("Sharadar rows lack 'closeunadj'; cannot build raw")
        factor = df["closeunadj"] / close
        # VolumeUnadj = Volume × Close / CloseUnadj
        volume = (df["volume"] / factor) if "volume" in df.columns else None

    out = pd.DataFrame({"date": pd.to_datetime(df["date"])})
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            out[col] = df[col] * factor
    if volume is not None:
        out["volume"] = volume.round().astype("Int64")
    out = out.sort_values("date").set_index("date")
    return out


def get_price_history(
    api_key: str,
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    frequency: str = "day",
    limit: Optional[int] = None,
    fields: Optional[List[str]] = None,
    adjusted: bool = False,
    *,
    base_url: Optional[str] = None,
) -> pd.DataFrame:
    """Sharadar counterpart of ``duk.fmp_api.get_price_history``: the same
    arguments, the same frame contract, so the CLI and the compute modules do not
    care which vendor answered."""
    from duk.datasource.base import shape_price_dataframe

    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    from_date, to_date = get_api_date_range(
        start_date=start_obj, end_date=end_obj, limit=limit, frequency=frequency
    )

    rows: List[Dict[str, Any]] = []
    for table in PRICE_TABLES:
        rows = price_table_api(
            table,
            symbol,
            api_key,
            base_url=base_url,
            from_date=from_date,
            to_date=to_date,
        )
        if rows:
            break
    if not rows:
        logger.warning(f"No Sharadar rows for {symbol}")
        return pd.DataFrame()

    df = frame_from_rows(rows, adjusted=adjusted)
    return shape_price_dataframe(
        df,
        frequency=frequency,
        limit=limit,
        fields=fields,
        start_date=start_date,
        end_date=end_date,
    )
