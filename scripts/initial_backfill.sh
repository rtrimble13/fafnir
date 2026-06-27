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

echo "==> [1/5] Security master (full universe)"
fafnir ingest securities --enrich

echo "==> [2/5] Daily prices from ${FROM_DATE} (resumable via watermarks)"
fafnir ingest prices --from "${FROM_DATE}"

echo "==> [3/5] Corporate actions"
fafnir ingest actions

echo "==> [4/5] Recompute adjustment factors"
fafnir adjust

echo "==> [5/5] Refresh marts + run data-quality checks"
fafnir db refresh-marts
fafnir dq run

echo "==> Backfill complete"
fafnir status
