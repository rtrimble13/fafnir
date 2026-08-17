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
#
# DRY RUN IS THE DEFAULT. Nothing is deleted until you pass --yes.
set -euo pipefail

SCOPE=""
CONFIRM="no"
VACUUM="no"

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
                master, xrefs, profiles, watermarks, DQ flags, ingestion runs and
                landing payloads. Identity sequences restart. Reference data and
                migrations are kept.

Options:
  --yes         Execute. Without it this only reports what would be deleted.
  --vacuum      VACUUM ANALYZE the affected tables afterwards (returns disk to
                the OS after a large truncate).
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
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "${SCOPE}" ]]; then
    echo "ERROR: --scope is required." >&2
    usage >&2
    exit 2
fi

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
    all)
        TABLES="core.daily_price, core.corporate_action, core.adjustment_factor,
                core.symbol_xref, core.company_profile, core.security,
                ops.data_quality_flag, ops.load_watermark, ops.ingestion_run,
                landing.fmp_raw"
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
# holds this line elsewhere (redact_secrets/SECRET_QUERY_PARAMS in duk/fmp_api.py
# and sources/base.py). The dbname is the useful confirmation anyway: it names the
# database about to be truncated.
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
