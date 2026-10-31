#!/usr/bin/env bash
# run_dq_checks.sh -- the nightly data-quality sweep (gaps, outliers, freshness,
# and dorq when [dq] engine says so). Writes flags to ops.data_quality_flag.
#
# The one scheduled DQ run: the fafnir-dq unit runs it after fafnir-daily (and the
# example crontab chains it after daily_update.sh). It runs after a failed load too:
# the SQL checks still run, and dorq, finding no session newer than the night it
# last judged, says so and writes nothing (`fafnir dq run --rerun` re-judges it).
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
