"""
`fafnir source vendor-gaps`: live securities the vendor has stopped serving.

On 2026-10-14, ten securities that trade every day -- FPAS, XJUL, XAPR, XFEB (last
bar 10-02), CMCI, VAIC (10-05), CHIP, MYCP, VMSD (10-08) and VAI (09-29) -- had
had no bar for days. The nightly asked FMP for each, every night, with no end date,
and FMP returned nothing past those dates; issuer pages and quote sites showed all
ten trading, and a live probe found no bar in either the raw or the split-adjusted
feed. Only 21 of the 15,809 securities polled were more than five sessions behind:
it was specific symbols, not the feed.

`stale` flags these, and the playbook's question is whether the security stopped
trading or the vendor stopped serving it. This answers the second, from what the
warehouse already holds, with the evidence a vendor ticket needs. A security is
listed when all of these hold:

  * it is in the nightly universe (:func:`fafnir.db.repository.nightly_universe`):
    listed, not delisted, actively trading or with a recent bar;
  * its last stored bar D is at least ``min_sessions`` open sessions behind the
    market's latest;
  * its newest payload in ``landing.fmp_raw`` was requested after the market's
    latest session, with no end date, and returned nothing after D;
  * at least :data:`VENUE_SHARE` of the nightly universe on its venue has a bar
    after D -- so the venue, or the feed, is not simply late;
  * it is absent from the newest delisted-companies payload.

Reads only: the output is for the vendor, and for `fafnir dq` triage.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.daily_price import ENDPOINT as PRICE_ENDPOINT
from fafnir.ingest.delisted import ENDPOINT as DELISTED_ENDPOINT

#: The share of a venue's nightly universe that must have a bar after D before a
#: security's silence is its own and not the venue's.
VENUE_SHARE = 0.95

DEFAULT_MIN_SESSIONS = 2


@dataclass
class VendorGap:
    symbol: str
    security_id: int
    exchange_code: Optional[str]
    last_bar: dt.date
    sessions_behind: int
    request_from: Optional[str]
    request_to: Optional[str]
    fetched_at: Optional[dt.datetime]
    payload_max_date: Optional[dt.date]
    venue_share: float
    open_flags: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "exchange": self.exchange_code,
            "last_bar": self.last_bar.isoformat(),
            "sessions_behind": self.sessions_behind,
            "request": {"from": self.request_from, "to": self.request_to},
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "payload_max_date": (
                self.payload_max_date.isoformat() if self.payload_max_date else None
            ),
            "venue_share_after": round(self.venue_share, 4),
            "open_flags": self.open_flags,
        }


@dataclass
class VendorGapReport:
    market_latest: Optional[dt.date]
    gaps: list[VendorGap]
    delisted_payload: bool

    def as_dict(self) -> dict:
        return {
            "market_latest": (
                self.market_latest.isoformat() if self.market_latest else None
            ),
            "delisted_payload_checked": self.delisted_payload,
            "gaps": [g.as_dict() for g in self.gaps],
        }


def _universe(db: Database) -> list[dict]:
    return db.fetchall(f"""
        SELECT s.security_id, s.primary_symbol, s.exchange_code,
               (SELECT p.trade_date FROM core.daily_price p
                 WHERE p.security_id = s.security_id
                 ORDER BY p.trade_date DESC LIMIT 1) AS last_bar
          FROM core.security s
         WHERE s.source <> 'operator'
           AND {repo.nightly_universe("s.")}
        """)


def _sessions(
    db: Database, exchange_code: str, after: dt.date, until: dt.date
) -> list[dt.date]:
    """The open sessions after ``after`` up to ``until``, in order: read once, so a
    night the load did not run (every security behind) is one query, not 15,000."""
    return [
        r["trade_date"]
        for r in db.fetchall(
            """
            SELECT trade_date FROM ref.trading_calendar
             WHERE exchange_code = %s AND is_open AND trade_date > %s
               AND trade_date <= %s
             ORDER BY trade_date
            """,
            (exchange_code, after, until),
        )
    ]


def _latest_payloads(db: Database, symbols: list[str]) -> dict[str, dict]:
    rows = db.fetchall(
        """
        SELECT DISTINCT ON (r.symbol) r.symbol, r.params, r.fetched_at,
               CASE WHEN jsonb_typeof(r.payload) = 'array' THEN
                    (SELECT max(left(e->>'date', 10))
                       FROM jsonb_array_elements(r.payload) e
                      WHERE e->>'date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}')
               END AS max_date
          FROM landing.fmp_raw r
         WHERE r.endpoint = %s AND r.symbol = ANY(%s)
         ORDER BY r.symbol, r.fetched_at DESC, r.raw_id DESC
        """,
        (PRICE_ENDPOINT, symbols),
    )
    return {r["symbol"]: r for r in rows}


def _delisted_symbols(db: Database) -> Optional[set[str]]:
    """The symbols on the newest delisted-companies payload, or None when none has
    been landed (the sweep landed nothing before v1.5.0)."""
    row = db.fetchone(
        """
        SELECT payload FROM landing.fmp_raw
         WHERE endpoint = %s AND jsonb_typeof(payload) = 'array'
         ORDER BY fetched_at DESC, raw_id DESC
         LIMIT 1
        """,
        (DELISTED_ENDPOINT,),
    )
    if row is None:
        return None
    return {
        str(e.get("symbol") or "").strip().upper()
        for e in row["payload"]
        if isinstance(e, dict)
    }


def _open_flags(db: Database, security_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for r in db.fetchall(
        r"""
        SELECT security_id, check_name, count(*) AS n
          FROM ops.data_quality_flag
         WHERE security_id = ANY(%s) AND resolved_at IS NULL
           AND (check_name = 'stale' OR check_name LIKE 'price\_%%')
         GROUP BY 1, 2
         ORDER BY 1, 2
        """,
        (security_ids,),
    ):
        out.setdefault(int(r["security_id"]), {})[r["check_name"]] = int(r["n"])
    return out


def vendor_gaps(
    db: Database,
    *,
    exchange_code: str = "NASDAQ",
    min_sessions: int = DEFAULT_MIN_SESSIONS,
) -> VendorGapReport:
    """The live securities the vendor has stopped serving. Reads only."""
    from fafnir.dq.dorq import market_latest

    latest = market_latest(db, exchange_code)
    if latest is None:
        return VendorGapReport(market_latest=None, gaps=[], delisted_payload=False)
    universe = [u for u in _universe(db) if u["last_bar"] is not None]
    behind = [u for u in universe if u["last_bar"] < latest]
    sessions = (
        _sessions(db, exchange_code, min(u["last_bar"] for u in behind), latest)
        if behind
        else []
    )
    candidates = []
    for u in behind:
        n = len(sessions) - bisect.bisect_right(sessions, u["last_bar"])
        if n >= min_sessions:
            candidates.append((u, n))
    delisted = _delisted_symbols(db)
    if not candidates:
        return VendorGapReport(latest, [], delisted is not None)
    payloads = _latest_payloads(db, [u["primary_symbol"] for u, _ in candidates])
    by_venue: dict[Optional[str], list[dt.date]] = {}
    for u in universe:
        by_venue.setdefault(u["exchange_code"], []).append(u["last_bar"])

    gaps: list[VendorGap] = []
    for u, n in candidates:
        symbol, d = u["primary_symbol"], u["last_bar"]
        payload = payloads.get(symbol)
        if payload is None:
            continue
        params = payload["params"] or {}
        if params.get("to"):
            continue  # an explicit window: not the nightly's open-ended request
        if payload["fetched_at"].date() <= latest:
            continue  # asked before the market's latest session could be served
        max_date = (
            dt.date.fromisoformat(payload["max_date"]) if payload["max_date"] else None
        )
        if max_date is not None and max_date > d:
            continue  # the vendor served something newer: not a gap of its own
        peers = by_venue.get(u["exchange_code"], [])
        share = sum(1 for last in peers if last > d) / len(peers) if peers else 0.0
        if share < VENUE_SHARE:
            continue
        if delisted is not None and symbol.upper() in delisted:
            continue
        gaps.append(
            VendorGap(
                symbol=symbol,
                security_id=int(u["security_id"]),
                exchange_code=u["exchange_code"],
                last_bar=d,
                sessions_behind=n,
                request_from=params.get("from"),
                request_to=params.get("to"),
                fetched_at=payload["fetched_at"],
                payload_max_date=max_date,
                venue_share=share,
            )
        )
    flags = _open_flags(db, [g.security_id for g in gaps])
    for g in gaps:
        g.open_flags = flags.get(g.security_id, {})
    gaps.sort(key=lambda g: (g.last_bar, g.symbol))
    return VendorGapReport(latest, gaps, delisted is not None)
