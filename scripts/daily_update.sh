#!/usr/bin/env bash
# daily_update.sh -- incremental daily maintenance. Wire this into cron.
#
# Order matters. The universe is reconciled before any data is pulled -- renames,
# then new listings, then delistings -- so the price step runs against a universe
# that matches what is actually trading today. Then prices (so dividend adjustment
# can value against fresh closes), actions, factors, marts.
#
# Data quality is not run here: it is the fafnir-dq unit's job, after this one
# (scripts/run_dq_checks.sh, ordered After=fafnir-daily). Running it here as well
# ran it twice a night, and the second dorq run replaced the first's shadow file.
#
# Each step is idempotent and incremental (watermark-driven), so a missed day is
# caught up automatically on the next run.
#
# Two modes, by [general] primary_price_vendor (#111):
#   fmp       -- FMP's night, below, unchanged since before Sharadar.
#   sharadar  -- `fafnir ingest sharadar` (universe, prices, actions for what
#                Sharadar feeds), the fundamentals from SF1's delta, then, while
#                [general] fmp_secondary is true, FMP's loaders for what FMP still
#                feeds (funds, unlinked names).
# Rolling back is the setting flipped to "fmp": FMP's loaders resume from their
# watermarks and, under precedence, never overwrite a row Sharadar wrote.
#
# Usage (cron): FAFNIR_DSN=... FMP_API_KEY=... SHARADAR_API_KEY=... scripts/daily_update.sh
set -euo pipefail

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# A setting that does not validate stops the night here, before anything loads.
PRIMARY=$(fafnir config get primary_price_vendor)
case "${PRIMARY}" in
    fmp | sharadar) ;;
    *)
        echo "!! unknown primary vendor '${PRIMARY}'"
        exit 1
        ;;
esac
echo "==> daily_update started ${START} (primary vendor: ${PRIMARY})"

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

fmp_night() {
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

    # Link tonight's new listings to their Sharadar identity (#62). AFTER prices, not
    # straight after the security master: the link is decided by comparing the
    # security's own bars with Sharadar's, and a security minted tonight has none until
    # the step above pulls its history. Upkeep: a missing Sharadar master or key costs
    # the link, never the night's load. Until the first full `fafnir vendor link` it
    # says so and does nothing.
    echo "==> Vendor links (new listings)"
    upkeep fafnir vendor link --vendor sharadar --new

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
}

sharadar_night() {
    # TICKERS and ACTIONS downloaded whole, the SEP/SFP/SF1 deltas, the vendor master,
    # renames/delistings/listings, then bars and actions for what Sharadar feeds. The
    # payload, so fatal, as `ingest prices` is in FMP's night. It recomputes the
    # factors it changes itself. See doc/ingestion.md, "Sharadar as primary".
    echo "==> Sharadar night"
    fafnir ingest sharadar

    # The statements from the SF1 delta the step above fetched (#106). Upkeep: a
    # fundamentals failure never costs the night's prices, and the deltas it did
    # not apply stay pending for the next night. Until the backfill has run
    # (doc/backfill.md) it says so and loads nothing. The factor panel it feeds is
    # refreshed with the other marts below.
    echo "==> Fundamentals (SF1 delta)"
    upkeep fafnir ingest fundamentals

    # FMP as the secondary vendor: only what it still feeds (repo.fmp_fed -- the funds
    # and the securities with no Sharadar link), while the reduced tier serves them
    # (#128). No universe sweep and no `vendor link --new`: Sharadar's universe step
    # replaces them. Upkeep throughout: an FMP outage must not cost the marts the
    # Sharadar night's data.
    # Read first: a value that does not validate stops the night, not reads as off.
    local secondary
    secondary=$(fafnir config get fmp_secondary)
    if [ "${secondary}" = "true" ]; then
        echo "==> FMP as secondary (funds, unlinked securities)"
        upkeep fafnir ingest tracked --allow-secondary
        upkeep fafnir ingest prices --allow-secondary
        upkeep fafnir ingest actions --allow-secondary
        upkeep fafnir adjust --changed
    else
        echo "==> FMP as secondary: off ([general] fmp_secondary = false)"
    fi
}

if [ "${PRIMARY}" = "sharadar" ]; then
    sharadar_night
else
    fmp_night
fi

echo "==> Refresh marts"
fafnir db refresh-marts

echo "==> daily_update finished"
fafnir status
