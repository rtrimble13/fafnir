"""
`fafnir vendor link`: link fafnir's existing securities to a vendor's identity
(plan SA-0401, #62).

About 21k FMP-minted securities must be linked to a Sharadar permaticker before
Sharadar can feed them or be compared with them. The screener never supplied a CUSIP
or CIK for most of them (ADR 0005), so a link rests on the ticker and the dates, and
the prices decide it (assessment §8.4).

For each FMP-minted security without an open Sharadar link:

1. **Candidates.** The Sharadar securities with prices (``stocks``, ``funds``) known
   by any ticker ``core.symbol_xref`` has held for it: their own ticker, a
   ``related_tickers`` entry (a dead issuer's ``CLA1`` lists ``CLA``), or the other
   side of a ticker change. They must also have a price history overlapping the
   security's own bars. A ticker is compared with ``.`` read as ``-`` (Sharadar's
   ``BRK.B`` is FMP's ``BRK-B``). A key a person rejected for this security
   (``core.security_vendor_rejection``, #63) is never a candidate for it. A key
   another open link already holds is tested like any other, but never won: the
   engine never moves a link, manual or not.
2. **The decisive test.** The overlap is cut into five equal windows, and the
   security's first stored bar in each is the sample. Its raw close must match
   Sharadar's ``closeunadj`` on the same date within max($0.01, 1 bp).
3. **Corroboration.** A shared CIK or CUSIP (an ISIN's CUSIP included), and the
   names' similarity (``company_name_similarity``).
4. **Decision.** Sharadar is the reliable vendor (plan decision of 2026-10-20):
   FMP's history going wrong is a reason to correct its bars, not to withhold the
   link. The first basis with a winner decides, recorded as the link's ``basis``:
   - ``price_history``: one candidate agrees on at least four samples
     (``ticker_span_price``).
   - ``latest_price``: none does, and one candidate with four samples agrees on
     its latest (``ticker_span_price``). FMP's history is back-adjusted, or
     another instrument's, while today's close is right: about 324 of the 807
     securities run 398 left with no monotonic candidate.
   - ``identifier``: no sample decides, and one candidate shares a CIK or CUSIP
     (``match_method = identifier``).
   - The confidence is 0.9 × agreement + 0.1 × corroboration whatever the basis,
     so a link resting on one agreeing sample scores about 0.2.
   - **Several win on a price basis:** the one with an identifier match is chosen
     if it is the only one; otherwise ``vendor_xref_ambiguous``
     (``several_agree``).
   - **A link whose samples disagree** anywhere also gets
     ``vendor_history_disagrees``, naming those dates: the bars Sharadar's are to
     replace (#75), audited in ``ops.vendor_correction``.
   - **The one that agrees is held** by another security's open link: the
     security is another row of the holder's issuer (``duplicate_of_linked``,
     naming it).
   - **One key, several securities:** the claimant that is live and trades under
     the vendor's current ticker is linked, and the others are
     ``duplicate_of_linked`` (a missed rename's two rows); with no such claimant
     none is linked (``key_claimed_twice``).
   - **None agrees:** ``vendor_xref_unmatched``, with a reason:
     - ``no_candidate``;
     - ``no_bars``: the security has no bars, so no span (a re-mint shell);
     - ``price_disagrees``: a candidate had four samples, neither they nor its
       latest agreed, and no identifier decided;
     - ``insufficient_overlap``: no candidate had four sampled bars to compare;
     - ``not_covered_asset_type``: a mutual fund, ``asset_type = 'fund'`` or an
       ``is_fund`` non-ETF with no candidate. Sharadar does not price them.
       Flagged at ``info``.

Writes are idempotent:
- A linked security leaves the scope, so it is not evaluated again.
- Flags go through :func:`repo.add_dq_flag_once`.
- When a later run reaches a different outcome, the open ``vendor_xref_*`` flags of
  the old one are resolved.

The evidence (sampled dates and both prices) is kept in the link and the flags, in
the database. It is vendor data, and never posted.

Sharadar's closes come from the ledger's current SEP/SFP bulk files, in one
filtered pass (:class:`BulkCloses`). For a small run (``--new``, a few
``--symbols``) they come from the API, one request per candidate
(:class:`ApiCloses`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional, Protocol, Sequence

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.ingest import sharadar_ledger as ledger
from fafnir.ingest.runlog import RunLog
from fafnir.ingest.security_master import company_name_similarity
from fafnir.logging_config import get_logger

logger = get_logger("ingest.vendor_link")

VENDOR = "sharadar"
#: The vendor tables with prices: a candidate must be one of these.
PRICED_TABLES = ("stocks", "funds")
SAMPLES = 5
MIN_AGREED = 4
ABS_TOLERANCE = Decimal("0.01")
REL_TOLERANCE = Decimal("0.0001")  # 1 bp
CREATED_BY = "fafnir vendor link"
ENDPOINT_FULL = "link"
ENDPOINT_NEW = "link:new"
UNMATCHED = "vendor_xref_unmatched"
AMBIGUOUS = "vendor_xref_ambiguous"
#: A link whose sampled history disagrees: the bars to correct (#75).
HISTORY = "vendor_history_disagrees"
#: Above this many candidates, read the bulk files rather than the API.
API_MAX_CANDIDATES = 200


class LinkError(RuntimeError):
    """The engine cannot run: no vendor master, or no prices to compare."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalise_cik(cik: Optional[str]) -> Optional[str]:
    """``0000320193`` and ``320193`` are one CIK."""
    digits = (cik or "").strip().lstrip("0")
    return digits or None


def isin_cusip(isin: Optional[str]) -> Optional[str]:
    """The CUSIP inside a US or Canadian ISIN (``US0378331005`` -> ``037833100``)."""
    isin = (isin or "").strip().upper()
    if len(isin) == 12 and isin[:2] in ("US", "CA"):
        return isin[2:11]
    return None


def identifier_match(security: dict, candidate: dict) -> Optional[str]:
    """``cik`` or ``cusip`` when the two share one, else None."""
    cik = normalise_cik(security.get("cik"))
    if cik and cik == normalise_cik(candidate.get("cik")):
        return "cik"
    theirs = {c.strip().upper()[:9] for c in candidate.get("cusips") or ()}
    ours = {
        c
        for c in (
            (security.get("cusip") or "").strip().upper()[:9],
            isin_cusip(security.get("isin")),
        )
        if c
    }
    if ours & theirs:
        return "cusip"
    return None


def ticker_key(ticker: str) -> str:
    """One spelling for a share class: Sharadar writes ``BRK.B`` for some (505
    stocks and funds, 2026-10-19) where FMP writes ``BRK-B``."""
    return ticker.strip().upper().replace(".", "-")


def _keys(tickers) -> set[str]:
    return {ticker_key(t) for t in tickers or ()}


def name_similarity(ours: Optional[str], theirs: Optional[str]) -> Optional[float]:
    """:func:`company_name_similarity`, where "no opinion" because the names are
    the same or one contains the other counts as 1.0, and a missing name as None."""
    if not ours or not theirs:
        return None
    ratio = company_name_similarity(ours, theirs)
    return 1.0 if ratio is None else round(ratio, 3)


def overlap(security: dict, candidate: dict) -> Optional[tuple[date, date]]:
    """The dates both price histories cover, or None."""
    spans = (
        security.get("first_trade_date"),
        security.get("last_trade_date"),
        candidate.get("first_price_date"),
        candidate.get("last_price_date"),
    )
    if None in spans:
        return None
    lo, hi = max(spans[0], spans[2]), min(spans[1], spans[3])
    return (lo, hi) if lo <= hi else None


def sample_windows(lo: date, hi: date, n: int = SAMPLES) -> list[tuple[date, date]]:
    """``lo..hi`` cut into ``n`` consecutive windows; the first bar in each is a
    sample, so samples are distinct dates spread over the overlap. A short overlap
    gives fewer windows (one per day at most)."""
    days = (hi - lo).days + 1
    n = min(n, days)
    edges = [lo + timedelta(days=days * i // n) for i in range(n + 1)]
    return [(edges[i], edges[i + 1] - timedelta(days=1)) for i in range(n)]


def agrees(ours: Decimal, theirs: Decimal) -> bool:
    """Within max($0.01, 1 bp) of the vendor's close."""
    return abs(ours - theirs) <= max(ABS_TOLERANCE, REL_TOLERANCE * abs(theirs))


# ---------------------------------------------------------------------------
# Sharadar's closes
# ---------------------------------------------------------------------------

Wanted = dict[tuple[str, str], set[date]]  # (vendor_table, ticker) -> dates
Closes = dict[tuple[str, str, date], Decimal]


class CloseSource(Protocol):
    def closes(self, wanted: Wanted) -> Closes: ...


class BulkCloses:
    """``closeunadj`` from the ledger's current full SEP/SFP files: one pass per
    table, parsing only the rows whose ticker and date are wanted
    (:func:`sharadar_ledger.select`)."""

    name = "bulk"

    def __init__(self, db: Database, bulk_dir: str | Path):
        self.db, self.bulk_dir = db, bulk_dir

    def closes(self, wanted: Wanted) -> Closes:
        out: Closes = {}
        for table in sorted({t for t, _ in wanted}):
            entry = repo.current_sharadar_full(self.db, table)
            if entry is None:
                raise LinkError(
                    f"the ledger holds no current {table} file: "
                    f"`fafnir source sharadar-bulk --tables {table}` first"
                )
            want = {
                ticker: {d.isoformat() for d in dates}
                for (t, ticker), dates in wanted.items()
                if t == table
            }
            for r in ledger.select(self.db, entry["file_id"], self.bulk_dir, want):
                if r["closeunadj"] is not None:
                    out[(table, r["ticker"], r["date"])] = r["closeunadj"]
        return out


class ApiCloses:
    """``closeunadj`` from the API: one request per ticker, bounded by its sample
    dates. For a handful of candidates, where reading a 1 GB file is waste."""

    name = "api"

    def __init__(self, client):
        self.client = client
        self.tables = {"stocks": client.T_SEP, "funds": client.T_SFP}

    def closes(self, wanted: Wanted) -> Closes:
        out: Closes = {}
        for (table, ticker), dates in sorted(wanted.items()):
            rows = self.client.query(
                self.tables[table],
                {
                    "ticker": ticker,
                    "from": min(dates).isoformat(),
                    "to": max(dates).isoformat(),
                },
                fields=["ticker", "date", "closeunadj"],
            )
            for r in rows:
                # An unknown filter is ignored, not refused (#49): check what came
                # back rather than trust the request.
                d = date.fromisoformat(str(r.get("date"))[:10])
                if r.get("ticker") == ticker and d in dates:
                    if r.get("closeunadj") is not None:
                        out[(table, ticker, d)] = Decimal(str(r["closeunadj"]))
        return out


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    security_id: int
    symbol: str
    outcome: str  # linked | ambiguous | unmatched
    reason: Optional[str] = None  # a link's basis; else why not linked
    vendor_key: Optional[str] = None
    confidence: Optional[float] = None
    valid_from: Optional[date] = None
    evidence: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)  # for the flag's detail


@dataclass
class LinkResult:
    dry_run: bool
    mode: str  # full | new | symbols
    run_id: Optional[int] = None
    price_source: Optional[str] = None
    already_linked: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    note: Optional[str] = None

    def counts(self) -> Counter:
        return Counter((o.outcome, o.reason) for o in self.outcomes)

    def linked(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "linked")

    def to_correct(self) -> int:
        """Links whose sampled history disagrees (a ``vendor_history_disagrees``
        flag each)."""
        return sum(
            1
            for o in self.outcomes
            if o.outcome == "linked" and disagreeing(o.evidence["samples"])
        )


def _index(candidates: Iterable[dict]) -> dict:
    """``alias -> [candidate]``, held keys included."""
    by_alias = defaultdict(list)
    for c in candidates:
        for alias in _keys(c["aliases"]):
            by_alias[alias].append(c)
    return by_alias


def _matched(security: dict, by_alias: dict) -> list[dict]:
    """Every vendor row sharing a ticker with the security, once each."""
    seen, out = set(), []
    for ticker in sorted(_keys(security["tickers"])):
        for c in by_alias.get(ticker, ()):
            if c["vendor_key"] not in seen:
                seen.add(c["vendor_key"])
                out.append(c)
    return out


def _candidates_for(security: dict, by_alias: dict, rejected=frozenset()) -> list[dict]:
    """The rows to price-test: not rejected for it, and overlapping its bars. A
    held key is one of them, so a second row of its holder's issuer is named as
    such (``duplicate_of_linked``) rather than left with no candidate."""
    sid = security["security_id"]
    return [
        c
        for c in _matched(security, by_alias)
        if (sid, c["vendor_key"]) not in rejected and overlap(security, c)
    ]


def ticker_matches(
    security: dict,
    candidates: Sequence[dict],
    rejected=frozenset(),
    linked_key: Optional[str] = None,
) -> list[dict]:
    """Every vendor row sharing a ticker with the security, with why it is or is
    not a candidate: ``linked`` (the security's own open link), ``candidate``,
    ``taken`` (another open link holds it), ``rejected`` (a person said no) or
    ``no_overlap``. For `vendor link show`."""
    keys = _keys(security["tickers"])
    sid = security["security_id"]
    out = []
    for c in candidates:
        if not keys & _keys(c["aliases"]):
            continue
        if c["vendor_key"] == linked_key:
            status = "linked"
        elif c["taken"]:
            status = "taken"
        elif (sid, c["vendor_key"]) in rejected:
            status = "rejected"
        elif not overlap(security, c):
            status = "no_overlap"
        else:
            status = "candidate"
        out.append(dict(c, status=status))
    order = ("linked", "candidate", "rejected", "no_overlap", "taken")
    return sorted(out, key=lambda c: (order.index(c["status"]), c["vendor_key"]))


def evaluate(
    db: Database,
    securities: Sequence[dict],
    candidates: Sequence[dict],
    source_for,  # Callable[[int], CloseSource]: given the candidate count
    rejected=frozenset(),  # {(security_id, vendor_key)} a person said no to
) -> tuple[list[Outcome], Optional[str]]:
    """Decide every security; writes nothing. Returns the outcomes and the name of
    the price source used (None when no price was needed)."""
    by_alias = _index(candidates)
    outcomes: dict[int, Outcome] = {}
    pairs: list[tuple[dict, dict, list[tuple[date, date]]]] = []
    for s in securities:
        sid, sym = s["security_id"], s["primary_symbol"]
        if s["asset_type"] == "fund":
            outcomes[sid] = Outcome(sid, sym, "unmatched", "not_covered_asset_type")
            continue
        cands = _candidates_for(s, by_alias, rejected)
        if not cands:
            matched = _matched(s, by_alias)
            said_no = sorted(
                c["vendor_key"] for c in matched if (sid, c["vendor_key"]) in rejected
            )
            held = sorted(
                c["vendor_key"]
                for c in matched
                if c["taken"] and (sid, c["vendor_key"]) not in rejected
            )
            apart = sorted(
                c["vendor_key"]
                for c in matched
                if not c["taken"] and (sid, c["vendor_key"]) not in rejected
            )
            outcomes[sid] = Outcome(
                sid,
                sym,
                "unmatched",
                _no_candidate_reason(s),
                candidates=[{"taken_key": k} for k in held]
                + [{"rejected_key": k} for k in said_no]
                + [{"no_overlap_key": k} for k in apart],
            )
            continue
        for c in cands:
            lo, hi = overlap(s, c)
            pairs.append((s, c, sample_windows(lo, hi)))

    # Our samples: the first bar in each window, by index probe.
    probes, owner = [], {}
    for p, (s, c, windows) in enumerate(pairs):
        for w, (lo, hi) in enumerate(windows):
            owner[len(probes)] = (p, w)
            probes.append((len(probes), s["security_id"], lo, hi))
    bars = repo.first_bars_on_or_after(db, probes)
    samples: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
    wanted: Wanted = defaultdict(set)
    for probe, (d, close) in sorted(bars.items()):
        p, _ = owner[probe]
        samples[p].append((d, close))
        c = pairs[p][1]
        wanted[(c["vendor_table"], c["ticker"])].add(d)

    source_name = None
    theirs: Closes = {}
    if wanted:
        source = source_for(len(wanted))
        source_name = source.name
        theirs = source.closes(dict(wanted))

    # Score each pair, then decide each security.
    scored: dict[int, list[dict]] = defaultdict(list)
    for p, (s, c, _) in enumerate(pairs):
        rows = []
        for d, ours in samples.get(p, []):
            v = theirs.get((c["vendor_table"], c["ticker"], d))
            rows.append({"date": d, "fmp": ours, "sharadar": v})
        agreed = sum(
            1
            for r in rows
            if r["sharadar"] is not None and agrees(r["fmp"], r["sharadar"])
        )
        scored[s["security_id"]].append(
            {
                "candidate": c,
                "samples": rows,
                "agreed": agreed,
                "of": len(rows),
                "identifier": identifier_match(s, c),
                "name_similarity": name_similarity(s["company_name"], c["name"]),
            }
        )
    by_id = {s["security_id"]: s for s in securities}
    for sid, results in scored.items():
        s = by_id[sid]
        outcomes[sid] = _decide(s, results)

    _settle_double_claims(outcomes, by_id)
    return [outcomes[s["security_id"]] for s in securities], source_name


def _no_candidate_reason(s: dict) -> str:
    """Why nothing shares a ticker and a span with the security.

    - ``not_covered_asset_type``: a mutual fund. FMP files them on NASDAQ as
      ``equity`` with ``is_fund`` (4,577 of the first production run's 4,826
      ``no_candidate``), and Sharadar prices none. A closed-end fund is
      ``is_fund`` too, but Sharadar has it, so only one with no candidate lands
      here.
    - ``no_bars``: a security with no bars has no span to overlap. Usually a
      re-mint shell (``security dedupe``).
    - ``no_candidate``: everything else.
    """
    if s.get("is_fund") and not s.get("is_etf"):
        return "not_covered_asset_type"
    if s.get("first_trade_date") is None:
        return "no_bars"
    return "no_candidate"


def _sample_json(rows: list[dict]) -> list[dict]:
    """Samples as JSON text: a flag's ``detail`` is plain JSON."""
    return [
        {
            "date": r["date"].isoformat(),
            "fmp": str(r["fmp"]),
            "sharadar": None if r["sharadar"] is None else str(r["sharadar"]),
        }
        for r in rows
    ]


def _summary(r: dict) -> dict:
    """A candidate in a flag's ``detail``, its samples included: they stay in the
    database (vendor data, never posted) and are what tells a split on one side
    (a constant ratio) from a different issuer (noise)."""
    c = r["candidate"]
    out = {
        "vendor_key": c["vendor_key"],
        "vendor_table": c["vendor_table"],
        "ticker": c["ticker"],
        "agreed": r["agreed"],
        "of": r["of"],
        "identifier": r["identifier"],
        "samples": _sample_json(r["samples"]),
    }
    if c["taken"]:
        out["held_by"] = c.get("held_by")
    return out


def latest_agrees(r: dict) -> bool:
    """Whether the candidate's most recent sample with a vendor close agrees.

    FMP's latest prints are its most reliable; its history is what goes wrong
    (back-adjusted bars stored as raw, another instrument's prices). A history
    back-adjusted after the last action agrees here and nowhere before it."""
    dated = [x for x in r["samples"] if x["sharadar"] is not None]
    if not dated:
        return False
    last = max(dated, key=lambda x: x["date"])
    return agrees(last["fmp"], last["sharadar"])


def disagreeing(samples: Sequence[dict]) -> list[dict]:
    """The samples where both vendors have a close and they differ."""
    return [
        x
        for x in samples
        if x["sharadar"] is not None and not agrees(x["fmp"], x["sharadar"])
    ]


def _decide(s: dict, results: list[dict]) -> Outcome:
    """The first basis with a winner decides; see the module docstring, step 4.

    - ``price_history``: at least four samples agree;
    - ``latest_price``: a candidate with four samples, its latest one agreeing;
    - ``identifier``: the only candidate sharing a CIK or CUSIP.

    Several winners on a price basis: an identifier picks one, else
    ``several_agree``. Several on the identifier basis decide nothing.
    """
    sid, sym = s["security_id"], s["primary_symbol"]
    tested = [r for r in results if r["of"] >= MIN_AGREED]
    bases = (
        ("price_history", [r for r in results if r["agreed"] >= MIN_AGREED]),
        ("latest_price", [r for r in tested if latest_agrees(r)]),
        ("identifier", [r for r in results if r["identifier"]]),
    )
    for basis, passing in bases:
        if not passing:
            continue
        if basis == "identifier":
            if len(passing) == 1:
                return _won(s, passing[0], basis, results)
            break
        if len(passing) > 1:
            with_id = [r for r in passing if r["identifier"]]
            if len(with_id) != 1:
                return Outcome(
                    sid,
                    sym,
                    "ambiguous",
                    "several_agree",
                    candidates=[_summary(r) for r in passing],
                )
            passing = with_id
        return _won(s, passing[0], basis, results)
    return Outcome(
        sid,
        sym,
        "unmatched",
        "price_disagrees" if tested else "insufficient_overlap",
        candidates=[_summary(r) for r in results],
    )


def _won(s: dict, r: dict, basis: str, results: list[dict]) -> Outcome:
    """The candidate ``r`` won on ``basis``: link it, unless another security's
    open link holds it (``duplicate_of_linked``, naming the holder)."""
    sid, sym = s["security_id"], s["primary_symbol"]
    c = r["candidate"]
    if c["taken"]:
        return Outcome(
            sid,
            sym,
            "ambiguous",
            "duplicate_of_linked",
            candidates=[
                {"vendor_key": c["vendor_key"], "linked_security": c.get("held_by")}
            ],
        )
    corroboration = (
        1.0
        if r["identifier"]
        else (r["name_similarity"] if r["name_similarity"] is not None else 0.0)
    )
    # An identifier link may rest on no sampled bar at all (gaps in our history).
    agreement = r["agreed"] / r["of"] if r["of"] else 0.0
    confidence = round(0.9 * agreement + 0.1 * corroboration, 3)
    return Outcome(
        sid,
        sym,
        "linked",
        basis,
        vendor_key=c["vendor_key"],
        confidence=min(confidence, 1.0),
        valid_from=overlap(s, c)[0],
        evidence={
            "basis": basis,
            "vendor_table": c["vendor_table"],
            "ticker": c["ticker"],
            "matched_on": sorted(_keys(s["tickers"]) & _keys(c["aliases"])),
            "samples": r["samples"],
            "agreed": r["agreed"],
            "of": r["of"],
            "identifier": r["identifier"],
            "name_similarity": r["name_similarity"],
            "others": [_summary(o) for o in results if o is not r],
        },
    )


def _settle_double_claims(outcomes: dict[int, Outcome], securities: dict) -> None:
    """A key several securities win is one security's at most.

    In the first production run these were rename chains: the row a missed rename
    left behind and the row minted for the new ticker, both holding the history FMP
    serves under either. When exactly one claimant is live and trades under the
    vendor's current ticker (742 of 905 keys), it is the security: it is linked,
    and every other claimant is ``duplicate_of_linked``, naming it, for `security
    merge`. Otherwise no claimant is linked (``key_claimed_twice``).
    """
    by_key: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes.values():
        if o.outcome == "linked":
            by_key[o.vendor_key].append(o)
    for key, claimants in by_key.items():
        if len(claimants) < 2:
            continue
        current = ticker_key(claimants[0].evidence["ticker"])
        winners = [
            o
            for o in claimants
            if securities[o.security_id].get("delisted_date") is None
            and ticker_key(securities[o.security_id]["primary_symbol"]) == current
        ]
        ids = sorted(o.security_id for o in claimants)
        winner = winners[0] if len(winners) == 1 else None
        if winner is not None:
            winner.evidence["duplicates"] = [i for i in ids if i != winner.security_id]
        for o in claimants:
            if o is winner:
                continue
            if winner is not None:
                o.outcome, o.reason = "ambiguous", "duplicate_of_linked"
                o.candidates = [
                    {"vendor_key": key, "linked_security": winner.security_id}
                ]
            else:
                o.outcome, o.reason = "ambiguous", "key_claimed_twice"
                o.candidates = [
                    {
                        "vendor_key": key,
                        "also_claimed_by": [i for i in ids if i != o.security_id],
                    }
                ]
            o.vendor_key = o.confidence = o.valid_from = None
            o.evidence = {}


def _write(db: Database, outcomes: Sequence[Outcome], run: RunLog) -> None:
    for o in outcomes:
        current = (
            None
            if o.outcome == "linked"
            else (UNMATCHED if o.outcome == "unmatched" else AMBIGUOUS)
        )
        note = f"vendor link run {run.run_id}: now {o.outcome}" + (
            f" ({o.reason})" if o.reason else ""
        )
        # Retire what no longer holds before writing what does: an open flag of
        # the other check, and one of the same check with another reason (it would
        # otherwise keep its slot, and the new reason would never be written). An
        # open flag with the same check and reason is kept, its detail refreshed.
        open_flags = repo.list_dq_flags(
            db,
            repo.DqFilter(checks=(UNMATCHED, AMBIGUOUS), security_ids=(o.security_id,)),
            limit=100,
        )
        stale, same = [], []
        for f in open_flags:
            kept = (
                f["check_name"] == current
                and (f.get("detail") or {}).get("reason") == o.reason
            )
            (same if kept else stale).append(f["dq_flag_id"])
        if stale:
            repo.resolve_dq_flags(
                db,
                repo.DqFilter(flag_ids=tuple(stale)),
                note=note,
                resolved_by=CREATED_BY,
            )
        if current is None:
            repo.insert_vendor_link(
                db,
                security_id=o.security_id,
                vendor=VENDOR,
                vendor_key=o.vendor_key,
                valid_from=o.valid_from,
                match_method=(
                    "identifier" if o.reason == "identifier" else "ticker_span_price"
                ),
                confidence=o.confidence,
                evidence=dict(o.evidence, run_id=run.run_id),
                created_by=CREATED_BY,
            )
            wrong = disagreeing(o.evidence["samples"])
            if wrong:
                repo.add_dq_flag_once(
                    db,
                    check_name=HISTORY,
                    severity="warn",
                    security_id=o.security_id,
                    table_name="core.daily_price",
                    record_key={"vendor": VENDOR, "vendor_key": o.vendor_key},
                    detail={
                        "vendor_key": o.vendor_key,
                        "basis": o.reason,
                        "agreed": o.evidence["agreed"],
                        "of": o.evidence["of"],
                        "samples": _sample_json(wrong),
                    },
                    ingestion_run_id=run.run_id,
                )
        elif same:
            # The same condition, still open: keep the flag, refresh its evidence
            # (a flag written before samples were kept has none).
            repo.refresh_open_flag_detail(
                db,
                same[0],
                {"reason": o.reason, "candidates": o.candidates},
                run.run_id,
            )
        else:
            repo.add_dq_flag_once(
                db,
                check_name=current,
                severity="info" if o.reason == "not_covered_asset_type" else "warn",
                security_id=o.security_id,
                table_name="core.security_vendor_xref",
                record_key={"vendor": VENDOR},
                detail={"reason": o.reason, "candidates": o.candidates},
                ingestion_run_id=run.run_id,
            )


def link(
    db: Database,
    source_for,
    *,
    security_ids: Optional[Sequence[int]] = None,
    new: bool = False,
    dry_run: bool = False,
) -> LinkResult:
    """Evaluate the securities in scope and, unless ``dry_run``, write the outcome.

    ``source_for(n)`` returns the :class:`CloseSource` for ``n`` candidate tickers.
    ``new`` narrows the scope to the securities first seen since the last
    successful full run. Without one there is nothing to be incremental to, so it
    does nothing and says so.
    """
    mode = "new" if new else ("symbols" if security_ids is not None else "full")
    result = LinkResult(dry_run=dry_run, mode=mode)
    since = None
    if new:
        last = repo.last_successful_run(db, VENDOR, ENDPOINT_FULL)
        if last is None:
            result.note = (
                "no full `fafnir vendor link` run yet: nothing to be incremental to"
            )
            return result
        since = last["started_at"]
    candidates = repo.vendor_link_candidates(db, VENDOR, PRICED_TABLES)
    if not candidates:
        raise LinkError(
            "core.vendor_security holds no Sharadar stocks or funds: "
            "`fafnir ingest sharadar-master` first"
        )
    securities = repo.link_scope(
        db, VENDOR, security_ids=security_ids, first_seen_since=since
    )
    result.already_linked = repo.count_linked(db, VENDOR)

    rejected = {
        (r["security_id"], r["vendor_key"]) for r in repo.vendor_rejections(db, VENDOR)
    }
    outcomes, result.price_source = evaluate(
        db, securities, candidates, source_for, rejected
    )
    result.outcomes = outcomes
    if dry_run:
        return result

    counts = result.counts()
    run = RunLog(
        db,
        source=VENDOR,
        endpoint=ENDPOINT_NEW if new else ENDPOINT_FULL,
        params={"mode": mode, "price_source": result.price_source},
    )
    with run:
        result.run_id = run.run_id
        _write(db, outcomes, run)
        run.rows_inserted = result.linked()
        run.symbols_requested = len(outcomes)
        run.params.update(
            evaluated=len(outcomes),
            to_correct=result.to_correct(),
            outcomes={f"{k[0]}:{k[1]}" if k[1] else k[0]: v for k, v in counts.items()},
        )
        db.commit()
    return result
