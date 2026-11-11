"""`fafnir security merge --across-vendors`: one company held twice after a rename,
the vendor-fed row and FMP's re-mint under the new ticker (#125).

FMP's screener mints a renamed company again under its new ticker and serves it the
company's whole history. The old row, which FMP's delisted feed then called
delisted, is the one the Sharadar cutover linked by permaticker and feeds. So
`ingest sharadar-universe` step 4 finds the fed row inactive on the old ticker with
another row on the vendor's ticker (``next = held``): NLOK and GEN, NYCB and FLG, 68
pairs on 2026-10-24.

`security merge` refuses them, rightly by its own lights: it needs the overlap to
agree to the tenth of a cent and the identifiers to match, because a disagreement
between two rows of one vendor means two instruments. Across two vendors the same
company disagrees for reasons that are not identity:

* **The history under the new ticker is adjusted.** FMP re-serves a renamed company
  scaled for what came later: ALNT is 2/3 of AMOT before the 2021 3:2 split, BFH
  is BFH*0.80 before the 2021 spin-off, FLG three times NYCB before its reverse
  split. The fed row holds the bars as they traded (FMP's nightly loads at the
  time, Sharadar's since).
* **Rounding.** NLOK and GEN differ by half a cent on 814 of 1,751 shared days.
* **New identifiers.** A reverse split or a redomicile issues a new CUSIP.

So identity is established from the vendor instead, and the fed row's bars win:

1. **The survivor is the fed row.** It holds the open feeding link, and the vendor's
   master lists that key live under the victim's ticker: the vendor says the fed
   key trades as the other row now. The victim has no link to the vendor.
2. **The CIK agrees** where both rows carry one, **or the vendor's master gives the
   fed key the victim's**. A company that changes registrant -- a redomicile (MTMT
   to MPU), a holding-company reorganisation (HHC to HHH), a de-SPAC under a new
   issuer (NCAC to PBM), a legal acquirer renamed (HTA to HR) -- is one security
   to the vendor, which then lists the new registrant's CIK. The fed row still
   carries the old one; FMP's re-mint, the new one. Any other CIK that differs is
   another registrant and refuses. A CUSIP or ISIN that differs is reported, not a
   blocker.
3. **They agree today**: at least 90% of the last 20 shared sessions close within
   1% of each other.
4. **On a session both hold, the survivor's bar is kept** (the merge's own rule), so
   the victim's adjusted copy of shared history goes. Its corporate actions dated
   from the survivor's first bar on go too: the survivor's are the vendor's view of
   the same events, and keeping both would count a dividend twice.
5. **A bar only the victim holds moves only where it joins**: the median ratio of
   the nearest shared sessions either side is within 1% of 1. One that does not is
   on another scale, and moving it would put a level break in a raw series. The
   operator decides those (``unjoined``): refuse the merge (the default), drop them,
   or carry them as they are. The test reads where the histories meet, so a scale
   change wholly inside the victim's own earlier history (a split before the fed
   row's first bar) is not seen: the plan names the victim's splits there, and
   `vendor correct --split-bearing` is how such bars are checked.

The survivor takes the victim's name, CUSIP, ISIN and CIK (they describe the
instrument as it trades now), and is relisted on the vendor's ticker
(`security relist`'s own plan and write), in the same transaction: one row, active,
fed, on the ticker the company trades under.

**The victim on a former ticker** (:data:`MODE_FORMER`). The same company is held
twice the other way round just as often: the fed row already trades on the vendor's
ticker, and FMP's row is the one left on the OLD ticker, inactive. `vendor link`
flags it ``duplicate_of_linked`` (MICS and RIME, the Singing Machine renamed
Algorhythm: 909 of the 929 such flags on 2026-11-11). Step 1 then reads the
vendor's ticker history instead of its current ticker:

* the vendor's events rename the fed key away from the victim's ticker
  (``tickerchangefrom``, :func:`former_rename`), to the ticker the vendor lists it
  as now, and the survivor trades on that ticker already. Where the events name no
  such rename, ``rename_date`` dates it, but only for a ticker the vendor's master
  lists among the key's related tickers;
* the CIK must agree (zero-padding aside): nothing in the vendor's word says an
  older registrant's CIK was the key's;
* steps 3 to 5 as above. "Today" is the last sessions the two rows share, the end
  of the victim's history.

What it writes differs in three ways. The survivor keeps its own name and
identifiers: they are the current ones, and the victim's are what the company was.
It is not relisted: it is listed on the vendor's ticker already. And the victim's
ticker period, which the merge deletes with the victim, is recorded on the
survivor from the vendor's rename into that ticker (failing that, the victim's own
period start, but never before its first bar) to the day before the rename away,
with the survivor's own period on the vendor's ticker starting at the rename: the
ticker history a rename applied in time would have left, so ``ticker_history``
still resolves the old ticker to the company on the days it traded under it. It
refuses when the recorded period would overlap another security's period on that
ticker (a reused ticker's earlier issuer).
"""

from __future__ import annotations

import bisect
from datetime import date, timedelta
from statistics import median
from typing import NamedTuple, Optional, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_universe as su
from fafnir.ingest.vendor_link import PRICED_TABLES, normalise_cik

#: The last shared sessions compared, and the share that must agree within
#: :data:`RECENT_TOLERANCE`: today's prices are the identity test that history
#: under an adjusted scale cannot be.
RECENT_SESSIONS = 20
RECENT_MIN_SESSIONS = 10
RECENT_AGREE_SHARE = 0.9
RECENT_TOLERANCE = 0.01
#: A victim-only bar joins the survivor's series when the median close ratio of
#: the :data:`NEIGHBOURS` nearest shared sessions on either side is within this of 1.
JOIN_TOLERANCE = 0.01
NEIGHBOURS = 3
#: Shared sessions whose closes differ by more than this ratio are "scaled".
SCALED_RATIO = 1.5

UNJOINED_REFUSE = "refuse"
UNJOINED_DROP = "drop"
UNJOINED_CARRY = "carry"
UNJOINED_CHOICES = (UNJOINED_REFUSE, UNJOINED_DROP, UNJOINED_CARRY)

#: Flags that stop holding once the victim's history and identity are the fed
#: row's: the vendor-identity flags `vendor link` raised on the unlinked re-mint.
IDENTITY_CHECKS = ("vendor_xref_unmatched", "vendor_xref_ambiguous")

#: The victim is FMP's re-mint on the ticker the vendor lists the fed key as.
MODE_REMINT = "remint"
#: The victim is FMP's row on a ticker the vendor renamed the fed key away from.
MODE_FORMER = "former_ticker"


class FormerTicker(NamedTuple):
    """How a victim on a former ticker hands its ticker history to the survivor."""

    #: The rename away from the victim's ticker: ``(date, old, new)``.
    rename: tuple
    #: Where the date came from: ``"events"`` or ``"rename_date"``.
    dated_by: str
    #: The victim's ticker period to record on the survivor, ``(symbol, from, to)``;
    #: None where the survivor holds a period on that ticker already.
    period: Optional[tuple]
    #: The survivor's open period on the vendor's ticker moved to start at the
    #: rename, ``(symbol, was, now)``; None where it starts there or later, or the
    #: rename was to another ticker (a chain).
    survivor_from: Optional[tuple]


def former_rename(
    events: Sequence[dict], victim_symbol: str
) -> Optional[tuple[date, str, str]]:
    """The newest of one key's renames away from ``victim_symbol`` (fafnir's
    spelling), as ``(date, old, new)``, or None."""
    found = [
        r for r in su.renames(list(events)) if su.fafnir_ticker(r[1]) == victim_symbol
    ]
    return found[-1] if found else None


def entered_on(events: Sequence[dict], symbol: str, before: date) -> Optional[date]:
    """The date of the key's newest rename INTO ``symbol`` before ``before``: when
    the vendor says it took that ticker. None where its events name none."""
    found = [
        day
        for day, _old, new in su.renames(list(events))
        if su.fafnir_ticker(new) == symbol and day < before
    ]
    return found[-1] if found else None


class CrossVendorPlan(NamedTuple):
    """What a cross-vendor merge would do, and every reason it should not."""

    base: repo.MergePlan
    vendor: Optional[str]
    vendor_key: Optional[str]
    vendor_ticker: Optional[str]
    identifier_notes: list
    recent_sessions: int
    recent_agree: int
    recent_max_dev: Optional[float]
    same: int
    within_1pct: int
    scaled: int
    other: int
    survivor_first: Optional[date]
    joined: list
    unjoined: list
    boundary_ratio: Optional[float]
    victim_actions_from_first: int
    survivor_actions_from_first: int
    victim_splits_before: int
    victim_identity: dict
    unjoined_policy: str
    blockers: list
    mode: str = MODE_REMINT
    former: Optional[FormerTicker] = None

    @property
    def victim_dates(self) -> list:
        """The victim's bars the merge moves."""
        if self.unjoined_policy == UNJOINED_CARRY:
            return sorted(self.joined + self.unjoined)
        return sorted(self.joined)

    @property
    def actions_before(self) -> date:
        """The victim's actions dated on or after this are dropped: from the
        survivor's first bar, or all of them when none of the victim's earlier
        bars moves."""
        first = self.survivor_first or date.max
        if any(d < first for d in self.victim_dates):
            return first
        return date.min


def _ratio(a, b) -> Optional[float]:
    return float(a) / float(b) if a and b and float(b) > 0 else None


def _same_cik(a, b) -> bool:
    """Two CIKs name one registrant: the same number, however zero-padded."""
    return normalise_cik(a) is not None and normalise_cik(a) == normalise_cik(b)


def plan_cross_vendor_merge(
    db: Database,
    *,
    victim_id: int,
    survivor_id: int,
    unjoined: str = UNJOINED_REFUSE,
    rename_date: Optional[date] = None,
) -> CrossVendorPlan:
    """Read the evidence for folding ``victim_id`` into the fed ``survivor_id``.

    ``rename_date`` dates the rename the vendor's events do not: the relist's move
    to the vendor's ticker for a re-mint, the rename away from the victim's ticker
    for a victim on a former one."""
    if unjoined not in UNJOINED_CHOICES:
        raise ValueError(f"unjoined must be one of {UNJOINED_CHOICES}")
    base = repo.compare_securities(db, survivor_id=survivor_id, victim_id=victim_id)
    victim_identity = db.fetchone(
        "SELECT company_name, cusip, isin, cik FROM core.security "
        "WHERE security_id = %s",
        (victim_id,),
    )
    blockers: list[str] = []

    # 1. The vendor's word: the survivor is fed, its key live, under the victim's
    # ticker -- or, for a victim on a former ticker, renamed away from it to the
    # ticker the survivor trades on.
    vendor = key = ticker = master = None
    mode, former = MODE_REMINT, None
    link = repo.feeding_link(db, survivor_id)
    victim_link = repo.feeding_link(db, victim_id)
    if victim_link is not None:
        blockers.append(
            f"the victim {victim_id} is fed by {victim_link['vendor']}: the survivor "
            "must be the fed row (its bars win the overlap and its link stays put); "
            "name them the other way round"
        )
    if link is None:
        blockers.append(
            f"the survivor {survivor_id} has no feeding vendor link: nothing says "
            "which vendor's history is the company's"
        )
    else:
        vendor, key = link["vendor"], link["vendor_key"]
        master = repo.vendor_master_row(db, vendor, key, PRICED_TABLES)
        if master is None:
            blockers.append(f"the {vendor} master has no {key}")
        else:
            ticker = su.fafnir_ticker(master["ticker"])
            if master["is_delisted"]:
                blockers.append(f"{vendor} lists {key} as delisted")
            if ticker != base.victim_symbol:
                former, why = _plan_former_ticker(
                    db,
                    vendor=vendor,
                    key=key,
                    master=master,
                    ticker=ticker,
                    base=base,
                    rename_date=rename_date,
                )
                if former is not None:
                    mode = MODE_FORMER
                blockers += why
    blockers += repo.vendor_link_conflicts(db, victim_id, survivor_id)

    # 2. Identifiers: a CIK must agree, unless the vendor gives the fed key the
    # victim's (a new registrant it carries as one security); a CUSIP or ISIN may
    # not, and is reported.
    notes = []
    vendor_cik = master["cik"] if master else None
    for field, survivor_value, victim_value in base.identity_mismatches:
        if field == "cik" and _same_cik(survivor_value, victim_value):
            continue  # one CIK, zero-padded differently
        if (
            field == "cik"
            and mode == MODE_REMINT
            and _same_cik(victim_value, vendor_cik)
        ):
            notes.append(
                f"cik {survivor_value} vs {victim_value}, the CIK {vendor} gives "
                f"{key}: a new registrant it carries as one security"
            )
        elif field == "cik":
            blockers.append(
                f"cik differs: survivor {survivor_value!r} vs victim "
                f"{victim_value!r}"
                + (f" ({vendor} gives {key} {vendor_cik!r})" if vendor_cik else "")
                + " -- another registrant is not a rename"
            )
        else:
            notes.append(f"{field} {survivor_value} vs {victim_value}")

    # 3 and 4. The overlap, classified; today's agreement.
    shared = db.fetchall(
        """
        SELECT s.trade_date, s.open AS so, s.high AS sh, s.low AS sl, s.close AS sc,
               v.open AS vo, v.high AS vh, v.low AS vl, v.close AS vc
          FROM core.daily_price s
          JOIN core.daily_price v ON v.trade_date = s.trade_date
         WHERE s.security_id = %s AND v.security_id = %s
         ORDER BY s.trade_date
        """,
        (survivor_id, victim_id),
    )
    same = within = scaled = other = 0
    days: list[date] = []
    ratios: list[float] = []
    for r in shared:
        ratio = _ratio(r["sc"], r["vc"])
        if ratio is None:
            continue
        days.append(r["trade_date"])
        ratios.append(ratio)
        if (r["so"], r["sh"], r["sl"], r["sc"]) == (r["vo"], r["vh"], r["vl"], r["vc"]):
            same += 1
        elif abs(ratio - 1) <= RECENT_TOLERANCE:
            within += 1
        elif ratio >= SCALED_RATIO or ratio <= 1 / SCALED_RATIO:
            scaled += 1
        else:
            other += 1
    recent = ratios[-RECENT_SESSIONS:]
    agree = sum(1 for x in recent if abs(x - 1) <= RECENT_TOLERANCE)
    max_dev = max((abs(x - 1) for x in recent), default=None)
    if len(recent) < RECENT_MIN_SESSIONS:
        blockers.append(
            f"only {len(recent)} recent shared sessions: too few to show the two rows "
            "trade as one instrument today"
        )
    elif agree < RECENT_AGREE_SHARE * len(recent):
        blockers.append(
            f"{agree} of the last {len(recent)} shared sessions close within "
            f"{RECENT_TOLERANCE:.0%}: the rows do not describe one instrument today"
        )

    # 5. The victim's own bars: joined where the nearest shared sessions agree.
    first_bar, _ = repo.security_bar_span(db, survivor_id)
    victim_only = [
        r["trade_date"]
        for r in db.fetchall(
            """
            SELECT v.trade_date FROM core.daily_price v
             WHERE v.security_id = %s
               AND NOT EXISTS (SELECT 1 FROM core.daily_price s
                                WHERE s.security_id = %s
                                  AND s.trade_date = v.trade_date)
             ORDER BY v.trade_date
            """,
            (victim_id, survivor_id),
        )
    ]
    joined, unjoined_days = [], []
    for d in victim_only:
        i = bisect.bisect_left(days, d)
        near = ratios[max(0, i - NEIGHBOURS) : i] + ratios[i : i + NEIGHBOURS]
        if near and abs(median(near) - 1) <= JOIN_TOLERANCE:
            joined.append(d)
        else:
            unjoined_days.append(d)
    boundary = median(ratios[:RECENT_SESSIONS]) if ratios else None
    if unjoined_days and unjoined == UNJOINED_REFUSE:
        blockers.append(
            f"{len(unjoined_days)} bars only the victim holds "
            f"({unjoined_days[0]}..{unjoined_days[-1]}) do not join the survivor's "
            "series: the nearest shared sessions put them on another scale"
            + (f" (ratio {boundary:.4g} where the histories meet)" if boundary else "")
            + ". Re-run with --unjoined drop to leave them, or --unjoined carry to "
            "move them as they are"
        )

    counts = db.fetchone(
        """
        SELECT (SELECT count(*) FROM core.corporate_action
                 WHERE security_id = %s AND ex_date >= %s) AS victim_n,
               (SELECT count(*) FROM core.corporate_action
                 WHERE security_id = %s AND ex_date >= %s) AS survivor_n,
               (SELECT count(*) FROM core.corporate_action
                 WHERE security_id = %s AND ex_date < %s
                   AND action_type = 'split') AS victim_splits
        """,
        (
            victim_id,
            first_bar or date.max,
            survivor_id,
            first_bar or date.max,
            victim_id,
            first_bar or date.max,
        ),
    )
    return CrossVendorPlan(
        base=base,
        vendor=vendor,
        vendor_key=key,
        vendor_ticker=ticker,
        identifier_notes=notes,
        recent_sessions=len(recent),
        recent_agree=agree,
        recent_max_dev=max_dev,
        same=same,
        within_1pct=within,
        scaled=scaled,
        other=other,
        survivor_first=first_bar,
        joined=joined,
        unjoined=unjoined_days,
        boundary_ratio=boundary,
        victim_actions_from_first=int(counts["victim_n"]),
        survivor_actions_from_first=int(counts["survivor_n"]),
        victim_splits_before=int(counts["victim_splits"]),
        victim_identity=dict(victim_identity),
        unjoined_policy=unjoined,
        blockers=blockers,
        mode=mode,
        former=former,
    )


def _plan_former_ticker(
    db: Database,
    *,
    vendor: str,
    key: str,
    master: dict,
    ticker: str,
    base: repo.MergePlan,
    rename_date: Optional[date],
) -> tuple[Optional[FormerTicker], list[str]]:
    """Whether the victim holds a ticker the vendor renamed the fed key away from,
    and the ticker periods the merge would leave. Returns the plan, or None and the
    blocker that says why not."""
    victim_symbol, survivor_id = base.victim_symbol, base.survivor_id
    not_one = (
        f"{vendor} lists {key} as {ticker}, not as the victim's {victim_symbol}: the "
        "vendor does not say these are one company"
    )
    events = repo.vendor_events_for_keys(db, vendor, su.RENAME_EVENTS, [key])
    rename = former_rename(events, victim_symbol)
    dated_by = "events"
    if rename is None:
        related = {su.fafnir_ticker(t) for t in (master.get("related_tickers") or [])}
        if victim_symbol not in related:
            return None, [
                not_one + f" ({vendor}'s events name no rename of {key} from "
                f"{victim_symbol}, nor does it list {victim_symbol} among {key}'s "
                "tickers)"
            ]
        if rename_date is None:
            return None, [
                not_one + f" ({vendor} lists {victim_symbol} among {key}'s tickers "
                "but its events date no rename from it: pass --rename-date)"
            ]
        rename, dated_by = (rename_date, victim_symbol, ticker), "rename_date"
    elif rename_date is not None and rename_date != rename[0]:
        return None, [
            f"--rename-date {rename_date} contradicts {vendor}'s own: {key} was "
            f"renamed {rename[1]} -> {rename[2]} on {rename[0]}"
        ]
    day, _old, new = rename
    blockers: list[str] = []
    survivor = db.fetchone(
        "SELECT primary_symbol FROM core.security WHERE security_id = %s",
        (survivor_id,),
    )
    if survivor["primary_symbol"] != ticker:
        blockers.append(
            f"the survivor {survivor_id} is on {survivor['primary_symbol']}, not the "
            f"ticker {vendor} lists {key} as ({ticker}): relist it first, or merge "
            "the re-mint that holds the vendor's ticker"
        )

    # The victim's period, from the vendor's rename into its ticker or, failing
    # that, the victim's own start; to the day before the rename away.
    period = None
    held = db.fetchval(
        "SELECT 1 FROM core.symbol_xref WHERE security_id = %s AND symbol = %s",
        (survivor_id, victim_symbol),
    )
    if not held:
        start = entered_on(events, victim_symbol, day) or _victim_period_start(
            db, base.victim_id, victim_symbol
        )
        end = day - timedelta(days=1)
        if start is None or start > end:
            blockers.append(
                f"no period of {victim_symbol} ends before the rename of {day}: "
                "nothing dates the ticker history to carry"
            )
        else:
            period = (victim_symbol, start, end)
            # Any other security's period on the ticker that the recorded one
            # would overlap, not only one starting the same day: a reused ticker's
            # earlier issuer (CHK, the old Chesapeake, to 2020) must keep its
            # dates to itself, or point-in-time resolution finds two securities.
            other = db.fetchone(
                """
                SELECT security_id, valid_from, valid_to FROM core.symbol_xref
                 WHERE symbol = %s AND security_id <> %s
                   AND valid_from <= %s
                   AND COALESCE(valid_to, 'infinity'::date) >= %s
                 ORDER BY valid_from LIMIT 1
                """,
                (victim_symbol, base.victim_id, end, start),
            )
            if other is not None:
                blockers.append(
                    f"security {other['security_id']} holds a {victim_symbol} period "
                    f"from {other['valid_from']} to {other['valid_to'] or 'open'}, "
                    f"which the victim's {start}..{end} would overlap: the victim's "
                    "cannot be recorded on the survivor"
                )

    # The survivor's period on the vendor's ticker starts at the rename, where the
    # rename was to that ticker and the period claims more.
    survivor_from = None
    if su.fafnir_ticker(new) == ticker:
        was = db.fetchval(
            "SELECT valid_from FROM core.symbol_xref "
            "WHERE security_id = %s AND symbol = %s AND valid_to IS NULL",
            (survivor_id, ticker),
        )
        if was is not None and was < day:
            survivor_from = (ticker, was, day)
            other = db.fetchval(
                "SELECT security_id FROM core.symbol_xref "
                "WHERE symbol = %s AND valid_from = %s AND security_id <> %s",
                (ticker, day, base.victim_id),
            )
            if other is not None:
                blockers.append(
                    f"security {other} holds a {ticker} period from {day} already: "
                    f"the survivor's cannot start there"
                )
    return FormerTicker(rename, dated_by, period, survivor_from), blockers


class CrossVendorDone(NamedTuple):
    report: repo.MergeReport
    relisted: object  # relist.RelistPlan; None for a victim on a former ticker
    identity_flags: list
    identity_before: dict  # empty when the survivor kept its own identity
    #: The ticker periods written for a victim on a former ticker.
    periods: tuple = ()


def apply_cross_vendor_merge(
    db: Database,
    plan: CrossVendorPlan,
    *,
    note: str,
    by: str,
    rename_date: Optional[date] = None,
) -> CrossVendorDone:
    """Merge, recompute the survivor's factors, close the identity flags the
    victim brought, and relist the survivor on the vendor's ticker, in the caller's
    transaction. For a victim on a former ticker, record its ticker period on the
    survivor instead, and neither adopt its identity nor relist. Raises
    :class:`repo.MergeRefused` or :class:`fafnir.ingest.relist.RelistRefused`; the
    caller rolls back."""
    from fafnir.ingest import adjustments
    from fafnir.ingest import relist as rl

    survivor_id = plan.base.survivor_id
    report = repo.merge_security(
        db,
        victim_id=plan.base.victim_id,
        survivor_id=survivor_id,
        blockers=plan.blockers,
        victim_dates=plan.victim_dates,
        actions_before=plan.actions_before,
    )
    adjustments.compute_for_security(db, survivor_id)
    flags_note = f"Merged across vendors into the {plan.vendor}-fed row: {note}"
    if plan.mode == MODE_FORMER:
        periods = _carry_former_ticker(db, survivor_id, plan.former, plan.vendor)
        flags = repo.resolve_dq_flags(
            db,
            repo.DqFilter(
                state="open", checks=IDENTITY_CHECKS, security_ids=(survivor_id,)
            ),
            note=flags_note,
            resolved_by=by,
        )
        return CrossVendorDone(report, None, flags, {}, periods)
    # The victim's name and identifiers describe the instrument as it trades now.
    before = repo.adopt_identity(db, survivor_id, **plan.victim_identity)
    flags = repo.resolve_dq_flags(
        db,
        repo.DqFilter(
            state="open", checks=IDENTITY_CHECKS, security_ids=(survivor_id,)
        ),
        note=f"Merged across vendors into the {plan.vendor}-fed row: {note}",
        resolved_by=by,
    )
    relist_plan = rl.plan_relist(db, security_id=survivor_id, rename_date=rename_date)
    if not _already(relist_plan):
        # Raises on any refusal, which rolls the merge back with it.
        rl.apply_relist(db, relist_plan, note=note, by=by)
    return CrossVendorDone(report, relist_plan, flags, dict(before))


def _already(plan) -> bool:
    """The survivor trades on the vendor's ticker already: nothing to relist."""
    return bool(plan.active_before) and plan.symbol == plan.vendor_ticker


def _victim_period_start(db: Database, victim_id: int, symbol: str) -> Optional[date]:
    """Where the victim's ticker period begins when the vendor's events date no
    rename into it: the victim's own period start, but never before its first bar.
    A period at the 1900 placeholder claims every earlier issuer of a reused
    ticker's dates; the victim's bars are the earliest date the row is evidence
    for."""
    own = db.fetchval(
        "SELECT min(valid_from) FROM core.symbol_xref "
        "WHERE security_id = %s AND symbol = %s",
        (victim_id, symbol),
    )
    first_bar, _ = repo.security_bar_span(db, victim_id)
    candidates = [d for d in (own, first_bar) if d is not None]
    return max(candidates) if candidates else None


def _carry_former_ticker(
    db: Database, survivor_id: int, former: FormerTicker, vendor: str
) -> tuple:
    """Write the ticker periods a victim on a former ticker leaves the survivor.

    After the merge, which deleted the victim's own periods: the victim's ticker
    from when the vendor took it (or the victim's period began) to the day before
    the rename away, and the survivor's open period on the vendor's ticker from the
    rename. Raises :class:`repo.MergeRefused` when a period the plan read as free
    is taken now; the caller rolls the merge back with it."""
    written = []
    if former.period is not None:
        symbol, start, end = former.period
        if not repo.insert_symbol_period(
            db,
            security_id=survivor_id,
            symbol=symbol,
            valid_from=start,
            valid_to=end,
            source=vendor,
        ):
            raise repo.MergeRefused(
                f"another security holds a {symbol} period from {start} now", None
            )
        written.append(("recorded", symbol, start, end))
    if former.survivor_from is not None:
        symbol, was, now = former.survivor_from
        moved = db.execute(
            """
            UPDATE core.symbol_xref SET valid_from = %s
             WHERE security_id = %s AND symbol = %s AND valid_to IS NULL
               AND valid_from = %s
            """,
            (now, survivor_id, symbol, was),
        )
        if moved != 1:
            raise repo.MergeRefused(
                f"the survivor's open {symbol} period no longer starts {was}", None
            )
        written.append(("moved", symbol, was, now))
    return tuple(written)


def victim_dates_summary(dates: Sequence[date]) -> str:
    return f"{len(dates)} ({dates[0]}..{dates[-1]})" if dates else "0"
