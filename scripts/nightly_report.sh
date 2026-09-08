#!/usr/bin/env bash
# nightly_report.sh -- summarise last night's automations and DQ queue with
# Claude, and email the result. Driven by fafnir-report.timer; runs as the
# agent user. See doc/nightly_report.md.
#
# Read-only with respect to the warehouse. The only things it writes are the
# two state files in STATE_DIR, and the mail it sends.
set -uo pipefail

FAFNIR_HOME="${FAFNIR_HOME:-/opt/fafnir}"
STATE_DIR="${FAFNIR_REPORT_STATE_DIR:-$HOME/fafnir-report}"
RECIPIENT="${FAFNIR_REPORT_TO:?set FAFNIR_REPORT_TO in the report env file}"
SENDER="${FAFNIR_REPORT_FROM:?set FAFNIR_REPORT_FROM in the report env file}"
WINDOW="${FAFNIR_REPORT_WINDOW:-18 hours ago}"

mkdir -p "$STATE_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FACTS="$WORK/facts.txt"
INPUT="$WORK/input.txt"
BODY="$WORK/body.txt"

# ---- 1. collect -----------------------------------------------------------
# Yesterday's evidence first, while last_facts.txt still holds it. Without a
# prior snapshot the model can only recite queue totals, and a total that is
# the same every morning is not news -- movement is. This is also why the
# collection is deterministic shell rather than the model going to look: the
# two nights are then comparable, and the cost is flat.
if [[ -f "$STATE_DIR/last_facts.txt" ]]; then
    cp "$STATE_DIR/last_facts.txt" "$WORK/prev.txt"
else
    echo "(no previous run on file -- this is the first report)" > "$WORK/prev.txt"
fi

"$FAFNIR_HOME/scripts/collect_facts.sh" "$WINDOW" > "$FACTS" 2>&1

{
    echo "########## PREVIOUS RUN (for computing movement) ##########"
    cat "$WORK/prev.txt"
    echo
    echo "########## TONIGHT (report on this) ##########"
    cat "$FACTS"
} > "$INPUT"

# ---- 2. summarise ---------------------------------------------------------
# `read -d ''` returns 1 at EOF even though it has read everything; the
# `|| true` keeps that from looking like a failure to a future `set -e`.
read -r -d '' PROMPT <<'PROMPT_END' || true
You are writing the morning operations email for the fafnir market-data
warehouse. Below is evidence collected from the host after last night's
automations: the previous run first, then tonight's.

Write a plain-text email body, at most 40 lines. Structure:

1. Line 1 must be exactly one word and nothing else: HEALTHY, DEGRADED or
   FAILED. Do not write a "Subject:" line -- the mailer builds its own from
   that first word. Then a blank line, then one sentence justifying it.
2. "What ran" -- each nightly unit (daily update, DQ sweep, dump, offsite
   backup), whether it succeeded, and how long it took. Note anything that
   did not run at all, and say when that is expected (reconcile is weekly).
3. "Data quality" -- the open DQ queue, error severity first. Lead with what
   MOVED against the previous run; raw totals alone are not news. price_*
   checks repeat per re-detection by design, so their flag counts are not
   counts of distinct problems -- report their security counts instead.
4. "Needs a human" -- a short bulleted list, or "nothing" if clean.

Report on tonight; use the previous block only to say what changed. No
preamble, no markdown headers or bold, no invented numbers. Where evidence is
missing or truncated, say so rather than guessing.
PROMPT_END

# --allowedTools is empty on purpose: everything the model needs is already in
# the prompt, so a headless run cannot stall waiting on a permission prompt.
#
# --max-turns is 3, not 1. At 1 this exits non-zero with "Reached max turns (1)"
# on a fraction of otherwise fine runs -- observed intermittently on identical
# input -- and each one costs a morning's summary, falling back to the raw
# evidence below. There is no tool loop to bound here (--allowedTools is empty),
# so the only thing a tight limit buys is that flake.
if ! claude -p "$PROMPT" \
        --allowedTools "" \
        --max-turns 3 \
        --effort medium \
        < "$INPUT" > "$BODY" 2>"$WORK/claude.err"; then
    # A failed summariser must not mean a silent morning: send the evidence raw
    # so the mail itself is the alert.
    {
        echo "UNSUMMARISED"
        echo
        echo "The summariser failed. Raw evidence follows."
        echo
        echo "--- claude stderr ---"
        cat "$WORK/claude.err"
        echo
        echo "--- evidence ---"
        cat "$FACTS"
    } > "$BODY"
fi

# ---- 3. send --------------------------------------------------------------
VERDICT=$(head -1 "$BODY" | tr -cd 'A-Za-z')
[[ -n "$VERDICT" ]] || VERDICT="UNKNOWN"

{
    printf 'From: %s\n' "$SENDER"
    printf 'To: %s\n' "$RECIPIENT"
    printf 'Subject: fafnir nightly %s -- %s\n' "$(date -u +%Y-%m-%d)" "$VERDICT"
    printf 'Content-Type: text/plain; charset=utf-8\n'
    printf '\n'
    cat "$BODY"
    printf '\n\n-- \nGenerated on %s. Evidence: %s/last_facts.txt\n' \
        "$(hostname)" "$STATE_DIR"
} | msmtp --read-recipients
SENT=$?

# Roll the baseline forward only on a successful send. If the mail did not go
# out, tonight's evidence must NOT become the "previous run" that tomorrow's
# movement is computed against -- otherwise a silent morning also loses the
# comparison that would have explained it.
if [[ $SENT -eq 0 ]]; then
    cp "$FACTS" "$STATE_DIR/last_facts.txt"
    cp "$BODY"  "$STATE_DIR/last_report.txt"
else
    echo "$(date -u +%FT%TZ) msmtp exited $SENT; baseline not advanced" >&2
fi
exit $SENT
