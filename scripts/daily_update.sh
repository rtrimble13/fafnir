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

# The declared universe (ref.tracked_symbol): mutual funds and anything else the
# screener cannot reach, because it has no listing venue to be screened on. AFTER
# the security master, not before -- both write on the same conflict key, and going
# second is what makes the declared asset_type and venue the ones that stand. Same
# upkeep treatment as the two steps above: a profile outage must not cost the
# night's prices. See doc/adr/0006-curated-fund-universe.md.
echo "==> Declared universe (tracked funds)"
upkeep fafnir ingest tracked

# Before prices: a name that stopped trading today should be marked before the
# price step asks it for fresh bars. Retaining it (rather than dropping it) is
# what keeps the accumulating history free of survivorship bias.
echo "==> Delisting sweep"
fafnir ingest delisted

echo "==> Incremental prices (active universe)"
fafnir ingest prices

# Incremental once `actions_mode` is "auto" in the config: one market-wide calendar
# sweep against a watermark, a full pull only for securities that have never had one,
# and a 1/30 slice reconciled against the per-symbol feed so a vendor coverage gap
# surfaces on a schedule rather than as a quietly wrong adjusted price. Until then it
# is the original full refresh, minus the delisted names it could never learn anything
# new about. Gate the switch on `fafnir source probe-actions`. See ADR 0007.
echo "==> Corporate actions"
fafnir ingest actions

# Only the securities whose actions actually moved tonight. A full recompute walks
# every security that has ever had an action and re-queries a prior close per dividend
# -- a second full refresh chained onto the first. `fafnir adjust` with no flags stays
# the backfill path and the way to rebuild after a schema or factor-logic change.
echo "==> Recompute adjustment factors (changed securities only)"
fafnir adjust --changed

echo "==> Refresh marts"
fafnir db refresh-marts

echo "==> Data-quality checks"
fafnir dq run

echo "==> daily_update finished"
fafnir status
