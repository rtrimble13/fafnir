"""
Sharadar for ``duk -S sharadar``: the warehouse's client, shaped into duk's frame.

Reads Sharadar's equity-price table (SEP) and fund table (SFP) so a series can be
checked against the warehouse from an independent vendor, and so a symbol fafnir
does not hold yet can be read before it is loaded.

**No HTTP is made here** (ADR 0017). Every request goes through
:class:`fafnir.sources.sharadar.SharadarClient`, the same object the loaders and the
probe use: its throttle, its retries, its byte metering, the key carried in the
``x-api-key`` header rather than the URL, and its redaction of the key from every
message that can leave. What this module owns is what duk does *with* the rows:
which tables are tried, in what order, and how the vendor's split-adjusted fields
become the raw or the adjusted series ``duk ph`` prints.

Field semantics (Sharadar's documented SEP contract):

- ``open``/``high``/``low``/``close``/``volume`` are **split-adjusted**;
- ``closeadj`` is adjusted for splits and dividends;
- ``closeunadj`` is the close as traded, the one unadjusted field.

``duk ph`` without ``--adj`` means *raw*, so the raw series is recovered with the
vendor's own identities (``OpenUnadj = Open x CloseUnadj / Close``, ``VolumeUnadj =
Volume x Close / CloseUnadj``): exact on bars with no later split, the vendor's
rounding on bars with one. With ``--adj`` every price is scaled by
``closeadj / close``. SEP (stocks) is read first and SFP (ETFs, closed-end funds)
when SEP has no rows, so one spelling of a ticker works for both.

Market cap (``duk -S sharadar mc``, ``ph --mcap``) is DAILY's ``marketcap``, in
millions of dollars to one decimal (a resolution of $100,000), converted to dollars.
It is Sharadar's own figure, the close as traded times SF1's ``sharesbas`` times its
``sharefactor``, read for comparison with the warehouse's: fafnir never stores it
(ADR 0019). DAILY covers SEP's equities only, so an ETF has none.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from duk.date_utils import get_api_date_range
from fafnir.sources.base import SourceError
from fafnir.sources.sharadar import (  # noqa: F401 -- re-exported
    DEFAULT_BASE_URL,
    PRICE_TABLES,
    SharadarClient,
    rows_from_payload,
)

logger = logging.getLogger(__name__)

#: Columns the price tables carry; the frame builder requires `date` and `close`.
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

#: Sharadar's daily metrics table, and the columns duk reads of it.
DAILY_TABLE = SharadarClient.T_DAILY
DAILY_FIELDS = ("ticker", "date", "marketcap")
#: DAILY's ``marketcap`` is in millions of dollars.
MARKET_CAP_UNIT = 1_000_000

#: One ``duk`` invocation makes a handful of requests, so it is never the thing
#: that trips the vendor's limit; the client's throttle exists for the loaders.
RATE_PER_MIN = 600


class SharadarAPIError(SourceError):
    """A Sharadar request failed or returned an unusable payload.

    A :class:`~fafnir.sources.base.SourceError`, so a caller that handles the
    warehouse client's errors handles duk's too; kept under its old name for the
    callers and tests that import it.
    """


def make_client(api_key: str, *, base_url: Optional[str] = None) -> SharadarClient:
    """The warehouse's Sharadar client, configured for a shell session."""
    if not api_key:
        raise ValueError("API key cannot be empty")
    return SharadarClient(api_key, base_url=base_url, rate_per_min=RATE_PER_MIN)


def price_table_api(
    table: str,
    symbol: str,
    api_key: str = "",
    *,
    base_url: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    client: Optional[SharadarClient] = None,
) -> List[Dict[str, Any]]:
    """Request one ticker's rows from a Sharadar price table (SEP or SFP).

    Returns the vendor's rows as dicts, unmodified, every page of them. Pass a
    ``client`` to reuse one across tables (its byte count then covers the whole
    read); otherwise one is made from ``api_key`` and ``base_url``. Raises
    :class:`SharadarAPIError` on transport failure, a non-2xx status or an error
    payload, with the key redacted.
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    if table not in PRICE_TABLES:
        raise ValueError(f"Unknown Sharadar price table {table!r}")
    client = client or make_client(api_key, base_url=base_url)

    filters: Dict[str, str] = {"ticker": symbol.upper()}
    if from_date:
        filters["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        filters["to"] = to_date.strftime("%Y-%m-%d")

    logger.debug(f"Requesting {table} rows for {symbol} from Sharadar")
    try:
        rows = client.query(table, filters)
    except SourceError as exc:
        # The client's message is already redacted; chain nothing, since the
        # original may hold a URL (ADR 0017, the base client's own rule).
        raise SharadarAPIError(
            f"Failed to fetch Sharadar {table} for {symbol}: {exc}. "
            "See doc/duk.md › Sources and [api].sharadar_base_url."
        ) from None
    logger.info(f"Retrieved {len(rows)} {table} rows for {symbol}")
    return rows


def daily_market_cap_api(
    symbol: str,
    api_key: str = "",
    *,
    base_url: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    client: Optional[SharadarClient] = None,
) -> List[Dict[str, Any]]:
    """One ticker's DAILY rows (``ticker``, ``date``, ``marketcap``), every page.

    The filters are checked in the rows that come back, because Sharadar ignores a
    parameter it does not know rather than refusing it (the client's item 5): a
    row for another ticker, or outside the window, is an error, not data.
    """
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    client = client or make_client(api_key, base_url=base_url)
    ticker = symbol.upper()
    filters: Dict[str, str] = {"ticker": ticker}
    if from_date:
        filters["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        filters["to"] = to_date.strftime("%Y-%m-%d")
    logger.debug(f"Requesting DAILY rows for {symbol} from Sharadar")
    try:
        rows = client.query(DAILY_TABLE, filters, fields=DAILY_FIELDS)
    except SourceError as exc:
        raise SharadarAPIError(
            f"Failed to fetch Sharadar DAILY for {symbol}: {exc}. "
            "See doc/duk.md › Sources and [api].sharadar_base_url."
        ) from None
    for row in rows:
        day = str(row.get("date") or "")[:10]
        if str(row.get("ticker") or "").upper() != ticker:
            raise SharadarAPIError(
                f"Sharadar DAILY answered {symbol} with a row for "
                f"{row.get('ticker')!r}: the ticker filter was not applied"
            )
        if (from_date and day < filters["from"]) or (to_date and day > filters["to"]):
            raise SharadarAPIError(
                f"Sharadar DAILY answered {filters} with a row dated {day}: the "
                "window was not applied"
            )
    logger.info(f"Retrieved {len(rows)} DAILY rows for {symbol}")
    return rows


def market_cap_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """DAILY rows as a date-indexed frame with one column, ``market_cap``, in
    dollars, oldest first."""
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "date": pd.to_datetime([str(r.get("date"))[:10] for r in rows]),
            "market_cap": [_as_float(r.get("marketcap")) for r in rows],
        }
    )
    out["market_cap"] = out["market_cap"] * MARKET_CAP_UNIT
    return out.sort_values("date").set_index("date")


def get_market_cap_history(
    api_key: str,
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    frequency: str = "day",
    limit: Optional[int] = None,
    *,
    base_url: Optional[str] = None,
    client: Optional[SharadarClient] = None,
) -> pd.DataFrame:
    """Sharadar's daily market cap for ``duk -S sharadar mc``: a ``market_cap``
    frame shaped as `ph` shapes a close (each period's last; ``limit`` by `ph`'s
    rule). Empty where DAILY has nothing: an ETF, an unknown ticker."""
    from duk.datasource.base import shape_price_dataframe

    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    from_date, to_date = get_api_date_range(
        start_date=start_obj, end_date=end_obj, limit=limit, frequency=frequency
    )
    rows = daily_market_cap_api(
        symbol,
        api_key,
        base_url=base_url,
        from_date=from_date,
        to_date=to_date,
        client=client,
    )
    df = market_cap_frame(rows).dropna(subset=["market_cap"]) if rows else None
    if df is None or df.empty:
        logger.warning(f"No Sharadar DAILY market cap for {symbol}")
        return pd.DataFrame()
    return shape_price_dataframe(
        df,
        frequency=frequency,
        limit=limit,
        start_date=start_date,
        end_date=end_date,
    )


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
    client: Optional[SharadarClient] = None,
    market_cap: bool = False,
) -> pd.DataFrame:
    """Sharadar counterpart of ``duk.fmp_api.get_price_history``: the same
    arguments, the same frame contract, so the CLI and the compute modules do not
    care which vendor answered. With ``market_cap`` the frame gains DAILY's
    ``market_cap`` (`ph --mcap`), None on a bar DAILY has no row for."""
    from duk.datasource.base import shape_price_dataframe

    start_obj = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end_obj = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    from_date, to_date = get_api_date_range(
        start_date=start_obj, end_date=end_obj, limit=limit, frequency=frequency
    )

    client = client or make_client(api_key, base_url=base_url)
    rows: List[Dict[str, Any]] = []
    for table in PRICE_TABLES:
        rows = price_table_api(
            table, symbol, client=client, from_date=from_date, to_date=to_date
        )
        if rows:
            break
    if not rows:
        logger.warning(f"No Sharadar rows for {symbol}")
        return pd.DataFrame()

    df = frame_from_rows(rows, adjusted=adjusted)
    if market_cap:
        caps = market_cap_frame(
            daily_market_cap_api(
                symbol, client=client, from_date=from_date, to_date=to_date
            )
        )
        df["market_cap"] = (
            caps["market_cap"].reindex(df.index) if not caps.empty else float("nan")
        )
        if fields and "market_cap" not in fields:
            fields = [*fields, "market_cap"]
    return shape_price_dataframe(
        df,
        frequency=frequency,
        limit=limit,
        fields=fields,
        start_date=start_date,
        end_date=end_date,
    )
