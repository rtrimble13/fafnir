#!/usr/bin/env bash
# initial_backfill.sh -- one-time historical backfill of the full universe.
#
# Resumable: the price loader tracks a per-symbol watermark, so re-running after
# an interruption continues where it left off rather than restarting. Designed to
# live within the FMP Professional limits (~300 req/min, 50 GB/month) via the
# client-side throttle.
#
# Usage:
#   FAFNIR_DSN=... FMP_API_KEY=... scripts/initial_backfill.sh [FROM_DATE]
# Example:
#   scripts/initial_backfill.sh 2010-01-01
set -euo pipefail

FROM_DATE="${1:-2010-01-01}"

echo "==> [1/7] Security master (full universe)"
# For current db implementation, drop --enrich.  Change made in fafnir v0.5.0 to avoid unnecessary API calls and reduce load on FMP servers. 2026-08-27
# fafnir ingest securities --enrich
fafnir ingest securities

# Declared securities (mutual funds and anything else with no listing venue) are
# minted here, before prices, so the price step below backfills them in the same
# run as the screened universe. A warehouse with nothing declared loads nothing
# and moves on.
echo "==> [2/7] Declared universe (tracked funds)"
fafnir ingest tracked

echo "==> [3/7] Delisting sweep (full feed)"
fafnir ingest delisted --full

# --include-inactive matters here and only here: a backfill that skips delisted
# names produces a history containing only the companies that survived to today,
# which overstates returns in every backtest run against it.
echo "==> [4/7] Daily prices from ${FROM_DATE} (resumable via watermarks)"
fafnir ingest prices --from "${FROM_DATE}" --include-inactive

echo "==> [5/7] Corporate actions"
fafnir ingest actions

echo "==> [6/7] Recompute adjustment factors"
fafnir adjust

echo "==> [7/7] Refresh marts + run data-quality checks"
fafnir db refresh-marts
fafnir dq run

echo "==> Backfill complete"
fafnir status
