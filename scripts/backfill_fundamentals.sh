#!/usr/bin/env bash
# backfill_fundamentals.sh -- (re)load SF1's statements from the ledger's SF1 file.
#
# The fundamentals backfill (#92) as one command, for re-runs (#106): after
# `scripts/reset_data.sh --scope fundamentals`, after a loader fix, or for a
# security linked after the backfill (--symbols). The first backfill on a host
# follows doc/backfill.md step by step, with its dump, probe and EDGAR checks;
# this is that runbook's steps 3, 6, 7 and 9 back to back.
#
# Resumable: the loader commits per vendor key and a re-run writes nothing it
# already holds, so re-running after an interruption picks up where it stopped.
# Resume with --keep-file, so it reads the file it started on.
#
# A whole run (no --symbols) starts from a fresh SF1 file. The nightly then takes
# over from that file and counts the deltas recorded before it as applied, since
# the file holds them. A whole run from an old file would put the values of the
# deltas applied since back to the old ones, so --keep-file is for a file fetched
# today.
#
# Run it in the day, not across the night's timer (22:30 ET), inside tmux. A whole
# run takes about 40 minutes on the test machine (doc/backfill.md, the projection).
#
# Usage:
#   FAFNIR_DSN=... SHARADAR_API_KEY=... scripts/backfill_fundamentals.sh [options]
# Examples:
#   scripts/backfill_fundamentals.sh --dry-run
#   scripts/backfill_fundamentals.sh
#   scripts/backfill_fundamentals.sh --keep-file          # resume
#   scripts/backfill_fundamentals.sh --symbols ZZA,ZZB    # newly linked securities
set -euo pipefail

SYMBOLS=""
KEEP_FILE="no"
DRY_RUN="no"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'USAGE'
Usage: scripts/backfill_fundamentals.sh [--symbols LIST] [--keep-file] [--dry-run]

Steps:
  1. A current SF1 file   fafnir source sharadar-bulk --tables SF1 --force
  2. The backfill         fafnir ingest fundamentals --backfill [--symbols LIST]
  3. The marts            fafnir db refresh-marts (the factor panel)
  4. The report           psql -f scripts/fundamentals_report.sql (aggregates)
  then `fafnir status`.

Options:
  --symbols LIST  Comma-separated tickers: only these securities' statements,
                  from the ledger's current SF1 file (no download). For a
                  security with no statements yet, such as one linked after the
                  backfill.
  --keep-file     Skip step 1 and read the ledger's current SF1 file: to resume a
                  run, or when the file was fetched today.
  --dry-run       Read the current file and print what a run would write; then
                  stop. Downloads and writes nothing.
  -h, --help      This message.

Needs FAFNIR_DSN (and SHARADAR_API_KEY for step 1). See doc/backfill.md, "The
fundamentals backfill (#92)", and doc/operations.md.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --symbols) SYMBOLS="${2:?--symbols needs a value}"; shift 2 ;;
        --symbols=*) SYMBOLS="${1#*=}"; shift ;;
        --keep-file) KEEP_FILE="yes"; shift ;;
        --dry-run) DRY_RUN="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

LOAD=(fafnir ingest fundamentals --backfill)
if [[ -n "${SYMBOLS}" ]]; then
    LOAD+=(--symbols "${SYMBOLS}")
fi

step() {
    echo "==> [$1/4] $2 ($(date -u +%H:%M:%SZ))"
}

if [[ "${DRY_RUN}" == "yes" ]]; then
    echo "==> Dry run: what the backfill would write ($(date -u +%H:%M:%SZ))"
    "${LOAD[@]}" --dry-run
    echo "==> Dry run finished ($(date -u +%H:%M:%SZ)): nothing downloaded or written"
    exit 0
fi

if [[ "${KEEP_FILE}" == "yes" || -n "${SYMBOLS}" ]]; then
    step 1 "A current SF1 file: skipped, reading the ledger's current one"
else
    step 1 "A current SF1 file"
    fafnir source sharadar-bulk --tables SF1 --force
fi

# Committed per vendor key: if this stops, run the script again with --keep-file.
step 2 "The backfill"
"${LOAD[@]}"

# The factor panel and the fundamentals aggregates read the statements: refresh
# them now rather than wait for the night.
step 3 "Refresh marts"
fafnir db refresh-marts

# Aggregates only, so it can be posted on an issue as it is.
step 4 "The report"
if [[ -n "${FAFNIR_DSN:-}" ]] && command -v psql >/dev/null; then
    psql "${FAFNIR_DSN}" -X -f "${SCRIPT_DIR}/fundamentals_report.sql"
else
    echo "skipped (needs psql and FAFNIR_DSN):"
    echo "  psql \"\$FAFNIR_DSN\" -X -f scripts/fundamentals_report.sql"
fi

echo "==> Fundamentals backfill complete ($(date -u +%H:%M:%SZ))"
fafnir status
