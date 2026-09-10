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
  the same constants, so what it closes cannot come back. Only six checks support
  it; a check that is not listed cannot be rechecked at all.
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
| `price_cross_field_violation`, `price_non_positive_price`, `price_subresolution_price`, `price_price_out_of_range`, `price_missing_or_nonnumeric_ohlc` | price loader | `add_dq_flag` (**no dedupe**) | *every* re-read of that bar — overlap window, watermark holds, backfills | **no** | no |
| `security_company_name_drift` | security master | `add_dq_flag_once` | not re-written: the upsert stores the new name | yes — **for the whole ticker** | no |
| `corporate_action_drift` | action reconciliation | `add_dq_flag_once` | the next reconciliation of that symbol, if the drift persists | yes — **for the whole symbol** | no |
| `symbol_change_conflict` | symbol-change sweep | `add_dq_flag_once` | no longer re-detected once the row is terminal; the sweep closes it itself when a retry applies the rename | yes | no |
| `split_invalid`, `dividend_invalid` | action loader | `add_dq_flag_once` | the next load of that action | yes | no |
| `tracked_symbol_unknown_to_source` | `ingest tracked` | `add_dq_flag_once` | the next `ingest tracked`, if the vendor still won't serve it | yes | no |

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
- **Leave open** — an **active** security with a gap of more than 180 days ending
  in the last 18 months, *especially* when the price level jumps across the gap.
  That is two issuers sharing one row: the security master let a new listing take
  over a row whose old issuer was never marked delisted. The row needs splitting,
  which is an operator decision and has no command. 79 securities in this state as
  of 2026-09-10.
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
loaded and the factor is right — resolve. If **both** jump, the corporate action
is missing.

**Resolve when** a real corporate event on/near the date explains it and the
adjusted series is smooth.

**Repair when** both series jump: load the action, then re-adjust.

```bash
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir ingest actions --symbols <SYM>
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir adjust --symbol <SYM>
sudo -u fafnir /opt/fafnir/.venv/bin/fafnir db refresh-marts
```

A genuine 50%+ move with no action (a biotech readout, a takeover collapse) is a
market fact — **accept**, and say in the note that no action exists and none
should. A resolve on a market fact is re-written the next night.

**Three shapes worth knowing before you classify one at scale.** The check reads
the raw series and already skips a split's exact ex-date, so a split is not a
generic explanation:

- **A near-miss split** — a matching ratio 1–5 days off the ex-date — is a
  *misdated* split, not a market move. Correct the ex-date; do not accept.
- **Spike-and-revert clusters** are corrupt vendor history: two instruments mixed
  under one ticker. Pair each outlier with the next one on the same security
  (`lead(close)`, `lead(prev_close)`) and treat it as a spike when the next flag's
  `prev_close` equals this `close` and the price returns within 25%. That found
  about 10,000 flags across 516 securities (PRG, BXMT, PLA, REA).
- **An isolated bad bar** can be re-fetched: the loader overwrites bars on
  re-ingest. Needs the FMP key — check it before planning the repair.

**Close with:** `repair-then-recheck` for a missing action or a re-fetchable bar
(`dq recheck --check outlier`); `accept` for a verified market fact or corrupt
vendor history nobody will re-fetch.

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
- **A tracked fund** → NAV publication lags, and the `MUTF` pseudo-venue is
  outside `SCREENER_EXCHANGES` so the delisted sweep cannot reach it. If the fund
  has actually closed: `sudo -u fafnir /opt/fafnir/.venv/bin/fafnir track rm <SYM> --closed <date>` —
  not a `dq resolve`, because `track rm` alone leaves it active and it will be
  flagged stale again every night.
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
  vanish from a payload). Leave it open and say so.

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
  running code that predates it.

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
- **Check every victim's company name against the rows you keep.**
  `is_retired_listing` matches echoes against **delisted rows by normalised name**,
  so folding every echo into a live *renamed* security invites the next load to
  re-mint it. Keep one retired row per ticker.

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

**Diagnose** — the two cases look identical in `fafnir status` and need opposite
treatment:

```sql
SELECT f.record_key->>'old_symbol' AS old_sym, f.record_key->>'new_symbol' AS new_sym,
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
