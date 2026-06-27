#!/usr/bin/env bash
# daily_update.sh -- incremental daily maintenance. Wire this into cron.
#
# Order matters: prices first (so dividend adjustment can value against fresh
# closes), then actions, then recompute factors, then refresh marts, then DQ.
# Each step is idempotent and incremental (watermark-driven), so a missed day is
# caught up automatically on the next run.
#
# Usage (cron): FAFNIR_DSN=... FMP_API_KEY=... scripts/daily_update.sh
set -euo pipefail

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "==> daily_update started ${START}"

# Make sure the current year's partition exists before loading into it.
fafnir db ensure-partitions

echo "==> Incremental prices (active universe)"
fafnir ingest prices

echo "==> Corporate actions"
fafnir ingest actions

echo "==> Recompute adjustment factors"
fafnir adjust

echo "==> Refresh marts"
fafnir db refresh-marts

echo "==> Data-quality checks"
fafnir dq run

echo "==> daily_update finished"
fafnir status
