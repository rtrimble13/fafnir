# Plan: fafnir-dba skill enhancements from the 2026-09-10 DQ session

- Status: **applied** — P1–P8 are in `.claude/skills/fafnir-dba/` as of the DQ
  triage fixes PR. The "MCP-tool and CLI enhancements" section at the end is not
  done and is not scheduled.
- Scope: what the on-host DBA agent got wrong, or had to derive from source,
  during the 2026-09-10 queue sweep, and the skill changes that follow.
- Touches: `.claude/skills/fafnir-dba/SKILL.md` and its five reference files.
- Related: [ADR 0010](../adr/0010-on-host-operations-agent.md) (why the agent
  exists), [doc/plans/db-operations-agent.md](db-operations-agent.md) (how it was
  built), and the nine loader/check/CLI fixes the same session produced.

## How this document was reviewed

It is the agent's own account of its session, kept as written. Its factual claims
were checked against the code before the recommendations were applied, and three
things are worth recording:

- **Verified against the source.** The durability matrix (P1) is accurate: the
  writer, the write path and the recheck support of every check match
  `dq/checks.py`, `dq/recheck.py`, `ingest/*.py` and `RECHECKABLE`. So is the
  acceptance trap it names — `corporate_action_drift` and
  `security_company_name_drift` both key `record_key` on `{"symbol": …}`, so an
  acceptance really does cover the whole symbol. So is the `price_*` exception:
  those go through `add_dq_flag`, which does not consult `accepted_at`.
- **Verified against the source (P7).** `landing.fmp_raw` genuinely does not hold
  the screener or delisted payloads: `ingest/security_master.py` and
  `ingest/delisted.py` are the only two loaders that never call `land_payload`.
  `automations.md` did say `symbol` mode, and did list `journalctl` as freely
  readable.
- **One overstatement, corrected on the way in.** P7 asks for
  `mart.v_security_price_coverage` to be added to `schema-map.md` as "the cheap
  coverage source". It was already there, and already used in that file's own
  example query. The skill change made instead was to say *why* to reach for it —
  `core.daily_price` is partitioned, so a per-security probe times out — which is
  the fact the session was actually missing.

The recommendations are recorded below as the agent wrote them.

---

## What the session did

- **Result:** the open queue went from **102,915 to about 3,100** flags. About 99,800 were closed, by the method that fit each case:
  - `dq recheck` where the condition was already gone;
  - `dq accept` where it's real and permanent;
  - `security merge` for 153 re-minted duplicate rows, which closed 57 duplicate-identity flags;
  - plain resolve only where the writer won't re-detect the condition.
- **Verification:** every batch had its own dry run first and was checked in SQL afterwards.
- **Left open on purpose:** about 3,100 flags, each pointing at a real defect or waiting on the FMP key or an operator.
- **Code:** eight defects were found and patched in a PR branch.

The skill got the big things right: the two-numbers triage (`cohort_size`, `prior_resolutions`), "facts from SQL, effects from the CLI", and dry runs before any change. Where it fell short, it was mostly because it predates `dq accept`, `dq recheck` and `security merge`. The rest came from host facts it doesn't record, and from failure patterns it had no recipe for.

---

## What cost time or caused mistakes

| # | What happened | Cost | Root cause in the skill |
|---|---|---|---|
| 1 | `sudo -u fafnir fafnir …` was refused. I told the user the merge was blocked by policy, but it was only a path mismatch | 3 exchanges, and a wrong claim | The skill says `sudo -u fafnir fafnir` everywhere. The sudoers rule allows only `/opt/fafnir/.venv/bin/fafnir` |
| 2 | Both ingest repairs (KRSA's bad bar, the missing-split test) failed with "No FMP API key" | A whole repair class discovered blocked only at run time | No check before planning. The skill assumes the repair tier can always run |
| 3 | `dq resolve … \| head -2` rolled back 81 resolves after printing "Resolved" | One false success report; caught only by checking in SQL | No rule to verify effects in SQL, and no warning that the CLI commits after it prints |
| 4 | `sql_read` statement timeouts, about 6 times | Retries and reworked queries | No guidance on the partitioned price table: per-security probes hit every partition. `mart.v_security_price_coverage` was the fast path |
| 5 | Joined against landing for screener and delisted payloads, which landing doesn't store, and got "173/173 off-screener" | A bogus finding, caught only because 100% looked wrong | `schema-map.md` doesn't say which endpoints landing keeps |
| 6 | Two duplication hypotheses (TLT "double-counted", 127 exact-amount pairs) were wrong | Retractions | No "confirm against the rows before claiming" rule for hypotheses |
| 7 | I read the prior `claude` bulk closures as a policy violation; the user had approved them | Friction | No way to record operator-directed bulk actions so later sessions read them correctly |
| 8 | The skill's "resolve when…" advice for gap, outlier, stale, sparse, dividend_* and so on would have been re-written by the next nightly | The core of the task, re-derived from source code | The playbooks predate `dq accept` and `dq recheck`. Nothing says which writer re-detects what |
| 9 | Sweep caps (25 per batch, 4 batches) and the never-close checks clashed with the operator's explicit "clear it all" | Improvised rules for when the operator takes over | No operator-directed bulk mode |
| 10 | `automations.md` says actions ship in `symbol` mode; production has been `auto` since 09-01 | Nearly missed the most important drift finding | Stale doc, and no instruction to read the mode from `ingestion_runs.params` |
| 11 | `journalctl` needs the `adm` or `systemd-journal` group, which `claude` isn't in | A dead end | `automations.md` lists journalctl as freely readable |
| 12 | Large id sets (up to 39k ids, 280 KB) came back through `sql_read` and had to be parsed from saved files | A lot of mechanical plumbing | No tool for "flag ids to a file". The CLI accepts ids but not `--ids-file` |

---

## Recommended skill changes, in priority order

### P1. Add a durability matrix, and rewrite every playbook's "resolve when" as "close with"

This was the most valuable thing I had to derive from source. Put it at the top of `dq-playbooks.md`:

| Check | Written by | Written through | Resolve re-written by | `dq accept` suppresses? | `dq recheck`? |
|---|---|---|---|---|---|
| gap, sparse_coverage, outlier, stale, security_duplicate_identity, security_missing_classification | nightly `dq run` | `dq.checks` (two guard clauses) | the next `dq run` if the condition still holds | yes | yes (not duplicate_identity) |
| dividend_no_prior_close, dividend_exceeds_price, adjustment_factor_extreme, adjustment_failed | `adjust` | `add_dq_flag_once` | the next `adjust` of that security | yes | dividend_no_prior_close only |
| price_scale_collapse | price loader | `add_dq_flag_once` | a re-read of that bar | yes | no |
| price_cross_field / non_positive / subresolution / out_of_range | price loader | `add_dq_flag` (no dedupe) | every re-read of that bar (overlap window, holds, backfills) | **no** | no |
| security_company_name_drift | security master | `add_dq_flag_once` | not re-written: the upsert stores the new name. **Unless** the name wasn't stored (MATR, PAAI) | yes (whole ticker) | no |
| corporate_action_drift | action reconciliation | `add_dq_flag_once` | the next reconciliation of that symbol if withdrawn events persist | yes (**whole symbol**) | no |
| symbol_change_conflict | symbol-change sweep | `add_dq_flag_once` | no longer re-detected once the row is terminal | yes | no |

Then give each playbook a **Close with** line: `recheck`, `accept`, `repair-then-recheck`, `merge`, or `leave open`, with the reason.

### P2. Add an environment check to `SKILL.md`

Run these before planning any triage:

```bash
sudo -n -l                                  # what the claude user may run; expect /opt/fafnir/.venv/bin/fafnir
F=/opt/fafnir/.venv/bin/fafnir              # never `sudo -u fafnir fafnir` (bare name is refused)
sudo -u fafnir $F dq list | head -3         # read-only; confirms the DB config resolves
sudo -u fafnir $F source probe-prices --symbol AAPL --date 1990-01-02   # 3 requests; fails fast if the FMP key is absent
```

- **If the FMP key is absent:** every repair needing a fresh vendor fetch is blocked. Say so in the first report, not when a repair fails.
- **Also check the actions mode:** read `params.mode` on the latest `corporate-actions` run. Don't trust `automations.md`.

### P3. Add two standing rules

- **Rule 10, verify every effect in SQL:** a CLI "Resolved N" / "Accepted N" line is not evidence. Re-query state, counts and note text before reporting.
- **Rule 11, never pipe a mutating command through `head` or anything else that closes early:** redirect to a file, then `tail`. Until the PR's commit-before-print fix is deployed, `dq resolve` prints before it commits.

### P4. Add an operator-directed bulk mode to `sweep-policy.md`

**What changes** when the operator explicitly says to clear a check or the whole queue:
- the 25-per-batch and 4-batch caps no longer apply;
- never-close checks may be **accepted, not resolved**, so the record is kept.

**What stays the same:**
- each batch is one cause with one note, and the note starts `Operator-directed <date>` / `Accepted at operator direction <date>` plus the evidence;
- each batch gets its own dry run, and nothing runs for real without an explicit yes;
- every effect is checked in SQL afterwards;
- flags that point at a repairable defect stay open, and the report says why.

That wording convention also answers issue 7: later sessions can read the history for what it is.

### P5. Add a "classify a systemic check" recipe section

The existing stop rule ("over 1% of the universe means systemic") says when to stop but not what to do next. The pattern that worked on every large check:
1. **Classify with aggregate SQL**, from the flag table alone where possible.
2. **Partition** the flags into batches, returning ids.
3. **Dry-run** each batch.
4. **One approval** for the whole plan.
5. **Run** the batches.
6. **Check in SQL.**

Include the recipes that actually worked:
- **Spike-and-revert pairs, from the flag table only:** pair each outlier with the next outlier on the same security (`lead(close)`, `lead(prev_close)`) and treat it as a spike when the next flag's `prev_close` equals this `close` and it returns within 25%. This found vendor histories that mix two instruments under one ticker (PRG, BXMT, PLA, REA): about 10,000 flags on 516 securities.
- **Split-like jumps:** the close-to-prev ratio is within 3% of ×k or ÷k, with k ≥ 3.
- **Where a gap sits, and the price across it:** one pass with `lead(trade_date)` and `lead(close)` over `WHERE security_id IN (…)`. That separates thin trading from a history block, and a block ending recently with a price-level jump from two issuers on one row (79 found).
- **Non-session dates:** `NOT EXISTS (SELECT 1 FROM ref.trading_calendar … is_open)`.
- **Median volume with a date bound** (`trade_date >= …`), so partitions are skipped. Without the bound it times out.

### P6. Update the per-check playbooks with what this session learned

- **sparse_coverage** (currently "not yet placed in a tier"):
  - Accept thin names with scattered gaps, fund coverage gaps and old blocks.
  - **Keep open** any active security with a gap of more than 180 days ending in the last 18 months, especially when the price jumps across the gap. That's the two-issuers-on-one-row pattern, and the row needs splitting.
  - **Keep open** heavily traded names near 0.80, using median volume since a recent date. The zero-volume share is **not** a liquidity measure, because FMP usually sends no bar on a no-trade day.
- **outlier:**
  - The check reads the raw series and already skips a split's exact ex-date.
  - A near-miss split (±1–5 days with a matching ratio) is a misdated split.
  - Spike-and-revert clusters are corrupt vendor history.
  - The loader overwrites bars on re-ingest, so an isolated bad bar can be re-fetched (needs the key).
- **corporate_action_drift:**
  - Resolve drift that's only missing or amended events: it's already repaired and won't recur.
  - For **withdrawn** events, first check whether the feed moved the event to another date (look for a feed row within ±5 days at about the same amount). If so, it's a duplicate to delete. If not, the warehouse is often right (TLT, JEPQ, SKOR) and the feed glitched.
  - Never accept drift unless you mean it: acceptance is keyed on the symbol, so it hides *all* future drift for that symbol.
- **dividend_no_prior_close:**
  - The skipped factor can only scale prices before the ex-date, and none are stored, so these change no adjusted price. Accept them.
  - Exception: a first bar about five years before the mint date means the history was truncated by a first load without `--from`. Keep those open and re-backfill.
- **stale:**
  - FMP publishes thin names' bars a day late, so a one-session lag creates hundreds of flags a night that clear themselves. Recheck the next day; don't accept them.
  - Use `core.security.updated_at` from the nightly load to test whether the screener still lists a name. Landing doesn't keep screener payloads.
- **security_duplicate_identity:**
  - Use `security merge <victim> <survivor>` by id. Keep the oldest row: it holds the ticker history back to 1900 and the identifiers.
  - Check every victim's name against the rows you retain: `is_retired_listing` matches echoes against **delisted rows by normalised name**, so folding every echo into a live renamed security invites re-minting. Keep one retired row per ticker.
  - `security dedupe` can't fold a ticker whose rows name two companies, which rules it out for most real cases.
- **price_\*:**
  - Closing these doesn't reset watermark holds; `count_price_quarantines` counts every state.
  - Re-flagging comes only from bars the loader re-reads.
  - Money-market funds' weekend zeros recur every week until the loader skips non-session dates (PR fix 1).
  - Funds are stored `asset_type = 'equity'` with `is_fund`, so NAV handling never applies (PR fix 2).

### P7. Refresh `schema-map.md` and `automations.md` facts

- **What landing keeps:** `landing.fmp_raw` holds price, dividends, splits, both calendars and symbol-change payloads. It does **not** hold `stock-list` (the screener) or `delisted-companies`.
- **The cheap coverage source:** `mart.v_security_price_coverage` gives first and last trade date, bar count and zero-volume bars. Use it instead of per-security probes of `core.daily_price`.
- **Actions mode:** `auto` since 2026-09-01. `corporate_action_drift` is the 30-night evaluation, and its drift rate (about 2% per nightly slice) is operator-facing.
- **Access limits:** `journalctl` for `fafnir-daily` needs group membership the agent doesn't have.
- **Backfill defaults:** a first load with no `--from` gets FMP's roughly 5-year default window (until PR fix 3 is deployed).

### P8. Add reporting discipline

- **Check suspicious results against the rows first,** before stating them. A 100% hit rate, or a "duplicate" found by pattern, gets a spot check.
- **When reporting a batch, state what will still come back**: `price_*` re-reads, weekly money-market zeros, new nightly stale flags. Don't leave the user to discover it.

---

## Suggested MCP-tool and CLI enhancements (for the server owner, not the skill)

1. **`dq_triage` without a check filter times out.** Add aggregate-first output and cursor pagination.
2. **An id-export tool,** for example `dq_ids(check_name, where…) → file`, or let `sql_read` write results to a file. Large id sets currently round-trip through 280 KB tool results.
3. **`fafnir dq accept|resolve --ids-file PATH`.** Passing 39,000 ids on the command line works only by luck of `ARG_MAX`.
4. **`landing_payload` by run id and date window.** Today it returns only the newest payload, so older vendor data (the KRSA splits) can't be inspected.
5. **A `flag_lineage(check_name)` tool** returning the durability-matrix row: the writer, which function it writes through, and what re-triggers it.
6. **Add `--dry-run` to `ingest prices --symbols … --from … --to …`,** at least to show the payload that would be upserted.
