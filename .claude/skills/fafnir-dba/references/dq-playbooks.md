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

## `gap` — a calendar session with no bar

Written by `fafnir dq run`. A trading day in `ref.trading_calendar` for which the
security has no row in `core.daily_price`.

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
`sudo -u fafnir fafnir ingest prices --symbols <SYM> --from <date> --to <date>`,
then re-check. Resolving two hundred gap flags individually is the wrong answer to
one missed night.

---

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
sudo -u fafnir fafnir ingest actions --symbols <SYM>
sudo -u fafnir fafnir adjust --symbol <SYM>
sudo -u fafnir fafnir db refresh-marts
```

A genuine 50%+ move with no action (a biotech readout, a takeover collapse) is a
market fact — resolve, and say in the note that no action exists and none should.

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

- **Delisted but unmarked** → `sudo -u fafnir fafnir ingest delisted`, then
  resolve. The security stopped trading; the flag was right and is now explained.
- **A tracked fund** → NAV publication lags, and the `MUTF` pseudo-venue is
  outside `SCREENER_EXCHANGES` so the delisted sweep cannot reach it. If the fund
  has actually closed: `sudo -u fafnir fafnir track rm <SYM> --closed <date>` —
  not a `dq resolve`, because `track rm` alone leaves it active and it will be
  flagged stale again every night.
- **A live, liquid name** → **never resolve.** The loader is failing for it. Look
  at `ingestion_runs` and the watermark; a watermark stuck behind a quarantined
  bar is the usual cause, and the fix is the `price_*` playbook below.

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

**Resolve when** the ex-date genuinely precedes the security's first bar (a
recently-added security with a short history).

**Repair when** it is mid-history: the real defect is a **price gap**, not the
dividend. Fix the gap first, then `fafnir adjust --symbol <SYM>`, then resolve
both.

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
evidence *against* adopting `actions_mode = "auto"`, and it must reach the
operator, because the alternative is a sweep that silently drops dividends.

---

## `adjustment_failed` — factors could not be computed

The security keeps its **previous** factors — none on a first backfill, stale
afterwards. So its adjusted series is silently wrong until this is fixed.

`detail` carries a raw Python exception string (`"<ExcType>: <message>"`). This is
the one DQ field not derived from readable market data, which is why it is kept
off the mart seam — treat its contents as a diagnostic for you, not as something
to quote outward verbatim.

**Repair:** `sudo -u fafnir fafnir adjust --symbol <SYM>`. **Resolve when** that
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

---

## `tracked_symbol_unknown_to_source` — a declared symbol the vendor won't return

**Resolve when** the fund closed — but retire it properly first:
`sudo -u fafnir fafnir track rm <SYM> --closed <date>`. **Escalate** a typo in
`track add`; the operator added a symbol that does not exist, and only they can
say what they meant.
