# Plan: economic data series, and a removal path for every fafnir db

- Status: **proposed**
- Scope: U.S. Treasury par nominal + par real (TIPS) curves first; FRED, BLS, BEA
  and FMP as the sources the same machinery admits afterwards. Plus a uniform
  `fafnir remove` family covering price history *and* economic series.
- Touches: `sql/migrations/0027…0031`, `src/fafnir/sources/`, new
  `src/fafnir/ingest/economic.py` and `src/fafnir/db/removal.py`,
  `src/fafnir/dq/checks.py` + `recheck.py`, `src/fafnir/cli.py`,
  `src/duk/datasource/db.py` + `src/duk/cli.py`, `src/fafnir_mcp/`,
  `scripts/daily_update.sh`, `doc/`, `.claude/skills/fafnir-dba/`
- New ADRs: 0012 (economic grain), 0013 (curves are assembled in `mart`),
  0014 (removal is an audited, suppressed act)

---

## 1. What is being asked for

Four things, and they are not equally hard:

1. **Hold economic time series**, starting with U.S. Treasury par yield curve rates
   and the TIPS par real curve, in a shape that is correct for those two and does
   not have to be re-grained when BLS, BEA, FRED and FMP arrive.
2. **Make `duk` read them** — including finally giving `duk yc` the db backend its
   own help text has been promising ("`yc` is live-only until the economic-series
   fast-follow lands").
3. **Check them** — `fafnir dq run` must have something to say about economic data,
   and the something must be right for rates and index levels, not a copy of the
   equity checks.
4. **Remove data series** — from *any* fafnir db, prices included.

Item 4 is the one that deserves the most suspicion. Every design decision in this
repository so far has been a decision to *retain*: delisted securities are kept
(no survivorship bias), raw OHLCV is immutable, a hand-written `DELETE` was judged
non-durable enough to justify a whole table (`ops.operator_override`, migration
0025). A removal feature is the first thing fafnir will ship that destroys data on
purpose. §9 is therefore mostly about what removal *refuses* to do.

---

## 2. What the codebase already decides for us

Seven things are settled and the design has to fit them rather than argue with
them:

| Established | Where | Consequence here |
|---|---|---|
| `mart` is the **whole** read seam; no reader outside the write path touches `core`/`ops` | ADR 0009, `duk/datasource/db.py` | every economic read is a `mart` view, and adding one is a grant to every reader |
| Three timelines: observation / knowledge / load | `doc/architecture.md` | an observation carries all three, not just `obs_date` + `loaded_at` |
| Never change a fact table's grain in place | `doc/extending.md` | bitemporality is built now, even though Treasury never revises |
| Quarantine, never silently drop | `dq/checks.py`, `ingest/daily_price.py` | a value that will not fit the column is flagged, not rounded away |
| Flag a standing condition **once**, not once per run | `repository.add_dq_flag_once` | economic gap/stale checks must dedupe on a stable `record_key` |
| A delete that the loader will undo is not a repair | migration 0025 preamble | removal has to write a suppression the loaders consult |
| Declared universe ≠ discovered universe | ADR 0006, `ref.tracked_symbol` | every economic series is *declared*; there is no screener |

And three seams that do **not** exist yet, found by reading the code rather than
assumed:

- **`BaseSource` is JSON-and-GET only.** `_get` calls `resp.json()` and raises
  `SourceError` on a non-JSON body, and there is no `_post`. Treasury publishes
  XML; BLS v2 requires `POST` with a JSON body. Both need a seam *before* any
  source module is written — see §4.1.
- **`ops.load_watermark` is keyed on `security_id`**, with `0` meaning
  "whole-endpoint". An economic series is not a security, so it has no
  `security_id` to key on. §5.2.
- **`ops.data_quality_flag.security_id` is the only entity column** on the DQ
  queue, and `mart.v_security_dq_open` is keyed on it. An economic flag has no
  security. §8.4.

---

## 3. The data model

### 3.1 Tidy long, not wide

A Treasury par curve *looks* like a wide table: one row per date, thirteen tenor
columns. Storing it that way is wrong for four reasons, all of which are already
true of the data:

- **Tenors come and go.** The 20-year was not published 1987–1993; the 30-year
  was discontinued 2002 and reinstated 2006; the 4-month bill was added in
  October 2022. Each of those is a migration against a wide table and nothing at
  all against a tidy one.
- **The real curve is a different width.** TIPS par real rates begin in 2003 and
  carry five tenors (5, 7, 10, 20, 30). A second wide table, then a third for the
  bill rates, and the "economic series" feature has become three bespoke tables.
- **BLS, BEA and FRED are natively tidy.** A wide curve table would be a
  special case that the other three sources never fit into.
- **One grain means one of everything else**: one watermark scheme, one DQ
  engine, one removal path, one `duk` command.

The wide shape is what a *reader* wants, so it is produced on read, in `mart` —
which is the same decision ADR 0001 made for adjusted prices. **ADR 0013.**

### 3.2 `core.economic_series` — the canonical series

Identity is a surrogate `series_id`, exactly as ADR 0002 argues for securities,
with a fafnir-assigned `series_key` as the stable human-facing name:

```
core.economic_series
  series_id            BIGINT IDENTITY PK
  series_key           TEXT NOT NULL UNIQUE      -- 'UST.PAR.NOMINAL.10Y'
  title                TEXT NOT NULL             -- '10-Year Treasury Par Yield'
  frequency            TEXT NOT NULL             -- daily|weekly|monthly|quarterly|annual
  units                TEXT NOT NULL             -- 'percent_per_annum', 'index_1982_84=100', ...
  unit_class           TEXT NOT NULL             -- rate|index|level|ratio|count  (drives DQ + resampling)
  seasonal_adjustment  TEXT NOT NULL             -- sa|nsa|not_applicable
  default_aggregation  TEXT NOT NULL             -- last|avg|sum   (how to resample DOWN)
  calendar_code        TEXT REFERENCES ref.observation_calendar_def   -- daily/weekly series only
  publication_lag_days INTEGER NOT NULL DEFAULT 1   -- how late "on time" is
  outlier_threshold    NUMERIC                   -- per-series; NULL => class default
  first_obs_date       DATE                      -- maintained by the loader
  last_obs_date        DATE
  discontinued_date    DATE                      -- set, never deleted (cf. delisted_date)
  is_tracked           BOOLEAN NOT NULL DEFAULT TRUE
  notes                TEXT
  created_at / updated_at  TIMESTAMPTZ
```

Four of these columns are the ones that stop the DQ checks from being wrong, and
each is there because the equivalent equity check has already failed once for the
matching reason:

- **`unit_class`** — a 50% close-to-close move is the outlier threshold for
  equities. For a rate series it is routine: the 2-year went 0.73 → 1.16 in a
  single 2022 week. The outlier check has to ask a *different question* of a rate
  (absolute change, in basis points) than of an index (relative change), and
  `unit_class` is what tells it which. A single global threshold would flag the
  entire 2022 hiking cycle and nothing else for the rest of history.
- **`publication_lag_days`** — CPI lands ~13 days after the month it describes;
  BEA's advance GDP estimate ~30 days after the quarter. Without a per-series
  allowance, a freshness check flags every monthly series for the first fortnight
  of every month, on a `record_key` that changes as the reference date moves, so
  `add_dq_flag_once` cannot dedupe it and the queue grows forever. That is
  precisely the NAV-lag failure `NAV_LAG_TRADING_DAYS` exists to prevent
  (`dq/checks.py`), reintroduced by a series that is simply published later.
- **`default_aggregation`** — resampling a *rate* by summing it is nonsense;
  resampling a *flow* by taking the last value is also nonsense. `duk` must not
  ask the user to know which, and must not guess.
- **`seasonal_adjustment`** — SA and NSA variants of one BLS series have the same
  title and different values. Keeping them as two series with the flag explicit is
  what stops someone charting a jump that is an adjustment method, not an economy.

### 3.3 `core.economic_observation` — the fact, bitemporal

```
core.economic_observation
  series_id         BIGINT NOT NULL REFERENCES core.economic_series
  obs_date          DATE   NOT NULL         -- FIRST DAY of the period described
  valid_from        DATE   NOT NULL         -- knowledge date: when this value became true
  valid_to          DATE                    -- NULL = current
  value             NUMERIC(28, 10) NOT NULL
  source            TEXT NOT NULL           -- which feed wrote THIS row
  source_flags      TEXT                    -- vendor footnotes (BLS 'P' preliminary, etc.)
  ingestion_run_id  BIGINT REFERENCES ops.ingestion_run
  loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
  PRIMARY KEY (series_id, obs_date, valid_from)
```

with `UNIQUE (series_id, obs_date) WHERE valid_to IS NULL` — one current value per
period, enforced rather than trusted.

Five decisions inside that:

**`obs_date` is the first day of the period.** August 2026 CPI is `2026-08-01`,
not `2026-08-31` and not `2026-09-11` (its release date). Combined with the
series' `frequency` this is unambiguous, and it matches FRED's convention so a
FRED-sourced series and a BLS-sourced one for the same concept land on the same
key. Q1 2026 GDP is `2026-01-01`.

**Bitemporal from day one, even though Treasury never revises.** The cost is one
`DATE` column and a partial unique index. The alternative is re-graining a fact
table later, which `doc/extending.md` forbids outright — and BEA *will* revise:
advance → second → third → annual → comprehensive, five different values for one
quarter over five years. `valid_from`/`valid_to` rather than extending.md's
sketched `vintage_date` because FRED/ALFRED natively serves knowledge
*intervals* (`realtime_start`/`realtime_end`), and because it is the same shape
`doc/extending.md` already specifies for the fundamentals milestone. One
bitemporal idiom in the warehouse, not two.

**A re-pull of unchanged data writes nothing.** The upsert compares the incoming
value to the current row and only closes-and-opens when they actually differ.
Without that, idempotency is lost the moment bitemporality is added: a nightly
re-pull of a 60-year daily series would mint 15,000 new "versions" a night, all
identical. This is the single most important property in the loader and gets a
named integration test.

**`valid_from` is the knowledge date, not `now()`.** FRED supplies it
(`realtime_start`). Treasury, BLS, BEA and FMP do not, so it is the load date —
the best available answer — and the data dictionary says so plainly rather than
implying a precision the feed does not have.

**A missing value is not a row.** Treasury publishes an empty cell for a tenor it
did not offer that day; BLS and FRED publish `.`. None of them becomes a
`NULL`-valued row and absolutely none becomes a zero. Absence is absence, and the
gap check (§8.1) is what gives absence a voice.

**`NUMERIC(28, 10)`**, and validated against that at the boundary. The `price_*`
family exists because `NUMERIC(20,6)` silently quantized a sub-penny shell to
`0.000000` and then failed a `CHECK` that aborted the whole batch
(`ingest/daily_price.py`, `_MONEY_SCALE`). Economic values span basis points to
billions of chained dollars, so the same trap is live here and gets the same
treatment: coerce to what the column would hold, judge *that*, quarantine what
does not survive.

### 3.4 Not partitioned — but partitionable

`core.daily_price` is range-partitioned yearly (ADR 0003). The economic fact
table is not, and should not be: the whole Treasury nominal history is ~13 tenors
× 250 days × 63 years ≈ 205k rows, the real curve ~29k, and a thousand
monthly BLS/BEA series is under a million. Partitioning that is cost without
benefit.

What ADR 0003 actually asks for is that the *grain* stay hypertable-compatible,
and it does: `obs_date` is in the primary key. Partitioning or a TimescaleDB
hypertable can be adopted later without a grain change. Say this in ADR 0012 so
the absence reads as a decision rather than an oversight.

### 3.5 `ref.tracked_series` — the declaration, and the source mapping

There is no screener for economic data. Every series fafnir holds is one an
operator asked for, which makes the declaration exactly the `ref.tracked_symbol`
pattern from ADR 0006 — an *input* to the series master, not a copy of it:

```
ref.tracked_series
  series_key          TEXT NOT NULL       -- the canonical key (may not exist in core yet)
  source              TEXT NOT NULL       -- treasury|fred|bls|bea|fmp
  source_series_code  TEXT NOT NULL       -- 'BC_10YEAR' | 'DGS10' | 'CUUR0000SA0' | 'T10101:1'
  priority            SMALLINT NOT NULL DEFAULT 1   -- 1 = the feed that writes the fact
  is_tracked          BOOLEAN NOT NULL DEFAULT TRUE
  note                TEXT
  added_at / untracked_at  TIMESTAMPTZ
  PRIMARY KEY (series_key, source, source_series_code)
```

with `UNIQUE (series_key) WHERE is_tracked AND priority = 1` — one and only one
feed may write a given series' facts.

**This table is where the flexibility requirement actually lives.** The 10-year
par nominal yield is available from Treasury (`BC_10YEAR`), from FRED (`DGS10`,
which *is* the Treasury series) and from FMP (`treasury-rates.year10`). They are
one economic concept and three vendor feeds, and — exactly as with tickers — the
vendor must never be the identity. Consequences that fall straight out:

- Re-pointing a series from FMP to Treasury is two `UPDATE`s of `priority`.
  Nothing downstream — not `duk`, not a mart view, not a saved query, not a DQ
  flag — knows or cares.
- A secondary feed can be held at `priority = 2` and used for cross-validation
  (§8.6) without ever being confused for the fact.
- `core.economic_observation.source` records which feed wrote each row, so "when
  did we switch, and which rows came from where" is answerable in SQL after the
  fact.

Two feeds never both write the same series. The grain would double, and the
question "what is the 10-year yield" would stop having one answer.

### 3.6 `ref.curve` / `ref.curve_tenor` — what makes a curve a curve

Tidy storage loses the thing `duk yc` needs: which series belong to one curve, at
what tenor, in what order.

```
ref.curve
  curve_key   TEXT PRIMARY KEY            -- 'UST.PAR.NOMINAL' | 'UST.PAR.REAL'
  curve_name  TEXT NOT NULL
  curve_type  TEXT NOT NULL               -- par_nominal | par_real | zero
  day_count / compounding   TEXT          -- documented, not guessed at by the bootstrapper
  note        TEXT

ref.curve_tenor
  curve_key      TEXT NOT NULL REFERENCES ref.curve
  series_id      BIGINT NOT NULL REFERENCES core.economic_series
  tenor_months   NUMERIC(9,4) NOT NULL    -- 1, 3, 12, 360 ...
  tenor_label    TEXT NOT NULL            -- 'month1', 'year10'  -- duk's existing vocabulary
  display_order  SMALLINT NOT NULL
  PRIMARY KEY (curve_key, series_id)
```

`tenor_label` deliberately reuses the exact strings `duk.rates_utils` already
parses (`month1`, `year1.5`), so `--tenors`, `--key-rates`, `bootstrap_zero_rates`
and `interpolate_rates` keep working against db-sourced data with no change to
any of them. They are pure compute over a tenor-keyed frame; the only thing that
changes is where the frame came from.

Seeded by migration for the two Treasury curves. A third curve later is a seed
row, not a schema change.

### 3.7 `ref.observation_calendar` — business days that are not trading days

`ref.trading_calendar` is keyed on `exchange_code` and seeded for exchanges. The
Treasury publishes on *federal* business days, which differ from NYSE sessions on
Columbus Day and Veterans Day (markets open, Treasury closed) and the other way
round for market closures. Putting `USGOV` into `ref.exchange` the way `MUTF` was
added for funds would be a stretch too far — a government is not a venue, and a
gap check that quietly used the wrong calendar would flag ~2 false gaps a year
per daily series, forever.

So: `ref.observation_calendar_def(calendar_code, name)` +
`ref.observation_calendar(calendar_code, obs_date, is_open, note)`, seeded for
`USGOV` by the same generator shape `ref.trading_calendar` uses. Monthly,
quarterly and annual series need no calendar at all — their expected cadence *is*
their frequency — so `calendar_code` is NULL for them, and the gap check reads
the frequency instead.

---

## 4. Sources: the part that has to stay pluggable

### 4.1 Prerequisite: widen `BaseSource` (no behaviour change for FMP)

`BaseSource._get` hard-codes `resp.json()` and there is no `_post`. Treasury is
XML; BLS v2 is `POST` with a JSON body. Refactor, in one commit, before any
source module is written:

- extract `_request(method, url, *, params, json_body) -> (bytes, status, nbytes)`
  carrying **all** of the existing throttle / 429-Retry-After / 5xx backoff /
  bandwidth metering / `redact_secrets` behaviour;
- `_get` becomes `_request("GET", …)` + `resp.json()` + the `Error Message`
  probe — byte-for-byte the same contract FMP has today;
- add `_get_text` and `_post_json` on top of `_request`.

`test_sources_base.py` gets the assertion that the FMP path is unchanged. This is
the whole of the "new source" tax on existing code.

### 4.2 The `EconomicSource` protocol

A new source is one module, one registry entry, and a seed. Nothing else in the
system changes — not the loader, not the DQ checks, not the mart views, not
`duk`, not the removal path.

```python
# src/fafnir/sources/economic.py
@dataclass(frozen=True)
class RawObservation:
    source_series_code: str
    obs_date: date              # period START, normalised by the CLIENT
    value: str | None           # unparsed; the loader validates and quarantines
    knowledge_date: date | None # realtime_start where the source has one
    flags: str | None           # vendor footnotes, verbatim

@dataclass(frozen=True)
class SeriesMeta:               # what the source knows about a series
    source_series_code: str
    title: str | None
    frequency: str | None
    units: str | None
    seasonal_adjustment: str | None

class EconomicSource(Protocol):
    name: str
    requires_key: bool
    supports_vintages: bool
    max_series_per_request: int
    def describe(self, codes: Sequence[str]) -> Iterable[SeriesMeta]: ...
    def fetch(self, codes, start: date, end: date) -> Iterable[tuple[RawObservation, ...]]: ...
```

Two rules that keep the protocol honest:

- **Period normalisation is the client's job, not the loader's.** BLS says
  `2026`/`M08`, BEA says `2026Q1`, Treasury says a timestamp, FRED says
  `2026-08-01`. Each client converts to a period-start `date`. The loader never
  learns a vendor's period vocabulary, which is the thing that would otherwise
  accumulate one `elif` per source forever.
- **Value parsing is the *loader's* job, not the client's.** The client passes the
  string through. The loader coerces it against `NUMERIC(28,10)` and quarantines
  what will not fit, so every source gets the same boundary validation and the
  same `economic_*` quarantine checks. A client that "cleans" a value is a client
  that can silently drop one.

Batching is declared (`max_series_per_request`) and executed by the client,
because it is wildly source-specific: BLS takes 50 series per request, FRED takes
one, and a single Treasury request returns a whole year of *every* tenor at once.

### 4.3 The four sources

| Source | Transport | Key | Vintages | Batching | Notes |
|---|---|---|---|---|---|
| `treasury` | XML over GET, `home.treasury.gov/…/pages/xml?data=…` | none | no | one request = one year × all tenors | the primary feed for both curves |
| `fred` | JSON GET, `api.stlouisfed.org/fred` | `FRED_API_KEY` (stubbed in config already) | **yes** (`realtime_start`/`realtime_end`) | 1 series/request | the only source that can supply a real knowledge date |
| `bls` | JSON **POST**, `api.bls.gov/publicAPI/v2` | `BLS_API_KEY` (stubbed) | no | 50 series, 20 years, **500 requests/day** | the day cap is a hard budget; the loader must respect it |
| `bea` | JSON GET, `apps.bea.gov/api` | `BEA_API_KEY` (stubbed) | no | 1 table/request | table+line, not a flat series code |
| `fmp` | existing `FMPClient` | `FMP_API_KEY` | no | one request = whole curve | secondary/fallback for the curve only |

All four API keys are **already** properties on `FafnirConfig`. No config change
is needed for the credentials.

**A candour note about the Treasury endpoints.** The XML feed hostnames and field
spellings below (`BC_1MONTH`…`BC_30YEAR` for the nominal curve, `TC_5YEAR`…
`TC_30YEAR` for the real curve) could not be verified from this session — the
network egress here blocks both `home.treasury.gov` and
`api.fiscaldata.treasury.gov`. They are therefore written down as *what the probe
must confirm*, not as fact. That is what `fafnir source probe-economic` is for
(§4.4), and it follows the precedent in `sources/probe.py`, whose opening line is
that field names cannot be settled from documentation.

### 4.4 `fafnir source probe-economic`

Same idiom as `probe-prices` / `probe-fund` / `probe-actions`: costs a handful of
requests, writes nothing, and answers what documentation cannot.

```bash
fafnir source probe-economic --source treasury --curve UST.PAR.NOMINAL
```

reports: the transport actually served (XML vs JSON vs an HTML error page), the
field names present, the tenors carried on the most recent date, the earliest
date the feed will serve, whether re-requesting an old window returns identical
values (i.e. whether this source revises), and — the one that matters most —
whether the values are **percent** (`4.21`) or **decimal** (`0.0421`). Getting
that wrong is a 100× error that every downstream chart would render without
complaint, and no schema constraint can catch it.

---

## 5. Ingestion

### 5.1 `src/fafnir/ingest/economic.py`

One loader, source-agnostic, mirroring `ingest/daily_price.py` step for step:

1. read `ref.tracked_series` for the tracked, `priority = 1` rows (optionally
   filtered by `--source` / `--series`);
2. mint any `core.economic_series` row that does not exist yet, from
   `source.describe()` — the declaration-to-master step, exactly what
   `fafnir ingest tracked` does for funds;
3. compute the window per series from the watermark minus an overlap
   (`economic_overlap_days`, defaulting wider than `overlap_days` because an
   economic revision lands later than a price correction);
4. fetch, in the client's own batches, with `RunLog` open the whole time;
5. `land_payload` the raw response — **in full**, including the periods the
   loader will decline to store;
6. set aside what is not storable-as-fact (annual-average pseudo-periods, §5.3);
7. validate each remaining value against `NUMERIC(28,10)` and quarantine the
   failures (never drop);
8. bitemporal upsert (§5.4);
9. refresh `first_obs_date`/`last_obs_date` on the series and advance the
   watermark.

### 5.2 Watermarks for a thing that is not a security

`ops.load_watermark` is keyed `(source, endpoint, security_id)` with `0` meaning
whole-endpoint. An economic series has no `security_id`. Two options were
weighed:

- overload `security_id` with `series_id` — **rejected**: two different entity
  spaces in one `BIGINT` column, silently colliding, and every existing query
  that joins watermarks to `core.security` starts returning nonsense rows.
- **chosen:** migration 0027 adds a nullable `series_id BIGINT` and moves the
  primary key to `(source, endpoint, COALESCE(security_id,0), COALESCE(series_id,0))`
  via a generated key column, with a `CHECK` that at most one of the two is
  non-zero. Existing rows are untouched and existing reads keep working.

Watermark endpoints are named per source and feed, e.g.
`treasury/daily_treasury_yield_curve`, so a source switch is visible in
`ops.load_watermark` rather than hidden behind a shared name — the same reason
`LEGACY_SPLIT_ADJUSTED_ENDPOINT` is kept around in the price loader.

### 5.3 What is landed but not stored

BLS publishes `M13` (annual average) alongside `M01`–`M12`, and `Q05` alongside
`Q01`–`Q04`. These are *published aggregates of the same series*, not observations
of a period, and storing them at monthly grain would corrupt every sum and every
resample that touched the series.

They are handled exactly as non-session bars are (`_drop_non_session`): the raw
payload lands in full, the aggregate periods are set aside with a documented
reason, and they are **not** quarantined — a quarantine is a claim that a real
observation was bad, and this is not that. An operator who wants the annual
average declares an annual-frequency series for it, which is what the vendor's
own annual series is.

### 5.4 The bitemporal upsert

For each incoming `(series_id, obs_date, value, knowledge_date)`:

```
current := the row with valid_to IS NULL for (series_id, obs_date)

if current is NULL:              INSERT (valid_from = knowledge_date or load_date)
elif current.value = value:      do nothing          <-- the idempotency property
else:                            UPDATE current SET valid_to = new_valid_from
                                 INSERT the new version
```

`rows_updated` on `ops.ingestion_run` counts revisions, which makes "did anything
get rewritten last night" a single query — and feeds the `economic_revision`
check (§8.4).

One guard worth naming: a value that arrives with a `knowledge_date` **earlier
than** the current row's `valid_from` is a feed serving an older vintage than the
one already stored. That is not a revision, it is a regression, and it is
refused with an `economic_vintage_regression` flag rather than being written.

### 5.5 CLI surface

```bash
fafnir series add UST.PAR.NOMINAL.10Y --source treasury --code BC_10YEAR \
    --frequency daily --units percent_per_annum --unit-class rate \
    --calendar USGOV --note "primary par nominal 10y"
fafnir series list [--source treasury] [--stale] [--all]
fafnir series untrack UST.PAR.NOMINAL.10Y [--discontinued 2026-06-30]
fafnir series show UST.PAR.NOMINAL.10Y            # metadata, coverage, open flags

fafnir ingest economic                            # all tracked, watermark-driven
fafnir ingest economic --source treasury
fafnir ingest economic --series UST.PAR.NOMINAL.10Y --from 1990-01-01
fafnir ingest curve UST.PAR.NOMINAL               # every tenor of one curve
```

`fafnir series untrack` carries the same two-meanings warning `track rm` does,
because it is the same trap: without `--discontinued` the series stays live and
the freshness check flags it every night from here on.

### 5.6 Nightly

`scripts/daily_update.sh` gains one step, after corporate actions and before
`db refresh-marts` / `dq run`, wrapped in the existing `upkeep` helper:

```bash
echo "==> Economic series"
upkeep fafnir ingest economic
```

`upkeep` (warn-and-continue) rather than fatal, for the reason already written
into that script for the universe steps: a source outage on an upkeep feed must
not cost the night's prices. A failed run still writes a failed
`ops.ingestion_run` row, which is what `doc/operations.md` tells the operator to
watch.

---

## 6. The read seam

Five `mart` relations. Every one is a grant to every mart reader (ADR 0009), so
each is listed here deliberately, and none of them exposes
`ops.data_quality_flag.detail` (ADR 0010's line).

| View | What it is | Why |
|---|---|---|
| `mart.v_economic_series` | the catalogue: key, title, units, `unit_class`, frequency, SA, source, coverage span, last obs | what `duk es list` reads |
| `mart.v_economic_observation` | **current knowledge only** (`valid_to IS NULL`) | the default read; the one nobody can get wrong |
| `mart.v_economic_observation_vintage` | every version, with `valid_from`/`valid_to` | point-in-time and revision-history reads |
| `mart.v_economic_series_coverage` | span, count, last obs, largest gap, revision count | the "don't probe the fact table" relation, mirroring `v_security_price_coverage` |
| `mart.v_yield_curve` | **long**: `(curve_key, obs_date, tenor_months, tenor_label, value)` | the join of curve → tenor → current observation |

`v_yield_curve` stays long and `duk` pivots it in pandas, for one reason: a
pivoted view has one column per tenor, so the discontinued 30-year and the
2022-vintage 4-month would each have been a migration. The pivot is three lines
of pandas and zero lines of schema.

The two-view split on knowledge time is not tidiness. **Selecting from the
vintage view without a `valid_to` filter silently double-counts every revised
observation** — a GDP quarter appears five times, a chart sums to five times the
economy. Making the safe read the short name and the sharp one the long name is
the cheapest defence available, and the rule also goes into the skill's
schema-map (§11).

---

## 7. `duk`

### 7.1 `duk yc -S db` — the promise the help text already makes

`yc` today force-falls back to live with a warning, and errors out if no FMP key
is configured. With the warehouse behind it:

- `-S db` becomes the default whenever a DSN is configured (`duk`'s ordinary
  `default_source` rule), with `-S live` still available and unchanged;
- **`--real`** (or `--curve UST.PAR.REAL`) selects the TIPS par real curve —
  the new capability;
- `--zero-rates`, `--tenors`, `--key-rates`, `--interval`, `--summary`,
  `--precision`, `-o` all keep working **untouched**: they are pure compute in
  `rates_utils` over a tenor-keyed frame, and the db path hands them the same
  frame the live path does.

The db path returns the identical DataFrame contract as `get_yield_curve` — wide
when several dates, tenor-indexed with a `years` column when one. An integration
test asserts the two paths agree on a shared date, which is the only way that
contract stays true.

Live-vs-db is then a genuine choice rather than a fallback, and there is a real
difference to state in `doc/duk.md`: live gives FMP's rounding and FMP's history;
db gives the Treasury's own publication, the full history back to 1990, the real
curve, and point-in-time reads.

### 7.2 `duk es` — the general economic command

Two letters, like every other `duk` command:

```bash
duk es list                                  # the catalogue
duk es list --source bls --frequency monthly --search "CPI"
duk es UST.PAR.NOMINAL.10Y                   # observations, current knowledge
duk es CPIAUCSL -s 2015-01-01 -e 2025-12-31 --json
duk es DGS10,DGS2 --wide                     # aligned frame, for a spread
duk es DGS10 --frequency month               # resampled with the series' own default_aggregation
duk es DGS10 --frequency month --agg avg     # ... or an explicit override
duk es GDPC1 --as-of 2014-05-01              # what was known on that date
duk es GDPC1 --vintages -s 2014-01-01        # the revision history itself
duk es UST.PAR.NOMINAL.10Y --coverage        # span, count, gaps, last obs
```

Option surface is deliberately `ph`'s: `-s/-e/-n`, `--csv/--json`, `-o`, `-p`,
`-q/-v`. Someone who can drive `duk ph` can drive `duk es` without reading
anything.

`--as-of` and `--vintages` are the whole payoff of §3.3 reaching a human. They
are also, as far as this design is concerned, the reason bitemporality is not
over-engineering: "what did the model see when it was trained" is the question a
research warehouse exists to answer.

`duk es` is **db-only**. `-S live` exits 1 with the ADR-0009-shaped message
("`es` reads the warehouse; re-run with `-S db`"), because there is no single
live API behind a catalogue that spans five vendors.

---

## 8. Data-quality checks

Named `economic_*` throughout, so an operator globs the family the way they
already glob `price_*`. Every one writes through `add_dq_flag_once`.

### 8.1 `economic_gap` — flagged per **run**, not per period

A missing period inside a series' active coverage, where "expected" comes from
the calendar for daily/weekly series and from the frequency otherwise.

**Contiguous missing periods are one flag**, with
`record_key = {series_key, from, to}`. This is not a nicety. Flagging per session
is exactly what turned ~2,900 thinly-traded securities into 599,808 gap rows and
buried the 29 that were genuinely broken (`GAP_MIN_SESSION_DENSITY`, `dq/checks.py`).
The 20-year Treasury's 1987–1993 hiatus is 1,500 consecutive missing days; it must
cost the queue one row, once, and an operator must be able to `dq accept` it in one
action — which is what `accepted_at` (migration 0024) is for: real, permanent, no
repair.

### 8.2 `economic_stale` — against the series' own publication lag

Latest observation older than `(one period + publication_lag_days + grace)`.
`publication_lag_days` is per-series (§3.2) and the grace is global. Without the
per-series term this check is a queue generator, for the documented NAV reason.

### 8.3 `economic_outlier` — the question depends on `unit_class`

| `unit_class` | Test | Default threshold |
|---|---|---|
| `rate` | absolute change, percentage points | 1.00 pp for daily, 3.00 pp for monthly+ |
| `index`, `level`, `count` | relative change | 0.20 (monthly+), 0.10 (daily) |
| `ratio` | absolute change | per-series only — no default is safe |

overridable per series via `outlier_threshold`. The 2022 hiking cycle is the
test case: a relative test flags it wholesale, an absolute-in-basis-points test
does not, and a series like a spread that can legitimately cross zero makes
"percent change" meaningless (a 0.01 → -0.01 move is a -200% change). That last
case is why `ratio` gets no default at all — the check declines to guess rather
than guessing wrong, which is the same move `GAP_MIN_SESSIONS_FOR_DENSITY` makes.

### 8.4 `economic_revision` — what bitemporality is for

A revision larger than the series' revision tolerance, raised when the loader
closes a version. Severity `info` for BEA/BLS (revision is their published
process) and `warn` for a source declared non-revising — a Treasury par rate that
changes after the fact is a vendor rewriting history, and nobody would otherwise
ever find out.

`detail` carries old value, new value, both `valid_from`s and the run id. It is a
**record**, not a condition, so it belongs in `NEVER_AUTO_RESOLVE` alongside
`corporate_action_drift` for the same stated reason: closing it silently discards
the only evidence that the rewrite happened.

**Two schema consequences** the flag surfaces: `ops.data_quality_flag` has only
`security_id` as an entity column, and `mart.v_security_dq_open` is keyed on it.
Migration 0027 adds a nullable `series_id` with a `CHECK` that at most one of the
two is set, and 0029 adds `mart.v_economic_series_dq_open` as the sibling view.
`dq list --series` and `dq_queue`'s filters follow.

### 8.5 The boundary family

`economic_value_out_of_range` and `economic_subresolution_value` — the quarantine
records for values that would not survive `NUMERIC(28,10)`. Direct descendants of
`price_price_out_of_range` / `price_subresolution_price`, and like them they are
records of rows that were **never stored**, so they have nothing to re-evaluate
and belong in `NEVER_AUTO_RESOLVE`.

### 8.6 Curve and cross-source checks

- **`economic_curve_incomplete`** — a curve date carrying fewer tenors than its
  recent norm. Catches the half-loaded day, which is otherwise invisible: a
  12-of-13 curve renders perfectly and bootstraps to a wrong zero curve.
- **`economic_series_unknown_to_source`** — a declared series the vendor will not
  return, mirroring `tracked_symbol_unknown_to_source` exactly.
- **`economic_source_disagreement`** (phase 3) — a `priority = 2` feed compared
  against the stored fact on a rotation, the way `actions_reconcile_buckets`
  reconciles 1/30 of the universe per night. This is what the multi-source
  mapping in §3.5 buys beyond mere portability.

### 8.7 Deliberately absent

An **inverted yield curve is not a defect**. Neither is a negative real yield, a
negative nominal rate, or a discontinuity at a policy break. Each is an economic
fact, and a check that flagged one would teach operators to ignore the queue.
Recording this in the plan and the playbooks is the point — `dq/recheck.py`'s
"What is deliberately not here" section exists for the same reason.

### 8.8 Recheckability

Following the `RECHECKABLE` discipline in `dq/recheck.py` — a check is
re-checkable only if its predicate is a function of warehouse state:

| Check | `dq recheck`? | `NEVER_AUTO_RESOLVE`? |
|---|---|---|
| `economic_gap` | yes | no |
| `economic_stale` | yes | no |
| `economic_curve_incomplete` | yes | no |
| `economic_series_unknown_to_source` | yes | no |
| `economic_outlier` | no — a judgement about vendor data | no |
| `economic_revision` | no | **yes** |
| `economic_value_out_of_range` | no | **yes** |
| `economic_subresolution_value` | no | **yes** |
| `economic_vintage_regression` | no | **yes** |
| `economic_source_disagreement` | no — a measurement | **yes** |

`_assert_scope_is_safe` in `recheck.py` already fails the import if a
`NEVER_AUTO_RESOLVE` check appears in `RECHECKABLE`, so this table is enforced,
not just documented.

---

## 9. Removal

### 9.1 Four different things are called "removing a series"

`fafnir track rm` already says two of these apart in its help text. There are
four, and conflating any two of them destroys data:

| # | Act | Prices | Economic | Data destroyed |
|---|---|---|---|---|
| 1 | **Stop loading it** | `track rm` *(exists)* | `series untrack` | none |
| 2 | **Retire it** — it genuinely ended | `track rm --closed` *(exists)* | `series untrack --discontinued` | none |
| 3 | **Drop the observations, keep the identity** — reload or repair | *(nothing scoped to one security)* | *(nothing)* | facts, recoverable by re-ingest |
| 4 | **Purge the entity** — it should never have existed | *(nothing)* | *(nothing)* | everything, permanently |

1 and 2 exist for prices and are additive for series. **3 and 4 are the new
feature**, and they need to exist for both dbs with one engine, because the
guards are the interesting part and maintaining two sets of them is how one set
goes stale.

### 9.2 Command surface

A new top-level group, deliberately not folded into `fafnir prices delete`:

```bash
fafnir remove observations --symbol MMSRX --from 2019-01-01 --to 2019-12-31 -m "…"
fafnir remove observations --series UST.PAR.NOMINAL.20Y -m "loaded under the wrong tenor mapping"
fafnir remove security --symbol XYZQ -m "test mint, never traded" --purge
fafnir remove series   --key TEST.SERIES.1 -m "scratch series" --purge
fafnir remove curve    --key UST.PAR.REAL -m "superseded by …"      # unbinds; series survive
fafnir remove list                       # the tombstone register
fafnir remove revoke 42 -m "re-admitting; the mapping is fixed"
```

`fafnir prices delete` stays exactly as it is and is **not** absorbed. It is a
different act: it removes bars the vendor has wrong *while the security keeps
loading*, and its suppression is per-bar in `ops.operator_override`. `remove` is
about an entity or a span, not about a wrong row. Two verbs for two acts, each
with its own record.

### 9.3 The engine: `src/fafnir/db/removal.py`

One `RemovalPlan`, shaped like `price_edits.EditPlan`: what would be deleted,
counted **per relation**, plus `refusals` and `notes`. A plan with any refusal
writes nothing and names every blocker, so an operator sees what to narrow.

Dry run is the default, and it has the same semantics `prices delete` already
uses — *make the change, count it, roll it back* — because that is the only way
the counts shown are the counts that would happen. `--yes` commits. `-m/--note`
is required and non-empty, enforced by a `CHECK` on the tombstone table the same
way `ck_operator_override_note` enforces it today.

### 9.4 Refusals — the actual design

Each one is a way this gets someone's data back from a backup.

| Refusal | Why |
|---|---|
| the security is a **merge survivor** | its history contains another issuer's bars; purging it destroys a company that was never named on the command line. `core.symbol_xref` with more than one closed period, or an `ops.operator_override` retargeted onto it, is the tell |
| the series is a **member of a live curve** | removing the 10-year silently changes every curve, bootstrap and spread that reads it. Remove the curve first, or pass `--detach-from-curve` and see the affected curves named |
| **unrevoked operator overrides** exist for the entity | those are the record of a deliberate operator decision. Removal must revoke them with a note that points at the tombstone, never orphan them |
| the plan exceeds **`--max-rows`** (default 250,000) | a `--purge` that turns out to span 60 years of a curve should stop and say so. `--allow-large` is the acknowledgement |
| `--purge` without `--yes`, or with an empty note | an irreversible act needs both |
| the entity **does not exist** | a typo'd key must not silently succeed |

Open DQ flags are *not* a refusal — they are closed by the removal itself, with
`resolved_by = 'removal:<id>'` and a note pointing at the tombstone, so the queue
never carries flags about data that is gone.

### 9.5 The tombstone: `ops.removal`

This is the part that makes removal *durable*, and it is the lesson migration
0025 already wrote down: *"A hand-written DELETE is also not durable. The feed
that produced the row still carries it, so the next … sweep re-inserts it."* A
purged security is re-minted by tomorrow's screener; a purged series is re-minted
by the next `ingest economic` off its `ref.tracked_series` row.

```
ops.removal
  removal_id     BIGINT IDENTITY PK
  entity_type    TEXT NOT NULL     -- security | economic_series | curve | observations
  entity_key     TEXT NOT NULL     -- symbol or series_key or curve_key
  security_id    BIGINT            -- as it was, for forensics; no FK (the row is gone)
  series_id      BIGINT            -- ditto
  scope          JSONB NOT NULL    -- {from, to} for an observation-span removal
  deleted_counts JSONB NOT NULL    -- {"core.daily_price": 15234, "ops.load_watermark": 1, ...}
  suppress       BOOLEAN NOT NULL DEFAULT TRUE   -- the loaders skip this key while active
  note           TEXT NOT NULL CHECK (btrim(note) <> '')
  created_by     TEXT NOT NULL
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
  revoked_at / revoked_by / revoked_note
```

A **sibling** of `ops.operator_override`, not an extension of it, for three
concrete reasons: override's `security_id` is `NOT NULL` and a series has none;
its unique index is keyed on `key_date`, which an entity removal has no value
for; and its `ck_operator_override_target` / `_action_type` constraints are
written tightly around `corporate_action`/`daily_price` and would have to be
loosened for every reader of that table. ADR 0014 records the choice.

`suppress` is consulted by `ingest securities` (skip a suppressed
`(source, symbol)`), `ingest tracked`, and `ingest economic` (skip a suppressed
`series_key`). `fafnir remove revoke` clears it, and the next load brings the
entity back — the same revoke semantics `override revoke` already has.

`deleted_counts` is what makes "what did we lose" answerable a year later, which
is the minimum a warehouse owes anyone for an irreversible act.

### 9.6 What removal does *not* touch

- **`landing.*` payloads survive by default.** Keeping them is what makes a
  removal reconstructible at all: the raw vendor responses are still there.
  `--purge-landing` exists for a genuine disk problem and says what it costs.
- **`ops.ingestion_run` history survives, always.** It is the lineage record and
  removing it would falsify the load history.
- **`ops.removal` rows are never deleted** — including on revoke, which stamps
  `revoked_*` rather than erasing.
- **`mart.security_latest` is materialized**, so a security removal ends by
  telling the operator to run `fafnir db refresh-marts`, or does it when `--yes`
  was passed. Same for adjustment factors on an observation-span removal that
  moves a dividend's reference close.

### 9.7 Who may run it

- **Never via MCP.** The ops profile is read-only by construction (ADR 0010) and
  gains no removal tool. Removal is a CLI act.
- The fafnir-dba skill may run `remove observations` and `remove series
  --discontinued` under the existing dry-run-first rule.
- **`remove … --purge` joins `scripts/reset_data.sh`, `db rollback` and
  `db migrate` in the propose-never-run bucket** (skill rule 6). It is
  irreversible and universe-shaped, and an agent's judgement is not the right
  last line of defence for that.

---

## 10. Migrations, and what they are allowed to assume

| # | Contents |
|---|---|
| `0027_economic_series.up.sql` | `core.economic_series`, `core.economic_observation`, `ref.tracked_series`, `ref.observation_calendar_def` + `ref.observation_calendar`; `ops.load_watermark.series_id` (§5.2); `ops.data_quality_flag.series_id` (§8.4) |
| `0028_yield_curves.up.sql` | `ref.curve`, `ref.curve_tenor`, and the seed rows + declarations for `UST.PAR.NOMINAL` (13 tenors) and `UST.PAR.REAL` (5 tenors) |
| `0029_economic_marts.up.sql` | the five `mart` views of §6, plus `mart.v_economic_series_dq_open` |
| `0030_removal_register.up.sql` | `ops.removal` |
| `0031_usgov_calendar.up.sql` | the `USGOV` federal-business-day calendar rows to the configured horizon, and the `ensure-horizon` extension that keeps rolling it forward |

Each gets a `.down.sql`. Grants ride the default privileges already established in
`0001` (core/ref/mart → `fafnir_read`/`fafnir_app`) and `0021`
(core/mart/ref/ops/landing/meta → `fafnir_ops`), so no new `GRANT` statements
should be needed — and `test_migrations_least_privilege.py` is what proves it,
since every one of these must apply as the ordinary `fafnir_ingest` role.

---

## 11. Documentation and the fafnir-dba skill

### Repo docs

- `doc/data_dictionary.md` — every new relation, at the same depth as the
  existing entries: grain, source, units, cadence, and for observations the
  knowledge-time semantics.
- `doc/architecture.md` — economic series in the ERD and the layer diagram.
- `doc/ingestion.md` — a source→table map per source, request budgets (BLS's 500/day
  is a real constraint), and the watermark endpoint names.
- `doc/duk.md` — `duk es`, `duk yc -S db`, and an honest live-vs-db comparison.
- `doc/operations.md` — the nightly economic step, and a **Removing data** section
  covering all four meanings of removal with the dry-run-first discipline.
- `doc/extending.md` — replace the roadmap's "Economic series: planned" with what
  shipped, and rewrite "Adding a new data source" around the `EconomicSource`
  protocol, since that section's current sketch (`vintage_date` on the
  observation) is superseded by §3.3.
- `doc/adr/0012`, `0013`, `0014`.
- `doc/index.md` — link the new plan, ADRs and sections.

### Skill (`.claude/skills/fafnir-dba/`)

| File | Change |
|---|---|
| `SKILL.md` | economic series in the "which tool for what" table; `remove … --purge` added to standing rule 6 (operator commands); a new standing rule for the knowledge-time filter |
| `references/schema-map.md` | grains for the six new relations; **"read `mart.v_economic_series_coverage`, never probe `core.economic_observation`"**; and the loud one — *selecting from the vintage view without a `valid_to` filter double-counts every revision* |
| `references/dq-playbooks.md` | a playbook per `economic_*` check, each with its row in the durability matrix; and §8.7's list of what is deliberately not a check |
| `references/data-semantics.md` | `obs_date` is the period start; SA vs NSA must never be mixed in one series; revision is normal for BEA/BLS and abnormal for Treasury; **a rate averages and a flow sums** — the resampling trap |
| `references/economic-sources.md` *(new)* | per source: cadence, revision behaviour, publication lag, request budget, what a failure looks like, and which `probe-economic` answer settles which question |
| `references/removal-policy.md` *(new)* | the four meanings; which the agent may run and which it proposes; dry-run-first; the post-removal verification queries; how to read `ops.removal` |
| `references/automations.md` | the economic nightly step and its place in the order |

The skill's own description line needs the economic vocabulary added, or it will
not trigger on "what does fafnir have on the 10-year".

---

## 12. Tests

Unit (no DB):

- value coercion against `NUMERIC(28,10)`: out-of-range, sub-resolution,
  non-numeric, vendor `.` and empty string — mirroring `test_validation.py`;
- period normalisation per source: BLS `M01`/`M13`/`Q05`, BEA `2026Q1`,
  Treasury timestamps, FRED dates → period-start;
- **the bitemporal upsert decision function**, table-driven: new / unchanged /
  revised / vintage-regression;
- curve pivot long→wide, including a date missing a tenor;
- `RemovalPlan` refusals, one test per row of §9.4;
- `duk es` argument validation and the `-S live` refusal;
- `BaseSource._request` preserves the FMP contract (§4.1).

Integration (`FAFNIR_TEST_DSN`):

- **idempotency**: load twice → identical rows, zero new versions. The single
  most important test in the feature;
- a changed value creates exactly one new version and closes exactly one;
- `--as-of` returns the pre-revision value;
- `economic_gap` flags a 400-day hiatus as **one** row, not 400;
- `economic_stale` does not fire inside a series' publication lag;
- `duk yc -S db` and `-S live` agree on a shared date to the published precision;
- purge → the nightly loader does **not** re-mint while suppressed → `remove
  revoke` → the next load restores it;
- `test_migrations_least_privilege.py` covers 0027–0031;
- `test_profiles.py` gains the new MCP tool names; `test_sql_placeholders.py`
  covers the new SQL.

---

## 13. Sequencing

Each phase is independently shippable and leaves the warehouse in a coherent
state.

| Phase | Contents | Why here |
|---|---|---|
| **0** | `BaseSource` widening (§4.1); `source probe-economic` skeleton | nothing else can be written correctly until the transport seam exists and the Treasury field names are confirmed against the live feed |
| **1** | migrations 0027/0028/0031; `treasury.py`; `ingest/economic.py`; `fafnir series *`; both Treasury curves loaded end to end | the requested starting point, and it exercises daily frequency, curves, tenor discontinuities and a no-key source |
| **2** | migration 0029; `duk yc -S db` incl. `--real`; `duk es`; the two MCP read tools | the data becomes readable; `yc`'s standing caveat is retired |
| **3** | `economic_*` DQ checks; `dq recheck` scope; `dq list --series`; nightly wiring; nightly-report coverage | the data becomes trustworthy |
| **4** | migration 0030; `fafnir remove` for **both** dbs; `ops.removal` suppression in all three loaders | needs the economic entities to exist so the engine is written against both cases at once, which is what keeps one set of guards |
| **5** | `fred.py`, `bls.py`, `bea.py`; `economic_source_disagreement`; FMP curve as secondary | the proof that §4.2 is a real seam rather than a diagram — three sources added with no change to the loader, checks, views, `duk` or removal |
| **6** | docs and skill references throughout, landing with the phase that makes each true | the repo's existing practice: `doc/` and the skill change in the same PR as the behaviour |

Phase 4 could move earlier if removal is the more urgent need — it depends on
phase 1 only for the economic half, and the price half could ship against
today's schema. It is placed here because writing the engine once, against both
entity types, is what stops the two halves from diverging.

---

## 14. Open questions for the operator

1. **Treasury feed shape.** The XML endpoints, field spellings and — critically —
   whether values are percent or decimal could not be verified from this
   environment (egress blocks both `home.treasury.gov` and
   `api.fiscaldata.treasury.gov`). Phase 0's probe settles all three before any
   loader logic depends on them. If `fiscaldata.treasury.gov`'s JSON API is
   reachable from the warehouse host, it is the better transport and removes the
   XML seam from §4.1's critical path — worth checking first.
2. **How far back?** Treasury nominal par rates go to 1990; the real curve to
   2003. Full history is ~205k + ~29k rows — trivial to store, and the default
   should be "all of it" unless there is a reason not to.
3. **FRED as primary or secondary?** FRED is the only source that supplies real
   knowledge dates, which would make `--as-of` genuinely exact rather than
   load-date-approximate for the Treasury series it mirrors. It costs a key.
   Recommendation: Treasury primary, FRED at `priority = 2` once phase 5 lands.
4. **Series-key naming.** `UST.PAR.NOMINAL.10Y` is proposed as
   `<source-domain>.<family>.<variant>.<tenor>`. It is the string users will type
   for years, so it is worth settling before the phase-1 seed rather than after.
5. **Removal default for `landing`.** Proposed: keep, always, unless
   `--purge-landing`. Confirm that matches the retention policy on the host's
   disk budget.
6. **`--max-rows` default.** 250,000 is a guess sized to "more than a decade of
   one daily series, less than a curve's whole history". Worth an operator's
   number.
