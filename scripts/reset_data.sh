#!/usr/bin/env bash
# reset_data.sh -- clear warehouse data so it can be reloaded from scratch.
#
# For when a reload has to REPLACE rather than top up: the split-adjusted price
# changeover (doc/adr/0004-unadjusted-price-feed.md), a corrected field mapping, a
# universe change, or a warehouse you simply want to rebuild.
#
# It never touches structure. Migrations (meta.schema_migration), partitions, and
# the seeded reference data (ref.exchange / ref.sector / ref.industry /
# ref.trading_calendar) all survive, so you go straight back to ingesting without
# re-running `fafnir db migrate` or `fafnir db seed`.
#
#   scripts/reset_data.sh --scope prices              # preview (default)
#   scripts/reset_data.sh --scope prices --yes        # actually do it
#   scripts/reset_data.sh --scope vendor:sharadar     # report only (#70)
#
# DRY RUN IS THE DEFAULT. Nothing is deleted until you pass --yes.
set -euo pipefail

SCOPE=""
CONFIRM="no"
VACUUM="no"
BULK_DIR="${FAFNIR_SHARADAR_BULK_DIR:-/var/lib/fafnir/landing/sharadar}"
BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"

usage() {
    cat <<'USAGE'
Usage: scripts/reset_data.sh --scope SCOPE [--yes] [--vacuum]

Scopes:
  prices        core.daily_price + core.adjustment_factor, and the price
                watermarks (both the current unadjusted endpoint and the retired
                split-adjusted one). Keeps the security master and corporate
                actions. This is the ADR-0004 changeover.

  actions       core.corporate_action + core.adjustment_factor. Keeps prices.

  market-data   prices + actions together (daily_price, corporate_action,
                adjustment_factor, price watermarks). Keeps the security master,
                so you skip re-ingesting ~8k securities.

  landing       landing.fmp_raw only. Reclaims disk; loses the raw-payload audit
                trail. Safe at any time -- nothing reads it during ingestion.

  dq-flags      ops.data_quality_flag only. Clears accumulated flag history.

  all           Every core/ops/landing table: prices, actions, factors, security
                master, xrefs, applied renames, profiles, vendor identity (the
                vendor masters, events, links and rejections -- links and
                rejections are not rebuilt by a reload), watermarks, DQ flags,
                ingestion runs, landing payloads
                and the Sharadar file ledger (the files under [sharadar] bulk_dir
                stay; the next download records them again). Identity sequences
                restart. Reference data and migrations are kept.

  vendor:sharadar
                A REPORT, never a deletion (#70, ADR 0015): what Sharadar
                supplied or what holds its data -- rows per table, minted
                securities and the operator edits on them, active corrections,
                the files under [sharadar] bulk_dir, and the copies outside the
                database (dumps, off-site) that its licence's 30-day deletion
                clause also reaches. Refuses --yes.

Options:
  --yes         Execute. Without it this only reports what would be deleted.
  --vacuum      VACUUM ANALYZE the affected tables afterwards (returns disk to
                the OS after a large truncate).
  --bulk-dir P  vendor:sharadar only: the [sharadar] bulk_dir to measure
                (default $FAFNIR_SHARADAR_BULK_DIR, else
                /var/lib/fafnir/landing/sharadar).
  -h, --help    This message.

Requires FAFNIR_DSN.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scope) SCOPE="${2:?--scope needs a value}"; shift 2 ;;
        --scope=*) SCOPE="${1#*=}"; shift ;;
        --yes|-y) CONFIRM="yes"; shift ;;
        --vacuum) VACUUM="yes"; shift ;;
        --bulk-dir) BULK_DIR="${2:?--bulk-dir needs a value}"; shift 2 ;;
        --bulk-dir=*) BULK_DIR="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "${SCOPE}" ]]; then
    echo "ERROR: --scope is required." >&2
    usage >&2
    exit 2
fi

case "${SCOPE}" in
    vendor:sharadar)
        if [[ "${CONFIRM}" == "yes" ]]; then
            echo "ERROR: vendor:sharadar is a report; it never deletes (#70)." >&2
            echo "       Under precedence a vendor's rows cannot be removed without" >&2
            echo "       rebuilding the series from the other vendor (ADR 0015):" >&2
            echo "       see doc/operations.md, 'Ending a vendor subscription'." >&2
            exit 2
        fi
        ;;
    vendor:*)
        echo "ERROR: unknown vendor scope '${SCOPE}': only vendor:sharadar is reported." >&2
        echo "       (vendor:fmp was dropped in plan v2: FMP's rows are the warehouse's base.)" >&2
        exit 2
        ;;
esac

DSN="${FAFNIR_DSN:?set FAFNIR_DSN}"

# Endpoint keys in ops.load_watermark. Only the price loader writes watermarks;
# the retired endpoint is included so the ADR-0004 changeover clears in one pass.
PRICE_ENDPOINT="historical-price-eod/non-split-adjusted"
LEGACY_PRICE_ENDPOINT="historical-price-eod/full"

# TABLES must be closed under foreign keys -- every table referencing one in the
# list has to be in the list too. TRUNCATE is deliberately issued WITHOUT CASCADE:
# if a future migration adds a referencing table, this fails loudly instead of
# silently wiping something nobody listed.
WATERMARK_SQL=""
RESTART_IDENTITY=""
case "${SCOPE}" in
    prices)
        TABLES="core.daily_price, core.adjustment_factor"
        WATERMARK_SQL="DELETE FROM ops.load_watermark WHERE source='fmp'
                        AND endpoint IN ('${PRICE_ENDPOINT}', '${LEGACY_PRICE_ENDPOINT}');"
        NEXT="fafnir ingest prices --include-inactive --from <start-date>
  fafnir adjust
  fafnir db refresh-marts
  fafnir dq run"
        ;;
    actions)
        TABLES="core.corporate_action, core.adjustment_factor"
        NEXT="fafnir ingest actions
  fafnir adjust
  fafnir db refresh-marts"
        ;;
    market-data)
        TABLES="core.daily_price, core.corporate_action, core.adjustment_factor"
        WATERMARK_SQL="DELETE FROM ops.load_watermark WHERE source='fmp'
                        AND endpoint IN ('${PRICE_ENDPOINT}', '${LEGACY_PRICE_ENDPOINT}');"
        NEXT="fafnir ingest prices --include-inactive --from <start-date>
  fafnir ingest actions
  fafnir adjust
  fafnir db refresh-marts
  fafnir dq run"
        ;;
    landing)
        TABLES="landing.fmp_raw"
        NEXT="(nothing -- landing is written by the next ingest run)"
        ;;
    dq-flags)
        TABLES="ops.data_quality_flag"
        NEXT="fafnir dq run"
        ;;
    vendor:sharadar)
        TABLES=""
        ;;
    all)
        TABLES="core.daily_price, core.corporate_action, core.adjustment_factor,
                core.symbol_xref, core.symbol_change, core.company_profile,
                core.security_vendor_xref, core.security_vendor_rejection,
                core.vendor_security, core.vendor_security_event,
                core.income_statement, core.balance_sheet, core.cash_flow_statement,
                core.security, ops.data_quality_flag,
                ops.load_watermark, ops.ingestion_run, landing.fmp_raw,
                landing.sharadar_file"
        RESTART_IDENTITY="RESTART IDENTITY"
        NEXT="fafnir ingest securities --enrich
  fafnir ingest prices --include-inactive --from <start-date>
  fafnir ingest actions
  fafnir adjust
  fafnir db refresh-marts
  fafnir dq run

  (or just: scripts/initial_backfill.sh <start-date>)"
        ;;
    *)
        echo "ERROR: unknown scope '${SCOPE}'." >&2
        usage >&2
        exit 2
        ;;
esac

# Normalize whitespace so the list prints and re-parses cleanly.
TABLES="$(echo "${TABLES}" | tr -s ' \n\t' ' ' | sed 's/^ //; s/ $//')"

# Describe the target WITHOUT echoing the DSN. A URL-form DSN
# (postgresql://user:pass@host/db -- supported, see src/fafnir/config.py) contains
# no spaces, so any "print a prefix of it" approach leaks the password into the
# terminal and into whatever log the operator redirected to. The project already
# holds this line elsewhere (redact_secrets/SECRET_QUERY_PARAMS in
# src/fafnir/sources/base.py, which duk shares). The dbname is the useful
# confirmation anyway: it names the database about to be truncated.
describe_dsn() {
    local dsn="$1" host="" db="" rest=""
    if [[ "${dsn}" == *"://"* ]]; then
        rest="${dsn#*://}"
        rest="${rest##*@}"              # drop any user:password@ (greedy: last @)
        host="${rest%%/*}"
        host="${host%%\?*}"
        if [[ "${rest}" == */* ]]; then
            db="${rest#*/}"
            db="${db%%\?*}"
        fi
    else
        # Key-value form. Anchored to start-or-whitespace so `host=` does not also
        # match inside another key, and `|| true` so a DSN missing either key does
        # not trip `set -e`.
        host="$(grep -oE '(^|[[:space:]])host=[^[:space:]]+' <<< "${dsn}" \
                | tail -1 | cut -d= -f2- || true)"
        db="$(grep -oE '(^|[[:space:]])dbname=[^[:space:]]+' <<< "${dsn}" \
              | tail -1 | cut -d= -f2- || true)"
    fi
    echo "db=${db:-<unknown>} host=${host:-<unknown>}"
}

echo "==> reset_data.sh  scope=${SCOPE}"
echo "    Target: $(describe_dsn "${DSN}")"
echo

# What a vendor supplied, and where its data is held: a report for the day its
# licence ends (Sharadar §10: delete everything within 30 days). Nothing here
# deletes. The counts are not vendor data, so the output can be shared.
if [[ "${SCOPE}" == "vendor:sharadar" ]]; then
    q() { psql "${DSN}" -tAc "$1" 2>/dev/null || echo '(unreadable)'; }
    row() { printf '    %-52s %s\n' "$1" "$(q "$2")"; }

    echo "==> Rows Sharadar supplied (source = 'sharadar'):"
    row "core.daily_price" "SELECT count(*) FROM core.daily_price WHERE source = 'sharadar'"
    row "core.corporate_action" \
        "SELECT count(*) FROM core.corporate_action WHERE source = 'sharadar'"
    row "core.security (minted by Sharadar)" \
        "SELECT count(*) FROM core.security WHERE source = 'sharadar'"
    row "core.symbol_xref" "SELECT count(*) FROM core.symbol_xref WHERE source = 'sharadar'"
    row "core.symbol_change" \
        "SELECT count(*) FROM core.symbol_change WHERE source = 'sharadar'"
    row "core.company_profile" \
        "SELECT count(*) FROM core.company_profile WHERE source = 'sharadar'"
    row "core.income_statement (every row; ADR 0018)" \
        "SELECT count(*) FROM core.income_statement WHERE source = 'sharadar'"
    row "core.balance_sheet" \
        "SELECT count(*) FROM core.balance_sheet WHERE source = 'sharadar'"
    row "core.cash_flow_statement" \
        "SELECT count(*) FROM core.cash_flow_statement WHERE source = 'sharadar'"
    echo
    echo "==> Rows that hold Sharadar's data (vendor = 'sharadar'):"
    row "core.vendor_security (its master)" \
        "SELECT count(*) FROM core.vendor_security WHERE vendor = 'sharadar'"
    row "core.vendor_security_event (its events)" \
        "SELECT count(*) FROM core.vendor_security_event WHERE vendor = 'sharadar'"
    row "core.security_vendor_xref (links; evidence: closes)" \
        "SELECT count(*) FROM core.security_vendor_xref WHERE vendor = 'sharadar'"
    row "core.security_vendor_rejection" \
        "SELECT count(*) FROM core.security_vendor_rejection WHERE vendor = 'sharadar'"
    row "ops.data_quality_flag (vendor_*; detail: closes)" \
        "SELECT count(*) FROM ops.data_quality_flag WHERE check_name LIKE 'vendor%'"
    row "ops.vendor_correction (Sharadar replaced; active)" \
        "SELECT count(*) || ' (' || count(*) FILTER (WHERE reverted_at IS NULL) || ' active)'
           FROM ops.vendor_correction WHERE new_source = 'sharadar'"
    row "ops.load_watermark" "SELECT count(*) FROM ops.load_watermark WHERE source = 'sharadar'"
    row "ops.ingestion_run (bookkeeping, no data)" \
        "SELECT count(*) FROM ops.ingestion_run WHERE source = 'sharadar'"
    row "landing.sharadar_file (files on disk, as recorded)" \
        "SELECT count(*) || ' files, ' || pg_size_pretty(coalesce(sum(bytes), 0))
           FROM landing.sharadar_file WHERE status <> 'deleted'"
    echo
    echo "==> Needs a person before anything is removed:"
    row "operator edits on Sharadar-minted securities" \
        "SELECT count(*) FROM ops.operator_override o
           JOIN core.security s USING (security_id) WHERE s.source = 'sharadar'"
    echo
    echo "==> Files on disk:"
    if [[ -d "${BULK_DIR}" && -r "${BULK_DIR}" ]]; then
        printf '    %-52s %s files, %s\n' "${BULK_DIR}" \
            "$(find "${BULK_DIR}" -type f | wc -l | xargs)" \
            "$(du -sh "${BULK_DIR}" 2>/dev/null | cut -f1)"
    else
        echo "    ${BULK_DIR}: not found or not readable (pass --bulk-dir <[sharadar] bulk_dir>)"
    fi
    echo
    echo "==> Copies outside the database (the 30-day clause reaches these too):"
    if [[ -d "${BACKUP_DIR}" && -r "${BACKUP_DIR}" ]]; then
        printf '    %-52s %s dumps, oldest %s\n' "logical dumps in ${BACKUP_DIR}" \
            "$(find "${BACKUP_DIR}" -maxdepth 1 -name 'fafnir_*.dump' | wc -l | xargs)" \
            "$(find "${BACKUP_DIR}" -maxdepth 1 -name 'fafnir_*.dump' -printf '%TY-%Tm-%Td\n' \
               2>/dev/null | sort | head -1)"
    else
        echo "    logical dumps: ${BACKUP_DIR} not found or not readable (set FAFNIR_BACKUP_DIR)"
    fi
    echo "    off-site copies: whatever scripts/backup_offsite.sh pushed to \$FAFNIR_BACKUP_REMOTE"
    echo "    the marts: mart.security_latest and the other materialized views, until refreshed"
    echo "    anything exported by hand (CSV, notebooks, saved duk output)"
    echo
    cat <<'EOF'
==> REPORT ONLY -- nothing was deleted, and this scope never deletes.
    What ending the subscription takes is in doc/operations.md,
    "Ending a vendor subscription".
EOF
    exit 0
fi

echo "==> Rows that would be removed:"
IFS=',' read -ra TABLE_ARR <<< "${TABLES}"
for t in "${TABLE_ARR[@]}"; do
    t="$(echo "${t}" | xargs)"
    printf '    %-28s %s\n' "${t}" \
        "$(psql "${DSN}" -tAc "SELECT count(*) FROM ${t};" 2>/dev/null || echo '(unreadable)')"
done
if [[ -n "${WATERMARK_SQL}" ]]; then
    printf '    %-28s %s\n' "ops.load_watermark (price)" \
        "$(psql "${DSN}" -tAc "SELECT count(*) FROM ops.load_watermark
             WHERE source='fmp' AND endpoint IN
             ('${PRICE_ENDPOINT}','${LEGACY_PRICE_ENDPOINT}');" 2>/dev/null || echo '(unreadable)')"
fi
echo

if [[ "${CONFIRM}" != "yes" ]]; then
    cat <<EOF
==> DRY RUN -- nothing was deleted.

    Re-run with --yes to execute:
      scripts/reset_data.sh --scope ${SCOPE} --yes

    Reference data (exchanges, sectors, industries, trading calendar),
    partitions and migrations are never touched by this script.
EOF
    exit 0
fi

echo "==> Deleting (single transaction; either all of it lands or none of it)"
psql "${DSN}" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
TRUNCATE ${TABLES} ${RESTART_IDENTITY};
${WATERMARK_SQL}
COMMIT;
SQL

if [[ "${VACUUM}" == "yes" ]]; then
    echo "==> VACUUM ANALYZE"
    for t in "${TABLE_ARR[@]}"; do
        t="$(echo "${t}" | xargs)"
        psql "${DSN}" -c "VACUUM ANALYZE ${t};" >/dev/null
    done
fi

# mart.security_latest is a materialized view over the tables just cleared, so it
# still holds the old snapshot until something refreshes it. Say so rather than
# refreshing here -- an empty mart after a reset is honest; a stale one is not.
echo
echo "==> Done. mart.security_latest still holds its pre-reset snapshot;"
echo "    'fafnir db refresh-marts' after reloading will bring it back in line."
echo
echo "==> Next:"
echo "  ${NEXT}"
