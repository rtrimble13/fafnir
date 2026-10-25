# ADR 0018: Bitemporal fundamentals on Sharadar SF1

- Status: Accepted
- Date: 2026-10-25
- Implemented by: #87 (the statement tables), #90 (the loader), #95 (point-in-time
  access), #106 (the nightly delta)
- Extends: [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md), whose
  temporal columns were designed for this
- Related: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md) and
  [ADR 0004](0004-unadjusted-price-feed.md) (store what was so, derive the rest),
  [ADR 0015](0015-multi-vendor-provenance-and-precedence.md) (provenance),
  [ADR 0016](0016-sharadar-identity-and-survivorship-floor.md) (the permaticker, the
  1998 floor), [ADR 0019](0019-derived-metrics-not-vendor-metrics.md) (the metrics
  read from these tables), the build plan
  [fundamentals-sf1.md](../plans/fundamentals-sf1.md)

## Context

A company's statements change after it files them: a later filing restates a period,
or re-presents it as a comparative on a new basis. A backtest that reads a period as it
stands today reads figures no investor had at the time. So a statement needs the date
the market could know it, and a restated statement needs its own.

The first plan sourced statements from FMP. The vendor assessment (§3.1) found two
reasons not to:
- **Fabricated filing dates.** Before FY1994 FMP sets the filing date to the period end:
  a zero-day lag, which is impossible.
- **No restatement history.** Its statement endpoints return one current version per
  period.

So FMP's statements are not loaded, for any year.

Sharadar's SF1, as the probe measured it (#83, 2026-10-24):

- **First-reported rows (ARQ, ARY, ART): `date` is the SEC filing date.**
  - Two public-record 10-Ks agree.
  - None of 1,731 rows precedes its period end.
  - No era's filings sit on one lag (they are not imputed), back to 1991.
- **Restated rows (MRQ, MRY, MRT) are dated by the period end**, so a restatement carries
  no date of its own. `lastupdated` is restamped on whole histories. SF1 keeps only a
  period's *latest* restatement.
- **Share counts and per-share values are in today's share terms**, re-expressed at every
  later split.
- **Coverage.** History runs back to 1991, with one share class per issuer, the primary.

## Decision

### 1. Three timelines, each bound to columns

| Timeline | Columns | Whose |
|---|---|---|
| Observation | `fiscal_date` (SF1 `reportperiod`) | the period the figures describe |
| Knowledge | `known_from` / `known_to`, half-open | when the market could know this version |
| Load | `loaded_at` / `superseded_at` | when fafnir held this row |

`valid_from` / `valid_to` are not used: elsewhere in the warehouse they are inclusive
dates, and these intervals are half-open.

### 2. Versions live on the knowledge timeline

- **A first-reported row is version 1**, known from its filing date (`knowledge_source =
  'filed'`).
- **A restated row that differs from the version in force is a new version**, known from
  the day fafnir loads it (`'observed'`). The previous version's `known_to` becomes that
  day.
- **A period with no first-reported row** is known from the issuer's next first-reported
  filing (`'estimated'`, flagged), or with none, from the load day (`'observed'`).
- **Never a knowledge date:** a restated row's `date` (the period end, before even the
  original filing) and `lastupdated` (a restamp).

Intervals chain the versions of one period, each `known_to` equal to the next
`known_from`. Successive periods are not chained: what was known about FY2015 stays known
when FY2016 is filed.

### 3. Vendor corrections live on the load timeline

When a first-reported row changes in SF1, the filing did not; Sharadar fixed its record.
The row is superseded (`superseded_at`) and the corrected one inserted with the same
version and knowledge interval. A restatement and a correction are told apart by SF1's own
dimensions: a first-reported row that changes is a correction, and a restated row that
differs is a restatement.

### 4. A version is stored in the share terms of its `known_from`

- **First-reported:** SF1's value ÷ the product of Sharadar's splits after the filing
  date, from ACTIONS (the list SF1 was adjusted by), as exact ratios.
- **Observed:** stored as given, since today's terms are its terms.

A later split leaves the recovered values unchanged within rounding. The content hash
compares share counts at 1,000-share precision, so **a split's re-expression is never a
version**. Per-share values are not stored as columns (ADR 0019).

### 5. Every year loads; cross sections start at the floor

Rows before 1998 load: the first panel years need them for TTM and growth. The factor
panel, and any cross-section the mart presents as complete, starts on 1998-01-01 (ADR
0016 §2).

### 6. The primary share class only

SF1 holds one class per issuer. Its statements attach to the security linked to that
class's permaticker. A secondary class has none; `duk` and the MCP tools answer it with
the issuer's primary class, through the shared CIK.

### 7. Two in-place updates, never a value

`known_to` is set once, from open to a date, in the transaction that inserts the next
version. `superseded_at` is set once. A stored value is never updated. A past state is
reproducible: the rows loaded and not superseded by time T, with `known_to` read from the
versions visible then.

## Consequences

- **No look-ahead by construction.** `CHECK (known_from >= fiscal_date)` is the storage
  tripwire. The panel's standing invariant, no input with `known_from > as_of_date`, is
  the read-side one (ADR 0019).
- **History before the backfill reads as first reported.** Every restatement SF1 holds at
  the backfill is known from the backfill day. When restatements happened historically is
  lost. So is any intermediate restatement, since SF1 kept only the latest. Historical
  growth rates compare first-reported with first-reported, on different bases where a
  company reclassified.
- **From the backfill on, a restatement is dated** within a day of Sharadar publishing
  it, and the history gains real restatement dates as it accrues.
- **A split missing from Sharadar's ACTIONS misstates every earlier period's share
  counts**, as it misstates recovered prices (#75). The checks that watch the one watch
  the other.
- **A secondary share class has no statements and no panel row.** A multi-class issuer's
  market cap is approximate (ADR 0019).
- **The vocabulary changes.** `knowledge_source` is `filed`, `observed` or `estimated`.
  The FMP prompt's `accepted` is dropped (SF1 has no acceptance time), and `observed` is
  new.
- **Removing Sharadar** (its licence's deletion clause) removes these tables whole: every
  row is `source = 'sharadar'`, and no other vendor writes statements.

## Alternatives considered

- **FMP's statements.** Rejected: fabricated filing dates before 1994, and no restatement
  history at all.
- **Estimate a restatement's date** as the filing date of the same period a year later,
  the filing whose comparatives would carry it. It would give historical growth rates a
  consistent basis. Rejected as the default: SF1's restated value is the latest, possibly
  from a filing two or three years on, so the estimate can leak a later restatement into
  an earlier date, and nothing in SF1 can test it. Deferred as a research mode beside the
  default; it needs no schema, only stored filing dates.
- **Versions on the load timeline only, with one knowledge date per version** (the FMP
  prompt's design). It holds the same information, but a point-in-time read must find
  "the latest version known by then", and nothing states where a version stops. The
  interval makes the read one predicate and the chain testable.
- **Store SF1's share terms as given.** Rejected: every split would rewrite every
  earlier period, and each version's terms would be "whenever it was loaded".
- **Version a split's re-expression.** Rejected: a false restatement of every earlier
  period at every split.
- **Floor `core` at 1998.** Rejected: the first panel years would lack their TTM and
  growth inputs. SF1's pre-1998 filing dates, unlike FMP's, look real.
- **Attach the issuer's statements to every share class.** Rejected: it double-counts
  an issuer in cross-sections, and needs an issuer entity the schema does not have.
