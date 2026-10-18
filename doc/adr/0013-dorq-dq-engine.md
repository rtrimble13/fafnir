# ADR 0013: dorq as a second data-quality engine, behind a shadow period

- Status: Accepted
- Date: 2026-10-07
- Implemented by: `src/fafnir/dq/dorq.py`, `src/fafnir/dq/compare.py`,
  `src/fafnir/dq/labels.py`, `src/fafnir/dq/recheck.py` (`recheck_dorq`),
  `fafnir dq run --engine/--shadow`, `fafnir dq compare`, `fafnir dq export-labels`
- Related: [doc/dorq.md](../dorq.md); the dorq development plan, §7 (integration)
  and §8 (cutover criteria), in [rtrimble13/dorq](https://github.com/rtrimble13/dorq)

## Context

The SQL checks in `fafnir.dq.checks` answer threshold questions. Is this move larger
than 50%? Is this session missing a bar? That is the right shape for a set-based
check, and the wrong shape for the question an operator actually works the queue to
answer: **is this bar a data error?** The 2026-09-15 outlier session took 300 open
`outlier` flags and sorted them by hand into causes: splits on file, misdated splits,
splits the feed never reported, bad prints, eras stored at the wrong scale, new
listings carrying another security's history, and real moves. That sort is now
`references/outlier-classification.md`, ten sections of queries and traps. `gap`
needed `sparse_coverage` and a density threshold to stop flagging thin names per
session. `stale` needed a two-session threshold to stop flagging the vendor's
publishing lag.

dorq is a C++ linter built for exactly that question. For each suspicious bar it
weighs the explanations against each other, reports the probability that the bar
is an error with the evidence for it, and suggests a repair. It is deterministic:
the same bars, context and settings give byte-identical output on any thread count.
It runs a 150M-bar history in about a minute.

## Decision

1. **dorq is a second engine, not a replacement, until the evidence says
   otherwise.** `fafnir dq run --engine sql|dorq|both`, defaulting to `[dq] engine`
   and that to `sql`. A host without dorq installed runs exactly as before.
2. **Export, run, ingest, with nothing new in the schema.** The bars stream from
   `COPY` into dorq's stdin, so the full history never touches disk. The calendar,
   actions and security master go beside them as files. dorq's `fafnir` output is
   already shaped as `ops.data_quality_flag` rows. They are written by one
   set-based insert carrying the same two `NOT EXISTS` guards (open and accepted)
   as every SQL check, so dedupe, resolution and acceptance mean what they mean
   today. Check names are `dorq_<check>`. Runs are recorded in `ops.ingestion_run`
   (`source = 'dorq'`). The nightly window is advanced in `ops.load_watermark`.
3. **Shadow first.** `[dq] dorq_shadow` is true by default. dorq's rows then go to
   `var/dorq-shadow/<as-of>.jsonl`, not the queue. `fafnir dq compare` measures a
   shadow night against the SQL checks (overlap both ways) and against the labels
   (precision at warn and at error). The queue takes dorq's flags only when an
   operator sets `dorq_shadow = false`, after the criteria of dorq plan §8 are met
   over at least 20 sessions.
4. **Recheck by re-running.** A dorq flag has no predicate to negate, but the
   model is deterministic, so the negation is to run it again. `dq recheck` re-runs
   dorq over the full history of the securities with open dorq flags, and closes
   each flag that run no longer emits. Two kinds are excluded:
   - the NEVER_AUTO_RESOLVE three, which name a repair;
   - the cross-sectional checks, which a run over a subset of securities cannot
     reproduce.
5. **The warehouse's decisions are dorq's labels.** `fafnir dq export-labels`
   writes them, so dorq can be calibrated on this warehouse (`dorq calibrate`) and
   measured against it:
   - every operator repair (with the bars as they stood, for `--restore`);
   - every outlier and dorq flag closed by judgement.

## Consequences

- One more binary to deploy (`/opt/dorq`, doc/dorq.md), versioned separately. Its
  version and config hash ride on every flag (`detail.dorq`). `fafnir status`
  names the version, and `dq recheck` notes when a different version wrote the flag
  it closes.
- The shadow period is a cost in operator attention: a nightly compare to read. It
  buys the evidence that the plan's cutover gate asks for, rather than a
  switch-over on faith.
- At cutover (dorq plan DR-0707), `outlier`, `gap`/`sparse_coverage` and `stale`
  retire in favour of their dorq successors, and their open flags are migrated by
  recheck, not by bulk resolve. Until then both engines' flags can be open on the
  same bar. `dq compare` counts that overlap, and the playbooks say which to work.
