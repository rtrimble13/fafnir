"""
Parameterized data-access functions over the fafnir schema.

Writes are key-based upserts (``ON CONFLICT ... DO UPDATE``) so every load is
idempotent. Reads return plain dicts/lists; the duk ``db`` datasource shapes
them into the DataFrame contracts the CLI expects.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, NamedTuple, Optional, Sequence

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
            -- COALESCE for the same reason as the venue above, and it is the
            -- same class of bug: a caller that does not carry a classification
            -- must not blank the one already stored. Without this the nightly
            -- `ingest securities` -- which upserts the whole active universe and
            -- passed neither field -- erased sector and industry for every
            -- security it touched, every night. It emptied 75%% of the master in
            -- eight days, and read as "sector never populates" rather than as an
            -- overwrite, because the surviving rows were exactly the delisted ones
            -- the nightly load no longer touches.
            sector_id           = COALESCE(EXCLUDED.sector_id,
                                           core.security.sector_id),
            industry_id         = COALESCE(EXCLUDED.industry_id,
                                           core.security.industry_id),
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


def security_bar_span(
    db: Database, security_id: int
) -> tuple[Optional[date], Optional[date]]:
    """The security's first and last stored bar dates, or (None, None) with no bars.

    Two ORDER BY ... LIMIT 1 probes rather than min()/max(): each is an index scan
    per partition that stops at its first row, which is what keeps this cheap enough
    to ask once per delisting the nightly sweep considers.
    """
    row = db.fetchone(
        """
        SELECT
            (SELECT trade_date FROM core.daily_price
              WHERE security_id = %s ORDER BY trade_date ASC LIMIT 1)  AS first_bar,
            (SELECT trade_date FROM core.daily_price
              WHERE security_id = %s ORDER BY trade_date DESC LIMIT 1) AS last_bar
        """,
        (security_id, security_id),
    )
    if row is None:
        return None, None
    return row["first_bar"], row["last_bar"]


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
# (duk/datasource/db.py::_resolve_security_id) MUST match these predicates and this
# ordering, so the loader and the read path always resolve the same ticker to the
# same id. It reads the equivalent `mart` views rather than these core relations --
# duk connects as a mart-only role (ADR 0008) -- so the relation names differ there
# and nothing else does. Change the ladder here and that copy needs the same change.
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
CHANGE_DISMISSED = "dismissed"
CHANGE_UNKNOWN = "unknown"

# Statuses that need no further attempt. A conflict is NOT one of them: it is
# retried on every sweep because it usually means the rename feed lagged the
# screener, and the collision clears itself once the duplicate is resolved.
#
# `dismissed` (0018) is here because some conflicts never clear on their own -- a
# pre-launch ticker shuffle, or the same rename emitted in both directions, where
# both securities are live and neither ever gives up the ticker. The sweep would
# re-conflict those nightly forever. Membership here is what makes a dismissal
# stick: the sweep skips the row, and record_symbol_change refuses to downgrade it
# back to `conflict` on the next pass.
TERMINAL_CHANGE_STATUSES = (CHANGE_APPLIED, CHANGE_IGNORED, CHANGE_DISMISSED)


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


class DuplicateRow(NamedTuple):
    """One ``core.security`` row competing for a ticker, and what hangs off it."""

    security_id: int
    first_seen_at: Optional[date]
    delisted_date: Optional[date]
    has_bars: bool
    has_actions: bool
    has_factors: bool
    company_name: Optional[str] = None

    @property
    def has_history(self) -> bool:
        """Mirrors :func:`security_has_history` -- what a delete would destroy."""
        return self.has_bars or self.has_actions or self.has_factors


class DuplicateGroup(NamedTuple):
    """Every ``core.security`` row holding one ticker.

    ``survivor_id`` is the row that owns the price history, and is None when the
    group cannot be resolved automatically -- see ``blocker``.
    """

    symbol: str
    rows: Sequence[DuplicateRow]
    survivor_id: Optional[int]
    blocker: Optional[str]

    @property
    def victims(self) -> list[DuplicateRow]:
        if self.survivor_id is None:
            return []
        return [r for r in self.rows if r.security_id != self.survivor_id]


def duplicate_symbol_groups(
    db: Database, *, symbol: Optional[str] = None, limit: int = 0
) -> list[DuplicateGroup]:
    """Tickers held by more than one ``core.security`` row, with a survivor chosen.

    The survivor is the row that holds the bars, and that is the whole rule. It is
    deliberately not "the oldest" or "the one with a CUSIP": bars are the thing a
    delete would destroy and attributes are rewritten by the next master load, so
    the row with history is the only defensible one to keep.

    A group is refused -- ``survivor_id`` None, ``blocker`` set -- when the rows
    name more than one company, or when that rule does not pick exactly one row:

    * the rows carry different company names. Same name and most rows empty is the
      re-mint this repairs; differing names is genuine ticker reuse, where two rows
      is *correct* (0009), or a rebrand. The bars rule cannot tell those apart --
      both look like one delisted row with the history and a newer empty one -- so
      the name is checked first and disagreement is always a refusal;
    * no row has bars, so there is no history to preserve and nothing to prefer;
    * more than one row has bars, which is either genuine ticker reuse (0009 says
      two rows is *correct* there) or a rename the sweep missed. Both need
      `security merge-rename` and a human reading the OHLC comparison.

    Set-based on purpose: the per-row ``EXISTS`` form of this question times out
    over 18,000 rows.
    """
    params: list[Any] = []
    # An operator-minted security (`security split-history`) shares its ticker with
    # the issuer it was split off by design; it is not a duplicate of anything.
    symbol_clause = f"WHERE s.{VENDOR_FED_SECURITY}"
    if symbol:
        symbol_clause += " AND s.primary_symbol = %s"
        params.append(symbol.upper())
    limit_clause = f"LIMIT {int(limit)}" if limit else ""

    rows = db.fetchall(
        f"""
        WITH dup AS (
            SELECT primary_symbol
              FROM core.security s
              {symbol_clause}
             GROUP BY primary_symbol
            HAVING count(*) > 1
             ORDER BY primary_symbol
             {limit_clause}
        ),
        r AS (
            SELECT s.security_id, s.primary_symbol, s.first_seen_at::date AS seen,
                   s.delisted_date, s.company_name
              FROM core.security s JOIN dup USING (primary_symbol)
             WHERE s.{VENDOR_FED_SECURITY}
        )
        SELECT r.security_id, r.primary_symbol, r.seen, r.delisted_date,
               r.company_name,
               (p.security_id IS NOT NULL) AS has_bars,
               (a.security_id IS NOT NULL) AS has_actions,
               (f.security_id IS NOT NULL) AS has_factors
          FROM r
          LEFT JOIN (SELECT DISTINCT security_id FROM core.daily_price
                      WHERE security_id IN (SELECT security_id FROM r)) p
                 ON p.security_id = r.security_id
          LEFT JOIN (SELECT DISTINCT security_id FROM core.corporate_action
                      WHERE security_id IN (SELECT security_id FROM r)) a
                 ON a.security_id = r.security_id
          LEFT JOIN (SELECT DISTINCT security_id FROM core.adjustment_factor
                      WHERE security_id IN (SELECT security_id FROM r)) f
                 ON f.security_id = r.security_id
         ORDER BY r.primary_symbol, r.security_id
        """,
        params,
    )

    grouped: dict[str, list[DuplicateRow]] = {}
    for row in rows:
        grouped.setdefault(row["primary_symbol"], []).append(
            DuplicateRow(
                security_id=int(row["security_id"]),
                first_seen_at=row["seen"],
                delisted_date=row["delisted_date"],
                has_bars=bool(row["has_bars"]),
                has_actions=bool(row["has_actions"]),
                has_factors=bool(row["has_factors"]),
                company_name=row["company_name"],
            )
        )

    out: list[DuplicateGroup] = []
    for sym, members in grouped.items():
        holders = [r for r in members if r.has_bars]
        # Names first, because a name disagreement outranks the bars rule. The bars
        # rule cannot tell a re-mint from genuine ticker reuse: both look like one
        # delisted row holding the history and a newer row holding none, and folding
        # the second case would delete a legitimately new issuer into a dead
        # company's identity -- exactly what 0009 mints a separate row to prevent.
        # `security_duplicate_identity` already reports distinct_company_names for
        # this reason, and the playbook reads it the same way: same name is a
        # repair, differing names are two companies and are nobody's to fold.
        names = {r.company_name.strip().casefold() for r in members if r.company_name}
        if len(names) > 1:
            out.append(
                DuplicateGroup(
                    sym,
                    members,
                    None,
                    f"{len(names)} distinct company names -- ticker reuse (two rows "
                    "is correct) or a rebrand; neither is this command's to fold",
                )
            )
        elif len(holders) == 1:
            out.append(DuplicateGroup(sym, members, holders[0].security_id, None))
        elif not holders:
            out.append(
                DuplicateGroup(
                    sym,
                    members,
                    None,
                    f"no row holds bars ({len(members)} rows) -- nothing to keep",
                )
            )
        else:
            out.append(
                DuplicateGroup(
                    sym,
                    members,
                    None,
                    f"{len(holders)} rows hold bars -- ticker reuse or a missed "
                    "rename; use `security merge-rename` after reading the OHLC "
                    "comparison",
                )
            )
    return out


def reopen_symbol_period(db: Database, *, security_id: int, symbol: str) -> bool:
    """Re-open the survivor's xref period after its usurpers are gone.

    Each mint closed the previous period (0012), so once the shells are deleted the
    survivor is left holding a *closed* period and the ticker resolves only as a
    former symbol -- `resolve_symbol` looks for `valid_to IS NULL` first. For a
    security that never stopped trading that is still the wrong answer, just a
    quieter one than before.

    Only re-opens when nothing else holds the ticker open and the security is not
    delisted; a delisted row keeps its closed period, which is what it means.
    """
    return bool(
        db.execute(
            """
            UPDATE core.symbol_xref x
               SET valid_to = NULL
             WHERE x.security_id = %s AND x.symbol = %s
               AND x.valid_to = (SELECT max(valid_to) FROM core.symbol_xref
                                  WHERE symbol = %s AND security_id = %s)
               AND NOT EXISTS (SELECT 1 FROM core.symbol_xref o
                                WHERE o.symbol = %s AND o.valid_to IS NULL)
               AND EXISTS (SELECT 1 FROM core.security s
                            WHERE s.security_id = %s AND s.delisted_date IS NULL)
            """,
            (security_id, symbol, symbol, security_id, symbol, security_id),
        )
    )


# ---------------------------------------------------------------------------
# Merging two securities that are one company
# ---------------------------------------------------------------------------
#
# fold_empty_security above is the automatic path and it handles the case the
# loader can reason about: a duplicate with nothing in it. Everything below is the
# manual path for the case it deliberately refuses -- both rows carry bars.
#
# That case is not hypothetical. It is what a security-master load produces when it
# runs BEFORE the rename sweep: the new ticker is minted as a fresh security, the
# price loader fills it (the vendor serves a renamed ticker's full continuous
# history, so the duplicate arrives with bars predating the rename), and from then
# on every sweep reports `conflict`. Six such duplicates were created on one day in
# 2026-08 by initial_backfill.sh, which had no rename step at all.
#
# Merging is destructive and irreversible, so the guards are the design. Identity
# has to be established from the vendor's own identifiers rather than inferred from
# a ticker, and the overlapping bars have to actually agree before one side is
# discarded.

# OHLC fields compared across the overlap. Volume is reported but does not block:
# a vendor restating volume across a rename is common and costs no price accuracy,
# while a restated *price* means the two rows disagree about what happened.
_MERGE_PRICE_FIELDS = ("open", "high", "low", "close")

# Identifiers that establish two rows are the same instrument. CIK identifies the
# SEC *registrant* and survives a rename, so it is necessary but not sufficient --
# a company's warrant and its common stock share a CIK. CUSIP and ISIN identify the
# instrument, which is the grain a merge operates on.
_MERGE_IDENTITY_FIELDS = ("cusip", "isin", "cik")


class MergeRefused(RuntimeError):
    """A merge failed a guard. Carries the plan so the caller can show the evidence."""

    def __init__(self, message: str, plan: "MergePlan") -> None:
        super().__init__(message)
        self.plan = plan


class MergePlan(NamedTuple):
    """What a merge would do, and every reason it should not happen.

    Produced by :func:`compare_securities`, which writes nothing. The same object
    backs `--dry-run` and the guard inside :func:`merge_security`, so the preview an
    operator approves is the check that runs.
    """

    survivor_id: int
    victim_id: int
    survivor_symbol: str
    victim_symbol: str
    # (field, survivor_value, victim_value) where both sides are known and differ.
    identity_mismatches: list[tuple[str, Any, Any]]
    survivor_bars: int
    victim_bars: int
    shared_days: int
    victim_only_bars: int
    disagreeing_days: int
    disagreement_sample: list[dict]
    volume_only_disagreements: int
    victim_actions: int
    colliding_actions: int
    victim_flags: int

    @property
    def blockers(self) -> list[str]:
        """Why this merge must not proceed unforced, in the order worth reading."""
        out: list[str] = []
        for field, survivor, victim in self.identity_mismatches:
            out.append(
                f"{field} differs: survivor {survivor!r} vs victim {victim!r} -- "
                f"these are not the same instrument"
            )
        if self.disagreeing_days:
            out.append(
                f"{self.disagreeing_days} of {self.shared_days} overlapping days "
                f"disagree on OHLC -- the two rows do not tell the same story about "
                f"those sessions"
            )
        return out


def compare_securities(db: Database, *, survivor_id: int, victim_id: int) -> MergePlan:
    """Everything a merge decision needs, without touching a row.

    Split out from :func:`merge_security` so the evidence can be shown before any
    of it is acted on -- a merge deletes a security_id, and an operator approving
    that should be reading the same comparison the guard reads.
    """
    if survivor_id == victim_id:
        raise ValueError("survivor and victim are the same security")

    rows = {
        int(r["security_id"]): r
        for r in db.fetchall(
            """
            SELECT security_id, primary_symbol, cusip, isin, cik
              FROM core.security WHERE security_id = ANY(%s)
            """,
            ([survivor_id, victim_id],),
        )
    }
    for sid in (survivor_id, victim_id):
        if sid not in rows:
            raise ValueError(f"no such security: {sid}")
    survivor, victim = rows[survivor_id], rows[victim_id]

    # A NULL on either side is missing data, not evidence of difference: FMP leaves
    # cik empty on most ETFs, and refusing on that would block the very merges this
    # exists for. Only a populated disagreement is a mismatch.
    mismatches = [
        (field, survivor[field], victim[field])
        for field in _MERGE_IDENTITY_FIELDS
        if survivor[field] is not None
        and victim[field] is not None
        and str(survivor[field]).strip() != str(victim[field]).strip()
    ]

    counts = db.fetchone(
        """
        SELECT (SELECT count(*) FROM core.daily_price
                 WHERE security_id = %s)          AS survivor_bars,
               (SELECT count(*) FROM core.daily_price
                 WHERE security_id = %s)          AS victim_bars,
               (SELECT count(*) FROM core.corporate_action
                 WHERE security_id = %s)          AS victim_actions,
               (SELECT count(*) FROM ops.data_quality_flag
                 WHERE security_id = %s)          AS victim_flags,
               (SELECT count(*) FROM core.corporate_action v
                 WHERE v.security_id = %s
                   AND EXISTS (SELECT 1 FROM core.corporate_action s
                                WHERE s.security_id = %s
                                  AND s.action_type = v.action_type
                                  AND s.ex_date = v.ex_date)) AS colliding_actions
        """,
        (survivor_id, victim_id, victim_id, victim_id, victim_id, survivor_id),
    )

    price_differs = " OR ".join(
        f"s.{f} IS DISTINCT FROM v.{f}" for f in _MERGE_PRICE_FIELDS
    )
    overlap = db.fetchone(
        f"""
        SELECT count(*)                                            AS shared_days,
               count(*) FILTER (WHERE {price_differs})             AS disagreeing_days,
               count(*) FILTER (WHERE NOT ({price_differs})
                                  AND s.volume IS DISTINCT FROM v.volume)
                                                                   AS volume_only
          FROM core.daily_price s
          JOIN core.daily_price v ON v.trade_date = s.trade_date
         WHERE s.security_id = %s AND v.security_id = %s
        """,
        (survivor_id, victim_id),
    )

    sample = db.fetchall(
        f"""
        SELECT s.trade_date,
               s.open AS survivor_open, v.open AS victim_open,
               s.high AS survivor_high, v.high AS victim_high,
               s.low  AS survivor_low,  v.low  AS victim_low,
               s.close AS survivor_close, v.close AS victim_close
          FROM core.daily_price s
          JOIN core.daily_price v ON v.trade_date = s.trade_date
         WHERE s.security_id = %s AND v.security_id = %s AND ({price_differs})
         ORDER BY s.trade_date
         LIMIT 10
        """,
        (survivor_id, victim_id),
    )

    shared = int(overlap["shared_days"])
    return MergePlan(
        survivor_id=survivor_id,
        victim_id=victim_id,
        survivor_symbol=survivor["primary_symbol"],
        victim_symbol=victim["primary_symbol"],
        identity_mismatches=mismatches,
        survivor_bars=int(counts["survivor_bars"]),
        victim_bars=int(counts["victim_bars"]),
        shared_days=shared,
        victim_only_bars=int(counts["victim_bars"]) - shared,
        disagreeing_days=int(overlap["disagreeing_days"]),
        disagreement_sample=sample,
        volume_only_disagreements=int(overlap["volume_only"]),
        victim_actions=int(counts["victim_actions"]),
        colliding_actions=int(counts["colliding_actions"]),
        victim_flags=int(counts["victim_flags"]),
    )


class MergeReport(NamedTuple):
    """What a completed merge actually moved."""

    plan: MergePlan
    bars_moved: int
    bars_dropped: int
    actions_moved: int
    actions_dropped: int
    flags_moved: int
    flags_dropped: int


def merge_security(
    db: Database, *, victim_id: int, survivor_id: int, force: bool = False
) -> MergeReport:
    """Absorb one security into another when BOTH carry history.

    The survivor keeps its security_id and gains everything the victim held that it
    did not already have; the victim row is deleted. On the overlap the **survivor
    wins** -- it is the row with the long history, and preferring the newly minted
    duplicate's copy of a session both rows agree on would be a coin flip dressed up
    as a rule. The guard above is what makes that safe: the overlap has to agree
    before either copy is discarded.

    This does NOT move the ticker. The caller pairs it with
    :func:`retarget_symbol`, because the rename is a separate fact with its own
    effective date, and a merge triggered by something other than a rename should
    not silently move a symbol.

    Adjustment factors are dropped for the victim and left stale on the survivor:
    they are derived from the corporate actions this call just changed, so they must
    be recomputed (``fafnir adjust --symbol ...``) rather than merged. Leaving them
    stale is visible and fixable; merging two derived series would not be.

    ``force`` overrides the guards and is for the case an operator has looked at the
    evidence and decided anyway -- it is not a retry.
    """
    plan = compare_securities(db, survivor_id=survivor_id, victim_id=victim_id)
    if not force and plan.blockers:
        raise MergeRefused(
            "refusing to merge: " + "; ".join(plan.blockers),
            plan,
        )

    # Bars first. ON CONFLICT DO NOTHING is the survivor-wins rule: a session both
    # rows hold keeps the survivor's copy, and the guard has already established
    # the two copies say the same thing.
    bars_moved = db.execute(
        """
        INSERT INTO core.daily_price
            (security_id, trade_date, open, high, low, close, volume, vwap,
             source, ingestion_run_id, loaded_at)
        SELECT %s, trade_date, open, high, low, close, volume, vwap,
               source, ingestion_run_id, loaded_at
          FROM core.daily_price WHERE security_id = %s
        ON CONFLICT (security_id, trade_date) DO NOTHING
        """,
        (survivor_id, victim_id),
    )
    db.execute("DELETE FROM core.daily_price WHERE security_id = %s", (victim_id,))

    # Corporate actions carry UNIQUE (security_id, action_type, ex_date), so the
    # victim's duplicates of actions the survivor already has must go before the
    # repoint rather than aborting it.
    actions_dropped = db.execute(
        """
        DELETE FROM core.corporate_action v
         WHERE v.security_id = %s
           AND EXISTS (SELECT 1 FROM core.corporate_action s
                        WHERE s.security_id = %s
                          AND s.action_type = v.action_type
                          AND s.ex_date = v.ex_date)
        """,
        (victim_id, survivor_id),
    )
    actions_moved = db.execute(
        "UPDATE core.corporate_action SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )

    # Derived from the actions just moved -- recomputed, never merged.
    db.execute(
        "DELETE FROM core.adjustment_factor WHERE security_id = %s", (victim_id,)
    )

    # Same rule as fold_empty_security: drop the victim's open flags the survivor
    # already carries before repointing the rest, or the repoint lands a second open
    # flag on a condition that already has one -- a ux_dq_flag_open_condition (0016)
    # violation that would abort the whole merge. price_* is excluded because its
    # repeats are load-bearing (count_price_quarantines bounds the watermark on
    # them).
    flags_dropped = db.execute(
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
    flags_moved = db.execute(
        "UPDATE ops.data_quality_flag SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )

    # Operator overrides (0025) follow the rows they describe, or the loaders would
    # start re-inserting what an operator removed from the victim's history. Where
    # the survivor already carries an active edit of the same kind on the same key,
    # the survivor's wins, like everything else here: the victim's is revoked (kept
    # as a record) rather than colliding with ux_operator_override_active.
    db.execute(
        """
        UPDATE ops.operator_override v
           SET revoked_at = now(), revoked_by = 'merge',
               revoked_note = 'superseded by the same edit on security ' || %s
         WHERE v.security_id = %s AND v.revoked_at IS NULL
           AND EXISTS (SELECT 1 FROM ops.operator_override s
                        WHERE s.security_id = %s AND s.revoked_at IS NULL
                          AND s.target = v.target
                          AND s.action_type IS NOT DISTINCT FROM v.action_type
                          AND s.key_date = v.key_date
                          AND s.operation = v.operation)
        """,
        (survivor_id, victim_id, survivor_id),
    )
    db.execute(
        "UPDATE ops.operator_override SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )

    # The victim's watermark is usually the *fresher* of the two -- it is the row the
    # daily load has been feeding since the duplicate was minted. Taking the later
    # date per endpoint stops the next incremental load from re-fetching a tail the
    # survivor now already holds. GREATEST ignores NULLs, so an endpoint only one
    # side has is carried across intact.
    db.execute(
        """
        INSERT INTO ops.load_watermark
            (source, endpoint, security_id, last_loaded_date, last_run_at)
        SELECT source, endpoint, %s, last_loaded_date, last_run_at
          FROM ops.load_watermark WHERE security_id = %s
        ON CONFLICT (source, endpoint, security_id) DO UPDATE
           SET last_loaded_date = GREATEST(ops.load_watermark.last_loaded_date,
                                           EXCLUDED.last_loaded_date),
               last_run_at      = GREATEST(ops.load_watermark.last_run_at,
                                           EXCLUDED.last_run_at),
               updated_at       = now()
        """,
        (survivor_id, victim_id),
    )
    db.execute("DELETE FROM ops.load_watermark WHERE security_id = %s", (victim_id,))

    # Then the remaining real FKs.
    db.execute(
        "UPDATE core.symbol_change SET security_id = %s WHERE security_id = %s",
        (survivor_id, victim_id),
    )
    # The victim's xref rows are dropped rather than repointed: the ticker they name
    # is the one the caller is about to open a fresh period for against the
    # survivor, with the rename's own effective date. Keeping them would leave two
    # open periods for one symbol and make point-in-time resolution ambiguous.
    db.execute("DELETE FROM core.symbol_xref WHERE security_id = %s", (victim_id,))
    # Attributes only; the next security-master load rewrites the survivor's.
    db.execute("DELETE FROM core.company_profile WHERE security_id = %s", (victim_id,))
    db.execute("DELETE FROM core.security WHERE security_id = %s", (victim_id,))

    return MergeReport(
        plan=plan,
        bars_moved=bars_moved,
        bars_dropped=plan.victim_bars - bars_moved,
        actions_moved=actions_moved,
        actions_dropped=actions_dropped,
        flags_moved=flags_moved,
        flags_dropped=flags_dropped,
    )


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


def dismiss_symbol_change(
    db: Database,
    *,
    old_symbol: str,
    new_symbol: str,
    change_date: Optional[date] = None,
    note: str,
    dismissed_by: str,
    source: str = "fmp",
) -> list[dict]:
    """Record that a reported rename is not a rename, so the sweep stops retrying it.

    Some conflicts can never clear themselves. `apply_symbol_change` refuses when
    the target ticker is held by a live security with its own history, which is the
    right call -- but when the feed row is simply wrong, that refusal repeats every
    night and the rename sits in an `error`-severity queue forever. The two families
    seen in production: a pre-launch ticker shuffle among securities that all went
    on to trade concurrently, and the same rename emitted in both directions.

    This is a judgement about the *feed*, not about the data. A rename that is real
    but blocked by a duplicate needs :func:`merge_security` instead, which reaches
    `applied` -- the point of keeping the two paths separate is that `dismissed`
    must never become the convenient way to silence a rename that should have been
    carried out.

    Only a non-terminal row can be dismissed, so this cannot overwrite an `applied`
    or `ignored` decision. ``change_date`` narrows to one feed row; omitted, it
    dismisses every unapplied row for the pair, which is what an operator looking at
    one bad ticker actually means. Returns the rows dismissed -- empty when nothing
    matched, which the caller reports rather than treating as success.
    """
    import json

    old_symbol = (old_symbol or "").strip().upper()
    new_symbol = (new_symbol or "").strip().upper()
    provenance = json.dumps(
        {
            "dismissed_by": dismissed_by,
            "dismissed_note": note,
        }
    )
    return db.fetchall(
        """
        UPDATE core.symbol_change
           SET status     = %s,
               -- COALESCE, not `detail ||`: detail is nullable, and NULL || jsonb
               -- is NULL, which would silently drop the provenance this write
               -- exists to keep.
               detail     = COALESCE(detail, '{}'::jsonb)
                            || %s::jsonb
                            || jsonb_build_object('dismissed_at', now()::text),
               updated_at = now()
         WHERE source     = %s
           AND old_symbol = %s
           AND new_symbol = %s
           AND (%s::date IS NULL OR change_date = %s::date)
           AND status <> ALL(%s)
     RETURNING old_symbol, new_symbol, change_date, status, security_id
        """,
        (
            CHANGE_DISMISSED,
            provenance,
            source,
            old_symbol,
            new_symbol,
            change_date,
            change_date,
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


def delisted_securities(db: Database, source: str = "fmp") -> dict[str, list[dict]]:
    """Every delisted security, grouped by the ticker it retired under.

    The mirror of :func:`listed_securities`, and needed for the same reason from
    the other side. The upsert arbitrates on the *partial* unique index over
    ``delisted_date IS NULL`` (0009), so a delisted row is invisible to it: a
    vendor list that still carries a retired name looks exactly like a brand-new
    listing, and inserting it mints a second security_id for a company already
    held. Only the caller can tell those two cases apart, and only if it can see
    what was retired -- which is what this returns.

    A list per symbol, not one row: a ticker can be retired more than once, and the
    caller weighs the incoming name against every retirement it has.
    """
    out: dict[str, list[dict]] = {}
    for row in db.fetchall(
        """
        SELECT security_id, primary_symbol, company_name, delisted_date
          FROM core.security
         WHERE delisted_date IS NOT NULL AND source = %s
        """,
        (source,),
    ):
        out.setdefault(row["primary_symbol"], []).append(
            {
                "security_id": row["security_id"],
                "company_name": row["company_name"],
                "delisted_date": row["delisted_date"],
            }
        )
    return out


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


def security_price_profile(db: Database, security_id: int) -> Optional[dict]:
    """What the price loader needs to know about a security before reading its bars.

    ``asset_type`` and ``is_fund`` decide whether a bar is judged as an exchange
    session or a NAV strike; ``exchange_code`` picks the calendar that says which
    dates were sessions at all. One read per symbol, not per bar. None if the
    security does not exist.
    """
    row = db.fetchone(
        "SELECT asset_type, is_fund, exchange_code FROM core.security "
        "WHERE security_id = %s",
        (security_id,),
    )
    return dict(row) if row else None


def open_sessions(
    db: Database, exchange_code: str, start: date, end: date
) -> Optional[tuple[frozenset, date, date]]:
    """The open sessions of one venue's calendar between two dates, with its span.

    Returns ``(open_dates, first, last)`` where ``first``/``last`` bound everything
    ref.trading_calendar holds for the venue -- not just the requested window --
    because the seed writes open days only: a weekend has no row at all, so "no row"
    means *closed* inside the calendar's span and *unknown* outside it, and only the
    span can tell those apart. None when the venue has no calendar rows at all.
    """
    bounds = db.fetchone(
        "SELECT min(trade_date) AS first, max(trade_date) AS last "
        "FROM ref.trading_calendar WHERE exchange_code = %s",
        (exchange_code,),
    )
    if not bounds or bounds["first"] is None:
        return None
    rows = db.fetchall(
        """
        SELECT trade_date FROM ref.trading_calendar
         WHERE exchange_code = %s AND is_open AND trade_date BETWEEN %s AND %s
        """,
        (exchange_code, start, end),
    )
    return frozenset(r["trade_date"] for r in rows), bounds["first"], bounds["last"]


# ---------------------------------------------------------------------------
# Declared universe (ref.tracked_symbol, migration 0019 / ADR 0006)
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
) -> bool:
    """Upsert one corporate action. Returns True if the row was new or CHANGED.

    The return value is not decoration: it is the changed-set that lets `fafnir
    adjust --changed` recompute the few securities whose actions actually moved
    instead of every security that has ever had one. So the DO UPDATE carries a
    WHERE that suppresses a no-op write -- re-loading an unchanged history now
    reports 0 rows and leaves `loaded_at` / `ingestion_run_id` alone, which is what
    makes "this run touched these securities" a true statement rather than "this
    run looked at these securities". A row is compared on its mutable columns only;
    the three identity columns are the conflict key and cannot differ.

    ``ingestion_run_id`` is updated too (it was not before), so it names the run
    that last *changed* the row -- the question anyone reading lineage is asking.

    A row an operator wrote (``source = 'operator'``, see :func:`add_operator_action`)
    is never overwritten: the operator wrote it because the feed has this event wrong
    or missing, and a vendor copy arriving at the same key is the thing being
    corrected, not a correction.
    """
    return (
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
            ingestion_run_id = EXCLUDED.ingestion_run_id,
            loaded_at = now()
        WHERE core.corporate_action.source <> 'operator'
          AND (core.corporate_action.record_date,
               core.corporate_action.payment_date,
               core.corporate_action.declaration_date,
               core.corporate_action.split_numerator,
               core.corporate_action.split_denominator,
               core.corporate_action.dividend_amount,
               core.corporate_action.currency)
              IS DISTINCT FROM
              (EXCLUDED.record_date,
               EXCLUDED.payment_date,
               EXCLUDED.declaration_date,
               EXCLUDED.split_numerator,
               EXCLUDED.split_denominator,
               EXCLUDED.dividend_amount,
               EXCLUDED.currency)
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
        > 0
    )


def corporate_actions_for(db: Database, security_id: int) -> list[dict]:
    return db.fetchall(
        """
        SELECT action_type, ex_date, split_numerator, split_denominator,
               dividend_amount, source
        FROM core.corporate_action
        WHERE security_id = %s
        ORDER BY ex_date ASC
        """,
        (security_id,),
    )


def delete_corporate_action(
    db: Database, *, security_id: int, action_type: str, ex_date: date
) -> bool:
    """Delete one corporate action. Returns True when a row was removed.

    The one caller is the reconciliation, and only for a dividend the per-symbol feed
    has *re-dated* (see ``fafnir.ingest.corporate_actions._redated_dividends``). Any
    other action the feed stops carrying is reported and kept: the feed can drop a
    real dividend, and a loader that deleted on every disagreement would be trusting
    the vendor on its worst day. Adjustment factors are derived from these rows and
    no row is left stamped with the run, so the caller recomputes them itself.
    """
    return (
        db.execute(
            """
        DELETE FROM core.corporate_action
        WHERE security_id = %s AND action_type = %s AND ex_date = %s
        """,
            (security_id, action_type, ex_date),
        )
        > 0
    )


# ---------------------------------------------------------------------------
# Operator overrides (migration 0025): corrections to vendor rows that the loaders
# must not undo
# ---------------------------------------------------------------------------

OPERATOR_SOURCE = "operator"


class OverrideRefused(RuntimeError):
    """An operator edit that would leave the rows, or their record, ambiguous."""


def _jsonable(row: dict) -> dict:
    """A row as JSON-safe scalars: exact decimals as strings, dates as ISO."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, date):  # datetime is a date subclass
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _insert_override(
    db: Database,
    *,
    security_id: int,
    target: str,
    action_type: Optional[str],
    key_date: date,
    operation: str,
    detail: dict,
    note: str,
    created_by: str,
) -> int:
    import json

    return int(
        db.fetchval(
            """
            INSERT INTO ops.operator_override
                (security_id, target, action_type, key_date, operation, detail,
                 note, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING override_id
            """,
            (
                security_id,
                target,
                action_type,
                key_date,
                operation,
                json.dumps(detail, default=str),
                note,
                created_by,
            ),
        )
    )


def corporate_action_by_id(db: Database, corporate_action_id: int) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT ca.corporate_action_id, ca.security_id, s.primary_symbol,
               ca.action_type, ca.ex_date, ca.record_date, ca.payment_date,
               ca.declaration_date, ca.split_numerator, ca.split_denominator,
               ca.dividend_amount, ca.currency, ca.source, ca.ingestion_run_id,
               ca.loaded_at
          FROM core.corporate_action ca
          JOIN core.security s USING (security_id)
         WHERE ca.corporate_action_id = %s
        """,
        (corporate_action_id,),
    )


def corporate_action_at(
    db: Database, *, security_id: int, action_type: str, ex_date: date
) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT corporate_action_id, action_type, ex_date, split_numerator,
               split_denominator, dividend_amount, source
          FROM core.corporate_action
         WHERE security_id = %s AND action_type = %s AND ex_date = %s
        """,
        (security_id, action_type, ex_date),
    )


def list_corporate_actions(
    db: Database,
    security_id: int,
    *,
    action_type: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> list[dict]:
    """A security's actions with the ids the operator commands take."""
    clauses, params = ["security_id = %s"], [security_id]
    if action_type:
        clauses.append("action_type = %s")
        params.append(action_type)
    if from_date:
        clauses.append("ex_date >= %s")
        params.append(from_date)
    if to_date:
        clauses.append("ex_date <= %s")
        params.append(to_date)
    return db.fetchall(
        f"""
        SELECT corporate_action_id, action_type, ex_date, split_numerator,
               split_denominator, dividend_amount, currency, source, loaded_at
          FROM core.corporate_action
         WHERE {' AND '.join(clauses)}
         ORDER BY ex_date, action_type
        """,
        params,
    )


def suppressed_action_keys(
    db: Database, security_id: Optional[int] = None
) -> set[tuple[int, str, date]]:
    """``(security_id, action_type, ex_date)`` for every action an operator deleted.

    The loaders set a vendor row at one of these keys aside instead of upserting it.
    The table holds a handful of rows, so the calendar sweep reads it whole once and
    the per-symbol path reads one security's slice.
    """
    sql = """
        SELECT security_id, action_type, key_date
          FROM ops.operator_override
         WHERE target = 'corporate_action' AND operation = 'delete'
           AND revoked_at IS NULL
    """
    params: list[Any] = []
    if security_id is not None:
        sql += " AND security_id = %s"
        params.append(security_id)
    return {
        (int(r["security_id"]), r["action_type"], r["key_date"])
        for r in db.fetchall(sql, params)
    }


def suppressed_price_dates(db: Database, security_id: int) -> frozenset[date]:
    """Trade dates of this security's bars an operator edited. The price loader sets
    a vendor bar on one of these dates aside instead of upserting it.

    Both operations count. A 'delete' keeps a removed bar out. An 'add' is a bar an
    operator re-dated or re-scaled (`fafnir prices shift|rescale`, migration 0026):
    the vendor still serves its own copy of that date -- the wrong-scale original, or
    whatever it has on the date a bar was moved to -- and upserting it would silently
    undo the correction.
    """
    return frozenset(
        r["key_date"]
        for r in db.fetchall(
            """
            SELECT DISTINCT key_date FROM ops.operator_override
             WHERE target = 'daily_price' AND revoked_at IS NULL AND security_id = %s
            """,
            (security_id,),
        )
    )


def _active_override(
    db: Database,
    *,
    security_id: int,
    target: str,
    action_type: Optional[str],
    key_date: date,
    operation: str,
) -> Optional[dict]:
    return db.fetchone(
        """
        SELECT override_id, note, created_by, created_at
          FROM ops.operator_override
         WHERE security_id = %s AND target = %s
           AND action_type IS NOT DISTINCT FROM %s
           AND key_date = %s AND operation = %s AND revoked_at IS NULL
        """,
        (security_id, target, action_type, key_date, operation),
    )


def delete_operator_action(
    db: Database,
    *,
    corporate_action_id: int,
    note: str,
    created_by: str,
    detail_extra: Optional[dict] = None,
) -> int:
    """Remove a corporate action and suppress its key. Returns the override id.

    Does not recompute adjustment factors: the caller does, once per security, after
    every edit in the batch has landed.
    """
    row = corporate_action_by_id(db, corporate_action_id)
    if row is None:
        raise OverrideRefused(f"No corporate action {corporate_action_id}.")
    if row["source"] == OPERATOR_SOURCE:
        added = _active_override(
            db,
            security_id=row["security_id"],
            target="corporate_action",
            action_type=row["action_type"],
            key_date=row["ex_date"],
            operation="add",
        )
        if added is not None:
            # Deleting it here would also suppress the key against the vendor,
            # which is not what undoing an addition means.
            raise OverrideRefused(
                f"Corporate action {corporate_action_id} was written by an operator "
                f"(override {added['override_id']}). Undo it with "
                f"`fafnir override revoke {added['override_id']}`."
            )
    override_id = _insert_override(
        db,
        security_id=row["security_id"],
        target="corporate_action",
        action_type=row["action_type"],
        key_date=row["ex_date"],
        operation="delete",
        detail={"row": _jsonable(row), **(detail_extra or {})},
        note=note,
        created_by=created_by,
    )
    db.execute(
        "DELETE FROM core.corporate_action WHERE corporate_action_id = %s",
        (corporate_action_id,),
    )
    return override_id


def add_operator_action(
    db: Database,
    *,
    security_id: int,
    action_type: str,
    ex_date: date,
    split_numerator: Optional[Decimal] = None,
    split_denominator: Optional[Decimal] = None,
    dividend_amount: Optional[Decimal] = None,
    currency: str = "USD",
    note: str,
    created_by: str,
    detail_extra: Optional[dict] = None,
) -> tuple[int, int]:
    """Write a corporate action the feed has wrong or missing.

    Returns ``(corporate_action_id, override_id)``. Refuses a key that already holds
    a row -- delete that one first, so the record says what was replaced -- and a
    future ex-date, for the reason the loader refuses one (see the module docstring of
    fafnir.ingest.corporate_actions).
    """
    if action_type == "split":
        if not (
            split_numerator
            and split_denominator
            and split_numerator > 0
            and split_denominator > 0
        ):
            raise OverrideRefused("A split needs a positive numerator and denominator.")
        dividend_amount = None
    elif action_type == "dividend":
        if dividend_amount is None or dividend_amount <= 0:
            raise OverrideRefused("A dividend needs a positive amount.")
        split_numerator = split_denominator = None
    else:
        raise OverrideRefused(f"Unknown action type {action_type!r}.")
    if ex_date > date.today():
        raise OverrideRefused(
            f"{ex_date} is in the future. A future ex-date back-adjusts today's "
            "prices for an event that has not happened; add it once it has gone ex."
        )
    existing = corporate_action_at(
        db, security_id=security_id, action_type=action_type, ex_date=ex_date
    )
    if existing is not None:
        raise OverrideRefused(
            f"Security {security_id} already has a {action_type} on {ex_date} "
            f"(corporate action {existing['corporate_action_id']}, source "
            f"{existing['source']}). Delete it first with `fafnir actions delete`."
        )
    action_id = int(
        db.fetchval(
            """
            INSERT INTO core.corporate_action
                (security_id, action_type, ex_date, split_numerator,
                 split_denominator, dividend_amount, currency, source, loaded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
            RETURNING corporate_action_id
            """,
            (
                security_id,
                action_type,
                ex_date,
                split_numerator,
                split_denominator,
                dividend_amount,
                currency,
                OPERATOR_SOURCE,
            ),
        )
    )
    override_id = _insert_override(
        db,
        security_id=security_id,
        target="corporate_action",
        action_type=action_type,
        key_date=ex_date,
        operation="add",
        detail={
            "row": _jsonable(
                {
                    "corporate_action_id": action_id,
                    "split_numerator": split_numerator,
                    "split_denominator": split_denominator,
                    "dividend_amount": dividend_amount,
                    "currency": currency,
                }
            ),
            **(detail_extra or {}),
        },
        note=note,
        created_by=created_by,
    )
    return action_id, override_id


def redate_operator_action(
    db: Database,
    *,
    corporate_action_id: int,
    new_ex_date: date,
    note: str,
    created_by: str,
) -> tuple[int, int, int]:
    """Move a corporate action to another ex-date.

    A delete of the old key (suppressed, so the feed's misdated copy cannot return)
    and an operator row at the new one, each naming the other. Returns
    ``(new_corporate_action_id, delete_override_id, add_override_id)``.
    """
    import json

    row = corporate_action_by_id(db, corporate_action_id)
    if row is None:
        raise OverrideRefused(f"No corporate action {corporate_action_id}.")
    if row["ex_date"] == new_ex_date:
        raise OverrideRefused(
            f"Corporate action {corporate_action_id} is already on {new_ex_date}."
        )
    # Checked before anything is written, so a refusal leaves no half of the pair.
    if new_ex_date > date.today():
        raise OverrideRefused(
            f"{new_ex_date} is in the future. A future ex-date back-adjusts today's "
            "prices for an event that has not happened."
        )
    clash = corporate_action_at(
        db,
        security_id=row["security_id"],
        action_type=row["action_type"],
        ex_date=new_ex_date,
    )
    if clash is not None:
        raise OverrideRefused(
            f"Security {row['security_id']} already has a {row['action_type']} on "
            f"{new_ex_date} (corporate action {clash['corporate_action_id']}). If "
            f"{corporate_action_id} is a duplicate of it, delete {corporate_action_id} "
            "instead of re-dating it."
        )
    delete_id = delete_operator_action(
        db,
        corporate_action_id=corporate_action_id,
        note=note,
        created_by=created_by,
        detail_extra={"redated_to": new_ex_date.isoformat()},
    )
    new_id, add_id = add_operator_action(
        db,
        security_id=row["security_id"],
        action_type=row["action_type"],
        ex_date=new_ex_date,
        split_numerator=row["split_numerator"],
        split_denominator=row["split_denominator"],
        dividend_amount=row["dividend_amount"],
        currency=row["currency"],
        note=note,
        created_by=created_by,
        detail_extra={
            "redated_from": row["ex_date"].isoformat(),
            "redated_from_override": delete_id,
        },
    )
    db.execute(
        "UPDATE ops.operator_override SET detail = detail || %s WHERE override_id = %s",
        (json.dumps({"redated_to_override": add_id}), delete_id),
    )
    return new_id, delete_id, add_id


def delete_operator_bars(
    db: Database,
    *,
    security_id: int,
    trade_dates: Sequence[date],
    note: str,
    created_by: str,
) -> list[int]:
    """Remove bars and suppress their dates. Returns one override id per bar removed.

    A date with no stored bar is skipped rather than suppressed: there is nothing to
    record the removal of, and suppressing a date the vendor has never sent would be
    a decision nobody made.

    A bar an operator wrote (`prices shift|rescale`) is refused, for the reason
    `delete_operator_action` refuses an operator action: deleting it here would leave
    its edit half-undone. `fafnir override revoke` undoes the edit instead.
    """
    written = active_price_adds(db, security_id, trade_dates)
    if written:
        first = min(written)
        raise OverrideRefused(
            f"{len(written)} bar(s) of security {security_id} "
            f"({', '.join(str(d) for d in sorted(written)[:5])}"
            f"{', ...' if len(written) > 5 else ''}) were written by an operator "
            f"(edit {written[first]['edit']}). Undo the edit with "
            f"`fafnir override revoke {written[first]['edit']}`."
        )
    ids: list[int] = []
    for d in sorted(set(trade_dates)):
        bar = db.fetchone(
            """
            SELECT trade_date, open, high, low, close, volume, vwap, source,
                   ingestion_run_id, loaded_at
              FROM core.daily_price WHERE security_id = %s AND trade_date = %s
            """,
            (security_id, d),
        )
        if bar is None:
            continue
        ids.append(
            _insert_override(
                db,
                security_id=security_id,
                target="daily_price",
                action_type=None,
                key_date=d,
                operation="delete",
                detail={"row": _jsonable(bar)},
                note=note,
                created_by=created_by,
            )
        )
        db.execute(
            "DELETE FROM core.daily_price WHERE security_id = %s AND trade_date = %s",
            (security_id, d),
        )
    return ids


# ---------------------------------------------------------------------------
# Bar transforms: `fafnir prices shift|rescale` (migration 0026)
# ---------------------------------------------------------------------------
PRICE_TRANSFORM_KINDS = ("shift", "rescale")

_BAR_COLUMNS = (
    "trade_date, open, high, low, close, volume, vwap, source, ingestion_run_id, "
    "loaded_at"
)


def stored_bars(
    db: Database, security_id: int, from_date: date, to_date: date
) -> list[dict]:
    """A security's stored bars in ``[from_date, to_date]``, every column, by date."""
    return db.fetchall(
        f"""
        SELECT {_BAR_COLUMNS} FROM core.daily_price
         WHERE security_id = %s AND trade_date BETWEEN %s AND %s
         ORDER BY trade_date
        """,
        (security_id, from_date, to_date),
    )


def active_price_adds(
    db: Database, security_id: int, trade_dates: Iterable[date]
) -> dict[date, dict]:
    """``{trade_date: {"override_id", "edit", "kind"}}`` for the operator-written bars
    among these dates -- the ones a vendor load, a delete or a second transform must
    leave alone."""
    dates = sorted(set(trade_dates))
    if not dates:
        return {}
    rows = db.fetchall(
        """
        SELECT key_date, override_id,
               (detail->'transform'->>'edit')::bigint AS edit,
               detail->'transform'->>'kind' AS kind
          FROM ops.operator_override
         WHERE security_id = %s AND target = 'daily_price' AND operation = 'add'
           AND revoked_at IS NULL AND key_date = ANY(%s)
        """,
        (security_id, dates),
    )
    return {
        r["key_date"]: {
            "override_id": int(r["override_id"]),
            "edit": int(r["edit"]) if r["edit"] is not None else None,
            "kind": r["kind"],
        }
        for r in rows
    }


def active_price_overrides_on(
    db: Database, security_id: int, trade_dates: Iterable[date]
) -> dict[date, list[dict]]:
    """Every active bar override on these dates, of either operation."""
    dates = sorted(set(trade_dates))
    if not dates:
        return {}
    out: dict[date, list[dict]] = {}
    for r in db.fetchall(
        """
        SELECT key_date, override_id, operation
          FROM ops.operator_override
         WHERE security_id = %s AND target = 'daily_price' AND revoked_at IS NULL
           AND key_date = ANY(%s)
         ORDER BY override_id
        """,
        (security_id, dates),
    ):
        out.setdefault(r["key_date"], []).append(dict(r))
    return out


def replace_operator_bars(
    db: Database,
    *,
    security_id: int,
    kind: str,
    params: dict,
    changes: Sequence[tuple[dict, dict]],
    note: str,
    created_by: str,
) -> tuple[int, list[int]]:
    """Replace vendor bars with transformed copies, recorded as one edit.

    ``changes`` pairs each stored bar (as :func:`stored_bars` returns it) with the row
    to write in its place -- same date for a rescale, another date for a shift. Every
    pair becomes a 'delete' override at the old date (the row as it stood) and an
    'add' at the new one (the row written, ``source = operator``), both carrying
    ``detail.transform`` with ``kind``, ``params``, the other half's id and ``edit``:
    the id of the edit's first override, which is how `override revoke` finds the
    rest. Returns ``(edit_id, override_ids)``.

    Validation is the caller's (fafnir.ingest.price_edits plans and checks every
    row); this writes what it is given. All old rows are removed before any new one
    is written, so a shift whose targets overlap its sources does not collide with
    itself on the primary key.
    """
    import json

    if kind not in PRICE_TRANSFORM_KINDS:
        raise OverrideRefused(f"Unknown bar transform {kind!r}.")
    if not changes:
        raise OverrideRefused("No bars to replace.")
    olds = [old for old, _ in changes]
    news = [new for _, new in changes]
    if len({o["trade_date"] for o in olds}) != len(olds) or len(
        {n["trade_date"] for n in news}
    ) != len(news):
        raise OverrideRefused("Two bars of one edit share a date.")

    transform = {"kind": kind, **params}
    delete_rows = db.fetchall(
        """
        INSERT INTO ops.operator_override
            (security_id, target, action_type, key_date, operation, detail, note,
             created_by)
        SELECT %s, 'daily_price', NULL, x.key_date, 'delete', x.detail, %s, %s
          FROM jsonb_to_recordset(%s::jsonb) AS x(key_date date, detail jsonb)
         ORDER BY x.key_date
        RETURNING override_id, key_date
        """,
        (
            security_id,
            note,
            created_by,
            json.dumps(
                [
                    {
                        "key_date": o["trade_date"].isoformat(),
                        "detail": {"row": _jsonable(o), "transform": transform},
                    }
                    for o in olds
                ],
                default=str,
            ),
        ),
    )
    delete_ids = {r["key_date"]: int(r["override_id"]) for r in delete_rows}
    edit_id = min(delete_ids.values())

    db.execute(
        "DELETE FROM core.daily_price WHERE security_id = %s AND trade_date = ANY(%s)",
        (security_id, [o["trade_date"] for o in olds]),
    )
    db.executemany(
        """
        INSERT INTO core.daily_price
            (security_id, trade_date, open, high, low, close, volume, vwap, source,
             ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NULL, now())
        """,
        [
            (
                security_id,
                n["trade_date"],
                n["open"],
                n["high"],
                n["low"],
                n["close"],
                n["volume"],
                n.get("vwap"),
                OPERATOR_SOURCE,
            )
            for n in news
        ],
    )

    add_rows = db.fetchall(
        """
        INSERT INTO ops.operator_override
            (security_id, target, action_type, key_date, operation, detail, note,
             created_by)
        SELECT %s, 'daily_price', NULL, x.key_date, 'add', x.detail, %s, %s
          FROM jsonb_to_recordset(%s::jsonb) AS x(key_date date, detail jsonb)
         ORDER BY x.key_date
        RETURNING override_id, key_date
        """,
        (
            security_id,
            note,
            created_by,
            json.dumps(
                [
                    {
                        "key_date": new["trade_date"].isoformat(),
                        "detail": {
                            "row": _jsonable(
                                {k: v for k, v in new.items() if k != "security_id"}
                            ),
                            "transform": {
                                **transform,
                                "edit": edit_id,
                                "from_date": old["trade_date"].isoformat(),
                                "from_override": delete_ids[old["trade_date"]],
                            },
                        },
                    }
                    for old, new in changes
                ],
                default=str,
            ),
        ),
    )
    add_ids = {r["key_date"]: int(r["override_id"]) for r in add_rows}
    db.execute(
        """
        UPDATE ops.operator_override o
           SET detail = jsonb_set(o.detail, '{transform}',
                                  (o.detail->'transform') || x.extra)
          FROM jsonb_to_recordset(%s::jsonb) AS x(override_id bigint, extra jsonb)
         WHERE o.override_id = x.override_id
        """,
        (
            json.dumps(
                [
                    {
                        "override_id": delete_ids[old["trade_date"]],
                        "extra": {
                            "edit": edit_id,
                            "to_date": new["trade_date"].isoformat(),
                            "to_override": add_ids[new["trade_date"]],
                        },
                    }
                    for old, new in changes
                ]
            ),
        ),
    )
    return edit_id, sorted([*delete_ids.values(), *add_ids.values()])


def override_by_id(db: Database, override_id: int) -> Optional[dict]:
    return db.fetchone(
        "SELECT * FROM ops.operator_override WHERE override_id = %s", (override_id,)
    )


def price_edit_of(override: dict) -> Optional[int]:
    """The edit a bar-transform override belongs to, or None for any other override."""
    if override.get("target") != "daily_price":
        return None
    transform = (override.get("detail") or {}).get("transform")
    if not transform or transform.get("edit") is None:
        return None
    return int(transform["edit"])


def price_edit_overrides(
    db: Database, *, security_id: int, edit_id: int, include_revoked: bool = False
) -> list[dict]:
    """Every override of one bar-transform edit, oldest first."""
    return db.fetchall(
        f"""
        SELECT * FROM ops.operator_override
         WHERE security_id = %s AND target = 'daily_price'
           AND (detail->'transform'->>'edit')::bigint = %s
           {'' if include_revoked else 'AND revoked_at IS NULL'}
         ORDER BY override_id
        """,
        (security_id, edit_id),
    )


def revoke_price_edit(
    db: Database, *, security_id: int, edit_id: int, note: str, revoked_by: str
) -> list[dict]:
    """Undo a whole `prices shift|rescale` edit, restoring the vendor bars it replaced.

    Unlike revoking a plain 'delete', this writes the removed rows back. A plain delete
    removed a bar the operator judged worthless, so leaving the key for the vendor to
    refill is the undo. A transform removed a bar only to put a corrected copy of it
    in its place; lifting the suppression without restoring the original would turn
    the undo into a deletion of the history, and the vendor overlap never re-reads an
    old window to refill it. The restored row is the vendor's own, as it stood, so a
    later load of that key writes the same values over it -- there is no second,
    competing version.

    The operator bars are removed first and the originals written after, so a shift
    whose targets overlap its sources restores cleanly. A key another edit has since
    written a bar onto is refused rather than overwritten. Returns the overrides
    revoked. Adjustment factors are the caller's to recompute.
    """
    rows = price_edit_overrides(db, security_id=security_id, edit_id=edit_id)
    if not rows:
        raise OverrideRefused(
            f"No active overrides of bar edit {edit_id} on security {security_id}."
        )
    adds = [r for r in rows if r["operation"] == "add"]
    deletes = [r for r in rows if r["operation"] == "delete"]
    db.execute(
        """
        DELETE FROM core.daily_price
         WHERE security_id = %s AND trade_date = ANY(%s) AND source = %s
        """,
        (security_id, [r["key_date"] for r in adds], OPERATOR_SOURCE),
    )
    restore_dates = [r["key_date"] for r in deletes]
    blocking = db.fetchall(
        """
        SELECT trade_date, source FROM core.daily_price
         WHERE security_id = %s AND trade_date = ANY(%s)
         ORDER BY trade_date
        """,
        (security_id, restore_dates),
    )
    if blocking:
        raise OverrideRefused(
            f"Cannot restore {len(blocking)} bar(s) of edit {edit_id}: security "
            f"{security_id} already has a bar on "
            f"{', '.join(str(b['trade_date']) for b in blocking[:5])}"
            f"{', ...' if len(blocking) > 5 else ''} (source "
            f"{blocking[0]['source']}). Revoke the edit that wrote it first."
        )
    db.executemany(
        """
        INSERT INTO core.daily_price
            (security_id, trade_date, open, high, low, close, volume, vwap, source,
             ingestion_run_id, loaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            (
                security_id,
                r["detail"]["row"]["trade_date"],
                r["detail"]["row"]["open"],
                r["detail"]["row"]["high"],
                r["detail"]["row"]["low"],
                r["detail"]["row"]["close"],
                r["detail"]["row"].get("volume", 0),
                r["detail"]["row"].get("vwap"),
                r["detail"]["row"].get("source") or "fmp",
                r["detail"]["row"].get("ingestion_run_id"),
                r["detail"]["row"].get("loaded_at") or datetime.now().isoformat(),
            )
            for r in deletes
        ],
    )
    db.execute(
        """
        UPDATE ops.operator_override
           SET revoked_at = now(), revoked_by = %s, revoked_note = %s
         WHERE override_id = ANY(%s)
        """,
        (revoked_by, note, [int(r["override_id"]) for r in rows]),
    )
    return rows


def list_operator_overrides(
    db: Database,
    *,
    security_id: Optional[int] = None,
    include_revoked: bool = False,
) -> list[dict]:
    clauses, params = ["TRUE"], []
    if security_id is not None:
        clauses.append("o.security_id = %s")
        params.append(security_id)
    if not include_revoked:
        clauses.append("o.revoked_at IS NULL")
    return db.fetchall(
        f"""
        SELECT o.override_id, o.security_id, s.primary_symbol, o.target,
               o.action_type, o.key_date, o.operation, o.detail, o.note,
               o.created_by, o.created_at, o.revoked_at, o.revoked_by,
               o.revoked_note
          FROM ops.operator_override o
          LEFT JOIN core.security s USING (security_id)
         WHERE {' AND '.join(clauses)}
         ORDER BY o.override_id
        """,
        params,
    )


def revoke_operator_override(
    db: Database, *, override_id: int, note: str, revoked_by: str
) -> dict:
    """Undo one edit, keeping its record. Returns the override as it stood.

    Revoking a 'delete' only lifts the suppression: the removed row is not written
    back, because the next load writes whatever the vendor serves for that key and a
    restored copy would be a second, competing version of it. Revoking an 'add'
    removes the operator's row. Adjustment factors are the caller's to recompute.
    """
    row = db.fetchone(
        "SELECT * FROM ops.operator_override WHERE override_id = %s", (override_id,)
    )
    if row is None:
        raise OverrideRefused(f"No override {override_id}.")
    if row["revoked_at"] is not None:
        raise OverrideRefused(
            f"Override {override_id} was already revoked by {row['revoked_by']} "
            f"at {row['revoked_at']}."
        )
    edit = price_edit_of(row)
    if edit is not None:
        # One half of a re-dated or re-scaled bar. Revoking it alone would either
        # strand the operator's bar without its record or lift the suppression under
        # it; the edit is undone whole, by revoke_price_edit.
        raise OverrideRefused(
            f"Override {override_id} is part of bar edit {edit} "
            f"({row['detail']['transform'].get('kind')}); it is undone as a whole with "
            "revoke_price_edit (`fafnir override revoke` does this for you)."
        )
    split = (row["detail"] or {}).get("split")
    if split:
        # One key of a history split. Revoking it alone lifts the suppression while
        # the destination keeps its copy, so the next load of the ticker's full
        # history puts the vendor's row back on the source and the same session is
        # then held by two securities. `--undo` reads the active split markers, so it
        # can no longer see this key to put it right either. A split is undone whole.
        src, dest = split.get("source_security_id"), split.get(
            "destination_security_id"
        )
        raise OverrideRefused(
            f"Override {override_id} is one key of the history split that moved "
            f"security {src}'s history to {dest}; revoking it alone would leave the "
            "same session on both securities. Undo the split as a whole: "
            f"`fafnir security split-history --security-id {src} "
            f"--into-security-id {dest} --undo -m '<why>'`."
        )
    if row["operation"] == "add":
        db.execute(
            """
            DELETE FROM core.corporate_action
             WHERE security_id = %s AND action_type = %s AND ex_date = %s
               AND source = %s
            """,
            (row["security_id"], row["action_type"], row["key_date"], OPERATOR_SOURCE),
        )
    db.execute(
        """
        UPDATE ops.operator_override
           SET revoked_at = now(), revoked_by = %s, revoked_note = %s
         WHERE override_id = %s
        """,
        (revoked_by, note, override_id),
    )
    return row


# ---------------------------------------------------------------------------
# Splitting a security's history: two issuers on one row
# ---------------------------------------------------------------------------
#
# The vendor serves a ticker's whole history, so a ticker that has been reused
# arrives as one continuous series: Sotheby's 2003-2019 and a SPAC listed in 2026,
# both on BID. The price loader resolves the ticker to the one security that holds
# it and stores everything there. `security merge` is the repair for one instrument
# held on two rows; this is the opposite repair, for two instruments held on one.
#
# A split moves a date range of bars and corporate actions to another security and
# leaves an active 'delete' override (0025) on the source for every key it moved,
# so the next load of the ticker's full history sets the vendor's copy aside instead
# of putting it back. Each of those overrides carries a `split` marker in `detail`
# naming both securities, which is what the undo reads.
#
# The destination is either an existing security (the old issuer is already held
# elsewhere) or a row minted here with source = 'operator'. An operator-minted
# security is never fed by the vendor loaders -- see VENDOR_FED_SECURITY -- because
# the ticker it carries now belongs to someone else: a per-symbol pull for it would
# fetch the new issuer's history into the old issuer's row.

# The predicate every loader universe and every ticker-grouping check applies. An
# operator-minted security holds a history the vendor serves under a ticker it has
# since given to another issuer, so a per-symbol pull, a reconciliation or a
# "two rows share this ticker" check has nothing true to say about it.
VENDOR_FED_SECURITY = "source <> 'operator'"

# What counts as the date a DQ flag is about. The per-session checks key on
# trade_date, stale on last_date, the price loader's quarantines on date.
_FLAG_DATE_SQL = (
    "(CASE WHEN COALESCE(f.record_key->>'trade_date', f.record_key->>'date', "
    "f.record_key->>'last_date') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
    "THEN COALESCE(f.record_key->>'trade_date', f.record_key->>'date', "
    "f.record_key->>'last_date')::date END)"
)

_SPLIT_PRICE_FIELDS = ("open", "high", "low", "close")
_SPLIT_VOLUME_FIELDS = ("volume", "vwap")


def _bar_field(value) -> Optional[Decimal]:
    """A bar field as a comparable number, NULL-safe. NULL is not zero: a vendor that
    sends no vwap and one that sends 0 disagree, and neither is the other's copy."""
    return None if value is None else Decimal(str(value))


_SPLIT_ACTION_FIELDS = (
    "split_numerator",
    "split_denominator",
    "dividend_amount",
    "currency",
)


def is_operator_security(db: Database, security_id: int) -> bool:
    """True for a security minted by `security split-history`, which no loader feeds."""
    return bool(
        db.fetchval(
            "SELECT source = %s FROM core.security WHERE security_id = %s",
            (OPERATOR_SOURCE, security_id),
        )
    )


class SplitPlan(NamedTuple):
    """What a history split would move, read before anything is written.

    ``new_security`` is the identity to mint (a dict of core.security columns) when
    the destination does not exist yet; ``destination_id`` is None in that case.
    """

    source_id: int
    source_symbol: str
    destination_id: Optional[int]
    destination_symbol: str
    new_security: Optional[dict]
    first_date: date
    last_date: date
    bars: list[dict]
    restorable: list[dict]
    actions: list[dict]
    duplicate_bar_dates: list[date]
    duplicate_action_keys: list[tuple[str, date]]
    flags: int
    bars_kept_before: Optional[dict]
    bars_kept_after: Optional[dict]
    remaining_bars: int

    @property
    def moves_anything(self) -> bool:
        return bool(self.bars or self.restorable or self.actions)


class SplitReport(NamedTuple):
    plan: SplitPlan
    destination_id: int
    bars_moved: int
    bars_restored: int
    bars_duplicate: int
    actions_moved: int
    actions_duplicate: int
    flags_moved: int
    flags_dropped: int
    override_ids: list[int]


class UnsplitReport(NamedTuple):
    source_id: int
    destination_id: int
    bars_returned: int
    bars_unrestored: int
    actions_returned: int
    flags_returned: int
    destination_deleted: bool


def _split_marker(source_id: int, destination_id: int, kind: str) -> dict:
    return {
        "split": {
            "source_security_id": source_id,
            "destination_security_id": destination_id,
            "kind": kind,
        }
    }


def _refuse_keys_already_overridden(
    db: Database,
    *,
    source_id: int,
    bar_dates: Sequence[date],
    action_keys: Sequence[tuple[str, date]],
) -> None:
    """Refuse a split over a key that already holds an active 'delete' override.

    Named by date, and with the remedy that fits: a bar edit (0026) is revoked whole,
    anything else one override at a time.
    """
    rows = db.fetchall(
        """
        SELECT override_id, target, action_type, key_date,
               (detail ? 'transform') AS is_edit,
               detail->'transform'->>'edit' AS edit
          FROM ops.operator_override
         WHERE security_id = %s AND revoked_at IS NULL AND operation = 'delete'
           AND (
                (target = 'daily_price' AND key_date = ANY(%s))
             OR (target = 'corporate_action'
                 AND (action_type, key_date) IN (
                     SELECT t.a, t.d
                       FROM unnest(%s::text[], %s::date[]) AS t(a, d)))
               )
         ORDER BY key_date, target
        """,
        (
            source_id,
            list(bar_dates),
            [t for t, _ in action_keys],
            [d for _, d in action_keys],
        ),
    )
    if not rows:
        return
    edits = sorted({r["edit"] for r in rows if r["is_edit"] and r["edit"]})
    named = ", ".join(
        f"{r['key_date']}"
        + (f" ({r['action_type']})" if r["action_type"] else "")
        + f" [override {r['override_id']}]"
        for r in rows[:8]
    )
    more = "" if len(rows) <= 8 else f" (and {len(rows) - 8} more)"
    if edits:
        remedy = (
            "Those written by `prices shift|rescale` are revoked whole -- "
            f"`fafnir override revoke <any id of edit {', '.join(edits)}>`. "
        )
    else:
        remedy = ""
    raise OverrideRefused(
        f"{len(rows)} key(s) of security {source_id} in this range already carry an "
        f"active operator 'delete' while still holding a row: {named}{more}. Moving "
        "one would write a second suppression at the same key. "
        + remedy
        + "Revoke the override (`fafnir override revoke <id>`), or choose a range "
        "that does not cover these keys."
    )


def plan_history_split(
    db: Database,
    *,
    source_id: int,
    to_date: date,
    from_date: Optional[date] = None,
    destination_id: Optional[int] = None,
    new_symbol: Optional[str] = None,
    new_name: Optional[str] = None,
    exchange_code: Optional[str] = None,
    asset_type: str = "equity",
    delisted_date: Optional[date] = None,
    restore_deleted: bool = False,
) -> SplitPlan:
    """Everything a split would do, and every reason it must not happen.

    Writes nothing. Raises :class:`OverrideRefused` for a split that cannot be made
    safely; the collision checks against an existing destination are part of that,
    so the plan an operator approves is the check that runs.
    """
    src = db.fetchone(
        "SELECT security_id, primary_symbol, exchange_code, source "
        "FROM core.security WHERE security_id = %s",
        (source_id,),
    )
    if src is None:
        raise OverrideRefused(f"No such security: {source_id}")
    if (destination_id is None) == (new_symbol is None):
        raise OverrideRefused(
            "Give exactly one destination: an existing security, or a new symbol."
        )
    if destination_id is not None and destination_id == source_id:
        raise OverrideRefused("The destination is the source security.")
    if to_date >= date.today():
        raise OverrideRefused(
            f"{to_date} is not in the past. A split moves a finished history; the "
            "source keeps trading under its ticker."
        )
    if from_date is not None and from_date > to_date:
        raise OverrideRefused(f"--from {from_date} is after --to {to_date}.")
    lo = from_date or date(1900, 1, 1)

    dest_row = None
    if destination_id is not None:
        dest_row = db.fetchone(
            "SELECT security_id, primary_symbol FROM core.security "
            "WHERE security_id = %s",
            (destination_id,),
        )
        if dest_row is None:
            raise OverrideRefused(f"No such security: {destination_id}")
    else:
        new_symbol = (new_symbol or "").strip().upper()
        if not new_symbol:
            raise OverrideRefused("A new security needs a symbol.")
        if not (new_name or "").strip():
            raise OverrideRefused(
                "A new security needs a name: it is what tells the two issuers apart."
            )

    already = db.fetchval(
        """
        SELECT count(*) FROM ops.operator_override
         WHERE security_id = %s AND revoked_at IS NULL AND detail ? 'split'
           AND key_date BETWEEN %s AND %s
        """,
        (source_id, lo, to_date),
    )
    if already:
        raise OverrideRefused(
            f"{already} key(s) of security {source_id} in this range were already "
            "split off. Undo that split first, or choose a range that does not "
            "overlap it."
        )

    bars = db.fetchall(
        """
        SELECT trade_date, open, high, low, close, volume, vwap, source,
               ingestion_run_id, loaded_at
          FROM core.daily_price
         WHERE security_id = %s AND trade_date BETWEEN %s AND %s
         ORDER BY trade_date
        """,
        (source_id, lo, to_date),
    )
    actions = db.fetchall(
        """
        SELECT corporate_action_id, action_type, ex_date, record_date, payment_date,
               declaration_date, split_numerator, split_denominator,
               dividend_amount, currency, source, ingestion_run_id, loaded_at
          FROM core.corporate_action
         WHERE security_id = %s AND ex_date BETWEEN %s AND %s
         ORDER BY ex_date, action_type
        """,
        (source_id, lo, to_date),
    )
    restorable: list[dict] = []
    if restore_deleted:
        stored = {b["trade_date"] for b in bars}
        for o in db.fetchall(
            """
            SELECT override_id, key_date, detail FROM ops.operator_override
             WHERE security_id = %s AND target = 'daily_price'
               AND operation = 'delete' AND revoked_at IS NULL
               AND key_date BETWEEN %s AND %s
             ORDER BY key_date
            """,
            (source_id, lo, to_date),
        ):
            row = (o["detail"] or {}).get("row")
            if row and o["key_date"] not in stored:
                restorable.append(
                    {"override_id": int(o["override_id"]), "row": row, **row}
                )

    remaining = int(
        db.fetchval(
            "SELECT count(*) FROM core.daily_price WHERE security_id = %s",
            (source_id,),
        )
    ) - len(bars)
    if bars and remaining == 0:
        raise OverrideRefused(
            f"Every bar security {source_id} holds is in this range. That is one "
            "instrument under the wrong row -- `security merge` -- or a rename, not "
            "two issuers to separate."
        )

    dates = [b["trade_date"] for b in bars] + [
        date.fromisoformat(str(r["trade_date"])[:10]) for r in restorable
    ]
    action_dates = [a["ex_date"] for a in actions]
    if not dates and not action_dates:
        raise OverrideRefused(
            f"Security {source_id} has nothing between {lo} and {to_date} to move."
        )
    first = min(dates + action_dates)
    last = max(dates + action_dates)

    # Every key the split moves gets a fresh 'delete' override on the source. A key
    # that already carries an active one collides with ux_operator_override_active,
    # and the split dies mid-transaction on a psycopg UniqueViolation rather than
    # refusing. Two routes reach that state, and both are ordinary:
    #   * `actions delete` then `actions add` -- the documented way to replace a wrong
    #     vendor row, which leaves a 'delete' override under a live operator row;
    #   * `prices shift|rescale` (0026), whose 'delete' of the vendor's bar sits under
    #     the operator's replacement.
    # A restorable key is not one of these: it reuses its own override rather than
    # writing a second, which is why only stored rows are checked here.
    _refuse_keys_already_overridden(
        db,
        source_id=source_id,
        bar_dates=[b["trade_date"] for b in bars],
        action_keys=[(a["action_type"], a["ex_date"]) for a in actions],
    )

    duplicate_bar_dates: list[date] = []
    duplicate_action_keys: list[tuple[str, date]] = []
    if destination_id is not None:
        theirs = {
            r["trade_date"]: r
            for r in db.fetchall(
                """
                SELECT trade_date, open, high, low, close, volume, vwap
                  FROM core.daily_price
                 WHERE security_id = %s AND trade_date = ANY(%s)
                """,
                (destination_id, dates),
            )
        }
        ours = {b["trade_date"]: b for b in bars}
        ours.update(
            {date.fromisoformat(str(r["trade_date"])[:10]): r for r in restorable}
        )
        clashes = []
        for d, t in sorted(theirs.items()):
            mine = ours[d]
            differs = [
                f
                for f in _SPLIT_PRICE_FIELDS
                if t[f] is None
                or mine.get(f) is None
                or Decimal(str(t[f])) != Decimal(str(mine[f]))
            ]
            # volume and vwap decide it too. A session the destination holds with a
            # different volume is not "already held identically": calling it a
            # duplicate deletes the source's row and keeps the destination's, so the
            # warehouse's only copy would silently become the worse one. `security
            # merge` surfaces exactly this as its volume_only bucket rather than
            # picking a side, and a split has no more right to pick one.
            differs += [
                f
                for f in _SPLIT_VOLUME_FIELDS
                if _bar_field(t.get(f)) != _bar_field(mine.get(f))
            ]
            if not differs:
                duplicate_bar_dates.append(d)
            else:
                shown = differs[0]
                clashes.append(
                    f"{d}: destination {shown} {t.get(shown)} vs source "
                    f"{mine.get(shown)}"
                )
        if clashes:
            raise OverrideRefused(
                f"{len(clashes)} session(s) are already held by security "
                f"{destination_id} with different values, so they are not the same "
                "bars: " + "; ".join(clashes[:5]) + ". Correct one side first, or "
                "narrow the range."
            )
        held = {
            (r["action_type"], r["ex_date"]): r
            for r in db.fetchall(
                """
                SELECT action_type, ex_date, split_numerator, split_denominator,
                       dividend_amount, currency
                  FROM core.corporate_action
                 WHERE security_id = %s AND ex_date BETWEEN %s AND %s
                """,
                (destination_id, first, last),
            )
        }
        action_clashes = []
        for a in actions:
            t = held.get((a["action_type"], a["ex_date"]))
            if t is None:
                continue
            if all(t[f] == a[f] for f in _SPLIT_ACTION_FIELDS):
                duplicate_action_keys.append((a["action_type"], a["ex_date"]))
            else:
                action_clashes.append(f"{a['action_type']} {a['ex_date']}")
        if action_clashes:
            raise OverrideRefused(
                f"Security {destination_id} already has a different "
                + ", ".join(action_clashes[:5])
                + ". Correct one side with `fafnir actions` first."
            )

    flags = int(
        db.fetchval(
            f"""
            SELECT count(*) FROM ops.data_quality_flag f
             WHERE f.security_id = %s AND {_FLAG_DATE_SQL} BETWEEN %s AND %s
            """,
            (source_id, first, last),
        )
    )
    before = db.fetchone(
        """
        SELECT trade_date, close FROM core.daily_price
         WHERE security_id = %s AND trade_date < %s
         ORDER BY trade_date DESC LIMIT 1
        """,
        (source_id, first),
    )
    after = db.fetchone(
        """
        SELECT trade_date, close FROM core.daily_price
         WHERE security_id = %s AND trade_date > %s
         ORDER BY trade_date LIMIT 1
        """,
        (source_id, last),
    )

    new_security = None
    if destination_id is None:
        exchange = exchange_code or src["exchange_code"]
        new_security = {
            "primary_symbol": new_symbol,
            "company_name": (new_name or "").strip(),
            "asset_type": asset_type,
            "exchange_code": exchange,
            "delisted_date": delisted_date or last,
        }
    return SplitPlan(
        source_id=source_id,
        source_symbol=src["primary_symbol"],
        destination_id=destination_id,
        destination_symbol=(
            dest_row["primary_symbol"] if dest_row is not None else new_symbol
        ),
        new_security=new_security,
        first_date=first,
        last_date=last,
        bars=bars,
        restorable=restorable,
        actions=actions,
        duplicate_bar_dates=duplicate_bar_dates,
        duplicate_action_keys=duplicate_action_keys,
        flags=flags,
        bars_kept_before=before,
        bars_kept_after=after,
        remaining_bars=remaining,
    )


def _mint_operator_security(db: Database, plan: SplitPlan) -> int:
    new = plan.new_security or {}
    ensure_exchange(db, new.get("exchange_code"))
    sid = int(
        db.fetchval(
            """
            INSERT INTO core.security
                (primary_symbol, company_name, asset_type, exchange_code,
                 is_actively_trading, is_etf, is_fund, delisted_date, source,
                 updated_at)
            VALUES (%s, %s, %s, %s, FALSE, %s, %s, %s, %s, now())
            RETURNING security_id
            """,
            (
                new["primary_symbol"],
                new["company_name"],
                new["asset_type"],
                new.get("exchange_code"),
                new["asset_type"] == "etf",
                new["asset_type"] == "fund",
                new["delisted_date"],
                OPERATOR_SOURCE,
            ),
        )
    )
    # A CLOSED period only. XREF_RESOLVE_SQL reads open periods, so the live ticker
    # keeps resolving to the source; this row records which ticker the old issuer
    # traded under and when. Skipped, not forced, if that (symbol, valid_from) is
    # already some other period's primary key.
    db.execute(
        """
        INSERT INTO core.symbol_xref
            (security_id, symbol, valid_from, valid_to, is_primary, source)
        VALUES (%s, %s, %s, %s, TRUE, %s)
        ON CONFLICT (symbol, valid_from) DO NOTHING
        """,
        (sid, new["primary_symbol"], plan.first_date, plan.last_date, OPERATOR_SOURCE),
    )
    return sid


def split_security_history(
    db: Database,
    *,
    source_id: int,
    to_date: date,
    note: str,
    created_by: str,
    from_date: Optional[date] = None,
    destination_id: Optional[int] = None,
    new_symbol: Optional[str] = None,
    new_name: Optional[str] = None,
    exchange_code: Optional[str] = None,
    asset_type: str = "equity",
    delisted_date: Optional[date] = None,
    restore_deleted: bool = False,
) -> SplitReport:
    """Move a date range of one security's history onto another security.

    Bars and corporate actions in the range move; every moved key is left suppressed
    on the source by an active 'delete' override that names the destination, so a
    load of the ticker's full history cannot re-insert them. A moved action is
    stored on the destination as an operator row (source = 'operator', with an 'add'
    override), so no reconciliation of the destination's own ticker treats it as
    withdrawn; the source's override keeps the vendor's row exactly as it stood.

    DQ flags about a date in the range follow the rows. Adjustment factors are the
    caller's to recompute, for both securities. Does not commit.
    """
    if not note or not note.strip():
        raise OverrideRefused("A split needs a note: it is the whole record.")
    plan = plan_history_split(
        db,
        source_id=source_id,
        to_date=to_date,
        from_date=from_date,
        destination_id=destination_id,
        new_symbol=new_symbol,
        new_name=new_name,
        exchange_code=exchange_code,
        asset_type=asset_type,
        delisted_date=delisted_date,
        restore_deleted=restore_deleted,
    )
    dest = (
        destination_id
        if destination_id is not None
        else _mint_operator_security(db, plan)
    )
    override_ids: list[int] = []
    restored_ids: list[int] = []
    duplicates = set(plan.duplicate_bar_dates)

    moved = 0
    for bar in plan.bars:
        d = bar["trade_date"]
        kind = "duplicate" if d in duplicates else "moved"
        if kind == "moved":
            db.execute(
                """
                INSERT INTO core.daily_price
                    (security_id, trade_date, open, high, low, close, volume, vwap,
                     source, ingestion_run_id, loaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    dest,
                    d,
                    bar["open"],
                    bar["high"],
                    bar["low"],
                    bar["close"],
                    bar["volume"],
                    bar["vwap"],
                    bar["source"],
                    bar["ingestion_run_id"],
                    bar["loaded_at"],
                ),
            )
            moved += 1
        override_ids.append(
            _insert_override(
                db,
                security_id=source_id,
                target="daily_price",
                action_type=None,
                key_date=d,
                operation="delete",
                detail={"row": _jsonable(bar), **_split_marker(source_id, dest, kind)},
                note=note,
                created_by=created_by,
            )
        )
        db.execute(
            "DELETE FROM core.daily_price WHERE security_id = %s AND trade_date = %s",
            (source_id, d),
        )

    import json

    restored = 0
    for r in plan.restorable:
        d = date.fromisoformat(str(r["trade_date"])[:10])
        if d not in duplicates:
            db.execute(
                """
                INSERT INTO core.daily_price
                    (security_id, trade_date, open, high, low, close, volume, vwap,
                     source, ingestion_run_id, loaded_at)
                VALUES (%s,%s,%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s,
                        %s::numeric,%s,%s,COALESCE(%s::timestamptz, now()))
                """,
                (
                    dest,
                    d,
                    r.get("open"),
                    r.get("high"),
                    r.get("low"),
                    r.get("close"),
                    r.get("volume"),
                    r.get("vwap"),
                    r.get("source") or "fmp",
                    r.get("ingestion_run_id"),
                    r.get("loaded_at"),
                ),
            )
            restored += 1
        kind = "duplicate-restored" if d in duplicates else "restored"
        db.execute(
            "UPDATE ops.operator_override SET detail = detail || %s "
            "WHERE override_id = %s",
            (json.dumps(_split_marker(source_id, dest, kind)), r["override_id"]),
        )
        restored_ids.append(int(r["override_id"]))

    dup_actions = set(plan.duplicate_action_keys)
    actions_moved = 0
    for a in plan.actions:
        key = (a["action_type"], a["ex_date"])
        kind = "duplicate" if key in dup_actions else "moved"
        override_ids.append(
            _insert_override(
                db,
                security_id=source_id,
                target="corporate_action",
                action_type=a["action_type"],
                key_date=a["ex_date"],
                operation="delete",
                detail={"row": _jsonable(a), **_split_marker(source_id, dest, kind)},
                note=note,
                created_by=created_by,
            )
        )
        db.execute(
            "DELETE FROM core.corporate_action WHERE corporate_action_id = %s",
            (a["corporate_action_id"],),
        )
        if kind == "duplicate":
            continue
        new_id = int(
            db.fetchval(
                """
                INSERT INTO core.corporate_action
                    (security_id, action_type, ex_date, record_date, payment_date,
                     declaration_date, split_numerator, split_denominator,
                     dividend_amount, currency, source, loaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                RETURNING corporate_action_id
                """,
                (
                    dest,
                    a["action_type"],
                    a["ex_date"],
                    a["record_date"],
                    a["payment_date"],
                    a["declaration_date"],
                    a["split_numerator"],
                    a["split_denominator"],
                    a["dividend_amount"],
                    a["currency"],
                    OPERATOR_SOURCE,
                ),
            )
        )
        override_ids.append(
            _insert_override(
                db,
                security_id=dest,
                target="corporate_action",
                action_type=a["action_type"],
                key_date=a["ex_date"],
                operation="add",
                detail={
                    "row": _jsonable({**a, "corporate_action_id": new_id}),
                    **_split_marker(source_id, dest, "moved"),
                },
                note=note,
                created_by=created_by,
            )
        )
        actions_moved += 1

    # Every override this split touched is stamped with one batch id, and the flags
    # it moves carry the same mark. Without it `--undo` keys only on the pair of
    # securities, so a second, disjoint split into the same destination is taken back
    # by an undo meant for the first; and it returns flags by the dates it happened to
    # put back, which both strands a flag about a date holding no row (a `gap` flag
    # keyed on the missing session) and hands over flags the destination raised
    # itself. `at` orders the batches: a restore-only split's ids are older than the
    # overrides it reuses, so ids alone do not say which split came last.
    batch = min(override_ids) if override_ids else min(restored_ids)
    db.execute(
        """
        UPDATE ops.operator_override
           SET detail = jsonb_set(
                   jsonb_set(detail, '{split,batch}', to_jsonb(%s::bigint)),
                   '{split,at}', to_jsonb(now()))
         WHERE override_id = ANY(%s)
        """,
        (batch, override_ids + restored_ids),
    )

    flags_dropped, flags_moved = _move_dated_flags(
        db,
        from_id=source_id,
        to_id=dest,
        stamp=batch,
        first=plan.first_date,
        last=plan.last_date,
    )
    return SplitReport(
        plan=plan,
        destination_id=dest,
        bars_moved=moved,
        bars_restored=restored,
        bars_duplicate=len(duplicates),
        actions_moved=actions_moved,
        actions_duplicate=len(dup_actions),
        flags_moved=flags_moved,
        flags_dropped=flags_dropped,
        override_ids=override_ids,
    )


def _move_dated_flags(
    db: Database,
    *,
    from_id: int,
    to_id: int,
    first: Optional[date] = None,
    last: Optional[date] = None,
    dates: Optional[Sequence[date]] = None,
    stamp: Optional[int] = None,
    stamped: Optional[int] = None,
) -> tuple[int, int]:
    """Repoint flags to another security. Returns ``(dropped, moved)``.

    Which flags: those about a date in [first, last], or in ``dates``, or -- with
    ``stamped`` -- exactly the ones a split batch moved, whatever their date. ``stamp``
    marks the moved flags with the batch that moved them, so the undo can ask for them
    back by name rather than by date: a `gap` flag is keyed on a session that holds no
    row, so it has no date among the rows an undo returns, and a flag the destination
    raised itself shares its dates with the ones it was handed.

    Same rule as :func:`merge_security`: an open flag the destination already
    carries for the same condition is dropped rather than repointed, or the repoint
    would violate ux_dq_flag_open_condition (0016). price_* is never dropped -- its
    repeats are counted.
    """
    if stamped is not None:
        where, params = "f.detail->>'split_batch' = %s", [str(stamped)]
    elif dates is not None:
        where, params = f"{_FLAG_DATE_SQL} = ANY(%s)", [list(dates)]
    else:
        where, params = f"{_FLAG_DATE_SQL} BETWEEN %s AND %s", [first, last]
    dropped = db.execute(
        f"""
        DELETE FROM ops.data_quality_flag f
         WHERE f.security_id = %s AND {where}
           AND f.resolved_at IS NULL
           AND f.check_name NOT LIKE 'price\\_%%'
           AND EXISTS (
                 SELECT 1 FROM ops.data_quality_flag s
                  WHERE s.security_id = %s AND s.resolved_at IS NULL
                    AND s.check_name = f.check_name
                    AND s.record_key IS NOT DISTINCT FROM f.record_key)
        """,
        [from_id, *params, to_id],
    )
    if stamp is not None:
        set_detail = (
            ", detail = jsonb_set(COALESCE(f.detail, '{}'::jsonb), "
            "'{split_batch}', to_jsonb(%s::bigint))"
        )
        set_params = [stamp]
    elif stamped is not None:
        set_detail = ", detail = COALESCE(f.detail, '{}'::jsonb) - 'split_batch'"
        set_params = []
    else:
        set_detail, set_params = "", []
    moved = db.execute(
        f"""
        UPDATE ops.data_quality_flag f SET security_id = %s{set_detail}
         WHERE f.security_id = %s AND {where}
        """,
        [to_id, *set_params, from_id, *params],
    )
    return dropped, moved


def undo_history_split(
    db: Database,
    *,
    source_id: int,
    destination_id: int,
    note: str,
    revoked_by: str,
) -> UnsplitReport:
    """Put back everything a split moved from ``source_id`` to ``destination_id``.

    Driven entirely by the active overrides carrying the split marker, so it undoes
    exactly what was moved and nothing the destination held before:

    * a moved bar or action returns to the source as it stood (from the override's
      ``detail.row``), the source's override is revoked, and the destination's copy
      is removed;
    * a key the destination already held identically ("duplicate") returns to the
      source and the destination keeps its own copy;
    * a bar restored from an earlier `prices delete` is removed from the destination
      and its original override is left active -- the split did not create it, so
      the undo does not lift it.

    Flags about a returned date go back. An operator-minted destination left with no
    history is deleted. Adjustment factors are the caller's to recompute. Does not
    commit.
    """
    if not note or not note.strip():
        raise OverrideRefused("An undo needs a note.")
    # One split, not every split between this pair. Two disjoint ranges may be moved
    # to the same destination -- the planner only refuses an overlapping one -- and an
    # undo of the second used to take the first back with it, silently and with no way
    # to ask for either alone. The newest batch is the one being taken back.
    batch = db.fetchval(
        """
        SELECT (detail->'split'->>'batch')::bigint
          FROM ops.operator_override
         WHERE revoked_at IS NULL
           AND detail->'split'->>'source_security_id' = %s
           AND detail->'split'->>'destination_security_id' = %s
         ORDER BY detail->'split'->>'at' DESC NULLS LAST,
                  (detail->'split'->>'batch')::bigint DESC NULLS LAST
         LIMIT 1
        """,
        (str(source_id), str(destination_id)),
    )
    rows = db.fetchall(
        """
        SELECT override_id, security_id, target, action_type, key_date, operation,
               detail
          FROM ops.operator_override
         WHERE revoked_at IS NULL
           AND detail->'split'->>'source_security_id' = %s
           AND detail->'split'->>'destination_security_id' = %s
           AND (detail->'split'->>'batch')::bigint IS NOT DISTINCT FROM %s
         ORDER BY override_id
        """,
        (str(source_id), str(destination_id), batch),
    )
    if not rows:
        raise OverrideRefused(
            f"No active split from security {source_id} to {destination_id}."
        )

    def _revoke(oid: int) -> None:
        db.execute(
            """
            UPDATE ops.operator_override
               SET revoked_at = now(), revoked_by = %s, revoked_note = %s
             WHERE override_id = %s
            """,
            (revoked_by, note, oid),
        )

    bars_returned = bars_unrestored = actions_returned = 0
    returned_dates: list[date] = []
    for o in rows:
        kind = o["detail"]["split"]["kind"]
        row = o["detail"].get("row") or {}
        d = o["key_date"]
        if o["target"] == "daily_price":
            if kind in ("restored", "duplicate-restored"):
                if kind == "restored":
                    db.execute(
                        "DELETE FROM core.daily_price "
                        "WHERE security_id = %s AND trade_date = %s",
                        (destination_id, d),
                    )
                db.execute(
                    "UPDATE ops.operator_override SET detail = detail - 'split' "
                    "WHERE override_id = %s",
                    (o["override_id"],),
                )
                bars_unrestored += 1
                returned_dates.append(d)
                continue
            db.execute(
                """
                INSERT INTO core.daily_price
                    (security_id, trade_date, open, high, low, close, volume, vwap,
                     source, ingestion_run_id, loaded_at)
                VALUES (%s,%s,%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s,
                        %s::numeric,%s,%s,COALESCE(%s::timestamptz, now()))
                ON CONFLICT (security_id, trade_date) DO NOTHING
                """,
                (
                    source_id,
                    d,
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("volume"),
                    row.get("vwap"),
                    row.get("source") or "fmp",
                    row.get("ingestion_run_id"),
                    row.get("loaded_at"),
                ),
            )
            if kind == "moved":
                db.execute(
                    "DELETE FROM core.daily_price "
                    "WHERE security_id = %s AND trade_date = %s",
                    (destination_id, d),
                )
            _revoke(o["override_id"])
            bars_returned += 1
            returned_dates.append(d)
        elif o["operation"] == "add":
            # The destination's operator copy of a moved action.
            db.execute(
                """
                DELETE FROM core.corporate_action
                 WHERE security_id = %s AND action_type = %s AND ex_date = %s
                   AND source = %s
                """,
                (destination_id, o["action_type"], d, OPERATOR_SOURCE),
            )
            _revoke(o["override_id"])
        else:
            db.execute(
                """
                INSERT INTO core.corporate_action
                    (security_id, action_type, ex_date, record_date, payment_date,
                     declaration_date, split_numerator, split_denominator,
                     dividend_amount, currency, source, ingestion_run_id, loaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::numeric,%s::numeric,%s::numeric,%s,%s,
                        %s, COALESCE(%s::timestamptz, now()))
                ON CONFLICT (security_id, action_type, ex_date) DO NOTHING
                """,
                (
                    source_id,
                    o["action_type"],
                    d,
                    row.get("record_date"),
                    row.get("payment_date"),
                    row.get("declaration_date"),
                    row.get("split_numerator"),
                    row.get("split_denominator"),
                    row.get("dividend_amount"),
                    row.get("currency") or "USD",
                    row.get("source") or "fmp",
                    row.get("ingestion_run_id"),
                    row.get("loaded_at"),
                ),
            )
            _revoke(o["override_id"])
            actions_returned += 1
            returned_dates.append(d)

    _, flags_returned = _move_dated_flags(
        db, from_id=destination_id, to_id=source_id, stamped=batch
    )

    deleted = False
    still_edited = db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM ops.operator_override "
        "WHERE security_id = %s AND revoked_at IS NULL)",
        (destination_id,),
    )
    left = db.fetchone(
        """
        SELECT EXISTS (SELECT 1 FROM core.daily_price WHERE security_id = %s) AS bars,
               EXISTS (SELECT 1 FROM core.corporate_action WHERE security_id = %s)
                   AS actions
        """,
        (destination_id, destination_id),
    )
    if (
        is_operator_security(db, destination_id)
        and not still_edited
        and not left["bars"]
        and not left["actions"]
    ):
        # What is left on it is about the split itself (a coverage flag on the moved
        # range); fold_empty_security's rule applies -- flags follow the survivor.
        # Its revoked overrides stay as they are: the record of what was undone.
        _move_remaining_flags(db, from_id=destination_id, to_id=source_id)
        # Derived from the actions just returned; nothing is lost with them.
        db.execute(
            "DELETE FROM core.adjustment_factor WHERE security_id = %s",
            (destination_id,),
        )
        db.execute(
            "DELETE FROM ops.load_watermark WHERE security_id = %s", (destination_id,)
        )
        db.execute(
            "DELETE FROM core.symbol_xref WHERE security_id = %s", (destination_id,)
        )
        db.execute(
            "DELETE FROM core.company_profile WHERE security_id = %s",
            (destination_id,),
        )
        db.execute(
            "DELETE FROM core.security WHERE security_id = %s", (destination_id,)
        )
        deleted = True
    return UnsplitReport(
        source_id=source_id,
        destination_id=destination_id,
        bars_returned=bars_returned,
        bars_unrestored=bars_unrestored,
        actions_returned=actions_returned,
        flags_returned=flags_returned,
        destination_deleted=deleted,
    )


def _move_remaining_flags(db: Database, *, from_id: int, to_id: int) -> None:
    db.execute(
        """
        DELETE FROM ops.data_quality_flag v
         WHERE v.security_id = %s AND v.resolved_at IS NULL
           AND v.check_name NOT LIKE 'price\\_%%'
           AND EXISTS (SELECT 1 FROM ops.data_quality_flag s
                        WHERE s.security_id = %s AND s.resolved_at IS NULL
                          AND s.check_name = v.check_name
                          AND s.record_key IS NOT DISTINCT FROM v.record_key)
        """,
        (from_id, to_id),
    )
    db.execute(
        "UPDATE ops.data_quality_flag SET security_id = %s WHERE security_id = %s",
        (to_id, from_id),
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


def securities_changed_by_run(db: Database, ingestion_run_id: int) -> list[int]:
    """Securities whose corporate actions were inserted or changed by one run.

    Pairs with :func:`upsert_corporate_action`, which stamps ``ingestion_run_id``
    only on a row it actually wrote. This is what `fafnir adjust --changed`
    recomputes: on a normal night a few hundred securities instead of every
    security in the warehouse that has ever had an action.
    """
    rows = db.fetchall(
        """
        SELECT DISTINCT security_id FROM core.corporate_action
        WHERE ingestion_run_id = %s
        ORDER BY security_id
        """,
        (ingestion_run_id,),
    )
    return [int(r["security_id"]) for r in rows]


def latest_run_id(db: Database, source: str, endpoint: str) -> Optional[int]:
    """The most recent ingestion run for a source/endpoint, whatever its status.

    Deliberately not restricted to ``status = 'success'``: a run that ended
    'partial' (something was quarantined) still wrote the rows it validated, and
    those securities still need their factors recomputed.
    """
    val = db.fetchval(
        """
        SELECT ingestion_run_id FROM ops.ingestion_run
        WHERE source = %s AND endpoint = %s
        ORDER BY ingestion_run_id DESC LIMIT 1
        """,
        (source, endpoint),
    )
    return int(val) if val is not None else None


def securities_without_actions_watermark(
    db: Database, endpoint: str, *, source: str = "fmp", include_inactive: bool = False
) -> list[dict]:
    """Securities that have never had a full corporate-actions pull.

    The calendar sweep only ever looks forward from its own watermark, so a security
    minted after that watermark was set -- an IPO, or a fund declared under ADR 0006
    -- would carry no history at all. These are the ones that still need the
    per-symbol full pull, and the absence of a watermark row is the whole test. Same
    rule the price loader already uses: no watermark, full history, once.
    """
    where = "" if include_inactive else "AND s.is_actively_trading "
    rows = db.fetchall(
        f"""
        SELECT s.security_id, s.primary_symbol
          FROM core.security s
          LEFT JOIN ops.load_watermark w
                 ON w.security_id = s.security_id
                AND w.source = %s
                AND w.endpoint = %s
         WHERE w.security_id IS NULL
           AND s.{VENDOR_FED_SECURITY}
           {where}
         ORDER BY s.security_id
        """,
        (source, endpoint),
    )
    return [
        {"security_id": int(r["security_id"]), "symbol": r["primary_symbol"]}
        for r in rows
    ]


def universe_securities(db: Database, *, include_inactive: bool = False) -> list[dict]:
    """The securities a nightly load should touch, as {security_id, symbol}.

    Excludes delisted names by default, for the reason the price loader already
    excludes them (`fafnir ingest prices`): a security that has stopped trading will
    never have another bar -- and never another corporate action either -- so
    re-polling it spends requests forever on a history that cannot change. A backfill
    passes ``include_inactive`` because a universe of only the survivors is exactly
    what makes a history survivorship-biased.
    """
    where = "AND is_actively_trading " if not include_inactive else ""
    rows = db.fetchall(f"""
        SELECT security_id, primary_symbol FROM core.security
         WHERE {VENDOR_FED_SECURITY}
        {where}
        ORDER BY security_id
        """)
    return [
        {"security_id": int(r["security_id"]), "symbol": r["primary_symbol"]}
        for r in rows
    ]


def securities_by_asset_type(
    db: Database, asset_types: Sequence[str], *, include_inactive: bool = False
) -> list[dict]:
    """Active securities of the given asset types, as {security_id, symbol}.

    Used to keep the declared fund universe on the per-symbol pull: a fund has no
    listing venue, so an exchange-oriented calendar feed cannot be assumed to carry
    its distributions (ADR 0006, ADR 0007).
    """
    if not asset_types:
        return []
    where = "" if include_inactive else "AND is_actively_trading "
    rows = db.fetchall(
        f"""
        SELECT security_id, primary_symbol FROM core.security
         WHERE asset_type = ANY(%s)
           AND {VENDOR_FED_SECURITY}
           {where}
         ORDER BY security_id
        """,
        (list(asset_types),),
    )
    return [
        {"security_id": int(r["security_id"]), "symbol": r["primary_symbol"]}
        for r in rows
    ]


def actions_reconciliation_slice(
    db: Database, *, buckets: int, bucket: int, include_inactive: bool = False
) -> list[dict]:
    """One deterministic 1/``buckets`` slice of the universe, as {security_id, symbol}.

    Sliced on ``security_id % buckets`` rather than sampled at random so that every
    security is reached exactly once per full cycle. A random sample of the same size
    leaves a long tail that is never checked -- and the point of the reconciliation is
    a bound on how stale any security's actions can be, which randomness cannot give.
    """
    if buckets < 1:
        raise ValueError("buckets must be >= 1")
    where = "" if include_inactive else "AND is_actively_trading "
    rows = db.fetchall(
        f"""
        SELECT security_id, primary_symbol FROM core.security
         WHERE security_id %% %s = %s
           AND {VENDOR_FED_SECURITY}
           {where}
         ORDER BY security_id
        """,
        (buckets, bucket % buckets),
    )
    return [
        {"security_id": int(r["security_id"]), "symbol": r["primary_symbol"]}
        for r in rows
    ]


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
    # The acceptance probe is the same lookup with the other predicate, so it is
    # built alongside and served by ix_dq_flag_accepted_condition (0024). Two
    # index-backed probes, rather than one `resolved_at IS NULL OR accepted_at IS
    # NOT NULL` that no partial index can serve -- see the note above.
    accepted_clauses = ["check_name = %s", "accepted_at IS NOT NULL"]
    if security_id is None:
        clauses.append("security_id IS NULL")
        accepted_clauses.append("security_id IS NULL")
    else:
        clauses.append("security_id = %s")
        accepted_clauses.append("security_id = %s")
        probe.append(security_id)
    if key_json is None:
        clauses.append("record_key IS NULL")
        accepted_clauses.append("record_key IS NULL")
    else:
        clauses.append("record_key = %s::jsonb")
        accepted_clauses.append("record_key = %s::jsonb")
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
              AND NOT EXISTS (
                SELECT 1 FROM ops.data_quality_flag
                WHERE {" AND ".join(accepted_clauses)}
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
                # Once per NOT EXISTS: the two probes take the same values in the
                # same order, differing only in the predicate they are matched on.
                *probe,
                *probe,
            ),
        )
        > 0
    )


# `price_*` is the family an operator globs for, not a promise that every member is
# a quarantine. These ones describe a bar that WAS stored, so they must not be
# counted as attempts to store it -- see count_price_quarantines, whose count is a
# budget that releases the watermark once it is spent. A stored-but-corrupted bar
# buying down a rejected bar's budget would let ingestion walk past a bar nobody had
# actually looked at.
NON_QUARANTINE_PRICE_CHECKS = ("price_scale_collapse",)


def count_price_quarantines(db: Database, security_id: int, date_iso: str) -> int:
    """How many times a given trade_date has been *quarantined* for this security.

    Used to bound the watermark hold on a persistently-bad bar, so it counts
    attempts to store a bar that failed -- the repeats of the `price_<reason>` flags
    the loader writes on rejection. :data:`NON_QUARANTINE_PRICE_CHECKS` is excluded:
    those share the prefix but describe a bar that was written, and one of those on
    the same date would otherwise spend a rejected bar's budget for it.
    """
    return int(
        db.fetchval(
            """
            SELECT count(*) FROM ops.data_quality_flag
            WHERE security_id = %s
              AND check_name LIKE 'price\\_%%'
              AND check_name <> ALL(%s)
              AND record_key->>'date' = %s
            """,
            (security_id, list(NON_QUARANTINE_PRICE_CHECKS), date_iso),
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

DQ_STATES = ("open", "resolved", "accepted", "all")

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

    ``security_ids`` is a set: empty means every security, and the CLI's `--symbol`
    resolves into a one-element form of it. ``since``/``until`` filter on
    ``detected_at`` -- when the check ran. ``trade_dates``
    filters on the session the flag is *about* (``record_key->>'trade_date'``), which
    is a different question and the one triage usually asks: "the gaps on these eleven
    days", not "the flags written on the night we happened to notice them".
    """

    state: str = "open"
    checks: Sequence[str] = ()
    severities: Sequence[str] = ()
    security_ids: Sequence[int] = ()
    since: Optional[date] = None
    until: Optional[date] = None
    trade_dates: Sequence[date] = ()
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
            or self.security_ids
            or self.since is not None
            or self.until is not None
            or self.trade_dates
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
        # Accepted rows are resolved too (0024's CHECK constraint), but they are a
        # different decision and asking for one should not return the other.
        clauses.append(f"{q}resolved_at IS NOT NULL")
        clauses.append(f"{q}accepted_at IS NULL")
    elif filt.state == "accepted":
        clauses.append(f"{q}accepted_at IS NOT NULL")
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
    if filt.security_ids:
        clauses.append(f"{q}security_id = ANY(%s)")
        params.append(list(filt.security_ids))
    if filt.since is not None:
        clauses.append(f"{q}detected_at >= %s")
        params.append(filt.since)
    if filt.until is not None:
        # Inclusive of the whole day: `--until 2024-08-28` has to include a flag
        # detected at 14:02 that day, which `detected_at <= %s` would drop -- the
        # date widens to midnight.
        clauses.append(f"{q}detected_at < (%s::date + 1)")
        params.append(filt.until)
    if filt.trade_dates:
        # The session the flag describes, not the night the check ran. Only the
        # checks keyed on a session carry this key (gap, outlier); a flag whose
        # record_key has no `trade_date` yields NULL and simply does not match,
        # which is the wanted behaviour -- `--trade-date` is a narrowing option
        # and must never widen a selection to a differently-keyed check.
        clauses.append(f"{q}record_key->>'trade_date' = ANY(%s)")
        params.append([d.isoformat() for d in filt.trade_dates])
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
               -- Acceptance travels with the row: 0024's down migration tells the
               -- operator to keep `dq list --state accepted --detail --json` before
               -- dropping these columns, and that is the only copy there will be.
               f.accepted_at, f.accepted_by, f.accepted_note,
               f.ingestion_run_id
          FROM ops.data_quality_flag f
          LEFT JOIN core.security s ON s.security_id = f.security_id
          {where}
         ORDER BY f.detected_at {direction}, f.dq_flag_id {direction}
         LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    )


def open_dq_flag_ids_for_record(
    db: Database, *, check_name: str, record_key: dict
) -> list[int]:
    """Open flags for one exact condition, by the key the check wrote.

    The DqFilter selection options (`--check`, `--symbol`, dates) cannot address a
    single condition, because a record_key is the check's own private shape. A
    command that has just repaired one specific condition needs to close that
    flag and no other, so it resolves by id and gets the ids from here.

    Matched on the record_key as a whole (jsonb equality, so key order does not
    matter), the same way :func:`add_dq_flag_once` decides a condition is already
    queued -- one predicate for "is this flagged" and "which flag is it".
    """
    import json

    rows = db.fetchall(
        """
        SELECT dq_flag_id FROM ops.data_quality_flag
         WHERE check_name = %s AND record_key = %s::jsonb AND resolved_at IS NULL
         ORDER BY dq_flag_id
        """,
        (check_name, json.dumps(record_key)),
    )
    return [int(r["dq_flag_id"]) for r in rows]


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


def accept_dq_flags(
    db: Database,
    filt: DqFilter,
    *,
    note: str,
    accepted_by: str,
) -> list[int]:
    """Accept conditions as real, permanent and unfixable. Returns the ids closed.

    The difference from :func:`resolve_dq_flags` is what happens next. A resolution
    is judged against the data, so it frees the condition's slot and the next
    `fafnir dq run` writes the flag again if the problem is still there -- which is
    the point, and why resolving a vendor's missing decade achieves nothing. An
    acceptance says the problem IS still there and always will be, so the checks
    skip it (see the guards in :mod:`fafnir.dq.checks` and the second probe in
    :func:`add_dq_flag_once`).

    ``note`` is required and has no default. Everything else about a flag can be
    re-derived from the data; the reason someone decided to stop asking cannot.
    """
    if not filt.is_narrowed:
        raise ValueError(
            "accept_dq_flags needs flag ids or at least one filter; refusing to "
            "accept the whole queue"
        )
    if not (note and note.strip()):
        raise ValueError(
            "accept_dq_flags needs a note: an accepted flag is a standing decision "
            "to stop looking, and the note is the whole record of why"
        )
    where, params = _dq_where(filt._replace(state="open"))
    rows = db.fetchall(
        f"""
        UPDATE ops.data_quality_flag
           SET resolved_at = now(), resolved_by = %s, resolution_note = %s,
               accepted_at = now(), accepted_by = %s, accepted_note = %s
         {where}
        RETURNING dq_flag_id
        """,
        [accepted_by, note, accepted_by, note, *params],
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
                           resolution_note = NULL,
                           -- Acceptance goes with it. A row that is open again is
                           -- one nobody has decided about, and leaving accepted_at
                           -- set would keep the checks skipping the condition while
                           -- the queue showed it as unjudged.
                           accepted_at = NULL, accepted_by = NULL,
                           accepted_note = NULL
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
