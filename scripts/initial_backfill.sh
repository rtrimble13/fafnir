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

echo "==> [1/6] Security master (full universe)"
fafnir ingest securities --enrich

echo "==> [2/6] Delisting sweep (full feed)"
fafnir ingest delisted --full

# --include-inactive matters here and only here: a backfill that skips delisted
# names produces a history containing only the companies that survived to today,
# which overstates returns in every backtest run against it.
echo "==> [3/6] Daily prices from ${FROM_DATE} (resumable via watermarks)"
fafnir ingest prices --from "${FROM_DATE}" --include-inactive

echo "==> [4/6] Corporate actions"
fafnir ingest actions

echo "==> [5/6] Recompute adjustment factors"
fafnir adjust

echo "==> [6/6] Refresh marts + run data-quality checks"
fafnir db refresh-marts
fafnir dq run

echo "==> Backfill complete"
fafnir status
