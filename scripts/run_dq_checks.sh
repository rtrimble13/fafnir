#!/usr/bin/env bash
# run_dq_checks.sh -- standalone data-quality sweep (gaps, outliers, freshness).
# Writes flags to ops.data_quality_flag. Run from cron more frequently than a
# full load if you want tighter monitoring.
#
# The summary comes from `fafnir dq list`, so this needs no psql and no
# FAFNIR_DSN of its own -- the CLI reads the same config the sweep just used.
set -euo pipefail

EXCHANGE="${1:-NASDAQ}"
echo "==> Running data-quality checks (exchange=${EXCHANGE})"
fafnir dq run --exchange "${EXCHANGE}"

echo
echo "==> Open data-quality flags:"
fafnir dq list

# To work the queue from here:
#   fafnir dq list --detail --check gap --symbol AAPL
#   fafnir dq resolve --check gap --symbol AAPL --note "backfilled" --yes
