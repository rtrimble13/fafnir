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

# Renames FIRST, before the security master reads the screener -- the same order
# daily_update.sh runs them in, and for the same reason. A renamed ticker is
# indistinguishable from a new listing on the screener side, so a master load that
# goes first mints a SECOND security_id for a company this warehouse already holds.
# The rename sweep can still fold that duplicate away while it is empty, but a
# backfill fills it with bars on the very next step -- and from then on the rename
# is a permanent `conflict` that only a manual `fafnir security merge-rename` can
# clear. This step was missing from this script, which is exactly how the
# ODVWZ/ALBT/MBAV/GREE/GEOA/TUGN conflicts of 2026-08 were created.
#
# On a genuinely empty database this does nothing (no old ticker is in the master
# yet) and costs one small feed read. It earns its place on every re-run.
echo "==> [1/7] Ticker renames"
fafnir ingest symbol-changes

echo "==> [2/7] Security master (full universe)"
# For current db implementation, drop --enrich.  Change made in fafnir v0.5.0 to avoid unnecessary API calls and reduce load on FMP servers. 2026-08-27
# fafnir ingest securities --enrich
fafnir ingest securities

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
