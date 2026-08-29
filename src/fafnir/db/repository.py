"""
Parameterized data-access functions over the fafnir schema.

Writes are key-based upserts (``ON CONFLICT ... DO UPDATE``) so every load is
idempotent. Reads return plain dicts/lists; the duk ``db`` datasource shapes
them into the DataFrame contracts the CLI expects.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple, Optional, Sequence

import psycopg

from fafnir.db.connection import Database

# ---------------------------------------------------------------------------
# Reference dimensions
# ---------------------------------------------------------------------------


def ensure_exchange(
    db: Database, code: str, name: str | None = None, country: str | None = None
) -> None:
    if not code:
        return
    db.execute(
        """
        INSERT INTO ref.exchange (exchange_code, exchange_name, country)
        VALUES (%s, %s, %s)
        ON CONFLICT (exchange_code) DO NOTHING
        """,
        (code, name, country),
    )


def get_or_create_sector(db: Database, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    db.execute(
        "INSERT INTO ref.sector (sector_name) VALUES (%s) ON CONFLICT (sector_name) DO NOTHING",
        (name,),
    )
    return db.fetchval(
        "SELECT sector_id FROM ref.sector WHERE sector_name = %s", (name,)
    )


def get_or_create_industry(db: Database, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    db.execute(
        "INSERT INTO ref.industry (industry_name) VALUES (%s) "
        "ON CONFLICT (industry_name) DO NOTHING",
        (name,),
    )
    return db.fetchval(
        "SELECT industry_id FROM ref.industry WHERE industry_name = %s", (name,)
    )


# ---------------------------------------------------------------------------
# Security master
# ---------------------------------------------------------------------------


def upsert_security(
    db: Database,
    *,
    primary_symbol: str,
    company_name: Optional[str],
    asset_type: str = "equity",
    exchange_code: Optional[str] = None,
    sector_id: Optional[int] = None,
    industry_id: Optional[int] = None,
    currency: str = "USD",
    country: Optional[str] = None,
    is_actively_trading: bool = True,
    is_etf: bool = False,
    is_fund: bool = False,
    market_cap_usd: Optional[float] = None,
    beta: Optional[float] = None,
    ipo_date: Optional[date] = None,
    delisted_date: Optional[date] = None,
    cik: Optional[str] = None,
    isin: Optional[str] = None,
    cusip: Optional[str] = None,
    source: str = "fmp",
) -> int:
    """Insert/update a security by its (source, primary_symbol) soft key.

    The conflict arbiter is 0009's *partial* index, which covers only rows with
    ``delisted_date IS NULL``. A delisted security is therefore invisible here: a
    reused ticker inserts a new row and mints a new security_id rather than
    overwriting the dead issuer's identity and price history.

    The exchange is deliberately NOT in the key (0012). It is an attribute of the
    listing, not of the company: keying on it meant a venue transfer (NYSE ->
    NASDAQ) failed to match and inserted a second listed row for one ticker, which
    then captured the ticker's xref period and left the company's entire price
    history unreachable by symbol. A transfer now updates the security that holds
    the history, the same way a rename does.

    Returns the security_id.
    """
    row = db.fetchone(
        """
        INSERT INTO core.security
            (primary_symbol, company_name, asset_type, exchange_code, sector_id,
             industry_id, currency, country, is_actively_trading, is_etf, is_fund,
             market_cap_usd, beta, ipo_date, delisted_date, cik, isin, cusip,
             source, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (source, primary_symbol)
            WHERE delisted_date IS NULL
        DO UPDATE SET
            company_name        = EXCLUDED.company_name,
            asset_type          = EXCLUDED.asset_type,
            -- COALESCE so a caller without the field (enrich_profiles on a profile
            -- that omits it) cannot blank the venue; otherwise this is what records
            -- a transfer onto the existing security.
            exchange_code       = COALESCE(EXCLUDED.exchange_code,
                                           core.security.exchange_code),
            sector_id           = EXCLUDED.sector_id,
            industry_id         = EXCLUDED.industry_id,
            currency            = EXCLUDED.currency,
            country             = EXCLUDED.country,
            is_actively_trading = EXCLUDED.is_actively_trading,
            is_etf              = EXCLUDED.is_etf,
            is_fund             = EXCLUDED.is_fund,
            -- COALESCE: a caller without screener fields (enrich_profiles, the
            -- bulk-list universes) must not blank out what the screener set.
            market_cap_usd      = COALESCE(EXCLUDED.market_cap_usd,
                                           core.security.market_cap_usd),
            beta                = COALESCE(EXCLUDED.beta, core.security.beta),
            ipo_date            = COALESCE(EXCLUDED.ipo_date, core.security.ipo_date),
            delisted_date       = EXCLUDED.delisted_date,
            cik                 = COALESCE(EXCLUDED.cik, core.security.cik),
            isin                = COALESCE(EXCLUDED.isin, core.security.isin),
            cusip               = COALESCE(EXCLUDED.cusip, core.security.cusip),
            updated_at          = now()
        RETURNING security_id
        """,
        (
            primary_symbol,
            company_name,
            asset_type,
            exchange_code,
            sector_id,
            industry_id,
            currency,
            country,
            is_actively_trading,
            is_etf,
            is_fund,
            market_cap_usd,
            beta,
            ipo_date,
            delisted_date,
            cik,
            isin,
            cusip,
            source,
        ),
    )
    return int(row["security_id"])


def upsert_symbol_xref(
    db: Database,
    *,
    security_id: int,
    symbol: str,
    valid_from: date | str | None = None,
    is_primary: bool = True,
    source: str = "fmp",
) -> None:
    """Map a ticker to a security_id for a validity period.

    ``valid_from=None`` means "the period this ticker is serving now, or the next
    one if it is free". Resolved in that order:

      1. The ticker's own open period, if it has one -- this call is a re-assertion
         of the current mapping, so it updates that row. Without this branch the
         nightly `ingest securities` re-asserts every ticker at the 1900 fallback
         and opens a SECOND open period for any ticker whose open period does not
         start there -- which is every ticker `retarget_symbol` has just renamed,
         since that opens the new ticker's period at the change date. The rename
         boundary 0011 exists to record is then erased by the next security-master
         run, and the ticker resolves to a period claiming validity since 1900 for
         dates on which it did not yet exist.
      2. The day after the last period it served, if every one is closed. A reused
         ticker therefore opens a *new* period instead of hijacking the dead
         issuer's row -- which, since XREF_RESOLVE_SQL only reads open periods, is
         what keeps a delisted company's price history addressable and stops the
         new issuer from inheriting it.
      3. '1900-01-01' for a ticker seen for the first time.

    An explicit ``valid_from`` skips all of it and addresses that period directly.
    """
    db.execute(
        """
        INSERT INTO core.symbol_xref (security_id, symbol, valid_from, is_primary, source)
        VALUES (
            %s, %s,
            COALESCE(
                %s::date,
                (SELECT max(valid_from) FROM core.symbol_xref
                  WHERE symbol = %s AND valid_to IS NULL),
                (SELECT max(valid_to) + 1 FROM core.symbol_xref
                  WHERE symbol = %s AND valid_to IS NOT NULL),
                '1900-01-01'::date
            ),
            %s, %s
        )
        ON CONFLICT (symbol, valid_from) DO UPDATE SET
            security_id = EXCLUDED.security_id,
            is_primary  = EXCLUDED.is_primary
        WHERE core.symbol_xref.valid_to IS NULL
        """,
        (security_id, symbol, valid_from, symbol, symbol, is_primary, source),
    )


def mark_delisted(db: Database, *, security_id: int, delisted_date: date) -> bool:
    """Flip a listed security to delisted and close its open ticker period.

    One-way and idempotent: a row that already carries a ``delisted_date`` is
    left untouched, so re-running the loader can never rewrite a delisting or
    resurrect a dead issuer. Returns True only when this call did the delisting.
    """
    row = db.fetchone(
        """
        UPDATE core.security
           SET is_actively_trading = FALSE,
               delisted_date       = %s,
               updated_at          = now()
         WHERE security_id = %s AND delisted_date IS NULL
        RETURNING security_id
        """,
        (delisted_date, security_id),
    )
    if row is None:
        return False
    # GREATEST, not `AND valid_from <= delisted_date`. That guard skipped any period
    # starting after the delisting instead of closing it, which was unreachable while
    # every period began at '1900-01-01' -- but a renamed security's current period
    # begins on its rename date, so a delisting backdated before that rename would
    # leave the ticker OPEN on a dead issuer. The next company to list under it then
    # loses the resolution race: XREF_RESOLVE_SQL orders open periods by valid_from
    # DESC, so the dead issuer's later-starting period wins and the newcomer's bars
    # are attributed to a company that no longer exists. Clamping closes every open
    # period while still satisfying CHECK (valid_to >= valid_from).
    db.execute(
        """
        UPDATE core.symbol_xref
           SET valid_to = GREATEST(valid_from, %s::date)
         WHERE security_id = %s AND valid_to IS NULL
        """,
        (delisted_date, security_id),
    )
    return True


def upsert_company_profile(
    db: Database,
    *,
    security_id: int,
    description: Optional[str],
    source: str = "fmp",
) -> None:
    """Store the long-form description.

    Everything else 0003 kept here either moved to ``core.security`` (0010:
    market_cap_usd, beta -- the screener supplies both for free) or was dropped.
    Description is the one attribute that genuinely needs the per-symbol profile
    request, which is why ``--enrich`` is now optional.
    """
    db.execute(
        """
        INSERT INTO core.company_profile (security_id, description, loaded_at, source)
        VALUES (%s, %s, now(), %s)
        ON CONFLICT (security_id) DO UPDATE SET
            description = EXCLUDED.description,
            loaded_at = now()
        """,
        (security_id, description, source),
    )


# Canonical security-id resolution. The duk db datasource
# (duk/datasource/db.py::_resolve_security_id) MUST use these identical queries so
# the loader and the read path always resolve the same ticker to the same id.
XREF_RESOLVE_SQL = (
    "SELECT security_id FROM core.symbol_xref "
    "WHERE symbol = %s AND valid_to IS NULL "
    "ORDER BY is_primary DESC, valid_from DESC LIMIT 1"
)
# Deterministic fallback: prefer the given source, then lowest id. Ordering (not a
# hard source filter) so a symbol that only exists under another source still
# resolves — and resolves identically in both code paths.
PRIMARY_RESOLVE_SQL = (
    "SELECT security_id FROM core.security WHERE primary_symbol = %s "
    "ORDER BY (source = %s) DESC, (delisted_date IS NULL) DESC, security_id ASC "
    "LIMIT 1"
)
# Last resort: a ticker whose period is CLOSED and which is nobody's current
# primary_symbol -- i.e. a name a company traded under before it was renamed. Once
# FB becomes META, "FB" is no longer open in the xref and no longer any security's
# primary_symbol, so without this a rename would make the old ticker address
# nothing at all and `duk ph FB` would answer "unknown symbol" about a company
# whose history is right there. Reached only after the two queries above fail, so
# a live owner of the ticker (reuse) and a delisted issuer still win over it.
HISTORICAL_XREF_RESOLVE_SQL = (
    "SELECT security_id FROM core.symbol_xref "
    "WHERE symbol = %s AND valid_to IS NOT NULL "
    "ORDER BY valid_to DESC, valid_from DESC LIMIT 1"
)


def resolve_security_id(
    db: Database, symbol: str, source: str = "fmp"
) -> Optional[int]:
    """Resolve a ticker to a security_id: current xref, then primary_symbol, then a
    ticker the security used to trade under."""
    val = db.fetchval(XREF_RESOLVE_SQL, (symbol,))
    if val is not None:
        return int(val)
    val = db.fetchval(PRIMARY_RESOLVE_SQL, (symbol, source))
    if val is not None:
        return int(val)
    val = db.fetchval(HISTORICAL_XREF_RESOLVE_SQL, (symbol,))
    return int(val) if val is not None else None


# ---------------------------------------------------------------------------
# Symbol changes (ticker renames)
# ---------------------------------------------------------------------------

# Outcomes of apply_symbol_change. These are the values stored in
# core.symbol_change.status (0011), plus UNKNOWN, which is deliberately *not*
# stored: the rename feed is global across every venue, so most of its rows are
# for tickers this warehouse has never tracked and recording them would grow an
# audit table of other people's renames.
CHANGE_APPLIED = "applied"
CHANGE_CONFLICT = "conflict"
CHANGE_IGNORED = "ignored"
CHANGE_UNKNOWN = "unknown"

# Statuses that need no further attempt. A conflict is retried on every sweep:
# it usually means the rename feed lagged the screener, and the collision clears
# itself once the duplicate is resolved.
TERMINAL_CHANGE_STATUSES = (CHANGE_APPLIED, CHANGE_IGNORED)


class SymbolChangeOutcome(NamedTuple):
    """What :func:`apply_symbol_change` did.

    ``folded_security_id`` is set when a duplicate security minted under the new
    ticker (by a security-master load that ran before the rename was known) was
    absorbed into the surviving one.
    """

    status: str
    security_id: Optional[int]
    folded_security_id: Optional[int] = None


def active_security_for_symbol(
    db: Database, symbol: str, source: str = "fmp"
) -> Optional[int]:
    """Resolve a ticker to a *currently listed* security_id, or None.

    Deliberately narrower than :func:`resolve_security_id`, which falls back to
    delisted rows so dead names stay addressable for research. A rename must never
    be applied to a delisted issuer: a ticker that reappears under a dead company's
    symbol is ticker *reuse*, and resurrecting that row is precisely what 0009
    exists to prevent.
    """
    val = db.fetchval(
        """
        SELECT x.security_id
          FROM core.symbol_xref x
          JOIN core.security s ON s.security_id = x.security_id
         WHERE x.symbol = %s AND x.valid_to IS NULL AND s.delisted_date IS NULL
         ORDER BY x.is_primary DESC, x.valid_from DESC
         LIMIT 1
        """,
        (symbol,),
    )
    if val is not None:
        return int(val)
    val = db.fetchval(
        """
        SELECT security_id FROM core.security
         WHERE primary_symbol = %s AND delisted_date IS NULL
         ORDER BY (source = %s) DESC, security_id ASC
         LIMIT 1
        """,
        (symbol, source),
    )
    return int(val) if val is not None else None


def security_has_history(db: Database, security_id: int) -> bool:
    """True if anything irreplaceable hangs off this security_id.

    "History" is the data a delete would destroy: bars, corporate actions, and the
    factors derived from them. Attributes (name, sector, market cap) do not count
    -- the next security-master load rewrites them from the source anyway.
    """
    return bool(
        db.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM core.daily_price      WHERE security_id = %s)
                OR EXISTS (SELECT 1 FROM core.corporate_action WHERE security_id = %s)
                OR EXISTS (SELECT 1 FROM core.adjustment_factor WHERE security_id = %s)
            """,
            (security_id, security_id, security_id),
        )
    )


def fold_empty_security(db: Database, *, victim_id: int, survivor_id: int) -> bool:
    """Absorb a history-free duplicate security into the one that owns the history.

    The only thing this is for: the security-master load saw the new ticker before
    the rename feed reported it, so it minted a bare row -- no bars, no actions, no
    factors, nothing but attributes the next load rewrites. Two rows for one
    company is a data error, and the duplicate is the one with nothing in it.

    This is the *sole* place fafnir deletes a security, and it refuses unless
    :func:`security_has_history` says the row is empty. Retaining rows is about not
    losing history (survivorship bias); a stub has none to lose. Returns False --
    changing nothing -- when the guard rejects the fold.
    """
    if victim_id == survivor_id or security_has_history(db, victim_id):
        return False
    # Soft references first (no FK, so nothing enforces this order but us).
    #
    # Drop the victim's open flags that the survivor already carries before
    # repointing the rest. Folding is the one moment two securities' flags become
    # one security's, so it is the one place a repoint can land a second open flag
    # on a condition that already has one -- which is both the inflation 0014 set
    # out to stop and, since 0016, a unique-index violation that would abort the
    # rename sweep. A redundant flag is not worth failing a load over, and the
    # survivor's row already says the same thing.
    #
    # price_* is excluded, as everywhere: a stub can carry those (a quarantined bar
    # writes a flag but no price row, so security_has_history still calls it empty)
    # and count_price_quarantines counts them to bound the watermark hold.
    db.execute(
        """
        DELETE FROM ops.data_quality_flag v
         WHERE v.security_id = %s
           AND v.resolved_at IS NULL
           AND v.check_name NOT LIKE 'price\\_%%'
           AND EXISTS (
                 SELECT 1 FROM ops.data_quality_flag s
                  WHERE s.security_id = %s
                    AND s.resolved_at IS NULL
                    AND s.check_name = v.check_name
                    AND s.record_key IS NOT DISTINCT FROM v.record_key
           )
        """,
        (victim_id, survivor_id),
    )
    db.execute(
        "UPDATE ops.data_quality_flag SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )
    db.execute("DELETE FROM ops.load_watermark WHERE security_id = %s", (victim_id,))
    # Then the real FKs.
    db.execute(
        "UPDATE core.symbol_change SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )
    db.execute("DELETE FROM core.symbol_xref WHERE security_id = %s", (victim_id,))
    db.execute("DELETE FROM core.company_profile WHERE security_id = %s", (victim_id,))
    db.execute("DELETE FROM core.security WHERE security_id = %s", (victim_id,))
    return True


def retarget_symbol(
    db: Database,
    *,
    security_id: int,
    old_symbol: str,
    new_symbol: str,
    change_date: date,
    company_name: Optional[str] = None,
    source: str = "fmp",
) -> None:
    """Move a listed security from one ticker to another, keeping its identity.

    Idempotent: re-running closes an already-closed period to the same date and
    re-opens the same xref row. The old ticker's period ends the day *before* the
    change so the two periods are contiguous and never both open -- point-in-time
    resolution (XREF_RESOLVE_SQL) reads only open periods, so an overlap would make
    the ticker ambiguous on the changeover day.
    """
    db.execute(
        """
        UPDATE core.symbol_xref
           SET valid_to = GREATEST(valid_from, %s::date - 1)
         WHERE security_id = %s AND symbol = %s AND valid_to IS NULL
        """,
        (change_date, security_id, old_symbol),
    )
    upsert_symbol_xref(
        db,
        security_id=security_id,
        symbol=new_symbol,
        valid_from=change_date,
        source=source,
    )
    db.execute(
        """
        UPDATE core.security
           SET primary_symbol = %s,
               company_name   = COALESCE(%s, company_name),
               updated_at     = now()
         WHERE security_id = %s AND delisted_date IS NULL
        """,
        (new_symbol, company_name, security_id),
    )


def apply_symbol_change(
    db: Database,
    *,
    old_symbol: str,
    new_symbol: str,
    change_date: date,
    company_name: Optional[str] = None,
    source: str = "fmp",
) -> SymbolChangeOutcome:
    """Carry one ticker rename onto the security that already exists.

    Without this, a rename reaches the warehouse as a *new listing*: the screener
    reports the new ticker, no active row matches it, and the upsert mints a second
    security_id -- stranding the company's bars, actions and price watermark on the
    old row, which no delisting sweep will ever close because a rename is not a
    delisting. Applying the rename to the existing security_id is what keeps one
    company one entity across the change.

    Outcomes:
      * ``applied``  -- the rename is now reflected in core.security and the xref.
      * ``conflict`` -- the new ticker already belongs to a different *listed*
        security that carries history. Merging two price histories is not a
        decision a loader should make silently, so nothing is changed.
      * ``ignored``  -- the old ticker belongs to a delisted issuer. That is ticker
        reuse, not a rename, and 0009 already handles it by minting a new id.
      * ``unknown``  -- the old ticker is not in the security master at all, and
        neither is the new one. Retryable: the security master may catch up.
    """
    old_symbol = (old_symbol or "").strip().upper()
    new_symbol = (new_symbol or "").strip().upper()
    if not old_symbol or not new_symbol or old_symbol == new_symbol:
        return SymbolChangeOutcome(CHANGE_IGNORED, None)

    security_id = active_security_for_symbol(db, old_symbol, source)
    if security_id is None:
        # Tell "we track this name, but it is dead" apart from "never heard of it".
        # The first is ticker reuse and is terminal.
        if db.fetchval(
            "SELECT 1 FROM core.security WHERE primary_symbol = %s "
            "AND delisted_date IS NOT NULL LIMIT 1",
            (old_symbol,),
        ):
            return SymbolChangeOutcome(CHANGE_IGNORED, None)
        # The old ticker is gone but the new one is ours: the end state this rename
        # asks for already holds, however it got there -- an earlier sweep, or an
        # operator resolving a conflict by hand. Saying so (rather than "unknown")
        # is what lets a conflict leave the review queue: a non-terminal audit row
        # can only be closed by a later sweep reaching a terminal outcome.
        already = active_security_for_symbol(db, new_symbol, source)
        if already is not None:
            return SymbolChangeOutcome(CHANGE_APPLIED, already)
        # Neither ticker is ours. Retryable: the security master may catch up.
        return SymbolChangeOutcome(CHANGE_UNKNOWN, None)

    folded: Optional[int] = None
    holder = active_security_for_symbol(db, new_symbol, source)
    if holder is not None and holder != security_id:
        if not fold_empty_security(db, victim_id=holder, survivor_id=security_id):
            return SymbolChangeOutcome(CHANGE_CONFLICT, security_id)
        folded = holder

    retarget_symbol(
        db,
        security_id=security_id,
        old_symbol=old_symbol,
        new_symbol=new_symbol,
        change_date=change_date,
        company_name=company_name,
        source=source,
    )
    return SymbolChangeOutcome(CHANGE_APPLIED, security_id, folded)


def symbol_change_status(
    db: Database,
    *,
    old_symbol: str,
    new_symbol: str,
    change_date: date,
    source: str = "fmp",
) -> Optional[str]:
    """Status this rename was last recorded with, or None if it is new to us."""
    return db.fetchval(
        """
        SELECT status FROM core.symbol_change
         WHERE source = %s AND old_symbol = %s AND new_symbol = %s AND change_date = %s
        """,
        (source, old_symbol, new_symbol, change_date),
    )


def record_symbol_change(
    db: Database,
    *,
    old_symbol: str,
    new_symbol: str,
    change_date: date,
    status: str,
    security_id: Optional[int] = None,
    company_name: Optional[str] = None,
    detail: Optional[dict] = None,
    source: str = "fmp",
) -> None:
    """Write the audit row for one observed rename.

    The DO UPDATE is guarded so a terminal status can never be downgraded: once a
    rename is applied, a later sweep re-reading the same feed row must not rewrite
    it as a conflict because the ticker it now points at is legitimately taken.
    """
    import json

    db.execute(
        """
        INSERT INTO core.symbol_change
            (old_symbol, new_symbol, change_date, security_id, company_name,
             status, detail, source, first_seen_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now(), now())
        ON CONFLICT (source, old_symbol, new_symbol, change_date) DO UPDATE SET
            security_id  = COALESCE(EXCLUDED.security_id, core.symbol_change.security_id),
            company_name = COALESCE(EXCLUDED.company_name, core.symbol_change.company_name),
            status       = EXCLUDED.status,
            detail       = EXCLUDED.detail,
            updated_at   = now()
        WHERE core.symbol_change.status <> ALL(%s)
        """,
        (
            old_symbol,
            new_symbol,
            change_date,
            security_id,
            company_name,
            status,
            json.dumps(detail, default=str) if detail is not None else None,
            source,
            # psycopg adapts a list to a PostgreSQL array; the public constant
            # stays a tuple so callers cannot mutate it.
            list(TERMINAL_CHANGE_STATUSES),
        ),
    )


def count_unapplied_symbol_changes(db: Database) -> int:
    """How many renames are awaiting a decision.

    Separate from :func:`unapplied_symbol_changes` because that one returns a
    capped page for display: reporting its length as the queue size would pin the
    count at the limit and hide a growing backlog.
    """
    return (
        db.fetchval(
            "SELECT count(*) FROM core.symbol_change WHERE status = %s",
            (CHANGE_CONFLICT,),
        )
        or 0
    )


def unapplied_symbol_changes(db: Database, limit: int = 50) -> list[dict]:
    """A page of renames awaiting a decision -- the review queue for `fafnir status`."""
    return db.fetchall(
        """
        SELECT old_symbol, new_symbol, change_date, status, security_id
          FROM core.symbol_change
         WHERE status = %s
         ORDER BY change_date DESC
         LIMIT %s
        """,
        (CHANGE_CONFLICT, limit),
    )


def listed_securities(db: Database, source: str = "fmp") -> dict[str, dict]:
    """Every listed security, keyed by the symbol the upsert arbitrates on.

    The key matches the partial unique index exactly (0012), so a security-master
    load can tell a genuinely new listing from a refresh of one it already had --
    without a second round trip per symbol. The exchange is not part of it, which
    is what stops a venue transfer being reported (and stored) as a new listing.

    The value carries the identity attributes that the same load then needs in
    order to notice when an *update* looks like it landed on the wrong company --
    see :func:`fafnir.ingest.security_master.check_company_name_drift`. Both uses
    come out of this one query.
    """
    return {
        row["primary_symbol"]: {
            "security_id": row["security_id"],
            "company_name": row["company_name"],
        }
        for row in db.fetchall(
            """
            SELECT security_id, primary_symbol, company_name FROM core.security
             WHERE delisted_date IS NULL AND source = %s
            """,
            (source,),
        )
    }


def security_asset_type(db: Database, security_id: int) -> Optional[str]:
    """The asset_type of one security, or None if it does not exist.

    Exists because the price loader has to know whether it is reading exchange bars
    or a fund NAV *before* it validates them: a NAV payload carries a close and no
    open/high/low, which is a correct bar for a fund and a defect for an equity.
    See :func:`fafnir.ingest.daily_price.load_symbol_prices`.
    """
    val = db.fetchval(
        "SELECT asset_type FROM core.security WHERE security_id = %s", (security_id,)
    )
    return str(val) if val is not None else None


# ---------------------------------------------------------------------------
# Declared universe (ref.tracked_symbol, migration 0018 / ADR 0006)
# ---------------------------------------------------------------------------


def upsert_tracked_symbol(
    db: Database,
    *,
    symbol: str,
    asset_type: str = "fund",
    exchange_code: Optional[str] = None,
    note: Optional[str] = None,
    source: str = "fmp",
) -> bool:
    """Declare a symbol the security master must hold. Returns True if newly declared.

    Re-declaring a symbol that was untracked revives it -- ``is_tracked`` back to
    true and ``untracked_at`` cleared -- rather than erroring. Tracking is a
    statement about the present, not an append-only log; the retirement that
    matters (a fund that actually closed) is a ``delisted_date`` on core.security,
    and nothing here touches that.

    ``added_at`` is preserved on revival, so the row still answers "since when has
    this been of interest" rather than resetting to the date of the last edit.
    """
    row = db.fetchone(
        """
        INSERT INTO ref.tracked_symbol
            (source, symbol, asset_type, exchange_code, note, is_tracked, untracked_at)
        VALUES (%s, %s, %s, %s, %s, TRUE, NULL)
        ON CONFLICT (source, symbol) DO UPDATE SET
            asset_type    = EXCLUDED.asset_type,
            exchange_code = EXCLUDED.exchange_code,
            -- COALESCE: re-declaring without a note must not erase the reason the
            -- row was created for.
            note          = COALESCE(EXCLUDED.note, ref.tracked_symbol.note),
            is_tracked    = TRUE,
            untracked_at  = NULL
        RETURNING (xmax = 0) AS inserted
        """,
        (source, symbol, asset_type, exchange_code, note),
    )
    return bool(row and row["inserted"])


def list_tracked_symbols(
    db: Database, *, source: str = "fmp", tracked_only: bool = True
) -> list[dict]:
    """The declared universe, with each symbol's security_id once it has one.

    The LEFT JOIN is the point: a declaration with a NULL security_id has not been
    loaded yet, which is exactly what `fafnir track list` needs to show and what
    tells an operator that `fafnir ingest tracked` has not run since they added it.
    """
    where = "WHERE t.source = %s" + (" AND t.is_tracked" if tracked_only else "")
    return db.fetchall(
        f"""
        SELECT t.source, t.symbol, t.asset_type, t.exchange_code, t.note,
               t.is_tracked, t.added_at, t.untracked_at,
               s.security_id, s.company_name, s.is_actively_trading,
               s.delisted_date
          FROM ref.tracked_symbol t
          LEFT JOIN core.security s
                 ON s.source = t.source
                AND s.primary_symbol = t.symbol
                AND s.delisted_date IS NULL
         {where}
         ORDER BY t.symbol
        """,
        (source,),
    )


def listed_security_for_declaration(
    db: Database, *, symbol: str, source: str = "fmp"
) -> Optional[dict]:
    """The LISTED security a declaration should load into, following renames.

    Three cases, and the reason this is not just :func:`resolve_security_id`:

    * The obvious one -- a listed security already carries this ticker.
    * The ticker was renamed since it was declared. ``retarget_symbol`` closed this
      ticker's xref period and opened one under the new name, so the declaration now
      names a ticker no listed security carries. Minting on that would fork the
      company's identity into two security_ids, which is precisely the failure ADR
      0005 exists to prevent -- except that here the screener cannot rescue it,
      because ref.tracked_symbol goes on naming the old ticker forever. So the
      closed period is followed, and the caller renames the declaration.
    * The ticker was *reused* by a new issuer after the old one delisted. Delisted
      rows are excluded from both queries, so this returns nothing and the caller
      mints a fresh security_id -- what 0009 already guarantees for the screened
      universe.

    Returns ``{security_id, primary_symbol}`` or None.
    """
    row = db.fetchone(
        """
        SELECT security_id, primary_symbol FROM core.security
         WHERE source = %s AND primary_symbol = %s AND delisted_date IS NULL
         LIMIT 1
        """,
        (source, symbol),
    )
    if row is not None:
        return dict(row)
    # Most recently closed period first: if this ticker served several securities
    # over time, the one that had it last is the one that was renamed away from it.
    return db.fetchone(
        """
        SELECT s.security_id, s.primary_symbol
          FROM core.symbol_xref x
          JOIN core.security s ON s.security_id = x.security_id
         WHERE x.symbol = %s AND s.delisted_date IS NULL
         ORDER BY x.valid_to DESC NULLS FIRST, x.valid_from DESC
         LIMIT 1
        """,
        (symbol,),
    )


def retarget_tracked_symbol(
    db: Database, *, old_symbol: str, new_symbol: str, source: str = "fmp"
) -> bool:
    """Move a declaration onto the ticker its security now trades under.

    Returns True when the declaration moved. When ``new_symbol`` is already
    declared, the old row is untracked instead of moved -- the destination
    declaration already says everything the moved one would have, and two rows
    pointing at one security would ask the loader to load it twice.
    """
    exists = db.fetchval(
        "SELECT 1 FROM ref.tracked_symbol WHERE source = %s AND symbol = %s",
        (source, new_symbol),
    )
    if exists:
        untrack_symbol(db, symbol=old_symbol, source=source)
        return False
    row = db.fetchone(
        """
        UPDATE ref.tracked_symbol SET symbol = %s
         WHERE source = %s AND symbol = %s
        RETURNING symbol
        """,
        (new_symbol, source, old_symbol),
    )
    return row is not None


def untrack_symbol(db: Database, *, symbol: str, source: str = "fmp") -> bool:
    """Stop declaring a symbol. Returns True only if this call did it.

    Idempotent and non-destructive: the row stays as the record of what was once
    declared, and the security keeps its security_id and every bar it has. Whether
    the security is also *retired* is a separate decision -- see
    :func:`mark_delisted`, which is what a fund closing or merging actually is.
    """
    row = db.fetchone(
        """
        UPDATE ref.tracked_symbol
           SET is_tracked = FALSE, untracked_at = now()
         WHERE source = %s AND symbol = %s AND is_tracked
        RETURNING symbol
        """,
        (source, symbol),
    )
    return row is not None


def count_recent_listings(db: Database, days: int = 7) -> int:
    """Securities that entered scope in the last ``days`` days."""
    return (
        db.fetchval(
            "SELECT count(*) FROM core.security "
            "WHERE first_seen_at >= now() - make_interval(days => %s)",
            (days,),
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Daily prices
# ---------------------------------------------------------------------------


def upsert_daily_prices(
    db: Database,
    rows: Sequence[dict],
    *,
    ingestion_run_id: Optional[int] = None,
    source: str = "fmp",
) -> int:
    """Bulk upsert raw OHLCV rows.

    Each row dict must have: security_id, trade_date, open, high, low, close,
    volume, and optionally vwap. Returns number of rows written.
    """
    if not rows:
        return 0
    params = [
        (
            r["security_id"],
            r["trade_date"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r.get("volume", 0),
            r.get("vwap"),
            source,
            ingestion_run_id,
        )
        for r in rows
    ]
    return db.executemany(
        """
        INSERT INTO core.daily_price
            (security_id, trade_date, open, high, low, close, volume, vwap,
             source, ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (security_id, trade_date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
            source = EXCLUDED.source, ingestion_run_id = EXCLUDED.ingestion_run_id,
            loaded_at = now()
        """,
        params,
    )


def max_price_date(db: Database, security_id: int) -> Optional[date]:
    return db.fetchval(
        "SELECT max(trade_date) FROM core.daily_price WHERE security_id = %s",
        (security_id,),
    )


# ---------------------------------------------------------------------------
# Corporate actions & adjustment factors
# ---------------------------------------------------------------------------


def upsert_corporate_action(
    db: Database,
    *,
    security_id: int,
    action_type: str,
    ex_date: date,
    split_numerator: Optional[float] = None,
    split_denominator: Optional[float] = None,
    dividend_amount: Optional[float] = None,
    currency: str = "USD",
    record_date: Optional[date] = None,
    payment_date: Optional[date] = None,
    declaration_date: Optional[date] = None,
    ingestion_run_id: Optional[int] = None,
    source: str = "fmp",
) -> None:
    db.execute(
        """
        INSERT INTO core.corporate_action
            (security_id, action_type, ex_date, record_date, payment_date,
             declaration_date, split_numerator, split_denominator, dividend_amount,
             currency, source, ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (security_id, action_type, ex_date) DO UPDATE SET
            record_date = EXCLUDED.record_date,
            payment_date = EXCLUDED.payment_date,
            declaration_date = EXCLUDED.declaration_date,
            split_numerator = EXCLUDED.split_numerator,
            split_denominator = EXCLUDED.split_denominator,
            dividend_amount = EXCLUDED.dividend_amount,
            currency = EXCLUDED.currency,
            loaded_at = now()
        """,
        (
            security_id,
            action_type,
            ex_date,
            record_date,
            payment_date,
            declaration_date,
            split_numerator,
            split_denominator,
            dividend_amount,
            currency,
            source,
            ingestion_run_id,
        ),
    )


def corporate_actions_for(db: Database, security_id: int) -> list[dict]:
    return db.fetchall(
        """
        SELECT action_type, ex_date, split_numerator, split_denominator, dividend_amount
        FROM core.corporate_action
        WHERE security_id = %s
        ORDER BY ex_date ASC
        """,
        (security_id,),
    )


def close_before(db: Database, security_id: int, d: date) -> Optional[Decimal]:
    """Raw close on the latest trade_date STRICTLY BEFORE ``d``.

    Used to value a dividend for adjustment: the reference price is the last close
    that still carried the dividend, i.e. the close before the ex-date, never the
    ex-date's own (already-lower) close. Returns NUMERIC as ``Decimal`` so the
    factor math stays exact.
    """
    return db.fetchval(
        """
        SELECT close FROM core.daily_price
        WHERE security_id = %s AND trade_date < %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (security_id, d),
    )


def replace_adjustment_factors(
    db: Database, security_id: int, factors: Sequence[dict]
) -> int:
    """Replace all adjustment factors for a security. factors: effective_date,
    cumulative_price_factor, cumulative_volume_factor."""
    db.execute(
        "DELETE FROM core.adjustment_factor WHERE security_id = %s", (security_id,)
    )
    if not factors:
        return 0
    params = [
        (
            security_id,
            f["effective_date"],
            f["cumulative_price_factor"],
            f["cumulative_volume_factor"],
        )
        for f in factors
    ]
    return db.executemany(
        """
        INSERT INTO core.adjustment_factor
            (security_id, effective_date, cumulative_price_factor,
             cumulative_volume_factor, computed_at)
        VALUES (%s,%s,%s,%s, now())
        """,
        params,
    )


def securities_with_actions(db: Database) -> list[int]:
    rows = db.fetchall(
        "SELECT DISTINCT security_id FROM core.corporate_action ORDER BY security_id"
    )
    return [int(r["security_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Watermarks & lineage
# ---------------------------------------------------------------------------


def get_watermark(
    db: Database, source: str, endpoint: str, security_id: int = 0
) -> Optional[date]:
    return db.fetchval(
        """
        SELECT last_loaded_date FROM ops.load_watermark
        WHERE source = %s AND endpoint = %s AND security_id = %s
        """,
        (source, endpoint, security_id),
    )


def count_watermarks(db: Database, source: str, endpoint: str) -> int:
    """How many per-security watermarks exist for a source/endpoint pair."""
    return (
        db.fetchval(
            """
            SELECT count(*) FROM ops.load_watermark
            WHERE source = %s AND endpoint = %s
            """,
            (source, endpoint),
        )
        or 0
    )


def set_watermark(
    db: Database,
    source: str,
    endpoint: str,
    last_loaded_date: date,
    security_id: int = 0,
) -> None:
    db.execute(
        """
        INSERT INTO ops.load_watermark
            (source, endpoint, security_id, last_loaded_date, last_run_at, updated_at)
        VALUES (%s,%s,%s,%s, now(), now())
        ON CONFLICT (source, endpoint, security_id) DO UPDATE SET
            last_loaded_date = GREATEST(
                ops.load_watermark.last_loaded_date, EXCLUDED.last_loaded_date),
            last_run_at = now(), updated_at = now()
        """,
        (source, endpoint, security_id, last_loaded_date),
    )


def land_payload(
    db: Database,
    *,
    endpoint: str,
    params: dict,
    symbol: Optional[str],
    http_status: Optional[int],
    payload: Any,
    payload_hash: str,
    nbytes: int,
    ingestion_run_id: Optional[int],
) -> None:
    import json

    db.execute(
        """
        INSERT INTO landing.fmp_raw
            (ingestion_run_id, endpoint, params, symbol, http_status, payload,
             payload_hash, bytes, fetched_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
        """,
        (
            ingestion_run_id,
            endpoint,
            json.dumps(params),
            symbol,
            http_status,
            json.dumps(payload),
            payload_hash,
            nbytes,
        ),
    )


def add_dq_flag(
    db: Database,
    *,
    check_name: str,
    severity: str = "warn",
    security_id: Optional[int] = None,
    table_name: Optional[str] = None,
    record_key: Optional[dict] = None,
    detail: Optional[dict] = None,
    ingestion_run_id: Optional[int] = None,
) -> None:
    """Record a data-quality flag. Every call inserts a row.

    That repetition is load-bearing only where each detection is itself the
    signal -- the price_* quarantines counted by `count_price_quarantines`.
    For a standing condition that a scheduled job re-detects over unchanged
    data, use `add_dq_flag_once` instead, or the open-DQ count inflates by one
    row per run per problem.
    """
    import json

    db.execute(
        """
        INSERT INTO ops.data_quality_flag
            (ingestion_run_id, security_id, table_name, record_key, check_name,
             severity, detail, detected_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
        """,
        (
            ingestion_run_id,
            security_id,
            table_name,
            json.dumps(record_key) if record_key else None,
            check_name,
            severity,
            json.dumps(detail) if detail else None,
        ),
    )


def add_dq_flag_once(
    db: Database,
    *,
    check_name: str,
    severity: str = "warn",
    security_id: Optional[int] = None,
    table_name: Optional[str] = None,
    record_key: Optional[dict] = None,
    detail: Optional[dict] = None,
    ingestion_run_id: Optional[int] = None,
) -> bool:
    """Flag a standing condition once per occurrence, not once per run.

    Skips the insert when an unresolved flag with the same
    (security_id, check_name, record_key) is already open. Returns True when a
    row was written.

    Use this for anything a scheduled job re-detects over unchanged data.
    `fafnir adjust` recomputes every security with actions on every nightly run
    and `fafnir dq run` re-scans the whole universe, so a plain insert turns one
    unresolved problem into one new unresolved flag per night -- and the open-DQ
    count `fafnir status` reports grows without bound while the number of actual
    problems stays flat, until the number an operator triages on says nothing.

    Use `add_dq_flag` where each detection is itself the signal rather than a
    restatement of the same one: `count_price_quarantines` counts the price_*
    flags for a (security_id, date) to decide when a persistently-bad bar stops
    holding the watermark, so deduplicating those would freeze that counter at 1
    and hold the watermark behind that bar forever.

    Matching is on the record_key as a whole (jsonb equality, so key order does
    not matter) with NULL treated as a value, not as unknown: a new gap date or a
    different ex-date is a different occurrence and is still recorded, while a
    keyless flag (`adjustment_failed`) dedupes per security.
    """
    import json

    key_json = json.dumps(record_key) if record_key else None

    # The nullable columns get `= %s` or `IS NULL` rather than one uniform
    # `IS NOT DISTINCT FROM`. Both forms of this are indexable and the tidier one
    # is not: IS NOT DISTINCT FROM cannot be an index condition, so the probe
    # degrades to a filter over every open flag of that check_name -- and this runs
    # once per candidate, 21,000 times on a universe-wide `fafnir adjust`. See
    # ix_dq_flag_open_condition (migration 0014), which this predicate matches.
    clauses = ["check_name = %s", "resolved_at IS NULL"]
    probe: list[Any] = [check_name]
    if security_id is None:
        clauses.append("security_id IS NULL")
    else:
        clauses.append("security_id = %s")
        probe.append(security_id)
    if key_json is None:
        clauses.append("record_key IS NULL")
    else:
        clauses.append("record_key = %s::jsonb")
        probe.append(key_json)

    return (
        db.execute(
            f"""
            INSERT INTO ops.data_quality_flag
                (ingestion_run_id, security_id, table_name, record_key, check_name,
                 severity, detail, detected_at)
            SELECT %s::bigint, %s::bigint, %s::text, %s::jsonb, %s::text,
                   %s::text, %s::jsonb, now()
            WHERE NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag
                WHERE {" AND ".join(clauses)}
            )
            """,
            (
                ingestion_run_id,
                security_id,
                table_name,
                key_json,
                check_name,
                severity,
                json.dumps(detail) if detail else None,
                *probe,
            ),
        )
        > 0
    )


def count_price_quarantines(db: Database, security_id: int, date_iso: str) -> int:
    """How many times a given trade_date has been quarantined for this security
    (price_* checks). Used to bound the watermark hold on a persistently-bad bar."""
    return int(
        db.fetchval(
            """
            SELECT count(*) FROM ops.data_quality_flag
            WHERE security_id = %s
              AND check_name LIKE 'price\\_%%'
              AND record_key->>'date' = %s
            """,
            (security_id, date_iso),
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Data-quality review queue (read + resolve)
# ---------------------------------------------------------------------------
#
# `fafnir dq run` fills ops.data_quality_flag; this is how an operator gets back
# out of it. Every function here takes the same :class:`DqFilter` and turns it into
# SQL in one place, which is the point rather than a tidiness: `fafnir dq resolve
# --check gap --symbol AAPL` closes exactly the rows `fafnir dq list --check gap
# --symbol AAPL` shows. A filter that meant one thing when listing and another when
# resolving is how a triage session closes flags nobody ever looked at.

DQ_STATES = ("open", "resolved", "all")

# Order severity by how much it wants attention. Alphabetically 'error' < 'info' <
# 'warn', which puts the worst first only by accident and 'info' above 'warn'.
_DQ_SEVERITY_RANK = "CASE severity WHEN 'error' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END"


class DqFilter(NamedTuple):
    """Which flags a queue operation applies to.

    One object, passed to every function below, so that listing, counting,
    summarising and resolving cannot disagree about what a set of options selects.

    ``state`` is about resolution: ``open`` (the default -- the queue), ``resolved``
    (the triage record) or ``all``. ``checks`` entries are exact names or a `*` glob
    (`price_*`). ``until`` is inclusive of the whole day.
    """

    state: str = "open"
    checks: Sequence[str] = ()
    severities: Sequence[str] = ()
    security_id: Optional[int] = None
    since: Optional[date] = None
    until: Optional[date] = None
    flag_ids: Sequence[int] = ()

    @property
    def is_narrowed(self) -> bool:
        """True when something other than ``state`` restricts the set.

        `fafnir dq resolve` refuses to run without this: an UPDATE under a bare
        state filter closes the entire queue, problems nobody has looked at
        included, and `reopen` cannot put it back because it cannot know which
        rows were open a moment earlier.
        """
        return bool(
            self.checks
            or self.severities
            or self.flag_ids
            or self.security_id is not None
            or self.since is not None
            or self.until is not None
        )


def _dq_check_pattern(column: str, value: str) -> tuple[str, list[Any]]:
    """Render one ``--check`` value as a predicate: exact match, or a `*` glob.

    `price_*` is the category the docs talk about constantly (its repeats are
    load-bearing -- see :func:`count_price_quarantines`) and `security_*` covers the
    per-field range checks, so matching a prefix is worth supporting. The literal
    `_` in those names is itself a LIKE wildcard, so the value is escaped before
    `*` becomes `%`; otherwise `price_*` would also match a future `priceXfoo`.
    """
    if "*" not in value:
        return f"{column} = %s", [value]
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{column} LIKE %s ESCAPE '\\'", [escaped.replace("*", "%")]


def _dq_where(filt: DqFilter, alias: str = "") -> tuple[str, list[Any]]:
    """Compile a :class:`DqFilter` into a WHERE clause and its parameters.

    ``alias`` qualifies the column names for the one query that joins
    (:func:`list_dq_flags`), so the joined and unjoined forms are the same
    predicate rather than two that have to be kept in step by hand.
    """
    if filt.state not in DQ_STATES:
        raise ValueError(f"state must be one of {DQ_STATES}, got {filt.state!r}")
    q = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    if filt.state == "open":
        clauses.append(f"{q}resolved_at IS NULL")
    elif filt.state == "resolved":
        clauses.append(f"{q}resolved_at IS NOT NULL")
    if filt.checks:
        ors: list[str] = []
        for value in filt.checks:
            sql, args = _dq_check_pattern(f"{q}check_name", value)
            ors.append(sql)
            params.extend(args)
        clauses.append("(" + " OR ".join(ors) + ")")
    if filt.severities:
        clauses.append(f"{q}severity = ANY(%s)")
        params.append(list(filt.severities))
    if filt.security_id is not None:
        clauses.append(f"{q}security_id = %s")
        params.append(filt.security_id)
    if filt.since is not None:
        clauses.append(f"{q}detected_at >= %s")
        params.append(filt.since)
    if filt.until is not None:
        # Inclusive of the whole day: `--until 2024-08-28` has to include a flag
        # detected at 14:02 that day, which `detected_at <= %s` would drop -- the
        # date widens to midnight.
        clauses.append(f"{q}detected_at < (%s::date + 1)")
        params.append(filt.until)
    if filt.flag_ids:
        clauses.append(f"{q}dq_flag_id = ANY(%s)")
        params.append(list(filt.flag_ids))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def dq_flag_totals(db: Database, filt: DqFilter = DqFilter()) -> dict:
    """Headline numbers under a filter: flags, distinct securities and checks, and
    the detection window.

    Separate from :func:`summarize_dq_flags` because securities cannot be summed
    across its rows -- one security with a gap and an outlier is one security, and
    adding the per-check counts would report it as two.
    """
    where, params = _dq_where(filt)
    row = db.fetchone(
        f"""
        SELECT count(*)                        AS flags,
               count(DISTINCT security_id)     AS securities,
               count(DISTINCT check_name)      AS checks,
               min(detected_at)                AS first_detected,
               max(detected_at)                AS last_detected
          FROM ops.data_quality_flag
          {where}
        """,
        params,
    )
    return row or {}


def summarize_dq_flags(db: Database, filt: DqFilter = DqFilter()) -> list[dict]:
    """One row per (check_name, severity): how many, over how many securities, and
    when that condition was first and last seen.

    This is the triage view. 40,000 flags is not something anyone reads, but "gap:
    842 over 201 securities, oldest 2024-01-03" is a decision.
    """
    where, params = _dq_where(filt)
    return db.fetchall(
        f"""
        SELECT check_name,
               severity,
               count(*)                    AS flags,
               count(DISTINCT security_id) AS securities,
               min(detected_at)            AS first_detected,
               max(detected_at)            AS last_detected
          FROM ops.data_quality_flag
          {where}
         GROUP BY check_name, severity
         ORDER BY {_DQ_SEVERITY_RANK} DESC, flags DESC, check_name
        """,
        params,
    )


def list_dq_flags(
    db: Database,
    filt: DqFilter = DqFilter(),
    *,
    limit: int = 50,
    offset: int = 0,
    newest_first: bool = True,
) -> list[dict]:
    """A page of individual flags, with the security's current ticker attached.

    LEFT JOIN, not JOIN: ``security_id`` is a soft reference (nullable, no FK), so a
    flag that is not about one security has none, and a flag whose security was
    folded away has to stay listable rather than silently leaving the queue while
    still being counted by it.
    """
    where, params = _dq_where(filt, alias="f")
    direction = "DESC" if newest_first else "ASC"
    return db.fetchall(
        f"""
        SELECT f.dq_flag_id, f.check_name, f.severity, f.security_id,
               s.primary_symbol, f.table_name, f.record_key, f.detail,
               f.detected_at, f.resolved_at, f.resolved_by, f.resolution_note,
               f.ingestion_run_id
          FROM ops.data_quality_flag f
          LEFT JOIN core.security s ON s.security_id = f.security_id
          {where}
         ORDER BY f.detected_at {direction}, f.dq_flag_id {direction}
         LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )


def resolve_dq_flags(
    db: Database,
    filt: DqFilter,
    *,
    note: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> list[int]:
    """Close the flags the filter selects, stamping who and why. Returns the ids
    actually closed.

    Only open flags are touched, whatever ``filt.state`` says: resolving an already
    resolved flag would overwrite an earlier operator's note with this one's, and
    silently attribute their decision to you. It also lets a caller tell "already
    closed" from "closed by me" by diffing the ids it asked for against the ids
    returned.

    Refuses a filter that narrows nothing -- see :attr:`DqFilter.is_narrowed`.

    Resolving is a judgement about a condition, not a repair of it. The next
    `fafnir dq run` re-detects a problem that is still there and flags it again,
    because setting ``resolved_at`` frees the condition's slot in
    ux_dq_flag_open_condition. That is the intended behaviour: a flag that comes
    back is the check saying the problem never went away.
    """
    if not filt.is_narrowed:
        raise ValueError(
            "resolve_dq_flags needs flag ids or at least one filter; refusing to "
            "close the whole queue"
        )
    where, params = _dq_where(filt._replace(state="open"))
    rows = db.fetchall(
        f"""
        UPDATE ops.data_quality_flag
           SET resolved_at = now(), resolved_by = %s, resolution_note = %s
         {where}
        RETURNING dq_flag_id
        """,
        [resolved_by, note, *params],
    )
    return [int(r["dq_flag_id"]) for r in rows]


def reopen_dq_flags(
    db: Database, flag_ids: Sequence[int]
) -> tuple[list[int], list[int]]:
    """Undo a resolution: back to open, provenance cleared. Returns
    ``(reopened, conflicted)``.

    The note goes with it. A "resolved because the exchange was shut" sitting on a
    row that is open again is a decision that no longer stands, and migration 0017
    has the schema refuse it.

    Ids only, never a filter: reopening is for the resolve you regret, and a bulk
    reopen would have to guess which of the closed rows under a filter were closed
    by that mistake.

    Each id gets its own savepoint because one of them can legitimately fail:
    ux_dq_flag_open_condition (0016) allows one open row per condition, so a
    condition re-flagged since it was closed has no free slot. That is the queue
    telling you it already carries the problem -- reported as a conflict, and not
    a reason to abandon the other ids in the same command.
    """
    reopened: list[int] = []
    conflicted: list[int] = []
    for flag_id in flag_ids:
        try:
            with db.conn.transaction():
                row = db.fetchone(
                    """
                    UPDATE ops.data_quality_flag
                       SET resolved_at = NULL, resolved_by = NULL,
                           resolution_note = NULL
                     WHERE dq_flag_id = %s AND resolved_at IS NOT NULL
                    RETURNING dq_flag_id
                    """,
                    (flag_id,),
                )
        except psycopg.errors.UniqueViolation:
            conflicted.append(int(flag_id))
            continue
        if row is not None:
            reopened.append(int(row["dq_flag_id"]))
    return reopened, conflicted


# ---------------------------------------------------------------------------
# Read API (used by duk db datasource and `fafnir status`)
# ---------------------------------------------------------------------------


def read_price_history(
    db: Database,
    symbol: str,
    start_date: Optional[date],
    end_date: Optional[date],
    adjusted: bool,
) -> list[dict]:
    """Return raw or adjusted OHLCV rows for a symbol, ascending by date."""
    security_id = resolve_security_id(db, symbol)
    if security_id is None:
        return []
    relation = "mart.v_daily_price_adjusted" if adjusted else "core.daily_price"
    clauses = ["security_id = %s"]
    params: list[Any] = [security_id]
    if start_date is not None:
        clauses.append("trade_date >= %s")
        params.append(start_date)
    if end_date is not None:
        clauses.append("trade_date <= %s")
        params.append(end_date)
    where = " AND ".join(clauses)
    return db.fetchall(
        f"""
        SELECT trade_date AS date, open, high, low, close, volume
        FROM {relation}
        WHERE {where}
        ORDER BY trade_date ASC
        """,
        params,
    )


def read_security_count(db: Database) -> dict:
    return db.fetchone("""
        SELECT
            count(*)                                   AS securities,
            count(*) FILTER (WHERE is_actively_trading) AS active,
            count(*) FILTER (WHERE delisted_date IS NOT NULL) AS delisted
        FROM core.security
        """) or {}
