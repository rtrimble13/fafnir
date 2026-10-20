# ADR 0015: Multi-vendor provenance, precedence and audited corrections

- Status: Accepted
- Date: 2026-10-18
- Implemented by: #45 (FMP loaders scoped to FMP-fed securities), #46 (the precedence
  write path and `ops.vendor_correction`), #55 (`core.security_vendor_xref`), #98
  (`fafnir vendor feed`), #70 (the per-vendor report, optional)
- Extends: [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md) (one
  `security_id` per security, whoever supplies its rows)
- Related: [ADR 0001](0001-raw-prices-plus-adjustment-factors.md) and
  [ADR 0004](0004-unadjusted-price-feed.md) (raw bars, factors derived),
  [ADR 0005](0005-automatic-universe-maintenance.md) (the universe maintains itself),
  the [Sharadar adoption plan](../plans/sharadar-adoption.md) and its
  [assessment](../plans/sharadar-coa-assessment.md)

## Context

Until now one vendor, FMP, supplied every row the warehouse holds, apart from the rows
an operator writes. The Sharadar plan adds a second vendor to the same tables, in the
same warehouse: Sharadar fills history FMP never had (issuers that died before fafnir
first ran, bars and actions FMP never served) and corrects FMP's rows where the two
disagree. Tiingo will supply mutual-fund NAVs, and FRED economic series.

Three things in the code assume a single vendor.

1. **Every write is last-writer-wins.** `repository.upsert_daily_prices` upserts with
   `ON CONFLICT (security_id, trade_date) DO UPDATE SET … source = EXCLUDED.source`.
   A second vendor routed through it would overwrite FMP's bar **and relabel it** as
   its own. `upsert_corporate_action` guards operator rows
   (`WHERE core.corporate_action.source <> 'operator'`) but no other vendor's.
2. **"Vendor-fed" means "not operator".** `VENDOR_FED_SECURITY = "source <> 'operator'"`
   decides which securities the FMP loaders feed. A security minted from Sharadar would
   therefore be fed by FMP, which serves a ticker's whole history whoever used it
   (data-semantics §14), and the upsert above would mix another issuer's bars into it.
3. **`source` says who minted a row, and nothing says who feeds it.** `source` exists
   on `core.security`, `core.symbol_xref`, `core.company_profile`, `core.daily_price`
   and `core.corporate_action`, defaulting to `fmp`. Nothing records which vendor
   should load a security's *next* row.

The vendors' licences add a fourth constraint. Each says what happens to its data
when the subscription ends:

- **Sharadar Personal Use License §10:** "Within thirty (30) days of termination,
  delete from all computer systems you own or operate all copies of the Services Data
  (including downloads, bulk files, caches, and extracts), all data sets that contain,
  substantially copy, or could reproduce the Services Data … If requested, you will
  promptly provide an affidavit certifying deletion."
- **FMP Terms of Service §6.2–6.3:** on termination, destroy "all copies or other
  embodiments of … any and all data or information contained in or derived from The
  Services"; "Customer must delete all Data it has received from FMP … including data
  cached". §5 may let a customer whose agreement is not terminated for cause retain
  "information printed or obtained through The Services"; the plan asks FMP in
  writing (#43).

So the warehouse must always be able to say which vendor supplied a row, and must
know what removing a vendor would take, even though the plan intends to keep both.

## Decision

### 1. Provenance: `source` is who supplied the row

Every fact row carries the party that supplied it in `source`: `fmp`, `sharadar`,
`tiingo`, `fred`, or `operator`. On `core.security`, `source` means **the vendor that
minted the security**, not the vendor that feeds it now. A security FMP minted keeps
`source = 'fmp'` after Sharadar starts feeding it.

### 2. Feed assignment is explicit

Which vendor loads a security's *new* rows is decided by a rule, not inferred from
`core.security.source`:

- While `primary_price_vendor = "fmp"`, the FMP loaders feed securities with
  `source = 'fmp'`, and only those (#45). Their universes, per-symbol guards and
  ticker checks all use that predicate.
- The Sharadar loaders feed securities with an open Sharadar link in
  `core.security_vendor_xref` whose `is_feed` is true (#55).
- At cutover, `fafnir vendor feed --vendor sharadar --all-linked` moves `is_feed`
  from FMP to Sharadar; flipping it back is the rollback (#98, #111).
- One vendor feeds a security's new rows at a time.
- Operator-minted securities (`security split-history`) are fed by nobody,
  unchanged.

### 3. Precedence: `operator > sharadar > tiingo > fmp`, and corrections are audited

When two parties hold a value for the same key, the higher one wins:

| Rank | Party | Why |
|---|---|---|
| 1 | `operator` | A person checked it against an outside reference (`fafnir actions`, `prices shift`/`rescale`, `ops.operator_override`). |
| 2 | `sharadar` | Permaticker-keyed (no reused-ticker contamination), revises its own history (`lastupdated`), the system of record. |
| 3 | `tiingo` | Mutual-fund NAVs; Sharadar carries none, so the two never hold the same key. |
| 4 | `fmp` | Ticker-keyed; known to restate and mis-scale bars (data-semantics §15, §21). |

FRED writes its own tables and is outside the ranking.

The write rules:

- **A vendor always revises its own rows.** FMP's restatement guard (data-semantics
  §21) still applies to FMP's rows, and Sharadar's `lastupdated` revisions to
  Sharadar's.
- **A vendor never writes over a higher-ranked row.** The key is skipped and
  counted (`skipped_foreign` in the run's `params`). FMP can never overwrite a
  Sharadar row, at cutover or after a rollback.
- **A higher-ranked vendor replaces a lower-ranked row only through the correction
  path.** The caller passes `correct=True` explicitly; an ordinary load fills empty
  keys and skips foreign ones. Each replaced row is written, as it stood, to
  `ops.vendor_correction` in the same transaction: the key, the old row, the old and
  new `source`, the run, and the reason (`reconcile_divergence`, `gap_fill`,
  `restatement`). `fafnir vendor corrections revert` writes the old row back.
- **Operator rows and suppressed keys beat every vendor.** No vendor writes a key an
  operator added, deleted or transformed, with or without `correct=True`
  (unchanged).

Corrections come from reconciliation (#66), not from every load. A difference between
two vendors can be an error in either one, or a convention: consolidated versus
primary-exchange volume, a dividend's ex-date versus pay-date. The historical
comparison (#69) sorts every divergence into a category. It checks a sample of each
category against an outside reference and states, per category, whether to correct it
in bulk, document it as a convention, or leave it flagged.

*Amended 2026-10-20 (plan decision log):* Sharadar wins on price, so there is no
per-category policy and no separate reconciliation. `fafnir vendor correct` (#75)
finds the differing FMP bars of linked securities and corrects them; the one
convention checked first is volume (#69). #66 is folded into #75.

### 4. Vendor attributes stay in vendor tables

Sharadar's names, sectors, identifiers, category and delisting reasons live in
`core.vendor_security` and `core.vendor_security_event` (#55, #58). They are never
written over the FMP-sourced descriptive columns of `core.security`. Which vendor's
attributes a reader sees is decided by a precedence view (#109), not by overwriting.

### 5. Separability is a report, not a guarantee

Because corrections replace rows in place, a series can hold rows from more than one
vendor, so removing one vendor is not a single `DELETE`:

- **Removing Sharadar:** delete its rows and the securities it minted, then restore
  the FMP rows it replaced from `ops.vendor_correction`. The audit table is what
  makes this possible.
- **Removing FMP:** delete its rows, the old FMP rows in `ops.vendor_correction`,
  `landing.fmp_raw`, and its descriptive columns. Securities FMP minted keep their
  `security_id`, because Sharadar rows hang off them.

`scripts/reset_data.sh --scope vendor:<v> --dry-run` (#70, optional) reports what each
vendor supplied, per table and outside the database (dumps, off-site copies, bulk
files), so the work a licence event would require is known in advance. It is built
only if a licence event makes it necessary.

*Built 2026-10-20 for Sharadar:* `scripts/reset_data.sh --scope vendor:sharadar` is the
report (it refuses `--yes`); the checklist that uses it is
[operations.md › Ending a vendor subscription](../operations.md#ending-a-vendor-subscription).
The deletion itself is still not built.

### 6. Licence hygiene

The repository is public, and every vendor licence forbids redistribution. No vendor
data is committed: tests use synthetic fixtures shaped like the feed. Reports posted
to issues and pull requests carry aggregates only, and cite `security_id` and dates,
never vendor values. Raw evidence stays on the host.

## Alternatives considered

- **Symmetric fill-only** (plan v1): each vendor fills only keys nobody holds, and no
  vendor ever replaces another's row. Rejected because it leaves every FMP error that
  Sharadar would fix in place. The warehouse would know a better value and keep
  serving the worse one.
- **One warehouse per vendor** (plan v1's typed landing copy plus a shadow run):
  rejected on 2026-10-18 in favour of one warehouse of market data. Two copies double
  the disk and the reconciliation work, and leave readers to choose between them.
- **Last writer wins** (today's upsert): rejected. It silently relabels another
  vendor's row and loses the value it replaced.

## Consequences

- **A series can change vendor partway through, and `mart` does not show it.**
  `mart.v_daily_price_raw` leaves out lineage columns (ADR 0009), so a reader of
  prices cannot see `source`. The operations tier can, in `core.daily_price.source`
  and `ops.vendor_correction`. The cutover date is recorded in the plan's decision
  log as the seam date.
- **Adjustment factors are always recomputed after any change to actions,** whether
  a correction, a revert or an excision. They are derived from mixed-vendor actions,
  as `fafnir adjust` already assumes.
- **Every vendor write path names its writer.** The upserts take it as `source`, which
  they already had; the FMP loaders pass the default, `fmp`, and behave exactly as
  today, because every vendor row today is FMP's (#46 proves it with the existing
  tests unmodified).
- **`ops.vendor_correction` holds FMP data.** It is FMP's to keep or delete under
  FMP's licence, like `landing.fmp_raw`, and is covered by the same retention answer
  (#43).
- **A rollback cannot quietly undo a correction.** FMP outranks nothing, so returning
  the feed to FMP leaves every Sharadar row and every correction in place. Reverting
  a correction is an explicit, recorded act.
