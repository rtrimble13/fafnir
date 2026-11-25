"""The audit sample that decides the dorq cutover (DE-03, DE-04; #268, #269).

dorq replaces the SQL checks in a domain only when it is significantly better in
both precision and recall (doc/plans/dorq-efficacy.md). Labels the queue derives
cannot show that: most come from the SQL checks' own dispositions, and the rest
were sampled from dorq's reports. An **audit cycle** is a sample neither engine
selected, drawn from one night of one domain, with every unit's probability of
being drawn known, so each estimate can be weighted back to the night.

**The unit.** A bar or a missing session, ``(security, trade date)``, on bars. A
statement period, ``(security, period type, fiscal date)``, on statements, or
``(security, "-", date)`` for a finding about no one period (a stale filer). A
report or flag is *keyed* at the unit its own key names: its security (each
member of a cross-sectional row) and the date it is about.

**The frame** of a night:

* every unit an engine keyed: dorq's conditions at warn and above (on statements,
  "as written": less those the ingest withholds, which are a stratum of their
  own), and the SQL checks' flags open when dorq's run started, new that night or
  standing;
* every unit **neither** keyed: on bars, each bar of the night's window and each
  missing session of a security that was trading, with those near the thresholds
  (a move of 30% to 50%, a session missing on a series that traded on 60% to 80%
  of the 20 sessions before the window) a stratum of their own; on statements,
  every period.

**The design.** Each stratum has a target ``n`` and so a rate ``n / N``. A unit's
inclusion probability is the largest rate of the strata it is in, and it is drawn
when ``hash(seed, unit) < probability``. The hash is MD5 of the unit's text key
(:func:`unit_hash`), computed in Python for the engines' units and in SQL for the
others, with the same result: the same night and seed give the same sample on any
host, and raising a target keeps every unit a smaller one drew.

**Blind verification.** Each unit records the data as it stood when drawn
(``data``) and, apart from it, what each engine said (``engines``). The verifier
sees the worksheet (:func:`worksheet`): the data, never the engines, the strata or
the probability. Item numbers follow the hash, not the strata. A verdict cites
the outside source and the value read, and becomes labels with the cycle, the
unit and the weight ``1 / probability`` (:func:`import_verdicts`). A random 10% of
the cycle is verified a second time, blind, by an independent agent; its
verdicts are kept on the unit, never as labels, and the cycle reports the
agreement.

**The gate** (:func:`gate`, ``dq compare --audit``): per engine, weighted
precision at warn and above and at error with Wilson intervals; recall on the
cycle's confirmed errors, and the paired difference with its one-sided 95% lower
bound (Newcombe's hybrid score method) and the exact McNemar test; dorq's
expected calibration error; the volume as written. Then one verdict line.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable, Optional, Sequence

from fafnir.db.connection import Database
from fafnir.dq import compare as cmp
from fafnir.dq import compare_statements as cst
from fafnir.dq import dorq as engine
from fafnir.dq import dorq_statements

DOMAINS = ("bars", "statements")
SEVERE = ("warn", "error")

# The bar, decided 2026-11-23 (doc/plans/dorq-efficacy.md, "Success bar").
PRECISION_WARN_BAR = 0.85
PRECISION_ERROR_BAR = 0.95
MIN_CONFIRMED_ERRORS = 100
VOLUME_BAR = 0.30
ECE_BAR = 0.05
AGREEMENT_BAR = 0.90
SECOND_SHARE = 0.10

Z_TWO_SIDED_95 = 1.959964
Z_ONE_SIDED_95 = 1.644854

#: A unit's hash is the first 13 hex digits of an MD5 (52 bits), as a fraction of
#: 2**52: exact in a double, so Python and Postgres compare it the same way.
HASH_DIGITS = 13
HASH_SCALE = float(2 ** (4 * HASH_DIGITS))

#: The oversample near the thresholds, among the bars neither engine keyed.
NEAR_MOVE = (0.30, 0.50)
NEAR_DENSITY = (0.6, 0.8)
DENSITY_SESSIONS = 20

#: How far a report or flag may sit from a unit and still be about it, for
#: recall (calendar days), as `dq compare` matches.
SLACK_DAYS = cmp.DEFAULT_SLACK_DAYS

#: The bars a worksheet shows on each side of a unit, and the reach for its
#: corporate actions (calendar days).
NEIGHBOUR_BARS = 5
ACTION_REACH_DAYS = 30

#: What a verifier may find on a bar, and the dorq codes that name it. A report is
#: *right* when its code is one its unit's fault names (an SQL flag: when one of
#: its successor codes is, :data:`fafnir.dq.compare.SUCCESSORS`). A diagnosis that
#: names the wrong fault is *other*: a bad print reported on a split nobody
#: recorded would delete correct bars.
BAR_FAULTS: dict[str, tuple[str, ...]] = {
    # a bar's prices differ from the outside source
    "bad_price": (
        "DQ101",
        "DQ102",
        "DQ104",
        "DQ106",
        "DQ108",
        "DQ201",
        "DQ204",
        "DQ209",
    ),
    # a stretch at the wrong scale (x100, a precision shift)
    "scale_error": ("DQ106", "DQ202", "DQ205"),
    # a stretch of another instrument's or a stale vendor's prices
    "wrong_history": ("DQ202", "DQ205", "DQ206"),
    # bars on the wrong dates
    "date_shift": ("DQ105", "DQ206"),
    # a bar for a day the security did not trade: a carry, a repeat, a non-session
    "phantom_bar": ("DQ103", "DQ105", "DQ501", "DQ502"),
    # a session it traded with no bar
    "missing_bar": ("DQ301", "DQ302", "DQ303", "DQ305"),
    # the feed stopped or repeats while the security trades
    "stale_feed": ("DQ301", "DQ303", "DQ304", "DQ501", "DQ502"),
    # the volume differs from the outside source
    "bad_volume": ("DQ107", "DQ401", "DQ402", "DQ403"),
    # a split happened and is not recorded; the bars are as traded
    "unreported_split": ("DQ203", "DQ601"),
    # a recorded split on the wrong date, with the wrong ratio, or that never was
    "split_misdated": ("DQ701",),
    "split_wrong": ("DQ702", "DQ703", "DQ704"),
    # a recorded distribution that is wrong
    "dividend_wrong": ("DQ705",),
}
#: What a verifier may find on a bar that is right: it matches the source; no bar,
#: and the security did not trade; a move the source confirms.
BAR_FACTS = ("matches_source", "no_trading", "real_move")

#: What a verifier may find on a statement period. Any statement code whose scope
#: names the wrong value or period is right: on statements the codes are
#: symptoms (a wrong revenue breaks an identity, jumps and ties at once).
STATEMENT_FAULTS = (
    "wrong_value",
    "scale_error",
    "ytd_as_quarter",
    "missing_period",
    "wrong_period",
    "duplicate_period",
    "stale_filer",
)
#: Right: the stored values match the filing; or they reproduce the filing
#: faithfully where the filing itself breaks an identity or a tie (decision 2,
#: 2026-11-23: not a data error).
STATEMENT_FACTS = ("matches_filing", "filing_fact", "no_filing")

#: The line items every statement unit's worksheet asks for, by statement. Each
#: unit adds the items any engine named on it, and two drawn at random, so the
#: list does not tell the verifier which items an engine named.
CORE_ITEMS: dict[str, tuple[str, ...]] = {
    "income": ("revenue", "net_income", "weighted_average_shares"),
    "balance_sheet": (
        "total_assets",
        "total_liabilities",
        "total_equity_parent",
        "cash_and_equivalents",
        "shares_outstanding",
    ),
    "cash_flow": ("net_cash_operating",),
}
RANDOM_ITEMS = 2


# ---------------------------------------------------------------------------
# The design
# ---------------------------------------------------------------------------

#: A unit: (security_id, date, period_type). period_type is None on bars.
Unit = tuple[int, dt.date, Optional[str]]


def unit_text(seed: int, domain: str, unit: Unit, salt: str = "") -> str:
    sid, day, period_type = unit
    return f"{seed}|{domain}|{sid}|{period_type or ''}|{day.isoformat()}{salt}"


def unit_hash(seed: int, domain: str, unit: Unit, salt: str = "") -> float:
    """The unit's draw, uniform on [0, 1): :data:`HASH_DIGITS` hex digits of the
    MD5 of :func:`unit_text`. :func:`_sql_hash` computes the same in Postgres."""
    digest = hashlib.md5(unit_text(seed, domain, unit, salt).encode()).hexdigest()
    return int(digest[:HASH_DIGITS], 16) / HASH_SCALE


def _sql_hash(sid: str, day: str, period_type: str = "''") -> str:
    """:func:`unit_hash` in SQL, over the named columns; ``%(seed)s`` and
    ``%(domain)s`` are parameters."""
    text = (
        f"%(seed)s::text || '|' || %(domain)s || '|' || {sid}::text || '|' || "
        f"coalesce({period_type}, '') || '|' || to_char({day}, 'YYYY-MM-DD')"
    )
    return (
        f"(('x' || substr(md5({text}), 1, {HASH_DIGITS}))::bit({4 * HASH_DIGITS})"
        f"::bigint::float8 / {HASH_SCALE!r})"
    )


@dataclass(frozen=True)
class Targets:
    """How many units each stratum should give, in expectation."""

    per_code: int  # each dorq code, at warn and above
    errors: int  # dorq at error, over every code
    withheld: int  # dorq's reports the statements ingest withholds
    per_check: int  # each SQL check, new and standing each
    random: int  # units neither engine keyed
    near: int  # of those, near the thresholds (bars)

    def of(self, stratum: str) -> int:
        if stratum == "dorq:error":
            return self.errors
        if stratum == "dorq:withheld":
            return self.withheld
        if stratum.startswith("dorq:"):
            return self.per_code
        if stratum.startswith("sql:"):
            return self.per_check
        if stratum == "none:near":
            return self.near
        return self.random


DEFAULT_TARGETS = {
    "bars": Targets(
        per_code=8, errors=25, withheld=0, per_check=10, random=40, near=25
    ),
    "statements": Targets(
        per_code=6, errors=20, withheld=10, per_check=6, random=30, near=0
    ),
}


def rates(sizes: dict[str, int], targets: Targets) -> dict[str, float]:
    """Each stratum's rate: its target over its frame, at most 1."""
    out = {}
    for stratum, n_frame in sizes.items():
        want = targets.of(stratum)
        out[stratum] = min(1.0, want / n_frame) if n_frame and want > 0 else 0.0
    return out


def inclusion(strata: Iterable[str], rate: dict[str, float]) -> float:
    """A unit's inclusion probability: the largest rate of its strata."""
    return max((rate.get(s, 0.0) for s in strata), default=0.0)


def draw_units(
    members: dict[Unit, set[str]], rate: dict[str, float], seed: int, domain: str
) -> dict[Unit, float]:
    """The engines' units drawn, with their inclusion probabilities."""
    out = {}
    for unit, strata in members.items():
        p = inclusion(strata, rate)
        if p > 0 and unit_hash(seed, domain, unit) < p:
            out[unit] = p
    return out


def second_subset(units: Sequence[Unit], seed: int, domain: str) -> set[Unit]:
    """The units drawn for the blind second verification: the tenth of the cycle
    (rounded up) with the smallest second hash."""
    if not units:
        return set()
    k = math.ceil(SECOND_SHARE * len(units))
    ranked = sorted(units, key=lambda u: unit_hash(seed, domain, u, "|second"))
    return set(ranked[:k])


# ---------------------------------------------------------------------------
# Statistics: weighted proportions, the paired test, calibration
# ---------------------------------------------------------------------------


def kish(weights: Sequence[float]) -> float:
    """Kish's effective sample size: (sum w)^2 / sum w^2. n for equal weights."""
    total = sum(weights)
    squares = sum(w * w for w in weights)
    return total * total / squares if squares else 0.0


def wilson(p: float, n: float, z: float = Z_TWO_SIDED_95) -> tuple[float, float]:
    """The Wilson score interval for a proportion ``p`` on ``n`` (effective)
    observations."""
    if n <= 0:
        return 0.0, 1.0
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Estimate:
    """A weighted proportion: Hajek's ratio, with a Wilson interval on Kish's
    effective n."""

    hits: float = 0.0  # weighted
    total: float = 0.0  # weighted
    count: int = 0  # unweighted
    weights: list[float] = field(default_factory=list)

    def add(self, hit: bool, weight: float = 1.0) -> None:
        self.hits += weight if hit else 0.0
        self.total += weight
        self.count += 1
        self.weights.append(weight)

    @property
    def value(self) -> Optional[float]:
        return self.hits / self.total if self.total else None

    @property
    def n_eff(self) -> float:
        return kish(self.weights)

    @property
    def interval(self) -> Optional[tuple[float, float]]:
        if self.value is None:
            return None
        return wilson(self.value, self.n_eff)

    def as_dict(self) -> dict:
        low_high = self.interval
        return {
            "value": self.value,
            "low": low_high[0] if low_high else None,
            "high": low_high[1] if low_high else None,
            "count": self.count,
            "n_eff": round(self.n_eff, 2),
        }


def newcombe_paired(
    a: float, b: float, c: float, d: float, n_eff: float, z: float = Z_ONE_SIDED_95
) -> Optional[tuple[float, float, float]]:
    """The difference of two paired proportions, (b - c) / (a + b + c + d), with
    Newcombe's hybrid score interval (1998, method 10) at ``z``. The cells are
    weighted sums: a both, b the first only, c the second only, d neither; the
    Wilson bounds use the effective n. Returns (difference, lower, upper)."""
    total = a + b + c + d
    if total <= 0:
        return None
    p1, p2 = (a + b) / total, (a + c) / total
    l1, u1 = wilson(p1, n_eff, z)
    l2, u2 = wilson(p2, n_eff, z)
    denom = (a + b) * (c + d) * (a + c) * (b + d)
    phi = (a * d - b * c) / math.sqrt(denom) if denom > 0 else 0.0
    delta = p1 - p2
    lower = delta - math.sqrt(
        max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2)
    )
    upper = delta + math.sqrt(
        max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2)
    )
    return delta, lower, upper


def mcnemar_one_sided(first_only: int, second_only: int) -> float:
    """The exact one-sided McNemar p-value that the first engine finds more:
    P(Binomial(b + c, 1/2) >= b), on unweighted discordant counts. 1 with none."""
    n = first_only + second_only
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(first_only, n + 1)) / 2**n


def ece(points: Sequence[tuple[float, bool, float]], bins: int = 10) -> Optional[float]:
    """Expected calibration error over (p_error, right, weight) points: the
    weighted mean |mean p - observed rate| over ``bins`` equal-width bins, each
    weighted by its share of the weight. dorq-eval's measure, weighted."""
    total = sum(w for _, _, w in points)
    if not total:
        return None
    sums: dict[int, list[float]] = {}
    for p, hit, w in points:
        k = min(bins - 1, max(0, int(p * bins)))
        acc = sums.setdefault(k, [0.0, 0.0, 0.0])
        acc[0] += w * p
        acc[1] += w * (1.0 if hit else 0.0)
        acc[2] += w
    return sum(
        (acc[2] / total) * abs(acc[0] / acc[2] - acc[1] / acc[2])
        for acc in sums.values()
    )


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------


@dataclass
class Night:
    """The night a cycle samples, and when its engines' outputs are read as of."""

    domain: str
    shadow_file: Path
    as_of: dt.date
    run_id: Optional[int] = None
    dorq_version: str = ""
    state_at: Optional[dt.datetime] = None
    previous_start: Optional[dt.datetime] = None
    since: Optional[dt.date] = None  # bars: the window reported

    @classmethod
    def of(cls, db: Database, domain: str, shadow_file: Path) -> "Night":
        as_of = cmp._date(Path(shadow_file).stem)
        if as_of is None:
            raise ValueError(f"{shadow_file}: not a dated shadow file")
        endpoint = (
            engine.WATERMARK_SHADOW
            if domain == "bars"
            else dorq_statements.ENDPOINT_SHADOW
        )
        run = engine.night_run(db, endpoint, as_of) or {}
        params = run.get("params") or {}
        since = cmp._date(run.get("since")) or run.get("window_from")
        return cls(
            domain=domain,
            shadow_file=Path(shadow_file),
            as_of=as_of,
            run_id=run.get("ingestion_run_id"),
            dorq_version=str(params.get("dorq_version") or ""),
            state_at=run.get("started_at"),
            previous_start=engine.previous_night_start(db, endpoint, as_of),
            since=since,
        )


@dataclass
class Frame:
    """The engines' units with their strata, the sizes of every stratum, and the
    volume each engine would have written."""

    night: Night
    members: dict[Unit, set[str]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    volume: dict = field(default_factory=dict)

    def add(self, unit: Unit, stratum: str) -> None:
        self.members.setdefault(unit, set()).add(stratum)

    def count_sizes(self) -> None:
        sizes: dict[str, int] = {}
        for strata in self.members.values():
            for s in strata:
                sizes[s] = sizes.get(s, 0) + 1
        self.sizes.update(sorted(sizes.items()))


def _keyed_bar_units(r: cmp.Report) -> list[Unit]:
    if r.security_id is not None:
        return [(r.security_id, r.first, None)]
    return [(m, r.first, None) for m in r.members]


def _bars_sql_flags(db: Database, night: Night) -> list[dict]:
    """The SQL bar checks' flags at warn and above open when dorq's run started."""
    return db.fetchall(
        """
        SELECT dq_flag_id, check_name, security_id, severity, detected_at,
               COALESCE(record_key->>'trade_date', record_key->>'last_date')::date AS d
          FROM ops.data_quality_flag
         WHERE check_name = ANY(%(checks)s) AND security_id IS NOT NULL
           AND severity IN ('warn', 'error')
           AND detected_at <= COALESCE(%(t)s::timestamptz, 'infinity')
           AND (resolved_at IS NULL
                OR resolved_at > COALESCE(%(t)s::timestamptz, 'infinity'))
           AND (accepted_at IS NULL
                OR accepted_at > COALESCE(%(t)s::timestamptz, 'infinity'))
        """,
        {"checks": list(cmp.SUCCESSORS), "t": night.state_at},
    )


def _is_new(detected_at, night: Night) -> bool:
    return night.previous_start is not None and detected_at > night.previous_start


def bars_frame(db: Database, night: Night) -> Frame:
    """The engines' units of a bars night, by stratum, and the volume as written."""
    out = Frame(night)
    keys: set[tuple] = set()
    for record in engine.read_records(night.shadow_file):
        r = cmp.Report.of(record)
        if r is None or r.severity not in SEVERE:
            continue
        keys.add(r.key)
        for unit in _keyed_bar_units(r):
            out.add(unit, f"dorq:{r.code}")
            if r.severity == "error":
                out.add(unit, "dorq:error")
    sql_new = 0
    flags = _bars_sql_flags(db, night)
    for f in flags:
        if f["d"] is None:
            continue
        new = _is_new(f["detected_at"], night)
        sql_new += new
        out.add(
            (int(f["security_id"]), f["d"], None),
            f"sql:{f['check_name']}:{'new' if new else 'standing'}",
        )
    previous = cmp.previous_shadow_file(night.shadow_file)
    dorq_new = None
    if previous is not None:
        before = {
            engine.condition_key(x)
            for x in engine.read_records(previous)
            if "check_name" in x
        }
        dorq_new = len(keys - before)
    out.volume = _volume(len(keys), dorq_new, len(flags), sql_new, previous)
    out.count_sizes()
    return out


def _statement_unit(sid: int, period_type: Optional[str], day: dt.date) -> Unit:
    return (sid, day, period_type if period_type in ("annual", "quarter") else "-")


def _statements_sql_flags(db: Database, night: Night) -> tuple[int, list[dict]]:
    """The thirteen SQL checks' flags at warn and above open when dorq's run
    started: how many, and those placed as `dq export-labels` places them."""
    from fafnir.dq import labels as lb

    rows = db.fetchall(
        """
        SELECT dq_flag_id, check_name, security_id, table_name, record_key, detail,
               severity, detected_at,
               COALESCE(%(t)s::timestamptz, now()) AS decided_at
          FROM ops.data_quality_flag
         WHERE check_name = ANY(%(checks)s) AND security_id IS NOT NULL
           AND severity IN ('warn', 'error')
           AND detected_at <= COALESCE(%(t)s::timestamptz, now())
           AND (resolved_at IS NULL OR resolved_at > COALESCE(%(t)s::timestamptz, now()))
           AND (accepted_at IS NULL OR accepted_at > COALESCE(%(t)s::timestamptz, now()))
        """,
        {"checks": list(cst.SUCCESSORS), "t": night.state_at},
    )
    return len(rows), [
        {**f, "placed": where} for f, where in lb.place_statement_flags(db, rows)
    ]


def statements_frame(db: Database, night: Night) -> Frame:
    """The engines' units of a statements night, by stratum, and the volume as
    written. The night's file is read a row at a time (#219)."""
    out = Frame(night)
    accepted = cst.AcceptedPredecessors.load(db, night.state_at)
    written: set[int] = set()
    # New against the previous night by condition atoms (fafnir DE-16).
    previous = cmp.previous_shadow_file(night.shadow_file)
    before = set(cst._atom_hashes(previous)) if previous is not None else None
    written_new: set[int] = set()
    for record in engine.read_records(night.shadow_file):
        r = cst.StatementReport.of(record)
        if r is None or r.security_id is None or r.severity not in SEVERE:
            continue
        unit = _statement_unit(r.security_id, r.period_type, r.first)
        if accepted.carries(r):
            out.add(unit, "dorq:withheld")
            continue
        written.add(cst._key_hash(r.key))
        if before is not None and cst.is_new(record, before):
            written_new.add(cst._key_hash(r.key))
        out.add(unit, f"dorq:{r.code}")
        if r.severity == "error":
            out.add(unit, "dorq:error")
    del accepted
    sql_standing, flags = _statements_sql_flags(db, night)
    sql_new = 0
    for f in flags:
        code, scope, first, _last = f["placed"]
        new = _is_new(f["detected_at"], night)
        sql_new += new
        out.add(
            _statement_unit(int(f["security_id"]), scope.get("period_type"), first),
            f"sql:{f['check_name']}:{'new' if new else 'standing'}",
        )
    dorq_new = len(written_new) if previous is not None else None
    out.volume = _volume(len(written), dorq_new, sql_standing, sql_new, previous)
    out.count_sizes()
    return out


def _volume(dorq_standing, dorq_new, sql_standing, sql_new, previous) -> dict:
    """What each engine would have put in the queue: its conditions at warn and
    above (dorq's as written), standing and new against the previous night."""

    def ratio(a, b):
        return a / b if a is not None and b else None

    return {
        "standing": {
            "dorq": dorq_standing,
            "sql": sql_standing,
            "ratio": ratio(dorq_standing, sql_standing),
        },
        "new": {
            "dorq": dorq_new,
            "sql": sql_new if previous is not None else None,
            "ratio": ratio(dorq_new, sql_new) if previous is not None else None,
            "previous": previous.name if previous is not None else None,
        },
        "bar": VOLUME_BAR,
    }


# The units neither engine keyed, in SQL. ``tmp_audit_engine`` holds the engines'.

_ENGINE_TEMP = """
CREATE TEMP TABLE IF NOT EXISTS tmp_audit_engine (
    security_id bigint, d date, period_type text
) ON COMMIT DROP
"""

_BARS_NEITHER = """
WITH sessions AS (
    SELECT trade_date AS d FROM ref.trading_calendar
     WHERE exchange_code = %(exchange)s AND is_open
       AND trade_date BETWEEN %(lo)s AND %(hi)s
), trading AS (
    -- securities fed in the sessions before the window, with their density
    SELECT p.security_id, count(*)::float8 / %(look)s AS density
      FROM core.daily_price p
      JOIN ref.trading_calendar c ON c.exchange_code = %(exchange)s
           AND c.trade_date = p.trade_date AND c.is_open
     WHERE p.trade_date BETWEEN %(look_lo)s AND %(look_hi)s
     GROUP BY 1
), bars AS (
    SELECT p.security_id, p.trade_date AS d,
           abs(p.close / NULLIF(lag(p.close) OVER w, 0) - 1) AS move
      FROM core.daily_price p
     WHERE p.trade_date BETWEEN %(look_lo)s AND %(hi)s
    WINDOW w AS (PARTITION BY p.security_id ORDER BY p.trade_date)
), units AS (
    SELECT security_id, d, COALESCE(move BETWEEN %(move_lo)s AND %(move_hi)s, false) AS near
      FROM bars WHERE d BETWEEN %(lo)s AND %(hi)s
    UNION ALL
    SELECT t.security_id, s.d, t.density BETWEEN %(density_lo)s AND %(density_hi)s
      FROM trading t
     CROSS JOIN sessions s
      JOIN core.security sec ON sec.security_id = t.security_id
     WHERE NOT EXISTS (SELECT 1 FROM core.daily_price p
                        WHERE p.security_id = t.security_id AND p.trade_date = s.d)
       AND (sec.delisted_date IS NULL OR sec.delisted_date >= s.d)
       AND (sec.feed_frozen_on IS NULL OR sec.feed_frozen_on > s.d)
), neither AS (
    SELECT u.security_id, u.d, NULL::text AS period_type, u.near FROM units u
     WHERE NOT EXISTS (SELECT 1 FROM tmp_audit_engine e
                        WHERE e.security_id = u.security_id AND e.d = u.d)
)
"""

_STATEMENTS_NEITHER = """
WITH periods AS (
    SELECT security_id, period_type, fiscal_date FROM core.income_statement
    UNION
    SELECT security_id, period_type, fiscal_date FROM core.balance_sheet
    UNION
    SELECT security_id, period_type, fiscal_date FROM core.cash_flow_statement
), neither AS (
    SELECT p.security_id, p.fiscal_date AS d, p.period_type, false AS near
      FROM periods p
     WHERE NOT EXISTS (SELECT 1 FROM tmp_audit_engine e
                        WHERE e.security_id = p.security_id
                          AND e.period_type = p.period_type
                          AND e.d = p.fiscal_date)
)
"""


def _load_engine_units(db: Database, units: Iterable[Unit]) -> None:
    with db.conn.cursor() as cur:
        cur.execute(_ENGINE_TEMP)
        cur.execute("TRUNCATE tmp_audit_engine")
        with cur.copy(
            "COPY tmp_audit_engine (security_id, d, period_type) FROM STDIN"
        ) as copy:
            for sid, day, period_type in units:
                copy.write_row((sid, day, period_type))


def neither_units(
    db: Database,
    frame: Frame,
    targets: Targets,
    seed: int,
    *,
    exchange_code: str,
) -> tuple[dict[str, int], dict[Unit, tuple[float, list[str]]]]:
    """The strata of units neither engine keyed, and those drawn, each with its
    inclusion probability and strata. Run inside a transaction (a temp table)."""
    night = frame.night
    _load_engine_units(db, frame.members)
    if night.domain == "bars":
        lo = night.since or night.as_of
        look = db.fetchall(
            """
            SELECT trade_date FROM ref.trading_calendar
             WHERE exchange_code = %s AND is_open AND trade_date < %s
             ORDER BY trade_date DESC LIMIT %s
            """,
            (exchange_code, lo, DENSITY_SESSIONS),
        )
        days = [r["trade_date"] for r in look] or [lo]
        sql = _BARS_NEITHER
        params = {
            "exchange": exchange_code,
            "lo": lo,
            "hi": night.as_of,
            "look_lo": min(days),
            "look_hi": max(days),
            "look": max(1, len(look)),
            "move_lo": NEAR_MOVE[0],
            "move_hi": NEAR_MOVE[1],
            "density_lo": NEAR_DENSITY[0],
            "density_hi": NEAR_DENSITY[1],
        }
    else:
        sql = _STATEMENTS_NEITHER
        params = {}
    params.update({"seed": seed, "domain": night.domain})
    counts = db.fetchone(
        sql + "SELECT count(*) AS n, count(*) FILTER (WHERE near) AS near FROM neither",
        params,
    )
    sizes = {"none:random": int(counts["n"] or 0)}
    if night.domain == "bars":
        sizes["none:near"] = int(counts["near"] or 0)
    rate = rates(sizes, targets)
    p_random, p_near = rate.get("none:random", 0.0), rate.get("none:near", 0.0)
    rows = db.fetchall(
        sql + f"""
        SELECT security_id, d, period_type, near FROM neither
         WHERE {_sql_hash("security_id", "d", "period_type")}
               < CASE WHEN near THEN greatest(%(p_random)s, %(p_near)s)
                      ELSE %(p_random)s END
        """,
        {**params, "p_random": p_random, "p_near": p_near},
    )
    drawn = {}
    for row in rows:
        unit = (int(row["security_id"]), row["d"], row["period_type"])
        strata = ["none:random"] + (["none:near"] if row["near"] else [])
        drawn[unit] = (inclusion(strata, rate), strata)
    return sizes, drawn


# ---------------------------------------------------------------------------
# The snapshot: what the verifier is shown, and what the engines said
# ---------------------------------------------------------------------------


def _securities(db: Database, units: Sequence[Unit]) -> dict[int, dict]:
    rows = db.fetchall(
        """
        SELECT security_id, primary_symbol, company_name, exchange_code, cik,
               asset_type, is_etf, is_fund, delisted_date, feed_frozen_on
          FROM core.security WHERE security_id = ANY(%s)
        """,
        (sorted({u[0] for u in units}),),
    )
    return {int(r["security_id"]): r for r in rows}


def _tickers(db: Database, units: Sequence[Unit]) -> dict[tuple, list[str]]:
    rows = db.fetchall(
        """
        SELECT u.sid, u.d, x.symbol
          FROM unnest(%s::bigint[], %s::date[]) AS u(sid, d)
          JOIN core.symbol_xref x ON x.security_id = u.sid
               AND x.valid_from <= u.d AND (x.valid_to IS NULL OR x.valid_to >= u.d)
         ORDER BY x.is_primary DESC, x.symbol
        """,
        ([u[0] for u in units], [u[1] for u in units]),
    )
    out: dict[tuple, list[str]] = {}
    for r in rows:
        out.setdefault((int(r["sid"]), r["d"]), []).append(r["symbol"])
    return out


def _num(value):
    if value is None:
        return None
    f = float(value)
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def bars_data(db: Database, units: Sequence[Unit], exchange_code: str) -> dict:
    """Per unit, the data as it stands: the security, its tickers that day, the
    bar (or none), five bars either side, the corporate actions within a month,
    and whether the day was a session."""
    if not units:
        return {}
    securities = _securities(db, units)
    tickers = _tickers(db, units)
    sids, days = [u[0] for u in units], [u[1] for u in units]
    bars = db.fetchall(
        """
        SELECT u.sid, u.d, p.trade_date, p.open, p.high, p.low, p.close, p.volume,
               p.source, p.loaded_at
          FROM unnest(%s::bigint[], %s::date[]) AS u(sid, d)
          JOIN core.daily_price p ON p.security_id = u.sid
               AND p.trade_date BETWEEN u.d - 21 AND u.d + 21
         ORDER BY u.sid, u.d, p.trade_date
        """,
        (sids, days),
    )
    actions = db.fetchall(
        f"""
        SELECT u.sid, u.d, a.action_type, a.ex_date, a.split_numerator,
               a.split_denominator, a.dividend_amount, a.source
          FROM unnest(%s::bigint[], %s::date[]) AS u(sid, d)
          JOIN core.corporate_action a ON a.security_id = u.sid
               AND a.ex_date BETWEEN u.d - {ACTION_REACH_DAYS:d}
                                 AND u.d + {ACTION_REACH_DAYS:d}
         ORDER BY u.sid, u.d, a.ex_date
        """,
        (sids, days),
    )
    sessions = {
        r["trade_date"]: bool(r["is_open"])
        for r in db.fetchall(
            "SELECT trade_date, is_open FROM ref.trading_calendar"
            " WHERE exchange_code = %s AND trade_date = ANY(%s)",
            (exchange_code, sorted(set(days))),
        )
    }
    near: dict[tuple, list[dict]] = {}
    for b in bars:
        near.setdefault((int(b["sid"]), b["d"]), []).append(b)
    acts: dict[tuple, list[dict]] = {}
    for a in actions:
        acts.setdefault((int(a["sid"]), a["d"]), []).append(
            {
                "type": a["action_type"],
                "ex_date": a["ex_date"].isoformat(),
                "ratio": (
                    f"{_num(a['split_numerator'])}:{_num(a['split_denominator'])}"
                    if a["split_numerator"] is not None
                    else None
                ),
                "amount": _num(a["dividend_amount"]),
                "source": a["source"],
            }
        )
    out = {}
    for unit in units:
        sid, day, _ = unit
        sec = securities.get(sid) or {}
        rows = near.get((sid, day), [])
        bar = next((b for b in rows if b["trade_date"] == day), None)
        before = [b for b in rows if b["trade_date"] < day][-NEIGHBOUR_BARS:]
        after = [b for b in rows if b["trade_date"] > day][:NEIGHBOUR_BARS]
        out[unit] = {
            **_security_data(sec, tickers.get((sid, day))),
            "date": day.isoformat(),
            "session": sessions.get(day),
            "bar": _bar(bar),
            "before": [_bar(b, short=True) for b in before],
            "after": [_bar(b, short=True) for b in after],
            "actions": acts.get((sid, day), []),
        }
    return out


def _security_data(sec: dict, tickers: Optional[list[str]]) -> dict:
    return {
        "ticker": (tickers or [None])[0] or sec.get("primary_symbol"),
        "tickers": tickers or [],
        "company": sec.get("company_name"),
        "exchange": sec.get("exchange_code"),
        "cik": sec.get("cik"),
        "asset_type": sec.get("asset_type"),
        "is_etf": sec.get("is_etf"),
        "is_fund": sec.get("is_fund"),
        "delisted": _iso(sec.get("delisted_date")),
        "feed_frozen_on": _iso(sec.get("feed_frozen_on")),
    }


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _bar(b: Optional[dict], *, short: bool = False) -> Optional[dict]:
    if b is None:
        return None
    if short:
        return {
            "date": b["trade_date"].isoformat(),
            "close": _num(b["close"]),
            "volume": _num(b["volume"]),
        }
    return {
        "open": _num(b["open"]),
        "high": _num(b["high"]),
        "low": _num(b["low"]),
        "close": _num(b["close"]),
        "volume": _num(b["volume"]),
        "source": b["source"],
        "loaded_at": _iso(b["loaded_at"]),
    }


_STATEMENT_META = {
    "security_id",
    "vendor_key",
    "period_type",
    "fiscal_date",
    "calendar_date",
    "fiscal_year",
    "fiscal_period",
    "period_start_date",
    "version_no",
    "known_from",
    "known_to",
    "knowledge_source",
    "filing_date",
    "loaded_at",
    "superseded_at",
    "reported_currency",
    "content_hash",
    "extra",
    "source",
    "ingestion_run_id",
}


def statement_items(db: Database) -> dict[str, list[str]]:
    """The line items of each statement, as ref.statement_line_item names them."""
    out: dict[str, list[str]] = {}
    for r in db.fetchall(
        "SELECT statement, column_name FROM ref.statement_line_item"
        " ORDER BY statement, column_name"
    ):
        out.setdefault(r["statement"], []).append(r["column_name"])
    return out


def items_to_verify(
    unit: Unit,
    named: Iterable[tuple[Optional[str], str]],
    all_items: dict[str, list[str]],
    seed: int,
) -> dict[str, list[str]]:
    """The line items a statement unit's worksheet asks for, by statement: the
    core set, the items an engine named (an item with no statement goes to each
    statement that has it), and :data:`RANDOM_ITEMS` more of each statement,
    drawn by the seed."""
    if unit[2] == "-":
        return {}
    out = {s: set(items) for s, items in CORE_ITEMS.items()}
    for statement, item in named:
        for s, items in all_items.items():
            if item in items and statement in (None, s):
                out.setdefault(s, set()).add(item)
    for s, items in all_items.items():
        rest = sorted(
            (i for i in items if i not in out.get(s, ())),
            key=lambda i: hashlib.md5(
                f"{unit_text(seed, 'statements', unit)}|{s}|{i}".encode()
            ).hexdigest(),
        )
        out.setdefault(s, set()).update(rest[:RANDOM_ITEMS])
    return {s: sorted(v) for s, v in sorted(out.items())}


def statements_data(
    db: Database, units: Sequence[Unit], verify: dict[Unit, dict[str, list[str]]]
) -> dict:
    """Per unit, the data as it stands: the security, the periods on file within
    two years, and for a period every version of each statement with the values
    of the items to verify."""
    if not units:
        return {}
    securities = _securities(db, units)
    tickers = _tickers(db, units)
    sids = [u[0] for u in units]
    days = [u[1] for u in units]
    pts = [u[2] for u in units]
    periods = db.fetchall(
        """
        SELECT u.sid, u.d, s.period_type, s.fiscal_date, s.fiscal_year,
               s.fiscal_period, min(s.filing_date) AS filed, max(s.version_no) AS versions
          FROM unnest(%s::bigint[], %s::date[]) AS u(sid, d)
          JOIN core.income_statement s ON s.security_id = u.sid
               AND s.fiscal_date BETWEEN u.d - 731 AND u.d + 731
         GROUP BY 1, 2, 3, 4, 5, 6
         ORDER BY 1, 2, 4, 3
        """,
        (sids, days),
    )
    by_unit_periods: dict[tuple, list[dict]] = {}
    for p in periods:
        by_unit_periods.setdefault((int(p["sid"]), p["d"]), []).append(
            {
                "period_type": p["period_type"],
                "fiscal_date": p["fiscal_date"].isoformat(),
                "fiscal_year": p["fiscal_year"],
                "fiscal_period": p["fiscal_period"],
                "filed": _iso(p["filed"]),
                "versions": p["versions"],
            }
        )
    versions: dict[tuple, dict[str, list[dict]]] = {}
    for statement, table in dorq_statements.STATEMENT_TABLE.items():
        rows = db.fetchall(
            f"""
            SELECT u.sid, u.d, u.pt, to_jsonb(s) AS row
              FROM unnest(%s::bigint[], %s::date[], %s::text[]) AS u(sid, d, pt)
              JOIN {table} s ON s.security_id = u.sid AND s.fiscal_date = u.d
                   AND s.period_type = u.pt
             ORDER BY u.sid, u.d, s.version_no
            """,
            (sids, days, pts),
        )
        for r in rows:
            unit = (int(r["sid"]), r["d"], r["pt"])
            row = r["row"]
            wanted = verify.get(unit, {}).get(statement, [])
            versions.setdefault(unit, {}).setdefault(statement, []).append(
                {
                    "version_no": row.get("version_no"),
                    "known_from": row.get("known_from"),
                    "filing_date": row.get("filing_date"),
                    "fiscal_year": row.get("fiscal_year"),
                    "fiscal_period": row.get("fiscal_period"),
                    "currency": row.get("reported_currency"),
                    "source": row.get("source"),
                    "values": {i: row.get(i) for i in wanted},
                }
            )
    out = {}
    for unit in units:
        sid, day, period_type = unit
        sec = securities.get(sid) or {}
        out[unit] = {
            **_security_data(sec, tickers.get((sid, day))),
            "fiscal_date": day.isoformat(),
            "period_type": period_type,
            "periods": by_unit_periods.get((sid, day), []),
            "statements": versions.get(unit, {}),
            "items": verify.get(unit, {}),
        }
    return out


def _bars_engines(db: Database, night: Night, units: Sequence[Unit]) -> dict:
    """Per unit, what each engine said about it: dorq's reports on the security
    that cover the date within the slack, every severity; the SQL checks' flags
    within the slack detected by the run's start, in whatever state."""
    slack = dt.timedelta(days=SLACK_DAYS)
    wanted = {u[0] for u in units}
    reports: dict[int, list[cmp.Report]] = {}
    for record in engine.read_records(night.shadow_file):
        r = cmp.Report.of(record)
        if r is None:
            continue
        for sid in r.securities:
            if sid in wanted:
                reports.setdefault(sid, []).append(r)
    flags = db.fetchall(
        """
        SELECT f.dq_flag_id, f.check_name, f.security_id, f.severity, f.detected_at,
               f.resolved_at, f.accepted_at,
               COALESCE(f.record_key->>'trade_date', f.record_key->>'last_date')::date AS d
          FROM ops.data_quality_flag f
         WHERE f.check_name = ANY(%(checks)s) AND f.security_id = ANY(%(sids)s)
           AND f.detected_at <= COALESCE(%(t)s::timestamptz, 'infinity')
        """,
        {"checks": list(cmp.SUCCESSORS), "sids": sorted(wanted), "t": night.state_at},
    )
    by_sid: dict[int, list[dict]] = {}
    for f in flags:
        if f["d"] is not None:
            by_sid.setdefault(int(f["security_id"]), []).append(f)
    out = {}
    for unit in units:
        sid, day, _ = unit
        dorq = [
            {
                "code": r.code,
                "check": r.check_name,
                "severity": r.severity,
                "p_error": r.p_error,
                "first": r.first.isoformat(),
                "last": r.last.isoformat(),
                "keyed": r.first == day,
                "cohort": r.security_id is None,
            }
            for r in reports.get(sid, ())
            if r.covers(day, slack)
        ]
        sql = [
            {
                "id": int(f["dq_flag_id"]),
                "check": f["check_name"],
                "severity": f["severity"],
                "date": f["d"].isoformat(),
                "keyed": f["d"] == day,
                "open": _open_at(f, night.state_at),
                "new": _is_new(f["detected_at"], night),
            }
            for f in by_sid.get(sid, ())
            if abs(f["d"] - day) <= slack
        ]
        out[unit] = {"dorq": dorq, "sql": sql}
    return out


def _open_at(f: dict, t: Optional[dt.datetime]) -> bool:
    def after(x):
        return x is None or (t is not None and x > t)

    return after(f["resolved_at"]) and after(f["accepted_at"])


def _covers_statement(
    sid_period: tuple[Optional[str], dt.date],
    period_type: Optional[str],
    first: dt.date,
    last: dt.date,
) -> bool:
    unit_pt, day = sid_period
    pad = dt.timedelta(days=SLACK_DAYS)
    same = unit_pt == "-" or period_type in (None, "", unit_pt)
    return same and first - pad <= day <= last + pad


def _statements_engines(db: Database, night: Night, units: Sequence[Unit]) -> dict:
    """Per unit, what each engine said about it, as for bars: dorq's reports on
    the security of the unit's period type (or none) whose fiscal dates reach the
    unit within the slack, marked when the ingest would withhold them; the SQL
    checks' flags, placed, likewise."""
    from fafnir.dq import labels as lb

    wanted: dict[int, list[Unit]] = {}
    for u in units:
        wanted.setdefault(u[0], []).append(u)
    accepted = cst.AcceptedPredecessors.load(db, night.state_at)
    out: dict[Unit, dict] = {u: {"dorq": [], "sql": []} for u in units}
    for record in engine.read_records(night.shadow_file):
        sid = record.get("security_id")
        if sid is None or int(sid) not in wanted:
            continue
        r = cst.StatementReport.of(record)
        if r is None:
            continue
        for unit in wanted[int(sid)]:
            if not _covers_statement(
                (unit[2], unit[1]), r.period_type, r.first, r.last
            ):
                continue
            keyed = _statement_unit(r.security_id, r.period_type, r.first) == unit
            out[unit]["dorq"].append(
                {
                    "code": r.code,
                    "check": r.check_name,
                    "severity": r.severity,
                    "p_error": _p_error(record),
                    "first": r.first.isoformat(),
                    "last": r.last.isoformat(),
                    "statement": r.statement,
                    "period_type": r.period_type,
                    "fiscal_year": r.fiscal_year,
                    "version_no": r.version_no,
                    "item": r.item,
                    "name": r.name,
                    "names": list(r.names),
                    "versions": list(r.versions),
                    "keyed": keyed,
                    "withheld": accepted.carries(r),
                }
            )
    del accepted
    rows = db.fetchall(
        """
        SELECT dq_flag_id, check_name, security_id, table_name, record_key, detail,
               severity, detected_at, resolved_at, accepted_at,
               COALESCE(accepted_at, resolved_at, %(t)s::timestamptz, now()) AS decided_at
          FROM ops.data_quality_flag
         WHERE check_name = ANY(%(checks)s) AND security_id = ANY(%(sids)s)
           AND detected_at <= COALESCE(%(t)s::timestamptz, now())
        """,
        {"checks": list(cst.SUCCESSORS), "sids": sorted(wanted), "t": night.state_at},
    )
    for f, (code, scope, first, last) in lb.place_statement_flags(db, rows):
        for unit in wanted.get(int(f["security_id"]), ()):
            pt = scope.get("period_type")
            if not _covers_statement((unit[2], unit[1]), pt, first, last):
                continue
            out[unit]["sql"].append(
                {
                    "id": int(f["dq_flag_id"]),
                    "check": f["check_name"],
                    "code": code,
                    "severity": f["severity"],
                    "first": first.isoformat(),
                    "last": last.isoformat(),
                    **{
                        k: scope.get(k)
                        for k in ("statement", "period_type", "version_no", "item")
                    },
                    "keyed": _statement_unit(unit[0], pt, first) == unit,
                    "open": _open_at(f, night.state_at),
                    "new": _is_new(f["detected_at"], night),
                }
            )
    return out


def _p_error(record: dict) -> Optional[float]:
    try:
        return float((record.get("detail") or {}).get("p_error"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Drawing a cycle
# ---------------------------------------------------------------------------


@dataclass
class Cycle:
    cycle_id: str
    night: Night
    seed: int
    targets: Targets
    frame: Frame
    rate: dict[str, float]
    items: list[dict]  # in item order: unit, strata, inclusion_p, data, engines, second

    def design(self) -> dict:
        drawn: dict[str, int] = {}
        for it in self.items:
            for s in it["strata"]:
                drawn[s] = drawn.get(s, 0) + 1
        return {
            "strata": {
                s: {
                    "N": n,
                    "target": self.targets.of(s),
                    "rate": self.rate.get(s, 0.0),
                    "drawn": drawn.get(s, 0),
                }
                for s, n in self.frame.sizes.items()
            },
            "targets": self.targets.__dict__,
            "volume": self.frame.volume,
            "second_share": SECOND_SHARE,
            "slack_days": SLACK_DAYS,
            "hash": f"md5, {HASH_DIGITS} hex digits",
        }


def draw_cycle(
    db: Database,
    domain: str,
    shadow_file: Path,
    *,
    cycle_id: str,
    seed: int,
    targets: Optional[Targets] = None,
    exchange_code: str = "NASDAQ",
) -> Cycle:
    """Draw a cycle: the frame, the design, the units, and each unit's snapshot.
    Reads only; :func:`write_cycle` stores it. Runs in one transaction (the
    neither-frame's temp table)."""
    if domain not in DOMAINS:
        raise ValueError(f"domain must be one of {DOMAINS}")
    targets = targets or DEFAULT_TARGETS[domain]
    night = Night.of(db, domain, shadow_file)
    frame = bars_frame(db, night) if domain == "bars" else statements_frame(db, night)
    rate = rates(frame.sizes, targets)
    engine_drawn = draw_units(frame.members, rate, seed, domain)
    with db.transaction():
        sizes, neither = neither_units(
            db, frame, targets, seed, exchange_code=exchange_code
        )
    frame.sizes.update(sizes)
    rate.update(rates(sizes, targets))
    drawn: dict[Unit, tuple[float, list[str]]] = {
        u: (p, sorted(frame.members[u])) for u, p in engine_drawn.items()
    }
    drawn.update(neither)
    units = sorted(drawn, key=lambda u: unit_hash(seed, domain, u))
    second = second_subset(units, seed, domain)
    if domain == "bars":
        engines = _bars_engines(db, night, units)
        data = bars_data(db, units, exchange_code)
    else:
        engines = _statements_engines(db, night, units)
        all_items = statement_items(db)
        verify = {
            u: items_to_verify(
                u,
                [
                    (x.get("statement"), x["item"])
                    for side in ("dorq", "sql")
                    for x in engines[u][side]
                    if x.get("item")
                ],
                all_items,
                seed,
            )
            for u in units
        }
        data = statements_data(db, units, verify)
    items = [
        {
            "item_no": n,
            "unit": u,
            "strata": drawn[u][1],
            "inclusion_p": drawn[u][0],
            "data": data.get(u, {}),
            "engines": engines.get(u, {"dorq": [], "sql": []}),
            "second_check": u in second,
        }
        for n, u in enumerate(units, start=1)
    ]
    return Cycle(
        cycle_id=cycle_id,
        night=night,
        seed=seed,
        targets=targets,
        frame=frame,
        rate=rate,
        items=items,
    )


def write_cycle(db: Database, cycle: Cycle, *, by: str, note: Optional[str]) -> None:
    """Store a drawn cycle and its units, in one transaction."""
    with db.transaction():
        db.execute(
            """
            INSERT INTO ops.dq_audit_cycle
                (cycle_id, domain, seed, night, shadow_file, dorq_run_id, dorq_version,
                 state_at, design, created_by, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                cycle.cycle_id,
                cycle.night.domain,
                cycle.seed,
                cycle.night.as_of,
                str(cycle.night.shadow_file),
                cycle.night.run_id,
                cycle.night.dorq_version or None,
                cycle.night.state_at,
                json.dumps(cycle.design(), default=str),
                by,
                note,
            ),
        )
        with db.conn.cursor() as cur:
            with cur.copy(
                "COPY ops.dq_audit_item (cycle_id, item_no, security_id, unit_date,"
                " period_type, strata, inclusion_p, data, engines, second_check)"
                " FROM STDIN"
            ) as copy:
                for it in cycle.items:
                    sid, day, period_type = it["unit"]
                    copy.write_row(
                        (
                            cycle.cycle_id,
                            it["item_no"],
                            sid,
                            day,
                            period_type,
                            it["strata"],
                            it["inclusion_p"],
                            json.dumps(it["data"], default=str),
                            json.dumps(it["engines"], default=str),
                            it["second_check"],
                        )
                    )


def format_design(cycle: Cycle) -> str:
    d = cycle.design()
    lines = [
        f"cycle {cycle.cycle_id}: {cycle.night.domain}, night {cycle.night.as_of}"
        f" ({cycle.night.shadow_file.name}, run {cycle.night.run_id}, dorq "
        f"{cycle.night.dorq_version or '?'}), seed {cycle.seed}",
        f"  {len(cycle.items)} units drawn, "
        f"{sum(1 for it in cycle.items if it['second_check'])} for the second "
        "verification",
        "",
        f"  {'stratum':<40} {'N':>9} {'target':>7} {'rate':>9} {'drawn':>6}",
    ]
    for s, row in d["strata"].items():
        lines.append(
            f"  {s:<40} {row['N']:>9} {row['target']:>7} {row['rate']:>9.5f}"
            f" {row['drawn']:>6}"
        )
    v = d["volume"]
    lines += [
        "",
        "  volume as written (warn and above):",
        f"    standing  dorq {v['standing']['dorq']}  sql {v['standing']['sql']}"
        f"  ratio {_fmt(v['standing']['ratio'])}",
        f"    new       dorq {_dash(v['new']['dorq'])}  sql {_dash(v['new']['sql'])}"
        f"  ratio {_fmt(v['new']['ratio'])}",
    ]
    return "\n".join(lines)


def _fmt(x: Optional[float], digits: int = 3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def _dash(x) -> str:
    return "-" if x is None else str(x)


# ---------------------------------------------------------------------------
# The worksheet and the verdicts
# ---------------------------------------------------------------------------


def load_cycle(db: Database, cycle_id: str) -> Optional[dict]:
    return db.fetchone(
        "SELECT * FROM ops.dq_audit_cycle WHERE cycle_id = %s", (cycle_id,)
    )


def load_items(db: Database, cycle_id: str) -> list[dict]:
    return db.fetchall(
        "SELECT * FROM ops.dq_audit_item WHERE cycle_id = %s ORDER BY item_no",
        (cycle_id,),
    )


def worksheet(
    cycle: dict, items: Sequence[dict], *, second: bool = False
) -> list[dict]:
    """The blind worksheet: per unit, its number and the data as it stood. Never
    the strata, the probability, the engines, or a verdict. ``second``: only the
    units drawn for the second verification."""
    out = []
    for it in items:
        if second and not it["second_check"]:
            continue
        out.append(
            {
                "cycle": cycle["cycle_id"],
                "domain": cycle["domain"],
                "item": it["item_no"],
                "security_id": it["security_id"],
                "date": it["unit_date"].isoformat(),
                **({"period_type": it["period_type"]} if it["period_type"] else {}),
                "data": it["data"],
            }
        )
    return out


def write_jsonl(rows: Iterable[dict], fh: IO[str]) -> int:
    n = 0
    for row in rows:
        fh.write(json.dumps(row, default=str, sort_keys=False) + "\n")
        n += 1
    return n


class VerdictError(ValueError):
    pass


VERDICTS = ("error", "correct", "unverifiable")


def _date(value, name: str, n: int) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise VerdictError(f"line {n}: {name} is not a date") from exc


def parse_verdicts(fh: IO[str], domain: str, items: dict[int, dict]) -> list[dict]:
    """The verdict file, checked against the cycle's units. One JSON object a
    line: ``item``, ``verdict`` (error, correct or unverifiable), ``kind``,
    ``source`` and ``value`` (what was read there), optional ``note``, ``first``
    and ``last`` (a span that holds the unit's date) and, on statements,
    ``line_items`` (the wrong items; none means the whole period)."""
    out = []
    seen: set[int] = set()
    for n, line in enumerate(fh, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            no = int(obj["item"])
            verdict = str(obj["verdict"])
        except (ValueError, KeyError, TypeError) as exc:
            raise VerdictError(f"line {n}: not a verdict ({exc})") from exc
        if no not in items:
            raise VerdictError(f"line {n}: item {no} is not in the cycle")
        if no in seen:
            raise VerdictError(f"line {n}: item {no} twice")
        seen.add(no)
        if verdict not in VERDICTS:
            raise VerdictError(f"line {n}: verdict must be one of {VERDICTS}")
        kind = str(obj.get("kind") or "")
        source = str(obj.get("source") or "").strip()
        value = str(obj.get("value") or "").strip()
        note = str(obj.get("note") or "").strip()
        if verdict == "unverifiable":
            if not note:
                raise VerdictError(f"line {n}: an unverifiable verdict needs a note")
        else:
            if not source or not value:
                raise VerdictError(
                    f"line {n}: a verdict cites its source and the value read there"
                )
            faults = BAR_FAULTS if domain == "bars" else STATEMENT_FAULTS
            facts = BAR_FACTS if domain == "bars" else STATEMENT_FACTS
            allowed = faults if verdict == "error" else facts
            if kind not in allowed:
                raise VerdictError(
                    f"line {n}: kind {kind!r} for a {verdict} on {domain}; one of "
                    + ", ".join(allowed)
                )
        item = items[no]
        day = item["unit_date"]
        first = _date(obj.get("first"), "first", n) or day
        last = _date(obj.get("last"), "last", n) or max(first, day)
        if not first <= day <= last:
            raise VerdictError(f"line {n}: first..last must hold the unit's date {day}")
        line_items = [str(x) for x in obj.get("line_items") or []]
        if line_items:
            if domain != "statements" or verdict != "error":
                raise VerdictError(f"line {n}: line_items are for a statements error")
            listed = {i for v in (item["data"].get("items") or {}).values() for i in v}
            unknown = [i for i in line_items if i not in listed]
            if unknown:
                raise VerdictError(
                    f"line {n}: {', '.join(unknown)} not among the unit's items"
                )
        out.append(
            {
                "item": no,
                "verdict": verdict,
                "kind": kind,
                "source": source,
                "value": value,
                "note": note,
                "first": first,
                "last": last,
                "line_items": line_items,
            }
        )
    return out


def _current_version(item: dict, statement: Optional[str] = None) -> Optional[int]:
    statements = (item["data"] or {}).get("statements") or {}
    versions = [
        v.get("version_no")
        for s, rows in statements.items()
        if statement in (None, s)
        for v in rows
        if v.get("version_no") is not None
    ]
    return max(versions) if versions else None


def _statement_of_item(item: dict, line_item: str) -> Optional[str]:
    for statement, items in ((item["data"] or {}).get("items") or {}).items():
        if line_item in items:
            return statement
    return None


def verdict_labels(cycle: dict, item: dict, v: dict, *, by: str) -> list[dict]:
    """The labels a first verdict states: ops.dq_label rows, with the cycle, the
    unit and the weight."""
    if v["verdict"] == "unverifiable":
        return []
    weight = 1.0 / float(item["inclusion_p"])
    note = f"{v['value']} [{v['source']}]" + (f" {v['note']}" if v["note"] else "")
    base = {
        "security_id": int(item["security_id"]),
        "first_date": v["first"],
        "last_date": v["last"],
        "kind": v["kind"],
        "note": note,
        "verified": True,
        "source": f"audit:{cycle['cycle_id']}",
        "decided_by": by,
        "sample_id": cycle["cycle_id"],
        "sample_item": int(item["item_no"]),
        "weight": weight,
    }
    if cycle["domain"] == "bars":
        if v["verdict"] == "error":
            return [
                {
                    **base,
                    "class": "data_error",
                    "expect": "|".join(BAR_FAULTS[v["kind"]]),
                }
            ]
        return [{**base, "class": "market_fact", "codes": "DQ"}]
    period_type = (
        item["period_type"] if item["period_type"] in ("annual", "quarter") else None
    )
    statements = sorted(((item["data"] or {}).get("statements") or {}))
    # Right, per statement as it stands: its current version. A report on an older
    # version is about values this verdict did not read, and stays unjudged.
    facts = [
        {
            **base,
            "period_type": period_type,
            "statement": s,
            "version_no": _current_version(item, s),
            "class": "market_fact",
            "codes": "DQ9",
        }
        for s in statements
    ] or [{**base, "period_type": period_type, "class": "market_fact", "codes": "DQ9"}]
    if v["verdict"] == "correct":
        return facts
    if not v["line_items"]:
        return [{**base, "period_type": period_type, "class": "data_error"}]
    out = [
        {
            **base,
            "period_type": period_type,
            "statement": _statement_of_item(item, line_item),
            "version_no": _current_version(item, _statement_of_item(item, line_item)),
            "item": line_item,
            "class": "data_error",
        }
        for line_item in v["line_items"]
    ]
    # The rest of the period was verified right: a report on another item is false.
    rest = f"Every listed item but {', '.join(v['line_items'])} matches. " + note
    return out + [{**f, "kind": "matches_filing", "note": rest} for f in facts]


_LABEL_INSERT = """
INSERT INTO ops.dq_label
    (security_id, first_date, last_date, class, kind, expect, codes, note, verified,
     source, decided_by, statement, period_type, version_no, item, sample_id,
     sample_item, weight)
SELECT x.security_id, x.first_date, x.last_date, x.class, x.kind,
       NULLIF(x.expect, ''), NULLIF(x.codes, ''), x.note, x.verified, x.source,
       x.decided_by, x.statement, x.period_type, x.version_no, x.item, x.sample_id,
       x.sample_item, x.weight
  FROM jsonb_to_recordset(%s::jsonb) AS x(
         security_id bigint, first_date date, last_date date, class text, kind text,
         expect text, codes text, note text, verified boolean, source text,
         decided_by text, statement text, period_type text, version_no integer,
         item text, sample_id text, sample_item integer, weight float8)
RETURNING label_id
"""


@dataclass
class VerdictResult:
    items: int = 0
    labels: int = 0
    unverifiable: int = 0
    replaced: int = 0
    refused: list[str] = field(default_factory=list)


def import_verdicts(
    db: Database,
    cycle: dict,
    items: Sequence[dict],
    verdicts: Sequence[dict],
    *,
    by: str,
    second: bool = False,
    replace: bool = False,
    dry_run: bool = False,
) -> VerdictResult:
    """Store verdicts. A first verdict becomes labels and marks the unit judged or
    unverifiable; one already judged is refused unless ``replace``, which revokes
    its labels first. A ``second`` verdict is kept on the unit, which must be one
    drawn for it, and never becomes a label."""
    by_no = {int(it["item_no"]): it for it in items}
    out = VerdictResult()
    rows: list[dict] = []
    revoke: list[int] = []
    for v in verdicts:
        it = by_no[v["item"]]
        if second:
            if not it["second_check"]:
                out.refused.append(f"item {v['item']}: not drawn for the second check")
                continue
            out.items += 1
            continue
        if it["status"] != "pending" and not replace:
            out.refused.append(f"item {v['item']}: already {it['status']}")
            continue
        if it["status"] != "pending":
            out.replaced += 1
            revoke.append(v["item"])
        out.items += 1
        labels = verdict_labels(cycle, it, v, by=by)
        out.unverifiable += not labels
        out.labels += len(labels)
        rows += labels
    if dry_run or out.refused:
        return out
    with db.transaction():
        if second:
            for v in verdicts:
                db.execute(
                    "UPDATE ops.dq_audit_item SET second = %s::jsonb"
                    " WHERE cycle_id = %s AND item_no = %s",
                    (
                        json.dumps(
                            {**v, "by": by, "at": dt.datetime.now(dt.timezone.utc)},
                            default=str,
                        ),
                        cycle["cycle_id"],
                        v["item"],
                    ),
                )
            return out
        if revoke:
            db.execute(
                """
                UPDATE ops.dq_label
                   SET revoked_at = now(), revoked_by = %s,
                       revoked_note = 'replaced by a re-verification of the unit'
                 WHERE sample_id = %s AND sample_item = ANY(%s) AND revoked_at IS NULL
                """,
                (by, cycle["cycle_id"], revoke),
            )
        if rows:
            db.fetchall(_LABEL_INSERT, (json.dumps(rows, default=str),))
        for v in verdicts:
            db.execute(
                "UPDATE ops.dq_audit_item SET status = %s, status_note = %s"
                " WHERE cycle_id = %s AND item_no = %s",
                (
                    "unverifiable" if v["verdict"] == "unverifiable" else "judged",
                    v["note"] or None,
                    cycle["cycle_id"],
                    v["item"],
                ),
            )
    return out


def cycle_labels(db: Database, cycle_id: str) -> dict[int, list[dict]]:
    """The active labels of a cycle, by unit."""
    out: dict[int, list[dict]] = {}
    for r in db.fetchall(
        """
        SELECT label_id, security_id, first_date, last_date, class, kind, expect,
               codes, verified, statement, period_type, version_no, item,
               sample_item, weight
          FROM ops.dq_label
         WHERE sample_id = %s AND revoked_at IS NULL
         ORDER BY sample_item, label_id
        """,
        (cycle_id,),
    ):
        out.setdefault(int(r["sample_item"]), []).append(r)
    return out


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _label(row: dict) -> cmp.Label:
    return cmp.Label(
        series=str(row["security_id"]),
        first=row["first_date"],
        last=row["last_date"],
        label_class=row["class"],
        expect=tuple(x for x in (row["expect"] or "").split("|") if x),
        codes=row["codes"] or "",
        kind=row["kind"],
        verified=row["verified"],
        statement=row["statement"],
        period_type=row["period_type"],
        version_no=row["version_no"],
        item=row["item"],
    )


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text[:10])


def _bar_report(sid: int, x: dict) -> cmp.Report:
    return cmp.Report(
        security_id=sid,
        code=x["code"],
        check_name=x["check"],
        severity=x["severity"],
        first=_day(x["first"]),
        last=_day(x["last"]),
        p_error=x.get("p_error"),
    )


def _statement_report(sid: int, x: dict) -> cst.StatementReport:
    return cst.StatementReport(
        security_id=sid,
        code=x["code"],
        check_name=x["check"],
        severity=x["severity"],
        first=_day(x["first"]),
        last=_day(x["last"]),
        statement=x.get("statement"),
        period_type=x.get("period_type"),
        fiscal_year=x.get("fiscal_year"),
        version_no=x.get("version_no"),
        item=x.get("item"),
        name=x.get("name"),
        names=tuple(x.get("names") or ()),
        versions=tuple(x.get("versions") or ()),
    )


def _judge_bar_sql(check: str, day: dt.date, labels: Sequence[cmp.Label]) -> str:
    right = other = false = False
    for lab in labels:
        if not lab.first <= day <= lab.last:
            continue
        if lab.is_fault:
            if cmp._sql_code_expected(check, lab):
                right = True
            else:
                other = True
        else:
            false = True
    return (
        "right" if right else "other" if other else "false" if false else "unlabelled"
    )


@dataclass
class EnginePrecision:
    """One engine's weighted precision: strict (right) and fault (right or another
    fault), with the counts behind them."""

    strict: Estimate = field(default_factory=Estimate)
    fault: Estimate = field(default_factory=Estimate)
    unlabelled: int = 0

    def add(self, outcome: str, weight: float) -> None:
        if outcome == "unlabelled":
            self.unlabelled += 1
            return
        self.strict.add(outcome == "right", weight)
        self.fault.add(outcome in ("right", "other"), weight)

    def as_dict(self) -> dict:
        return {
            "strict": self.strict.as_dict(),
            "fault": self.fault.as_dict(),
            "unlabelled": self.unlabelled,
        }


@dataclass
class Gate:
    cycle_id: str
    domain: str
    night: dt.date
    dorq_version: Optional[str]
    drawn: int = 0
    judged: int = 0
    unverifiable: int = 0
    pending: int = 0
    confirmed_errors: int = 0
    precision: dict[str, dict[str, EnginePrecision]] = field(default_factory=dict)
    precision_by_code: dict[str, EnginePrecision] = field(default_factory=dict)
    precision_before_withholding: EnginePrecision = field(
        default_factory=EnginePrecision
    )
    recall: dict[str, Estimate] = field(default_factory=dict)
    recall_any: dict[str, Estimate] = field(default_factory=dict)
    paired: dict = field(default_factory=dict)
    ece: Optional[float] = None
    ece_points: int = 0
    volume: dict = field(default_factory=dict)
    second: dict = field(default_factory=dict)
    lines: list[tuple[str, Optional[bool], str]] = field(default_factory=list)
    verdict: str = ""

    def as_dict(self) -> dict:
        return {
            "cycle": self.cycle_id,
            "domain": self.domain,
            "night": self.night,
            "dorq_version": self.dorq_version,
            "units": {
                "drawn": self.drawn,
                "judged": self.judged,
                "unverifiable": self.unverifiable,
                "pending": self.pending,
                "confirmed_errors": self.confirmed_errors,
            },
            "precision": {
                engine_name: {sev: p.as_dict() for sev, p in by_sev.items()}
                for engine_name, by_sev in self.precision.items()
            },
            "precision_by_code": {
                code: p.as_dict() for code, p in self.precision_by_code.items()
            },
            "precision_before_withholding": self.precision_before_withholding.as_dict(),
            "recall": {k: v.as_dict() for k, v in self.recall.items()},
            "recall_any": {k: v.as_dict() for k, v in self.recall_any.items()},
            "paired": self.paired,
            "ece": self.ece,
            "ece_points": self.ece_points,
            "volume": self.volume,
            "second": self.second,
            "lines": [
                {"measure": m, "met": ok, "detail": detail}
                for m, ok, detail in self.lines
            ],
            "verdict": self.verdict,
        }


def _agrees(first: Sequence[dict], second: dict) -> Optional[bool]:
    """Whether the second verdict agrees with the first: the same verdict (an error
    or not). None when either could not be judged."""
    if not first or second.get("verdict") not in ("error", "correct"):
        return None
    first_error = any(lab["class"] == "data_error" for lab in first)
    return first_error == (second["verdict"] == "error")


def gate(cycle: dict, items: Sequence[dict], labels: dict[int, list[dict]]) -> Gate:
    """The cutover gate on one cycle (#269): pure, over what the database holds."""
    domain = cycle["domain"]
    out = Gate(
        cycle_id=cycle["cycle_id"],
        domain=domain,
        night=cycle["night"],
        dorq_version=cycle.get("dorq_version"),
    )
    out.precision = {
        "dorq": {"warn": EnginePrecision(), "error": EnginePrecision()},
        "sql": {"warn": EnginePrecision(), "error": EnginePrecision()},
    }
    out.recall = {"dorq": Estimate(), "sql": Estimate()}
    out.recall_any = {"dorq": Estimate(), "sql": Estimate()}
    cells = {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0}
    discordant = {"dorq_only": 0, "sql_only": 0}
    error_weights: list[float] = []
    points: list[tuple[float, bool, float]] = []
    slack = SLACK_DAYS
    agreement = {"checked": 0, "agree": 0, "disagree": []}
    for it in items:
        out.drawn += 1
        status = it["status"]
        own = labels.get(int(it["item_no"]), [])
        if it["second_check"] and it.get("second"):
            agrees = _agrees(own, it["second"])
            if agrees is not None:
                agreement["checked"] += 1
                agreement["agree"] += agrees
                if not agrees:
                    agreement["disagree"].append(int(it["item_no"]))
        if status == "unverifiable":
            out.unverifiable += 1
            continue
        if status != "judged" or not own:
            out.pending += 1
            continue
        out.judged += 1
        w = 1.0 / float(it["inclusion_p"])
        labs = [_label(r) for r in own]
        faults = [lab for lab in labs if lab.is_fault]
        sid = int(it["security_id"])
        engines = it["engines"] or {}
        # precision: what each engine keyed at the unit
        for x in engines.get("dorq", ()):
            if not x.get("keyed"):
                continue
            r = _bar_report(sid, x) if domain == "bars" else _statement_report(sid, x)
            outcome = (
                cmp.judge(r, labs, 0) if domain == "bars" else cst.judge(r, labs, 0)
            )
            severe = x["severity"] in SEVERE
            if severe:
                out.precision_before_withholding.add(outcome, w)
            if severe and not x.get("withheld"):
                out.precision["dorq"]["warn"].add(outcome, w)
                out.precision_by_code.setdefault(x["code"], EnginePrecision()).add(
                    outcome, w
                )
                if x["severity"] == "error":
                    out.precision["dorq"]["error"].add(outcome, w)
            if x.get("p_error") is not None and outcome in ("right", "false"):
                points.append((float(x["p_error"]), outcome == "right", w))
        for f in engines.get("sql", ()):
            if not (f.get("keyed") and f.get("open")) or f["severity"] not in SEVERE:
                continue
            if domain == "bars":
                outcome = _judge_bar_sql(f["check"], _day(f["date"]), labs)
            else:
                outcome = cst.judge(
                    _statement_report(sid, {**f, "code": f["code"]}), labs, 0
                )
            out.precision["sql"]["warn"].add(outcome, w)
            if f["severity"] == "error":
                out.precision["sql"]["error"].add(outcome, w)
        # recall: a confirmed error, found by an engine covering it
        if not faults:
            continue
        out.confirmed_errors += 1
        error_weights.append(w)
        found = {"dorq": False, "sql": False}
        found_any = {"dorq": False, "sql": False}
        for x in engines.get("dorq", ()):
            if x["severity"] not in SEVERE or x.get("withheld"):
                continue
            r = _bar_report(sid, x) if domain == "bars" else _statement_report(sid, x)
            for lab in faults:
                overlaps = (
                    cmp._overlaps(r, lab, slack)
                    if domain == "bars"
                    else cst._label_overlaps(r, lab, slack)
                )
                if overlaps:
                    found_any["dorq"] = True
                    found["dorq"] = (
                        found["dorq"] or not lab.expect or r.code in lab.expect
                    )
        for f in engines.get("sql", ()):
            if f["severity"] not in SEVERE:
                continue
            for lab in faults:
                if domain == "bars":
                    near = (
                        lab.first - dt.timedelta(days=slack)
                        <= _day(f["date"])
                        <= lab.last + dt.timedelta(days=slack)
                    )
                    strict = near and cmp._sql_code_expected(f["check"], lab)
                else:
                    scope = {
                        k: f.get(k)
                        for k in ("statement", "period_type", "version_no", "item")
                    }
                    strict, near = cst._flag_finds(
                        (f["code"], scope, _day(f["first"]), _day(f["last"])),
                        lab,
                        slack,
                    )
                found_any["sql"] = found_any["sql"] or near
                found["sql"] = found["sql"] or strict
        for name in ("dorq", "sql"):
            out.recall[name].add(found[name], w)
            out.recall_any[name].add(found_any[name], w)
        d_hit, s_hit = found["dorq"], found["sql"]
        cell = "a" if d_hit and s_hit else "b" if d_hit else "c" if s_hit else "d"
        cells[cell] += w
        if d_hit and not s_hit:
            discordant["dorq_only"] += 1
        elif s_hit and not d_hit:
            discordant["sql_only"] += 1
    paired = newcombe_paired(
        cells["a"], cells["b"], cells["c"], cells["d"], kish(error_weights)
    )
    out.paired = {
        "cells_weighted": cells,
        "discordant": discordant,
        "difference": paired[0] if paired else None,
        "lower_one_sided_95": paired[1] if paired else None,
        "upper_one_sided_95": paired[2] if paired else None,
        "mcnemar_p": mcnemar_one_sided(discordant["dorq_only"], discordant["sql_only"]),
    }
    out.ece = ece(points)
    out.ece_points = len(points)
    out.volume = (cycle.get("design") or {}).get("volume") or {}
    out.second = {
        "drawn": sum(1 for it in items if it["second_check"]),
        "checked": agreement["checked"],
        "agreement": (
            agreement["agree"] / agreement["checked"] if agreement["checked"] else None
        ),
        "disagree": agreement["disagree"],
        "bar": AGREEMENT_BAR,
    }
    out.precision_by_code = dict(sorted(out.precision_by_code.items()))
    _verdict(out)
    return out


def _verdict(g: Gate) -> None:
    """The verdict lines, then the verdict."""
    pw = g.precision["dorq"]["warn"].strict.value
    pe = g.precision["dorq"]["error"].strict.value
    lower = g.paired.get("lower_one_sided_95")
    standing = (g.volume.get("standing") or {}).get("ratio")
    new = (g.volume.get("new") or {}).get("ratio")

    def at_most(x, bar):
        return None if x is None else x <= bar

    def at_least(x, bar):
        return None if x is None else x >= bar

    g.lines = [
        (
            "precision, warn and above",
            at_least(pw, PRECISION_WARN_BAR),
            f"{_fmt(pw)} (bar {PRECISION_WARN_BAR})",
        ),
        (
            "precision, error",
            at_least(pe, PRECISION_ERROR_BAR),
            f"{_fmt(pe)} (bar {PRECISION_ERROR_BAR})",
        ),
        (
            "recall, dorq above SQL at one-sided 95%",
            None if lower is None else lower > 0,
            f"difference {_fmt(g.paired.get('difference'))}, lower bound {_fmt(lower)}",
        ),
        (
            "volume standing, as written",
            at_most(standing, VOLUME_BAR),
            f"{_fmt(standing)} of SQL's (bar {VOLUME_BAR})",
        ),
        (
            "volume new per night, as written",
            at_most(new, VOLUME_BAR),
            f"{_fmt(new)} of SQL's (bar {VOLUME_BAR})",
        ),
        (
            "calibration (ECE)",
            at_most(g.ece, ECE_BAR),
            f"{_fmt(g.ece)} (bar {ECE_BAR})",
        ),
    ]
    agreement = g.second.get("agreement")
    if g.pending:
        g.verdict = f"incomplete: {g.pending} of {g.drawn} units not yet verified"
    elif agreement is None:
        g.verdict = "incomplete: the second verification is not recorded"
    elif agreement < AGREEMENT_BAR:
        g.verdict = (
            f"not usable: second verification agreed on {agreement:.0%} (bar "
            f"{AGREEMENT_BAR:.0%}); re-verify items "
            + ", ".join(str(n) for n in g.second["disagree"])
        )
    elif g.confirmed_errors < MIN_CONFIRMED_ERRORS:
        g.verdict = (
            f"not enough errors yet: {g.confirmed_errors} confirmed of about "
            f"{MIN_CONFIRMED_ERRORS}"
        )
    else:
        failing = [m for m, ok, _ in g.lines if ok is not True]
        g.verdict = "met" if not failing else "not met: " + "; ".join(failing)


def _estimate_line(name: str, e: Estimate) -> str:
    iv = e.interval
    return (
        f"    {name:<6} {_fmt(e.value)}"
        + (f"  [{iv[0]:.3f}, {iv[1]:.3f}]" if iv else "")
        + f"  on {e.count} (n_eff {e.n_eff:.1f})"
    )


def format_gate(g: Gate) -> str:
    lines = [
        f"audit cycle {g.cycle_id}: {g.domain}, night {g.night}, dorq "
        f"{g.dorq_version or '?'}",
        f"  units: {g.drawn} drawn, {g.judged} judged, {g.unverifiable} unverifiable, "
        f"{g.pending} pending; {g.confirmed_errors} confirmed errors",
        "",
        "  precision, strict (right / judged), weighted, 95% Wilson on Kish's n:",
    ]
    for sev in ("warn", "error"):
        lines.append(f"   at {'warn and above' if sev == 'warn' else 'error'}:")
        for name in ("dorq", "sql"):
            lines.append(_estimate_line(name, g.precision[name][sev].strict))
    if g.domain == "statements":
        lines.append(
            _estimate_line(
                "dorq before withholding", g.precision_before_withholding.strict
            )
        )
    if g.precision_by_code:
        lines.append("   dorq by code (warn and above):")
        for code, p in g.precision_by_code.items():
            lines.append(_estimate_line(code, p.strict))
    lines += ["", "  recall on confirmed errors (a code the fault names), weighted:"]
    for name in ("dorq", "sql"):
        lines.append(_estimate_line(name, g.recall[name]))
    p = g.paired
    lines += [
        f"    paired: dorq - sql {_fmt(p.get('difference'))}, one-sided 95% lower "
        f"bound {_fmt(p.get('lower_one_sided_95'))} (Newcombe); discordant "
        f"{p['discordant']['dorq_only']} dorq only, {p['discordant']['sql_only']} "
        f"sql only, exact McNemar p {p['mcnemar_p']:.4f}",
        "",
        f"  calibration: ECE {_fmt(g.ece)} on {g.ece_points} reports",
    ]
    v = g.volume
    if v:
        lines += [
            "  volume as written (warn and above):",
            f"    standing  dorq {_dash(v['standing']['dorq'])}  sql "
            f"{_dash(v['standing']['sql'])}  ratio {_fmt(v['standing']['ratio'])}",
            f"    new       dorq {_dash(v['new']['dorq'])}  sql "
            f"{_dash(v['new']['sql'])}  ratio {_fmt(v['new']['ratio'])}",
        ]
    s = g.second
    lines += [
        f"  second verification: {s['checked']} of {s['drawn']} checked, agreement "
        f"{_fmt(s['agreement'])}"
        + (
            f"; disagree on {', '.join(map(str, s['disagree']))}"
            if s["disagree"]
            else ""
        ),
        "",
    ]
    for measure, ok, detail in g.lines:
        mark = "met" if ok else "not met" if ok is False else "n/a"
        lines.append(f"  {measure:<42} {mark:<8} {detail}")
    lines += ["", f"  verdict: {g.verdict}"]
    return "\n".join(lines)
