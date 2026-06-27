#!/usr/bin/env bash
# run_dq_checks.sh -- standalone data-quality sweep (gaps, outliers, freshness).
# Writes flags to ops.data_quality_flag. Run from cron more frequently than a
# full load if you want tighter monitoring.
set -euo pipefail

EXCHANGE="${1:-NASDAQ}"
echo "==> Running data-quality checks (exchange=${EXCHANGE})"
fafnir dq run --exchange "${EXCHANGE}"

echo "==> Open data-quality flags by check:"
psql "${FAFNIR_DSN:?set FAFNIR_DSN}" -c \
  "SELECT check_name, severity, count(*) AS open_flags
     FROM ops.data_quality_flag
    WHERE resolved_at IS NULL
    GROUP BY check_name, severity
    ORDER BY open_flags DESC;"
