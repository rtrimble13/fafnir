# DQ playbooks

One entry per `check_name` the codebase actually writes. Read the entry before
resolving anything of that kind.

**Two facts that change how the queue is counted, before any playbook:**

- **`price_*` flags repeat by design.** The price loader uses `add_dq_flag`, not
  `add_dq_flag_once`, because each detection is itself the signal —
  `count_price_quarantines` counts them to decide when a persistently bad bar has
  held the watermark long enough. Every other check is one row per open condition
  (`add_dq_flag_once`, migrations 0014/0016). Filter `price_*` out before treating
  a count as a count of problems.
- **A quarantined bar holds the watermark.** Up to `MAX_QUARANTINE_HOLDS = 5`
  runs, then the loader steps past it. So a symbol accumulating `price_*` flags is
  usually also a symbol that has **stopped advancing** — check `watermarks`. That
  is the more urgent half and it is invisible in the flag itself.

---

## Durability: what happens to a flag after you close it

**Read this before choosing how to close anything.** Resolving is not the only
disposition, and for most checks it is the wrong one. Three commands close a flag
and they are not interchangeable:

- **`dq recheck`** re-evaluates a check against its own open flags and closes the
  ones whose condition is no longer true. It is the negation of the check, run by
  the same constants, so what it closes cannot come back. Only six SQL checks
  support it, plus the `dorq_*` checks by re-running dorq (below); a check that is
  not listed cannot be rechecked at all.
- **`dq accept`** is a terminal disposition for a condition that is real,
  understood, and has no repair. It sets `accepted_at`, and `add_dq_flag_once`
  skips a condition already accepted — so the writer stops re-detecting it. It
  does nothing for a check written through `add_dq_flag` (the `price_*`
  quarantines), which does not consult acceptance.
- **`dq resolve`** records a judgement and frees the condition's slot. **It does
  not stop the writer.** If the condition is still true when the writer next runs,
  the flag comes straight back. That is by design (standing rule 1), and it is why
  a resolve is only ever right where the writer will not re-detect.

| Check | Written by | Written through | A resolve is re-written by | `dq accept` suppresses? | `dq recheck`? |
|---|---|---|---|---|---|
| `gap`, `sparse_coverage`, `outlier`, `stale`, `security_duplicate_identity`, `security_missing_classification` | nightly `dq run` | `dq.checks` | the next `dq run`, if the condition still holds | yes | yes — except `security_duplicate_identity` |
| `dividend_no_prior_close`, `dividend_exceeds_price`, `adjustment_factor_extreme`, `adjustment_failed` | `adjust` | `add_dq_flag_once` | the next `adjust` of that security | yes | `dividend_no_prior_close` only |
| `price_scale_collapse` | price loader | `add_dq_flag_once` | a re-read of that bar | yes | no |
| `price_restatement_conflict` | nightly price load (not `--from`) | `add_dq_flag_once` | a nightly re-read of that date while the vendor still restates it beyond the guard. An explicit `ingest prices --from` re-fetch of the date **resolves it itself** | yes | no |
| `price_split_scale_mismatch` | price loader | `add_dq_flag` (**no dedupe**: a quarantine) | every re-read of that bar | **no** | no |
| `split_duplicate_suspect` | `adjust` | `add_dq_flag_once` | the next `adjust` of that security, while both splits are on file | yes | no |
| `tracked_symbol_delisted` | `ingest tracked` | `add_dq_flag_once` | the next `ingest tracked`, while the declaration names a delisted identity | yes | no |
| `price_cross_field_violation`, `price_non_positive_price`, `price_subresolution_price`, `price_price_out_of_range`, `price_missing_or_nonnumeric_ohlc` | price loader | `add_dq_flag` (**no dedupe**) | *every* re-read of that bar — overlap window, watermark holds, backfills | **no** | no |
| `security_company_name_drift` | security master | `add_dq_flag_once` | not re-written: the upsert stores the new name | yes — **for the whole ticker** | no |
| `corporate_action_drift` | action reconciliation | `add_dq_flag_once` | the next reconciliation of that symbol, if the drift persists | yes — **for the whole symbol** | no |
| `symbol_change_conflict` | symbol-change sweep | `add_dq_flag_once` | no longer re-detected once the row is terminal; the sweep closes it itself when a retry applies the rename | yes | no |
| `split_invalid`, `dividend_invalid` | action loader | `add_dq_flag_once` | the next load of that action | yes | no |
| `tracked_symbol_unknown_to_source` | `ingest tracked` | `add_dq_flag_once` | the next `ingest tracked`, if the vendor still won't serve it | yes | no |
| `security_delisting_contradicted` | `ingest delisted` | `add_dq_flag_once` | the next sweep, while the vendor has bars after the date and the record is within 60 days. **Never a bare resolve**: `security venue-transfer` accepts it, `security delist` resolves it | yes — and an acceptance is how the sweep knows the record is a move | no |
| `security_inactive_but_trading` (info) | `ingest securities` | `add_dq_flag_once` | the next load that turns the row inactive again within 5 sessions of a bar | yes | no |
| `dorq_*` | `dq run --engine dorq\|both` with `--no-shadow` | `dq.dorq.ingest` (the same two guards as `dq.checks`) | the next dorq run whose window still reports it — nightly, only dates within its `--since`, plus any date of a security whose history was new or rewritten since the night before (the backfill); a `--full` run, any date | yes | yes, by re-running dorq — except the Never three and the cross-sectional `dorq_cohort_gap`, `dorq_cohort_move` |

**The two acceptance traps.** `corporate_action_drift` and
`security_company_name_drift` are both keyed on `record_key = {"symbol": …}`. An
acceptance is matched on that key, so accepting one hides **every future drift for
that symbol**, not the instance you looked at. Never accept either unless you mean
exactly that.

**The `price_*` trap.** Acceptance does not suppress them and recheck cannot reach
them, because each detection is the signal (`count_price_quarantines` reads them to
decide when a bar stops holding the watermark). The only thing that stops a
`price_*` flag is the bar no longer being re-read, or no longer being bad. Closing
them is bookkeeping; say so in the note.

Every playbook below carries a **Close with** line naming which of these applies.

---

## `gap` — a calendar session with no bar

Written by `fafnir dq run`. A trading day in `ref.trading_calendar` for which the
security has no row in `core.daily_price`.

Only for securities that trade densely enough for a missing day to mean something.
A security holding a bar for less than `GAP_MIN_SESSION_DENSITY` (0.80) of the
sessions in its own window gets one `sparse_coverage` flag instead — see below —
and no per-session `gap` flags at all. Windows shorter than
`GAP_MIN_SESSIONS_FOR_DENSITY` (60 sessions) are always treated as dense, so a
newly-listed symbol is still checked per session.

**Diagnose.** Count securities per gap date first (the query in SKILL.md). Then:

```sql
-- Is this date a gap for the whole venue, or just this security?
SELECT s.exchange_code, count(*) AS securities_with_gap
  FROM ops.data_quality_flag f JOIN core.security s USING (security_id)
 WHERE f.check_name = 'gap' AND f.resolved_at IS NULL
   AND f.record_key->>'trade_date' = '2026-07-14'
 GROUP BY 1;
```

**Resolve when** the date is a confirmed venue holiday or halt for that security
only, and peers on the same venue traded (so the calendar is wrong for this
security, not the load).

**Escalate / repair when** many securities share the date — that is a missed load,
not a market fact. Re-ingest the window first:
`sudo -u fafnir /opt/fafnir/.venv/bin/fafnir ingest prices --symbols <SYM> --from <date> --to <date>`,
then re-check. Resolving two hundred gap flags individually is the wrong answer to
one missed night.

**Close with `dq recheck --check gap`** after any repair: the bar now exists, so
the recheck closes it durably. Where the session is a market fact the security will
never have a bar for, **accept** — a resolve is re-written by the next `dq run`.
Never a bare resolve.

---

## `sparse_coverage` — the security does not trade every session

Written by `fafnir dq run` (the gap check), severity `info`. One flag per security,
not per session: `record_key` is empty on purpose, so the condition dedupes for the
life of the security while the numbers move underneath it. They live in `detail`:
`bars`, `sessions`, `density`, `from`, `to`, `exchange`.

**What it means.** The security holds bars for under 80% of the sessions in its own
window. For a name whose median daily volume is single digits, an absent bar is a
fact about liquidity, not about the load — the vendor has no bar to return. This
flag exists so that fact is stated once rather than 3,000 times, and so the
securities whose missing days *do* look like real holes stay visible.

**Diagnose.** Read `detail` first; it is the whole picture. Then confirm the
absence is the vendor's, not ours:

```sql
-- Do the bars we do hold look like a thin name, or like a truncated load?
SELECT count(*) AS bars, count(*) FILTER (WHERE volume = 0) AS zero_volume,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY volume) AS median_volume
  FROM core.daily_price WHERE security_id = <id>;
```

A re-ingest of one thin window settles it: `ingest prices --symbols <SYM> --from
<d> --to <d>` returning ~0 `rows_inserted` means the bars are not there to fetch.

**Do not** resolve it as "backfilled" and do not bulk re-ingest the cohort on the
strength of the flag alone. Nothing has been repaired: the condition is still true
the next night, and closing it only frees the slot for `dq run` to rewrite.

**Escalate when** the density is high (near the 0.80 line) on a liquid name — that
is not thinness, and the per-session check was silenced for it. Read the two
constants in `src/fafnir/dq/checks.py` before arguing with the classification.

**Zero-volume share is not a liquidity measure.** FMP usually sends *no bar* on a
no-trade day rather than a zero-volume one, so the zero-volume count says almost
nothing. Use median volume with a date bound instead (`trade_date >= …`) — without
the bound the query walks every partition and times out.

**Close with:**

- **`accept`** — a thin name with scattered gaps, a fund's coverage gaps, or an old
  block of missing history. These are permanent and true; a resolve returns on the
  next `dq run`.
- **`accept`** — a dead Sharadar history (`source = 'sharadar'`, delisted): its bars
  are final, and the re-ingest above does not apply (FMP never feeds it). In bulk,
  by rule: see `sweep-policy.md`, "Dead Sharadar histories".
- **Leave open** — an **active** security with a gap of more than 180 days ending
  in the last 18 months, *especially* when the price level jumps across the gap.
  That is two issuers sharing one row: the security master let a new listing take
  over a row whose old issuer was never marked delisted. The row needs splitting,
  which is an operator decision and has no command. 79 securities in this state as
  of 2026-09-10. Do not "fix" it with `prices delete` unless the earlier segment is
  junk: it is often the only copy of a delisted company (see the survivorship check
  in `outlier-classification.md`). When a new listing's pre-launch segment *is*
  junk, deleting it (bars before the first real bar create no `gap` flags) lets
  `dq recheck --check sparse_coverage` close this flag — CAPA, PBL and HBTC on
  2026-09-15.
- **Leave open** — a heavily traded name near 0.80 by median volume since a recent
  date. Per the escalation above, the per-session check was silenced for a security
  that should have it.

## `outlier` — a close-to-close move of 50% or more

Written by `fafnir dq run` (`DEFAULT_OUTLIER_THRESHOLD = 0.5`).

**Diagnose.** The single most common cause is a **split**, and in the **raw**
series a split *is* a real jump (ADR 0001, ADR 0004) — so the flag is expected:

```sql
SELECT action_type, ex_date, split_numerator, split_denominator
  FROM core.corporate_action
 WHERE security_id = <id> AND ex_date BETWEEN <date> - 5 AND <date> + 5;
```

Then compare the two series over the date with `price_history`. **The decisive
test:** if the *raw* series jumps and the *adjusted* one does not, the action is
loaded and the factor is right. If **both** jump, the corporate action is missing.

**The check skips a split only on the flagged bar's exact date** (`ca.ex_date =
m.trade_date`). A split dated on a weekend, a holiday or a session with no stored
bar sits between two bars and is flagged every night while the adjusted series is
correct — so **accept** it, never resolve: a resolve is re-written by the next
`dq run`.

**Accept when** a real corporate event between the previous bar and this one
explains it, the adjusted series is smooth, **and the price levels are plausible**.
A vendor split row can be as fabricated as the history it matches: AKR's raw closes
reached 149,613,176 and HUN carried a "10:1 split" it never had, both with a smooth
adjusted series. Read `min_close`/`max_close` in `mart.v_security_price_coverage`
before accepting. `references/outlier-classification.md` has the full sort order.

**Repair when** both series jump: load the action, then re-adjust.

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir ingest actions --symbols <SYM>
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir adjust --symbol <SYM>
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir db refresh-marts
```

A genuine 50%+ move with no action (a biotech readout, a takeover collapse) is a
market fact — **accept**, and say in the note that no action exists and none
should. A resolve on a market fact is re-written the next night.

**The shapes worth knowing before you classify them at scale.** The check reads the
raw series and skips only a split's exact ex-date, so a split is not a generic
explanation. The queries and thresholds for each are in
`references/outlier-classification.md`:

- **A near-miss split** — one jump of the split's ratio on a session 1–5 days off
  the ex-date, and the price holds — is a *misdated* split, not a market move.
  Correct the ex-date with `fafnir actions redate <id> --ex-date <d>`; do not accept.
  **Before redating, compare the bar you are dating from across every run's
  payload** in `landing.fmp_raw`, not only the latest. FMP restates a new ticker's
  first bars on the post-split scale (×k the price, ÷k the volume), and a restated
  bar looks exactly like an earlier ex-date. On 2026-10-04 OPNW was redated
  09-29 → 09-25 off such a bar and had to be undone, though the as-traded 09-25 bar
  was in the payloads of earlier runs.
  **Two** jumps of the split's size a few days apart are not a misdate: the pre-split
  history is mis-adjusted, and moving the date fixes neither.
- **A split the feed never reported** — a clean ×k or ÷k jump (within 3%) that holds
  for 40 sessions, never reverts, with volume moving the opposite way by 2× or more
  and no action within ±120 days — is added with
  `fafnir actions add --symbol <SYM> --ex-date <d> --split N:D`. A ratio near an
  integer is not evidence on its own. Flat identical prices on heavy volume before
  the jump (EQC 1997: 0.9475 for days on millions of shares) mean the vendor stored
  older bars at the wrong scale — a split row would disguise that, so leave it open.
- **A one-tick move on a sub-dime price** (1/16 → 3/16, 0.01 → 0.03) is a market fact
  whose percentage is large only because the price is. Accept.
- **A duplicate split** — the same ratio twice, days apart, one at a pre-announced
  date — is removed with `fafnir actions delete <id>`. A bare SQL `DELETE` is
  re-inserted by the next load while the feed still carries the row.
- **Spike-and-revert clusters** are corrupt vendor history: two instruments mixed
  under one ticker. Pair each outlier with the next one on the same security
  (`lead(close)`, `lead(prev_close)`) and treat it as a spike when the next flag's
  `prev_close` equals this `close` and the price returns within 25%. That found
  about 10,000 flags across 516 securities (PRG, BXMT, PLA, REA). The pairing rule
  misses edges, so a leftover open flag is often **the other half of a pair already
  accepted** — find the partner in any state before treating it as new, and look at
  the window: most are two or three scales alternating day by day, not a clean block.
- **An isolated bad bar** can be re-fetched: the loader overwrites bars on
  re-ingest. **Probe first** (`source probe-prices --symbol <SYM> --date <d>`, read
  the whole output): when FMP has corrected it, re-fetch with `ingest prices
  --symbols <SYM> --from <d> --to <d>` and keep the session (GEVX, 251,705 → 20.57).
  When the vendor re-serves the same bad value, `fafnir prices delete --symbol <SYM>
  --date <d>` removes it and keeps it out; the session then reads as a gap, to
  accept. Scan the security's whole history for other bars far from their
  neighbours first, so one delete batch takes them all (RUSS: 11 zero-volume bars at
  ~5×, none after 2011).
- **A bar on a non-session day** (a weekend print of another instrument, a
  money-market fund's weekend NAV stored before the loader set these aside) is
  removed with `fafnir prices delete --symbol <SYM> --non-session`. Check the
  weekday distribution first: a history dated one day early (FVI, WLL — no Fridays,
  Sunday bars that are Monday sessions) would lose every Monday. And
  `ref.trading_calendar` marks MLK Day 1990–1997 closed although NYSE traded, so
  real bars on those dates look non-session.
- **The first bar of a new listing after years of silence** is usually a fund or SPAC
  that took a ticker whose earlier history FMP still serves under it. Earlier
  segments that are junk (sub-penny on zero volume, stray bars after delisting) can
  be deleted. **An earlier segment that is a real delisted company may be the only
  copy the warehouse has** (Sotheby's under BID, Thoratec under THOR): look for another
  row holding it by name and `core.symbol_xref` before deleting anything, and when
  there is none move the earlier history onto its own security with `security
  split-history` rather than deleting it — or, where the boundary is not clean enough
  for that, keep both histories and accept the boundary.
- **The newest bar of some ETFs** is provisional and FMP restates it the next night,
  sometimes from or to the wrong scale. Recheck the next day; never delete a newest
  bar on its first night.

Every one of these is `--dry-run` first, `--note` always, and recorded in
`ops.operator_override` (`fafnir override list`). A deleted bar survives only in its
override's `detail.row`: `override revoke` lifts the suppression but **does not write
the row back** — the next load, or an explicit `ingest prices`, does. A bar *edited* by
`prices shift|rescale` is the exception: revoking any override of that edit removes
the operator's bars and writes the vendor's back exactly as they stood, because the
edit replaced them rather than judging them worthless. Deletes need migration 0025
on the host and bar edits need 0026 — check `schema_state` before planning with them.

**Corporate actions behave the same way.** Revoking an `actions delete` override,
or the pair of overrides `actions redate` leaves, lifts the suppression and removes
the operator's row. It **does not bring the vendor's row back**. That happens only
if a later actions load re-reads the date: the calendar sweep's overlap window is
about a week, and the per-symbol rotation reaches each security once a month.

After a revoke, read `core.corporate_action` for the security. If the vendor's event
is missing and its date is confirmed, add it with `actions add`, as an operator
row that no feed overwrites. On 2026-10-06, undoing OPNW's redate left it with no
split at all.

**Close with:** `repair-then-recheck` for a missing or misdated action, a duplicate,
a re-fetchable or deleted bar (`dq recheck --check outlier` — a deleted bar, a
deleted previous bar, or a split on the flag's date closes it; run `db refresh-marts`
first and read the dry run's count against the flags you changed); `accept` for a
verified market fact, a split sitting between two stored bars, the unpaired half of
an accepted spike-and-revert, a two-issuer boundary whose earlier history must be
kept, or corrupt vendor history nobody will re-fetch. `prices shift|rescale` (then recheck) for real
prices stored at the wrong date or the wrong scale, including an off-scale era a
fabricated split makes look smooth — rescale the era and delete the split together.
**Leave open**, with the reason in the report, a scale you cannot prove, and
corruption that continues into the newest bars.

---

## `stale` — no recent bar for an actively-trading security

Written by `fafnir dq run` (freshness, against a venue's calendar).

**Diagnose.** Three causes, and they need opposite treatment:

```sql
SELECT s.primary_symbol, s.is_actively_trading, s.delisted_date, s.is_fund,
       (SELECT max(trade_date) FROM core.daily_price p
         WHERE p.security_id = s.security_id) AS last_bar,
       (SELECT last_loaded_date FROM ops.load_watermark w
         WHERE w.security_id = s.security_id AND w.endpoint LIKE '%price%'
         LIMIT 1) AS watermark
  FROM core.security s WHERE s.security_id = <id>;
```

- **Delisted but unmarked** → `sudo -u fafnir /opt/fafnir/.venv/bin/fafnir ingest delisted`, then
  resolve. The security stopped trading; the flag was right and is now explained.
- **A tracked fund** → NAV publication lags, and the delisted sweep will not
  retire it: the feed's own rows for funds carry a venue the sweep does not read
  (its recorded `MUTF` pseudo-venue is not what keeps it out). If the fund
  has actually closed: `sudo -u fafnir /opt/fafnir/.venv/bin/fafnir track rm <SYM> --closed <date>` —
  not a `dq resolve`, because `track rm` alone leaves it active and it will be
  flagged stale again every night.
- **A name stamped delisted, or turned inactive, that kept trading** → a venue
  transfer (ADR 0014). Not a `stale` flag (the check reads active securities only),
  but the same question; see `security_delisting_contradicted` below.
- **A live, liquid name** → **never resolve.** The loader is failing for it. Look
  at `ingestion_runs` and the watermark; a watermark stuck behind a quarantined
  bar is the usual cause, and the fix is the `price_*` playbook below.

**The threshold, and why one-session flags are not yours to close.** A security is
stale once it is missing `STALE_MIN_SESSIONS_BEHIND` (2) open sessions counting the
market's latest; a NAV-priced one gets `NAV_LAG_TRADING_DAYS` (1) more. FMP
publishes thin names' bars a day late often enough that a lower threshold turned
the vendor's lag into a queue. Anything that clears itself overnight is not a
defect: **recheck it the next day, do not accept it.**

**To test whether the screener still lists a name**, read `core.security.updated_at`
— the nightly security-master load stamps it. `landing.fmp_raw` does **not** keep
screener payloads, so there is nothing to join against there.

**Close with `dq recheck --check stale`.** The recheck is the exact negation of the
check and closes a flag whose security has taken a later bar, is no longer far
enough behind, or is no longer actively trading. Where the security really has
stopped (a closed fund) retire it properly first — `track rm <SYM> --closed <date>`
— and the recheck closes the flag on its own. **Accept** only a security that will
never take another bar and cannot be retired.

### A live name the vendor stopped serving

`fafnir source vendor-gaps` lists them: in the nightly universe, behind by two or
more sessions, the newest payload an open-ended request that returned nothing after
the last bar, 95% of the venue ahead, and not on the delisted feed. On 2026-10-14
that was ten names that traded every day (FPAS, XJUL, XAPR, XFEB, CMCI, VAIC, CHIP,
MYCP, VMSD, VAI). The nightly report has the section.

- **Report, don't close.** The security trades; the vendor does not serve it. Its
  `stale` flag is right and stays open. Never resolve or accept it, and never
  `track rm` or delist it on the strength of the vendor's silence.
- Confirm once with `source probe-prices --symbol <SYM> --date <latest>`: no bar in
  either feed is the vendor, not the load. A bar there is the loader's problem: see
  the watermark, above.
- The list is the evidence for an FMP ticket (an operator task). If the vendor
  catches up, the next load fills the bars and `dq recheck --check stale` closes the
  flags; if a label is wanted meanwhile, state it:
  `dq label add --security-id <id> --first <first missing> --kind feed_gap
  --verified -m "<the outside source that shows it trading>"`.

---

## The `price_*` family — what closing one actually does

**Read this before closing any `price_*` flag.** They behave unlike every other
check, in four ways:

- **Acceptance does not suppress them and recheck cannot reach them.** They are
  written through `add_dq_flag`, not `add_dq_flag_once`, because
  `count_price_quarantines` counts them to decide when a bar stops holding the
  watermark. Closing one is bookkeeping — say so in the note.
- **Re-flagging comes only from bars the loader re-reads**: the overlap window,
  a watermark hold, a backfill. A bar nothing re-reads never comes back.
- **Closing them does not release a watermark hold.** `count_price_quarantines`
  counts flags in *every* state, so a resolve does not shorten the hold.
- **Two recurring populations were loader defects, fixed in the 2026-09-10 DQ
  fixes.** Money-market funds' weekend zeros recurred every week until the loader
  learned to set aside bars dated on non-session days. Funds are stored
  `asset_type = 'equity'` with `is_fund`, so the NAV allowance keyed on
  `asset_type` alone never applied to any of them — 3,780 real NAV bars were
  rejected as cross-field violations. On a warehouse running code that predates
  those fixes, expect both to keep coming back.

### Quarantines from the Sharadar loader (#75)

`ingest sharadar-prices` quarantines through the same checks. Its flags carry
`detail.source = 'sharadar'` and the vendor's ticker in `record_key.symbol`. Three
things differ from FMP's:

- **A later run may have loaded the bar.** A quarantined date is not held, so every
  `sharadar-prices` run reads it again, and a fix to the loader can let it in. Run
  403 (2026-10-20) quarantined 146,710 bars as `price_cross_field_violation` that
  the rounding clamp (v1.8.1) loads, and their flags stayed open: nothing re-checks
  a `price_*` flag. The condition is gone when the security holds a bar on the
  date. Resolve those by rule, first: it is bookkeeping, and it is most of the
  queue.
- **Every re-run flags the rest again.** The loader writes through `add_dq_flag`,
  so a second `--backfill` run adds a second open flag for each bar it still
  refuses. Work them after the last backfill run. Several open flags on one
  security, check and date are one condition: close all but the newest by rule.
- **There is no other vendor to re-fetch from** on a dead history: FMP never feeds
  it. A bar Sharadar's own data fails validation on stays absent, and leaves a
  `gap` on its date that belongs to the quarantine. Decide it by its reason's
  entry below, and say in the note that a later backfill run flags it again.

Both cleanups take thousands of ids, past `dq resolve`'s command line, so they run
in batches, in operator-directed bulk mode (sweep-policy.md). Add the dry runs' totals
and compare them with the file's line count before the real run:

```bash
# Flags whose bar has since loaded.
psql "$FAFNIR_DSN" -Atc "SELECT f.dq_flag_id FROM ops.data_quality_flag f
 WHERE f.check_name LIKE 'price\_%' AND f.detail->>'source' = 'sharadar'
   AND f.resolved_at IS NULL AND f.accepted_at IS NULL
   AND EXISTS (SELECT 1 FROM core.daily_price p
                WHERE p.security_id = f.security_id
                  AND p.trade_date = (f.record_key->>'date')::date)" > /tmp/sharadar_loaded
# Older duplicates of one bar's flag.
psql "$FAFNIR_DSN" -Atc "SELECT dq_flag_id FROM (
  SELECT f.dq_flag_id, row_number() OVER (
           PARTITION BY f.security_id, f.check_name, f.record_key->>'date'
           ORDER BY f.dq_flag_id DESC) AS n
    FROM ops.data_quality_flag f
   WHERE f.check_name LIKE 'price\_%' AND f.detail->>'source' = 'sharadar'
     AND f.resolved_at IS NULL AND f.accepted_at IS NULL) d
 WHERE n > 1" > /tmp/sharadar_duplicates

wc -l < /tmp/sharadar_loaded                     # the prediction
xargs -a /tmp/sharadar_loaded -n 5000 fafnir dq resolve --by claude --by-rule \
  -m "Operator-directed <date>: the bar loaded on a later sharadar-prices run" \
  --dry-run | grep -o 'Dry run: [0-9,]*' | tr -d , | awk '{s += $3} END {print s}'
```

Then the same command without `--dry-run`. Run the loaded-bar cleanup before
building the duplicates file, since a flag can be in both.

---

## `price_missing_or_nonnumeric_ohlc` — OHLC absent or unparseable

Quarantined by the price loader; the bar is **not stored**.

**Diagnose.** Scope first, because the scope decides everything:

```sql
SELECT count(DISTINCT security_id) AS securities, count(*) AS flags
  FROM ops.data_quality_flag
 WHERE check_name = 'price_missing_or_nonnumeric_ohlc' AND resolved_at IS NULL;
```

**If every bar in a load quarantined**, FMP renamed the OHLC fields. The loader
accepts `open…close` and `adjOpen…adjClose`; a third spelling needs adding to
`_OHLC_ALIASES` in `src/fafnir/ingest/daily_price.py`. Confirm with
`landing_payload` — the payload shows the actual field names — then **write the
patch as a PR**, do not edit the deployed checkout (rule 7).

**Resolve when** it is a one-off vendor blank on a day the security genuinely did
not trade.

---

## `price_non_positive_price` — a zero or negative price

**Resolve when** a single halted session sent a zero, confirmed in
`landing_payload`. **Escalate** a run of them: a security quoted at zero for days
is either delisted (mark it) or the feed is broken for it.

---

## `price_price_out_of_range` — exceeds `NUMERIC(20,6)`

> The doubled prefix is real: `_reject_reason()` returns `price_out_of_range` and
> the caller prefixes `price_`. Match on the literal `price_price_out_of_range`.

**Never resolve routinely.** This is a real quote the column cannot hold. Report
it: the security cannot be represented at this scale, and excluding it is cheaper
than rewriting every `core.daily_price` partition.

---

## `price_subresolution_price` — below the quantize cliff

Money is `NUMERIC(20,6)` with `ROUND_HALF_UP`, so the cliff is **5e-7**. Same
verdict as above: **report, do not resolve.** The security is unrepresentable.

---

## `price_scale_collapse` — a real OHLC range flattened to one value

**Never resolve on a run of them.** The bar *stored*, so this is not a quarantine
— it is a measurement that the stored bar is wrong in a way nothing downstream can
detect: `open = high = low = close`, indistinguishable from a genuine no-trade
day. The flag's `detail` carries the source high/low that `core.daily_price` no
longer has.

A security quoted between roughly 5e-7 and 1.5e-6 produces these continuously.
**Returns and volatility computed over a run of them are fictional, not merely
wrong** — say that plainly when reporting. Resolving the flag does not fix the
bar; it deletes the only record of what was lost.

---

## `price_restatement_conflict` — the vendor restates a stored bar

Warn, **repair-first**. The nightly re-read of the last five sessions served a bar
that differs from the stored one by more than `[prices] restatement_max_ratio`
(close, 1.25) or `restatement_max_volume_ratio` (volume, 3), either way. The loader
kept the stored bar and wrote this, once per open `(symbol, date)`. It is not a
quarantine: it spends no watermark budget, and the watermark moved past the date.

`detail` has `stored` (OHLCV and the `ingestion_run_id` that stored it), `incoming`,
`close_ratio`, `volume_ratio`, `over` (which tripped) and `splits_near` (any split
on file within 30 days).

**Odd lots are no longer flagged** (G7, `[prices] restatement_min_volume`, 100). A
first print under 100 shares re-read as exactly 100 at the same OHLC was ~85 flags a
night until then; 353 were accepted by class on 2026-10-19 and 10-21. On a warehouse
running G7, any such flag predates it. A volume-only conflict that is still raised
has a side above 100: a real revision or a vendor error, judged like any other.

**Which side is right?** FMP's first print usually is. What it restated from
2026-09-16 to 10-14:
- **another instrument's level** (OUSL ~11 -> ~58, QCJL ~26 -> 0.07, KEO, MILK,
  XNDX): keep the stored bar. Probe; the series' outside history (issuer page)
  settles it;
- **a split's scale before the split** (GCDT x6, ZBAO x50, `splits_near` names it):
  keep the stored bar;
- **a correction of a wrong first print** (SKYD 10-02: a phantom 2:1 first print,
  correctly re-served at 9.5): the incoming side is right, and the guard blocked it.
  That is its documented cost.

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir source probe-prices --symbol <SYM> --date <d>
```

**Repair.** Stored side right: nothing to change; the vendor's wrong re-read is held
off every night. Incoming side right: re-fetch the date explicitly, which overwrites,
prints what it restated, and resolves this flag itself:

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir ingest prices --symbols <SYM> --from <d> --to <d>
```

Read the restated table it prints: ZBAO's re-fetch put the prices right and returned
volume x50 in the same breath. If neither side is right, `prices delete` the bar.

**Close with:** the explicit re-fetch (it resolves the conflict on every date it
wrote). Stored side right and the vendor still restates nightly: **accept**, with the
evidence (`--per-flag --label data_error --kind wrong_instrument` when it is another
instrument), so the nightly stops re-asking; the stored bar stays protected either
way. If the vendor has since gone back to serving the stored value (probe), resolve
with that in the note. Never resolve on the count.

---

## `price_split_scale_mismatch` — a new bar at a coming split's scale

Warn, a **quarantine** (repeats per re-read, holds the watermark). A bar with no
stored copy, dated within 30 days before a split on file, whose close is the last
stored close times the split's price factor (x50 for a 1:50) within 10%, on a
volume consistent with the vendor's adjusted bar. It was not stored. `detail` names
the split, the factor and which volume shape it matched.

**Repair-first.** Probe the date (`source probe-prices`): the split-adjusted feed
should equal the incoming bar. Then re-fetch once the vendor serves the raw bar, or
let the watermark budget release it and fill the gap by hand-checked re-fetch. If
the split on file is itself wrong (misdated, a phantom), fix the action (`actions
delete|redate`) and re-fetch. **Close with:** the re-fetch that stores the raw bar,
then resolve with the evidence. Never accept: it is a bar the warehouse does not hold.

---

## `split_duplicate_suspect` — one split on file twice

Warn, **repair-first**, written by `adjust`. Two split rows on one security with the
same ratio, no more than ten sessions apart (GLE 1:10 on 10-07 and 10-08; NXGL 1:20
on 10-09 and 10-12). The bars move once, and the adjusted series moves twice:
adjusted prices before the earlier date were x100 and x400. No SQL check saw them;
dorq's DQ701 did. `detail.splits` gives the close ratio across each ex-date against
the split factor, and `moves_on` the date the raw closes actually move.

**Repair.** Probe the split-adjusted feed on the bar before each ex-date: the
cumulative split it applies shows which one it knows.

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir source probe-prices --symbol <SYM> --date <bar before each ex-date>
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir actions list --symbol <SYM> --type split
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir actions delete <the other id> --dry-run \
    --by claude -m "duplicate of the <ratio> on <moves_on>: closes move once, on <d>; probe: ..."
```

**Close with:** the `actions delete` (factors recompute in the same transaction; the
next `adjust` writes no flag), then resolve with the delete's override id. Never
resolved by a run. The delete becomes a `split_deleted` label at the next export.

---

## `tracked_symbol_delisted` — a declared ticker over a delisted row

Warn. `ingest tracked` found a declaration whose vendor profile describes a
security fafnir holds as delisted (same CUSIP, or the same normalised name), and
minted nothing. Before v1.5.0 it minted a new row every night, which the delisting
sweep then stamped (BURU: 815532 on 10-13, 833074 on 10-14, beside 286436).
`detail.delisted_security_id` names the row.

**A repair either way:**
- **It relisted** (a real move back to a venue: the vendor's bars continue, an
  exchange notice says so) — `security venue-transfer <id>` reverses the delisting,
  after which the declaration finds a listed row and loads into it.
- **It is gone** (OTC, wound up, the declaration was a mistake) —
  `track rm <SYM>`.

**Close with:** the venue transfer or the `track rm`, then resolve with which and
why. `track add --allow-delisted` is not a fix: it declares a ticker that now
belongs to a different issuer, and `ingest tracked` still mints nothing over a
delisted row of the same identity.

---

## `split_invalid` / `dividend_invalid` — unusable action values

**Diagnose** against `landing_payload` for the actions endpoint. **Resolve** for
confirmed vendor junk on one action, with the payload quoted in the note.
**Escalate** if systematic — a whole load of invalid splits is a feed format
change.

---

## `dividend_exceeds_price` — dividend larger than the prior raw close

**Resolve when** it is a genuine special or liquidating distribution (these exist,
and are large). Verify against the vendor payload and the price around the
ex-date. **Escalate otherwise**: a dividend an order of magnitude over the price
is usually a units error (cents reported as dollars), and loading it will poison
the adjustment factor chain for the whole history.

---

## `dividend_no_prior_close` — ex-date with no bar to value against

Dividend factors are valued against the prior raw close, so with no prior bar the
factor cannot be computed.

**The factor these flags describe changes no stored price.** A skipped dividend
factor can only scale prices *before* the ex-date, and by construction none are
stored. So the ordinary case is real, permanent and harmless.

**Close with `accept`** when the ex-date genuinely precedes the security's first
bar. A resolve is re-written by the next `adjust` of that security.

**One exception, and it is the one worth finding.** A first bar about five years
before the security was minted means the history was **truncated by a first load
with no `--from`** — FMP's default window, not the security's real start. Keep
those open, re-backfill with an explicit `--from`, re-`adjust`, then
`dq recheck --check dividend_no_prior_close`. (Fixed in the loader as of the
2026-09-10 DQ fixes: a first load now starts at `calendar_start_year`. Histories
truncated before that deploy still need the re-backfill.)

**Repair when** it is mid-history: the real defect is a **price gap**, not the
dividend. Fix the gap first, then `fafnir adjust --symbol <SYM>`, then
`dq recheck`.

**A security with no bars at all can never be closed by the recheck**, because its
rule is "a prior raw close now exists". The usual case is a stray dividend on an
empty echo row: the calendar sweep attaching a renamed company's distribution to
the shell its old ticker left (GBF → AGGM, 2026-10-06). Delete the stray row with
`actions delete <id>`; its override keeps it out. Then **resolve the flag by id**:
with no dividend left, `adjust` has nothing to re-detect. Confirm first that the
renamed company still holds its own copy of the distribution.

---

## `corporate_action_drift` — the calendar sweep disagreed with the per-symbol feed

**Never resolve silently.** The data is *already repaired* — the reconciliation
fixed it. The flag is telling you the **market-wide sweep cannot be trusted for
that asset type**, which is a different and more important fact.

```sql
SELECT s.asset_type, s.is_fund, count(*) AS drifts
  FROM ops.data_quality_flag f JOIN core.security s USING (security_id)
 WHERE f.check_name = 'corporate_action_drift' AND f.resolved_at IS NULL
 GROUP BY 1, 2;
```

**Resolve only** with a note naming the asset type and what the sweep missed.
**Escalate** if the queue is non-empty after a full 30-night cycle: that is the
evidence the operator needs for the `actions_mode` decision, because the
alternative is a sweep that silently drops dividends. Read the mode from the
latest `corporate-actions` run's `params.mode` — do not trust `automations.md`.

**Read `detail` before deciding: the four kinds need different answers.**

- **`missing_from_calendar` / `amended` only** → already repaired by the
  reconciliation, and it will not recur for that event. **Resolve**, with the note
  naming what the sweep missed.
- **`redated`** → the reconciliation found the two feeds had dated one distribution
  differently and deleted the duplicate copy. Already repaired. **Resolve.**
- **`withdrawn_by_source`** → the feed no longer carries an event the warehouse
  holds, and the loader deliberately kept it. First check whether the feed simply
  moved it: look for a feed row within ±5 days at about the same amount. If there
  is one, it is a duplicate to delete. If there is not, **the warehouse is usually
  right and the feed glitched** (TLT, JEPQ and SKOR all had a real distribution
  vanish from a payload). Leave it open and say so. Only FMP's own rows are
  compared: an `operator` or `sharadar` row is never reported here.

**Never `accept` this check unless you mean the whole symbol.** Acceptance is keyed
on `record_key = {"symbol": …}`, so it hides *every* future drift for that symbol —
including the next real one.

---

## `adjustment_failed` — factors could not be computed

The security keeps its **previous** factors — none on a first backfill, stale
afterwards. So its adjusted series is silently wrong until this is fixed.

`detail` carries a raw Python exception string (`"<ExcType>: <message>"`). This is
the one DQ field not derived from readable market data, which is why it is kept
off the mart seam — treat its contents as a diagnostic for you, not as something
to quote outward verbatim.

**Repair:** `sudo -u fafnir /opt/fafnir/.venv/bin/fafnir adjust --symbol <SYM>`. **Resolve when** that
succeeds. **Escalate** past ~1% of the universe: that is systemic, and `adjust`
already exits non-zero for it.

---

## `adjustment_factor_extreme` — an implausible cumulative factor

**Resolve when** a real, verified large split explains it (deep histories with
repeated splits reach genuinely large cumulative factors).

**Escalate otherwise:** a bad corporate action is poisoning the factor chain, and
every adjusted price for that security is wrong. Check the action list before
resolving; do not resolve on the factor alone.

---

## `security_company_name_drift` — a ticker's company name changed materially

Advisory. The security master keys a listed security on `(source, symbol)` (0012),
assuming one issuer per ticker; this is the safety net under that assumption.

`detail` has the old name, the incoming name and the similarity score.

**Resolve when** the price history continues sensibly across the change — a
rebrand (Google → Alphabet) or a vendor switching to an abbreviation
(International Business Machines → IBM). **Say which in the note.**

**Escalate when** two different issuers really share the ticker: the identity
assumption is wrong for this universe, and the second issuer has been silently
*updating* the first.

---

## `security_duplicate_identity` — one ticker, two or more security rows

**Never resolve with `fafnir dq resolve`.** The flag is a measurement of the
master's shape: while the extra rows exist the next `dq run` re-detects it, and
closing it only hides a forked company.

A ticker names one issuer at a time. Two rows under one symbol means the identity
has forked: the bars sit on one `security_id` and the ticker resolves to another,
because every mint closes the previous period in `core.symbol_xref` and the newest
open period wins. Nothing downstream complains — `duk ls` asks for one security and
gets one. It is simply the wrong one, with no history.

`detail` carries `row_count`, `rows_without_bars` and `distinct_company_names`.
Those three separate the two causes:

- **`distinct_company_names = 1`, most rows without bars → re-minting.** The vendor
  is still listing a name the warehouse retired, and each load minted another row.
  This is a repair, not a judgement. Confirm with:

```sql
SELECT security_id, primary_symbol, company_name, delisted_date, first_seen_at,
       (SELECT count(*) FROM core.daily_price p WHERE p.security_id = s.security_id)
         AS bars
  FROM core.security s
 WHERE primary_symbol = '<SYM>' ORDER BY security_id;
```

  Keep the row with bars, delete the shells, re-point the xref period. The
  loader-side cause is fixed by `is_retired_listing` in
  `fafnir/ingest/security_master.py`; a warehouse still accumulating these is
  running code that predates it. A ticker a *rename* moved away from is covered
  too, by `repo.renamed_away_securities`, which weighs the echo against the
  renamed security's former name (`core.symbol_change.detail.old_company_name`),
  its current name and the rename feed's name. A rename applied before that was
  recorded has no former name on file, and a vendor spelling variant ("Mattr
  Corp. (MAPP)") matches none of them, so those echoes still re-mint -- until a
  `security merge` folds one back in, which records its name on the rename
  (`detail.echo_names`) and stops the next one.

- **Names differ → genuine ticker reuse.** A new issuer took a dead ticker, and
  two rows is *correct* (0009). Nothing to repair. Say so and leave it open, or
  escalate to have the check taught about this pair — do not close it as though
  the data were wrong.

**Close with `security merge <victim_id> <survivor_id>`, by id.** It folds the
duplicate onto the row holding the history and closes the flag itself — but only
for a ticker that is single afterwards, which is the correct behaviour. Two rules
that decide whether the merge is right:

- **Keep the oldest row.** It holds the ticker history back to 1900 and the
  identifiers; the re-minted row holds neither.
- **Read the `Would record ...` line before merging into a live renamed
  security.** When the victim holds a ticker the survivor was renamed away from,
  the dry run says the merge will record the victim's name on that rename, and the
  next load then declines the vendor's entry. No such line, and the tickers
  differ, means no applied rename links them: `is_retired_listing` would only see
  **delisted rows by normalised name** for the victim's ticker, so unless one
  remains under the victim's name, the next load re-mints it. Keep one retired row
  per ticker in that case.

Three shapes need more than the merge itself:

- **A SPAC merger the feed never sent as a ticker change** (COLA → SAIQ,
  2026-10-06). The old ticker is delisted, so `merge-rename` refuses ("not a listed
  security") and nothing retargets a delisted row. Merge the **old row into the live
  new one** (`security merge <old_id> <new_id>`; it takes security ids, victim
  first). Keeping the old row instead just gets the new ticker minted again.
  - The old row's ticker period and identifiers (the SPAC's CUSIP, ISIN and CIK)
    are deleted with it.
  - `--note` is stored only on the duplicate-identity flags the merge closes, and a
    two-ticker pair has none.
  - So write the lineage and identifiers into a note that persists, such as the
    overrides of the clearing step below.
- **The OHLC guard tripped by vendor restatement**: one-cent or sub-cent
  differences, with closes otherwise equal. Never `--force`. Instead:
  1. Confirm the survivor holds every one of those dates.
  2. Delete the disagreeing bars **on the victim** with `prices delete`.
  3. Re-run the dry run until it shows 0 disagreeing, then merge.
  4. `override revoke` the delete overrides the merge re-points onto the survivor.
     There they sit over the survivor's own stored bars, and would only stop the
     loader updating those sessions.
- **Victim flags whose twin on the survivor is already closed** move across
  **open**, because the merge drops a victim flag only against an *open* twin.
  Resolve each one by id afterwards, citing the twin's id and how it was closed
  (WKEY → WQEY, 2026-10-07: six copies of accepted outliers).

`security dedupe` cannot fold a ticker whose rows name two different companies,
which rules it out for most real cases; reach for `merge` by id instead.

**Escalate**, do not merge, when the names differ and it is genuine ticker reuse:
two rows is correct there and the merge would destroy an issuer.

---

## `security_missing_classification` — a listed security with no sector/industry

Informational, and repair-first. `sector` and `industry` ride in on the company
screener with every universe load, so a listed security without them means either
the vendor omitted the fields for that symbol or the write path dropped them.

**The count is the diagnostic.** One security is a vendor omission. The whole
active universe at once is a single write-path regression wearing thousands of
flags — in 2026-08 an `upsert_security` that assigned `sector_id` from `EXCLUDED`
without `COALESCE` blanked 26,393 rows over eight days, and every night reported
success. Resolving those individually would have been the worst possible response.

```sql
SELECT count(*) FILTER (WHERE sector_id IS NULL) AS unclassified,
       count(*) AS listed
  FROM core.security WHERE is_actively_trading AND delisted_date IS NULL;
```

**Repair** by running `fafnir ingest securities` on a build that carries the
screener classification through `upsert_security`, then `fafnir db refresh-marts`
so `mart.security_latest` — and therefore `screen_securities` — agrees.

**Resolve when** the universe load has run since and the security is still
unclassified: that is the vendor's gap, not the warehouse's. Say which in the note.

---

## `symbol_change_conflict` — two live claims on one ticker

**Never resolve with `fafnir dq resolve`.** The nightly sweep re-detects the
conflict and re-flags it, so a resolve changes nothing. The terminal state lives
in `core.symbol_change` (0018), and two commands write it — both of which close
the flag for you.

**Read `detail.reason` first.** Two different refusals raise this flag:

- **"... a rename does not change what an instrument is"** or **"... plus the
  designator letter ..."**: the sweep's instrument guard (ADR 0012) refused a
  rename that would move a share onto its own warrant, right or unit ticker, or
  back (`ABCD -> ABCDU`, `ZKP -> ZKPU`). That is the vendor shuffling one SPAC's
  tickers, never a rename. It cannot clear itself, and `merge-rename` refuses it
  too. Close it with `fafnir security dismiss-rename <OLD> <NEW> -m "<why>"`.
- **"new ticker already belongs to another listed security ..."**: two live
  claims on one ticker. Diagnose below.

**Diagnose** — the two cases look identical in `fafnir status` and need opposite
treatment:

```sql
SELECT f.detail->>'reason' AS reason,
       f.record_key->>'old_symbol' AS old_sym, f.record_key->>'new_symbol' AS new_sym,
       o.cusip AS old_cusip, n.cusip AS new_cusip, o.cik AS old_cik, n.cik AS new_cik,
       (SELECT max(trade_date) FROM core.daily_price WHERE security_id = o.security_id)
         AS old_last_bar
  FROM ops.data_quality_flag f
  LEFT JOIN core.security o ON o.primary_symbol = f.record_key->>'old_symbol'
  LEFT JOIN core.security n ON n.primary_symbol = f.record_key->>'new_symbol'
 WHERE f.check_name = 'symbol_change_conflict' AND f.resolved_at IS NULL;
```

- **CUSIP/ISIN agree → one company, two rows.** A real rename that a
  security-master load beat the rename sweep to. Merge:
  `merge-rename <OLD> <NEW> --dry-run` **always first**, then without, then
  `fafnir db refresh-marts`. It refuses unless the identifiers and the overlapping
  OHLC agree — **read the blockers, never `--force`** (rule 4).
- **Identifiers differ, or both still trading → not a rename.** A pre-launch
  ticker shuffle, or the same change emitted both ways. Neither can ever clear
  itself. `fafnir security dismiss-rename <A> <B> -m "<why>"`. The `-m` is
  required: it is a judgement about the *feed*.

If neither fits, re-run `fafnir ingest symbol-changes`, which retries every
non-terminal row.

**From Sharadar** (`detail.source = 'sharadar'`, #93): `ingest sharadar-universe`
resolved the rename through the security's Sharadar link and raises the same flag,
with the same record_key, so the commands above apply.
- **`detail.reason = ticker_mismatch:<T>`**: Sharadar says the linked security
  moved from OLD to NEW, but fafnir has it on `<T>`. Either the link is wrong
  (`vendor link show <T> --evaluate`, then `vendor link reject`), or fafnir missed
  a rename: compare `core.symbol_change` for `<T>`.
- **Closed by** the next `sharadar-universe` run, once the security is on the new
  ticker, as the FMP sweep closes its own.

**The sweep now closes this flag itself** when a retry applies the rename — in the
same commit as the rename, matched on the `(old, new)` record_key — and clears
leftovers from before that existed the next time it sees the terminal row. So a
conflict flag on an *already-applied* rename needs `ingest symbol-changes`, not an
operator. Before that fix, MAPP→MATR sat open for nine days after its retry
succeeded and no command could reach it. Dismissed and ignored rows are untouched.

---

## `tracked_symbol_unknown_to_source` — a declared symbol the vendor won't return

**Resolve when** the fund closed — but retire it properly first:
`sudo -u fafnir /opt/fafnir/.venv/bin/fafnir track rm <SYM> --closed <date>`. **Escalate** a typo in
`track add`; the operator added a symbol that does not exist, and only they can
say what they meant.

---

## `security_delisting_contradicted` — the vendor has bars after a delisting

Warn. FMP's delisted feed reported a delisting, and its price feed has bars
**after** the date, so the sweep did not stamp it (ADR 0014). The security is still
listed and loading. `detail` holds:

- `feed_venue`, the venue the feed says it left;
- `bars_after`, `first_after` and `last_after`;
- `median_volume_before_20` and `median_volume_after`.

The record key is `{"symbol", "delisted_date"}`: one feed record.

**Diagnose.** The bars cannot tell a move between listed venues from a move to OTC:
GRP-UN kept printing after it left the NYSE for OTCQX. Volume will not tell them
apart either: USAC traded 18% of its median on its first TXSE day. Look for a public
notice: the issuer's investor relations or press releases, or the new exchange's
listing alerts.

**Decide**, after showing the dry run and getting an explicit yes (standing rules 2
and 6):

```bash
# It moved to another listed venue (ET: NYSE -> TXSE on 2026-10-05):
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir security venue-transfer <id> --venue TXSE \
    --date <first session on the new venue> -m "<from>->TXSE <date> (<source>)" \
    --by claude --dry-run
# It left for OTC, or stopped trading:
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir security delist <id> --date <date> \
    -m "<why> (<source>)" --by claude --dry-run
```

Then `fafnir db refresh-marts`.

- **`venue-transfer`** accepts this flag, which is how the sweep declines the
  record from then on (`known_transfer`). It also reverses a stamped delisting,
  and records the venue as an override the nightly load keeps.
- **`delist`** stamps the delisting and resolves this flag.
- **Refused because another security holds the ticker**: fold that duplicate in
  with `security merge` first.
- **Neither is clear**: leave it open. The sweep declines the record every night
  while the flag is open (`pending_decision`), so the security keeps loading.

**Never a bare `dq resolve`.** It frees the slot, the sweep asks the vendor again,
and once the record is past its 60 days it is stamped by default.

**From Sharadar** (`detail.source = 'sharadar'`, #93): `ingest sharadar-universe`
found a `delisted` event the security's own stored bars contradict (`detail.reason`:
`traded_after` or `precedes_first_bar`, with `first_bar`/`last_bar`). It declines the
record the same way while the flag is open or accepted, so the same decisions
apply. Check the link first: a wrong link puts another issuer's delisting on this
security (`vendor link show <SYM> --evaluate`).

**Close with:** `security venue-transfer` (accepts it) or `security delist`
(resolves it).

---

## `security_inactive_but_trading` — the vendor turned a still-trading row inactive

Info. The universe load copied the vendor's `isActivelyTrading: false` onto a row
whose last bar is within 5 sessions. FMP did this to ET, and five others, the week
they moved to TXSE, while quoting their prices. The security stays in the nightly
price universe while it has a bar in the last 30 days, so nothing is frozen. This
flag is the early warning, often before the delisted feed reports a move.

**Diagnose.** Bars after the flag's `last_bar` mean it is still trading: look for a
public notice of a move. No bars for weeks means a wind-down the vendor reported
promptly.

**Close with:**

- a move: `security venue-transfer`, which resolves it and holds the row active
  against the next load;
- a wind-down: `dq resolve` with the evidence once the bars have stopped. The
  security drops out of the price universe 30 days after its last bar.

---

## `vendor_xref_unmatched` — no Sharadar identity for an FMP security

Warn; `info` for a mutual fund. `fafnir vendor link` (#62) found no Sharadar
permaticker this security can be linked to. The record key is `{"vendor":
"sharadar"}`, one flag per security. `detail.reason` says why, and
`detail.candidates` lists what was considered:

| Reason | Meaning | `candidates` |
|---|---|---|
| `no_candidate` | No Sharadar stock or fund shares a ticker with it over an overlapping span | `taken_key` (another security's link holds it), `rejected_key` (a person said no), `no_overlap_key` (the ticker matched, the dates did not) |
| `no_bars` | The security has no bars, so no span to compare: usually a re-mint shell | as above |
| `price_disagrees` | A candidate had 4+ sampled bars and they did not agree | `vendor_key`, `ticker`, `agreed` of `of`, and the `samples` (date, `fmp`, `sharadar`) |
| `insufficient_overlap` | No candidate had 4 sampled bars to compare | as above |
| `not_covered_asset_type` | A mutual fund (`asset_type = 'fund'`, or `is_fund` and not an ETF with no candidate): Sharadar does not price them | as for `no_candidate` |

**Diagnose.** The link's evidence is in the database, never in a post.
- **`taken_key`:** another security already holds the permaticker, which usually
  means a duplicate identity. Look for `security_duplicate_identity` on the same
  ticker.
- **`price_disagrees`:** the candidate's `samples` hold both closes. A constant
  ratio between them (2, 1/10, …) is a split on one side only; noise is a different
  issuer. `duk -S db ph <SYM>` against `duk -S sharadar ph <SYM>` shows the whole
  overlap.
- **`no_bars`:** a shell with no history. `security dedupe` folds those.
- **`not_covered_asset_type`:** expected. Nothing to do.

**Decide.** A link a person makes is #63's (`match_method = manual`); the engine
never moves one. Until #63 ships there is no command for it: report, do not write
links by hand.

**Close with:** the next `fafnir vendor link` that links it, which resolves the flag
itself. A `not_covered_asset_type` flag may be accepted (`dq accept`) with a note.

---

## `vendor_xref_ambiguous` — more than one Sharadar identity fits

Warn. `fafnir vendor link` could not choose. `detail.reason`:

- **`several_agree`:** two or more candidates agreed on price, and no identifier
  (CIK, CUSIP) singled one out. That means share classes, or a vendor duplicate.
  `candidates` lists them with their agreement.
- **`duplicate_of_linked`:** one permaticker won for several FMP securities, and
  `candidates[0].linked_security` is the one linked to it (the claimant live on the
  vendor's current ticker). This row is another `security_id` for the same issuer,
  almost always a rename the loader missed: the old ticker's row and the new
  one's, each holding the history FMP serves under both. Fold it into the linked
  security with `security merge <security_id> <linked_security>`, after a dry run
  and an explicit yes. The merge moves the link and the history the linked row
  lacks. It refuses when an identifier differs (a rename's new CUSIP) or the
  overlapping bars disagree, which it did for 306 of 319 in #64: propose `--force`
  only with the blockers read and the operator's yes, never as a batch.
- **`key_claimed_twice`:** as above, but no claimant is live on the current ticker
  (a chain of delisted rows, usually), so none was linked. `candidates` names the
  key and `also_claimed_by`. Fold the rows, then the next `vendor link` links the
  survivor.

**Close with:** the fold (the next `vendor link` resolves this flag, or links the
survivor), or a manual link (#63).

---

## `vendor_history_disagrees` — a linked security's history to correct

Warn. `fafnir vendor link` linked the security, and some of its sampled raw
closes disagree with Sharadar's `closeunadj`. Sharadar is the reliable vendor
(plan decision of 2026-10-20), so the disagreeing bars are what to correct. The
record key is `{"vendor": "sharadar", "vendor_key": ...}`. `detail` has the link's
`basis`, `agreed` of `of`, and the disagreeing `samples` (date, `fmp`, `sharadar`).
Vendor data: never posted.

- **`basis = price_history`** (4 of 5 agree): one old bar off, usually a split or a
  bad print on one side.
- **`basis = latest_price`:** only the latest sample agrees. A constant ratio
  before some date is FMP history back-adjusted and stored as raw. Noise is
  another instrument's prices under the ticker.
- **`basis = identifier`:** no sample agrees, and a shared CIK or CUSIP made the
  link.

**Diagnose.** `fafnir vendor link show <SYM> --evaluate` prints the link, its
basis and the samples. Before you trust a `latest_price` or `identifier` link,
compare the names: the confidence is low on purpose.

**Decide.** A wrong link is a `vendor link reject` (it closes the link): propose
it, never run it. A right link with a wrong history is `fafnir vendor correct`
(#75), which replaces the disagreeing bars under ADR 0015 and keeps each replaced
bar in `ops.vendor_correction`. It is a bulk write: propose the operator's run,
with `--dry-run` first; do not run it yourself.

**Close with:** `vendor correct`, which resolves the flag itself once every sampled
date agrees; or the rejection. A sampled date it did not correct is an
operator-edited bar or a bar Sharadar's own data fails validation on: report it.

---

## `sharadar_split_ratio_mismatch` — Sharadar's ratio and its ACTIONS disagree

Warn, once per security. `fafnir ingest sharadar-prices` (#75) recovers raw bars with
the product of the ACTIONS splits after each date. On some bars SEP's own
`closeunadj / close` disagreed with that product beyond the two closes' 3-dp
rounding, so those bars were recovered with the vendor's ratio instead and still
loaded. Record key `{"vendor": "sharadar", "vendor_key": K}`; `detail` has the bar
count and the first and last such dates.

**Diagnose.** A constant ratio over a date range that ends at a date is a split
ACTIONS lacks (or records on another date): the stored raw bars are right, and the
adjustment factors, built from ACTIONS (#73), are missing that split. Compare
`duk -S db ph <SYM>` across the range's end with the security's actions
(`fafnir actions list`).

**Decide.** A missing split is an `actions add` (operator row), proposed with the
evidence, then `fafnir adjust` for the security. A ratio that varies bar to bar is
the vendor's own noise: `dq accept` with the note.

**Close with:** the action added and the factors recomputed, or `dq accept`.

---

## `vendor_history_overlap` — one FMP row holds another issuer's era

Warn. `fafnir vendor overlap` (#68) found history a reused ticker glued onto an FMP
security. FMP files every issuer of a ticker on the row that holds it today, so BID
held Sotheby's 2003–2019 as well as its current issuer. The flag is on the FMP
security. Its record key is `{"vendor": "sharadar", "vendor_key": K, "kind": ...}`,
and `detail` has the range (`from`, `to`), its `bars`, and the security's own
`linked_key`:

| `kind` | Meaning | Extra `detail` |
|---|---|---|
| `dead_issuer` | A dead Sharadar issuer that #71 would mint (ADR 0016 §3) shares a ticker with this security, which holds bars inside its price span. #71 will not mint `K` while this is open | `agreed` of `of`, and the `samples` (date, `fmp`, `sharadar`) |
| `earlier_history` | The security is linked to `K`, and its bars start over 30 days before `K`'s first price (where that is after Sharadar's December 1997 start) | none |

**Diagnose.** Vendor data stays on the host; post counts only.
- **`dead_issuer` with agreeing samples:** this row holds the dead issuer's bars.
  - If the row is itself delisted and unlinked, it *is* the dead issuer: propose
    `vendor link accept --vendor-key K` (rule 12: propose, never run).
  - Otherwise propose the vendor-assisted path in
    [operations.md](../../../../doc/operations.md#separating-two-issuers-held-on-one-security):
    `vendor mint-delisted --permaticker K --allow-overlap`, then `security
    split-history … --into-security-id <minted row>`, each with `--dry-run` first.
- **`dead_issuer` with disagreeing samples:** the bars are another issuer's. Minting
  `K` with `--allow-overlap` moves nothing.
- **`earlier_history`:** compare the bars just before `to` with those after it
  (`duk -S db ph <SYM>`). A level jump where `K` begins is an earlier issuer: the
  same split, into a row minted for that issuer (if Sharadar knows it, it shows up
  as a `dead_issuer` flag too). A smooth history means FMP has more of this
  issuer than Sharadar does: `dq accept` with the evidence.

**Close with:** the repair, after which the next `vendor overlap` resolves the flag
(it resolves every open flag whose overlap is gone); or `dq accept` with a note.

---

## The `dorq_*` family — dorq's judgement of a bar

Written by `fafnir dq run --engine dorq` (or `both`) **only with `--no-shadow`**
(`[dq] dorq_shadow = false`). Until the shadow period meets the cutover criteria
(dorq plan §8), dorq writes to `<dorq_shadow_dir>/<as-of>.jsonl` instead
(`/var/lib/fafnir/dorq-shadow` on the host) and the queue carries none of these.
`doc/dorq.md` covers the engine.

A `dorq_*` flag is not a threshold crossing. dorq weighed the explanations of a
bar — a bad print, a scale error, an unreported split, a feed outage, a real move —
and `detail.p_error` is the probability that the bar is a **data error**. Read
`detail` before anything else:

| Field | What it tells you |
|---|---|
| `code`, `check` | dorq's check (`DQ201` bad-print …); `dorq explain <code>` prints its page |
| `p_error` | P(data error). `error` severity is ≥ 0.9, `warn` ≥ 0.6 |
| `hypotheses` | each explanation's posterior: the runner-up says what else it could be |
| `evidence` | the terms behind it (jump size, reversion, volume ratio, tick grid, peers) |
| `suggested_action` | the repair dorq proposes: a `kind` (`delete_bars`, `add_split`, `rescale`, `redate_split`, `split_history` …) and its fields. The table below maps each to a fafnir command |
| `provisional` | the newest bars: no "after" yet. The next night judges them again |
| `dorq.version`, `dorq.config_hash` | which binary and settings wrote it |

**Diagnose with the evidence, not the price chart alone.** Compare `p_error` with
the runner-up hypothesis. A `dorq_bad_print` at 0.62 whose runner-up is
`market_move` at 0.38 is a coin with a bias, not a verdict. Read the bars around it
with `price_history`.

**The map to repairs.** Each code has the repair the outlier classification found
by hand (`references/outlier-classification.md`, sections in brackets):

| Check | Repair | Close with |
|---|---|---|
| `dorq_bad_print`, `dorq_ohlc_close_mismatch` | `prices delete` after probing the vendor (§2f, §3) | the repair, then `dq recheck` |
| `dorq_unreported_split`, `dorq_cohort_move` | `actions add --split N:D` (§2d) | the repair, then `dq recheck` |
| `dorq_scale_shift` | `prices rescale` over the era (§2i) | **Never** — see below |
| `dorq_date_shift` | `prices shift` (§2i) | the repair, then `dq recheck` |
| `dorq_history_segment` | `security split` (§2h) | the repair, then `dq recheck` |
| `dorq_split_misdated` | `actions redate` (§2c) | the repair, then `dq recheck` |
| `dorq_split_without_jump`, `dorq_split_double_applied` | an action or bar repair | **Never** — see below |
| `dorq_split_ratio_mismatch`, `dorq_dividend_implausible` | correct the action on file | the repair, then `dq recheck` |
| `dorq_bad_context_row` | correct or delete the action on file (`actions delete`, then `actions add`). A skipped split can also surface as `dorq_unreported_split`: fix the row first | the repair, then `dq recheck` |
| `dorq_missing_run`, `dorq_cohort_gap` | `ingest prices --symbols … --from … --to …` | the repair, then `dq recheck` |
| `dorq_frequency_gap` | a thin series dorq reads as weekly or monthly, missing whole periods: re-fetch them with `ingest prices`. If the vendor has no bar, the security did not trade | the repair, then `dq recheck`; **accept** a period with no trading |
| `dorq_stale_feed` | as `stale`. fafnir leaves out stale-feed reports on securities not actively trading, by `stale`'s own rule, so every one here is on a security still trading | as `stale`. `dq recheck` closes it once the security stops trading |
| `dorq_repeated_price` | as `stale` | as `stale` |
| `dorq_zero_range_with_volume` | re-fetch the bar. Check first for a cash takeover pinning the price (below) | the repair, then `dq recheck`; **accept** a deal-price pin |
| `dorq_move_on_zero_volume` | probe the vendor (§3), then re-fetch the bar; `prices delete` if it re-serves an indicative quote | the repair, then `dq recheck` |
| `dorq_volume_scale_shift` | `prices rescale --factor 1 --volume-factor V` over the era (§2i), or re-fetch it | the repair, then `dq recheck` |
| `dorq_non_session_bar` | `prices delete --non-session`, after the weekday check under `outlier`: a history dated a day early wants `prices shift` (§2i) | the repair, then `dq recheck` |
| `dorq_precision_shift` | an era of computed, back-adjusted prices in the raw feed (WZRD, SMUP): `prices rescale` it to as-traded once an outside source confirms the scale (§2i) | the repair, then `dq recheck` |

NAV-priced funds never carry `dorq_zero_range_with_volume` or the volume checks:
the `nav_funds` profile in dorq's `priors/fafnir.toml` ignores them for those
series, because a fund strikes one price a day on no volume.

**Accept when** the evidence shows a real event dorq could not know about — a
halt, a tender at a fixed price — and say what it was. **Resolve** only a report
you judged wrong: the next run writes it again if its window still holds the bar,
and your note becomes a label for the next calibration (`dq export-labels`), so say
*why* it is not an error.

**Recheck** re-runs dorq over the full history of the securities with open flags
and closes each flag dorq no longer emits. When the binary has changed since the
flag was written, the note says so — a newer model, not a repair, may be what
closed it.

## `dorq_bad_print`, `dorq_unreported_split` — check for a cash dividend first

**On dorq ≤ 0.8.1** (`detail.dorq.version`), before any repair: is there a cash
dividend on file at the report's date (`fafnir actions list --symbol <SYM>`) that
explains the drop, `prev_close − dividend ≈ close`? dorq up to 0.8.1 ignores the
dividends on file, so a large distribution's ex-date drop reads as a bad print
(DQ201) or an unreported split (DQ203). That is about 140 a year, mostly December
fund distributions.

That is a known false positive. **Resolve** it, and put the dividend in the note:
`cash dividend 2.41 on file at the ex-date; prev_close 48.10 − 2.41 = close 45.69;
dorq ≤ 0.8.1 ignores dividends`. The note becomes a `market_fact` label for the
code (`dq export-labels`), which is what it is. Do not accept it: an accepted
`dorq_*` flag exports as a `data_error`. Do not delete the bar or add a split.
dorq's next release reads the dividend (doc/dorq.md, "Known false positives").

## `dorq_scale_shift` — an era stored at the wrong scale

**On dorq ≤ 0.8.1, check first for a split on file at that date whose ratio
matches the move**: `detail.hypotheses.explained_split` is present. dorq up to
0.8.1 reports such a split anyway when share volume does not shift with it,
about 100 a year. That is the known false positive, not an era at the wrong scale.
Report it with the split row (`fafnir actions list --symbol <SYM>`). **Don't
rescale**: the bars are right, and the split on file already explains them. It
stays Never-tier: the operator closes it.

Otherwise: never auto-resolve. A run of bars ×100, ×0.01 or ×1000 the level
either side, with no volume change to make it a split. The repair is `prices
rescale` over the **whole era** (outlier classification §2i). Resolving one flag
leaves every other bar of the era wrong, and the adjusted series built on it.
Report the era's bounds from `detail`, propose the rescale, and leave the flag to
`dq recheck` once the operator has run it.

## `dorq_split_without_jump` — a split on file the bars never show

Never auto-resolve. Either the split is not real (the vendor fabricated or
duplicated it; `actions delete`), or the bars already carry it (the history was
stored adjusted, and the factor applies it a second time). Both corrupt every
adjusted price before the ex-date, and telling them apart needs the pre-split
levels (§2b, the AKR/HUN pattern). Report both readings with the numbers; the
operator decides.

## `dorq_split_double_applied` — the bars apply one split twice

Never auto-resolve. The level moves by the split's ratio at the ex-date and again
a few sessions later. One of the two moves is a vendor restatement, and every
adjusted price before it is off by the ratio. The repair is a rescale of the bars
between the two moves, after probing the vendor's payloads for which one is the
restatement (§3). Report; do not close.

## `dorq_zero_range_with_volume` — a flat bar on ordinary volume

Open, high, low and close are one price, on at least the series' median traded
volume, among nearby bars that do have a range. dorq reads it as a lost range: a
close copied into the other three fields, or a price at a scale that rounds the
range away (`price_scale_collapse`'s pattern).

**Check for a cash takeover first.** In a security's final sessions, a flat bar
on heavy volume is usually the deal price pinning the stock, which is a market
fact. On the first shadow night (2026-10-09), 640 reports had ranged bars nearby.
27 of them fell within 7 days of the series' last bar, including 20 of the 117 on
securities trading 10,000 shares a day or more. The largest:

| Symbol | Session | Flat at | Volume | Delisted |
|---|---|---:|---:|---|
| HOLX (Hologic) | 2026-04-07 | 76.01 | 101,956,189 | 2026-04-08 |
| EXAS (Exact Sciences) | 2026-03-23 | 104.91 | 21,562,141 | 2026-03-24 |
| RNAM (Avidity) | 2026-02-26 | 71.34 | 16,835,884 | 2026-03-02 |

Check `delisted_date` and the last sessions' closes. A delisting within days of
the bar, with the closes before it at or near one price, is the pin. **Accept**
it, and name the deal in the note.

Otherwise re-fetch the bar (`ingest prices --symbols <SYM> --from <d> --to <d>`).
If the vendor serves the same flat bar on a price small enough for its range to
round away, it is `price_scale_collapse`'s case: report it, and do not resolve.

**Under dorq 0.8.0, most of these reports are something else.** 15,632 of the
first night's 16,272 were thin ETFs printing one 100-share lot a day, where a
single trade makes a flat bar. That is a dorq defect, to be fixed in dorq. Do not
work those one by one, and do not filter them in fafnir.

