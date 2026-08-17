"""
Corporate-actions loader: splits and cash dividends from FMP.

Handles the field-name variation across FMP responses (numerator/denominator vs
splitFrom/splitTo; dividend vs adjDividend). Validates and upserts idempotently.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest.runlog import RunLog
from fafnir.logging_config import get_logger
from fafnir.sources.fmp import FMPClient, payload_hash

logger = get_logger("ingest.actions")


def _parse_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _split_ratio(rec: dict) -> tuple[Optional[float], Optional[float]]:
    num = rec.get("numerator", rec.get("splitTo"))
    den = rec.get("denominator", rec.get("splitFrom"))
    try:
        num = float(num) if num not in (None, "") else None
        den = float(den) if den not in (None, "") else None
    except (TypeError, ValueError):
        return None, None
    if not num or not den or num <= 0 or den <= 0:
        return None, None
    return num, den


def load_actions(db: Database, fmp: FMPClient, symbols: Iterable[str]) -> int:
    """Load splits + dividends for the given symbols. Returns actions upserted."""
    symbols = list(symbols)
    with RunLog(
        db, source="fmp", endpoint="corporate-actions", params={"symbols": len(symbols)}
    ) as run:
        total = 0
        for symbol in symbols:
            sec_id = repo.resolve_security_id(db, symbol)
            if sec_id is None:
                logger.warning("Unknown symbol %s; skipping actions", symbol)
                continue

            # Splits
            splits = fmp.splits(symbol)
            repo.land_payload(
                db,
                endpoint="splits",
                params={"symbol": symbol},
                symbol=symbol,
                http_status=200,
                payload=splits,
                payload_hash=payload_hash(splits),
                nbytes=0,
                ingestion_run_id=run.run_id,
            )
            for rec in splits:
                ex_date = _parse_date(rec.get("date"))
                num, den = _split_ratio(rec)
                if ex_date is None or num is None:
                    run.rows_quarantined += 1
                    repo.add_dq_flag(
                        db,
                        check_name="split_invalid",
                        security_id=sec_id,
                        table_name="core.corporate_action",
                        record_key={"symbol": symbol, "date": str(rec.get("date"))},
                        ingestion_run_id=run.run_id,
                    )
                    continue
                repo.upsert_corporate_action(
                    db,
                    security_id=sec_id,
                    action_type="split",
                    ex_date=ex_date,
                    split_numerator=num,
                    split_denominator=den,
                    ingestion_run_id=run.run_id,
                )
                total += 1

            # Dividends
            divs = fmp.dividends(symbol)
            repo.land_payload(
                db,
                endpoint="dividends",
                params={"symbol": symbol},
                symbol=symbol,
                http_status=200,
                payload=divs,
                payload_hash=payload_hash(divs),
                nbytes=0,
                ingestion_run_id=run.run_id,
            )
            for rec in divs:
                ex_date = _parse_date(rec.get("date"))
                # `dividend` is the as-declared cash amount; `adjDividend` is
                # restated into today's share terms. core.daily_price holds
                # unadjusted prices, so the as-declared amount is the one that
                # divides into the raw prior close -- adjDividend is only a
                # fallback for rows where FMP omits `dividend` entirely.
                amount = rec.get("dividend")
                if amount in (None, ""):
                    amount = rec.get("adjDividend")
                try:
                    amount = float(amount) if amount not in (None, "") else None
                except (TypeError, ValueError):
                    amount = None
                if ex_date is None or amount is None or amount < 0:
                    run.rows_quarantined += 1
                    repo.add_dq_flag(
                        db,
                        check_name="dividend_invalid",
                        security_id=sec_id,
                        table_name="core.corporate_action",
                        record_key={"symbol": symbol, "date": str(rec.get("date"))},
                        ingestion_run_id=run.run_id,
                    )
                    continue
                repo.upsert_corporate_action(
                    db,
                    security_id=sec_id,
                    action_type="dividend",
                    ex_date=ex_date,
                    dividend_amount=amount,
                    record_date=_parse_date(rec.get("recordDate")),
                    payment_date=_parse_date(rec.get("paymentDate")),
                    declaration_date=_parse_date(rec.get("declarationDate")),
                    ingestion_run_id=run.run_id,
                )
                total += 1

            run.rows_inserted = total
            # Same unit boundary as the price loader: one symbol's splits and
            # dividends land together or not at all, and an interruption keeps
            # every symbol already processed.
            db.commit()
        run.symbols_requested = len(symbols)
        run.bytes_downloaded = fmp.bytes_downloaded
        logger.info(
            "Loaded %d corporate actions across %d symbols", total, len(symbols)
        )
        return total
