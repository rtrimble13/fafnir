#!/usr/bin/env bash
# daily_update.sh -- incremental daily maintenance. Wire this into cron.
#
# Order matters. The universe is reconciled before any data is pulled -- renames,
# then new listings, then delistings -- so the price step runs against a universe
# that matches what is actually trading today. Then prices (so dividend adjustment
# can value against fresh closes), actions, factors, marts, DQ.
#
# Each step is idempotent and incremental (watermark-driven), so a missed day is
# caught up automatically on the next run.
#
# Usage (cron): FAFNIR_DSN=... FMP_API_KEY=... scripts/daily_update.sh
set -euo pipefail

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "==> daily_update started ${START}"

# Universe maintenance is upkeep, not the payload. A source outage on the rename
# feed or the screener must not cost the night's prices for every symbol, so these
# two steps warn and continue instead of tripping `set -e`. The failure is not
# swallowed: the loader still writes a failed ops.ingestion_run row, which is what
# doc/operations.md tells the operator to watch. A credential or connectivity
# problem serious enough to matter fails the price step below anyway, and that one
# is still fatal.
upkeep() {
    if ! "$@"; then
        echo "!! '$*' failed -- continuing so the data load still runs."
        echo "!! Check: SELECT * FROM ops.ingestion_run WHERE status = 'failed'"
        echo "!!        ORDER BY started_at DESC LIMIT 5;"
    fi
}

# Keep partitions + trading calendar rolled forward to the horizon (auto-extends
# to current year + horizon_extra_years; no yearly config edits needed).
fafnir db ensure-horizon

# Renames FIRST, before the security master reads the screener. A renamed ticker
# is indistinguishable from a new listing on the screener side, so if the master
# ran first it would mint a second security_id for a company fafnir already holds
# -- stranding its price history, watermark and corporate actions on the old
# ticker, which nothing would ever close (a rename is not a delisting).
echo "==> Ticker renames"
upkeep fafnir ingest symbol-changes

# Then the universe itself: this is how an IPO, a spin-off or a new ETF enters
# scope. A newly minted security has no price watermark, so the price step below
# pulls its full available history on this same run -- nothing else to do.
echo "==> Security master (new listings)"
upkeep fafnir ingest securities

# Before prices: a name that stopped trading today should be marked before the
# price step asks it for fresh bars. Retaining it (rather than dropping it) is
# what keeps the accumulating history free of survivorship bias.
echo "==> Delisting sweep"
fafnir ingest delisted

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
