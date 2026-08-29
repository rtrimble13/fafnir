#!/usr/bin/env bash
# backup_offsite.sh -- weekly off-site backup of the warehouse, with retention.
#
# Dump -> restic -> any remote restic can reach (rclone:dropbox:..., B2, SFTP).
# See doc/adr/0007-offsite-backup-storage.md for why it is shaped this way; the
# two decisions that matter are repeated here because they look like mistakes:
#
#   1. The dump is written UNCOMPRESSED (-Z0). pg_dump's own compression is the
#      enemy of deduplication: change one byte early in a stream and every byte
#      after it differs, so two weekly dumps of a warehouse that is 98%
#      append-only would share nothing and cost full price each. Uncompressed,
#      restic's content-defined chunking sees the unchanged history as unchanged
#      and stores only the week's new bytes -- then compresses them itself.
#
#   2. Directory format (-Fd), not custom (-Fc). One file per table means an
#      untouched table is a no-op, and it is the only format pg_dump parallelises
#      (-j). The cost is scratch disk: budget roughly the size of the database.
#
# Usage (cron):  FAFNIR_BACKUP_REPO=... RESTIC_PASSWORD_FILE=... backup_offsite.sh
#        state:  backup_offsite.sh --state-only
#         prune: backup_offsite.sh --prune          (monthly; see below)
set -euo pipefail

# --- Configuration ----------------------------------------------------------
# Where restic keeps the repository. Examples:
#   rclone:dropbox-fafnir:                 (Dropbox via an App-folder rclone remote)
#   b2:fafnir-backups:/warehouse           (Backblaze B2, ideally with Object Lock)
#   sftp:u123456@u123456.your-storagebox.de:/backups
REPO="${FAFNIR_BACKUP_REPO:?set FAFNIR_BACKUP_REPO to the restic repository}"

# restic needs its passphrase non-interactively. Prefer a root-only file over
# RESTIC_PASSWORD in the environment, which leaks into /proc and `ps e`.
: "${RESTIC_PASSWORD_FILE:=/etc/fafnir/restic.pass}"
export RESTIC_PASSWORD_FILE RESTIC_REPOSITORY="${REPO}"

DUMPDIR="${FAFNIR_BACKUP_DUMPDIR:-/var/backups/fafnir/pgdump}"
PGHOST_ARG="${FAFNIR_BACKUP_PGHOST:-/var/run/postgresql}"
PGUSER_ARG="${FAFNIR_BACKUP_PGUSER:-fafnir_ingest}"
PGDB="${FAFNIR_BACKUP_PGDATABASE:-fafnir}"
JOBS="${FAFNIR_BACKUP_JOBS:-4}"

# The recycle plan. Two policies, because the two snapshot kinds have different
# value per byte: a full dump is large and reconstructible from FMP, while the
# state dump is tiny and holds the only copy of decisions a human made.
KEEP_FULL="${FAFNIR_BACKUP_KEEP_FULL:---keep-last 4 --keep-weekly 8 --keep-monthly 12 --keep-yearly 2}"
KEEP_STATE="${FAFNIR_BACKUP_KEEP_STATE:---keep-daily 14 --keep-weekly 8 --keep-monthly 24}"

MODE=full
DO_PRUNE=0
for arg in "$@"; do
    case "$arg" in
        --state-only) MODE=state ;;
        --prune)      DO_PRUNE=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

psql_q() { psql -h "${PGHOST_ARG}" -U "${PGUSER_ARG}" -d "${PGDB}" -tAc "$1"; }

echo "==> backup_offsite started $(date -u +%Y-%m-%dT%H:%M:%SZ) (mode=${MODE})"

# --- Preflight --------------------------------------------------------------
# Fail before the dump, not after it. A dump that completes and then cannot be
# uploaded has spent an hour and filled the disk for nothing.
command -v restic >/dev/null || { echo "!! restic not installed"; exit 1; }
[ -r "${RESTIC_PASSWORD_FILE}" ] || { echo "!! cannot read ${RESTIC_PASSWORD_FILE}"; exit 1; }

if ! restic cat config >/dev/null 2>&1; then
    echo "!! repository ${REPO} is unreachable or not initialised."
    echo "!! Initialise once with: restic init"
    exit 1
fi

install -d -m 0750 "$(dirname "${DUMPDIR}")"

if [ "${MODE}" = full ]; then
    # Scratch space, not compressed: the dump is roughly the size of the live
    # database. Refuse rather than fill the disk out from under PostgreSQL.
    DB_BYTES=$(psql_q "SELECT pg_database_size('${PGDB}')")
    FREE_KB=$(df -Pk "$(dirname "${DUMPDIR}")" | awk 'NR==2 {print $4}')
    NEED_KB=$(( DB_BYTES / 1024 * 12 / 10 ))          # 20% headroom
    if [ "${FREE_KB}" -lt "${NEED_KB}" ]; then
        echo "!! need ~$(( NEED_KB / 1024 )) MB free for the dump, have $(( FREE_KB / 1024 )) MB."
        echo "!! Free space, or stream instead:"
        echo "!!   pg_dump -Fc -Z0 ... | restic backup --stdin --stdin-filename fafnir.dump"
        exit 1
    fi
fi

# --- Dump -------------------------------------------------------------------
# pg_dump -Fd insists on creating the directory itself.
rm -rf "${DUMPDIR}"

if [ "${MODE}" = full ]; then
    # Every schema, including mart and meta. A restore without meta re-applies
    # every migration; a restore without mart has nothing for `db refresh-marts`
    # to refresh. Derived data is cheap here -- mart is a screening snapshot,
    # not a second copy of the price history.
    echo "==> pg_dump (all schemas, uncompressed, -j${JOBS})"
    pg_dump -h "${PGHOST_ARG}" -U "${PGUSER_ARG}" -d "${PGDB}" \
        -Fd -Z0 -j "${JOBS}" -f "${DUMPDIR}"
    TAG=fafnir-full
    KEEP="${KEEP_FULL}"
else
    # The state dump: everything a human decided that FMP cannot tell us again.
    # ref.tracked_symbol is the declared universe (ADR 0006), ops carries DQ
    # resolutions and their notes, meta carries the migration ledger. Small
    # enough to take daily, and the part of a loss that actually hurts -- prices
    # can be re-ingested, judgement calls cannot.
    echo "==> pg_dump (ref + ops + meta only)"
    pg_dump -h "${PGHOST_ARG}" -U "${PGUSER_ARG}" -d "${PGDB}" \
        -Fd -Z0 -n ref -n ops -n meta -f "${DUMPDIR}"
    TAG=fafnir-state
    KEEP="${KEEP_STATE}"
fi

echo "==> dump size: $(du -sh "${DUMPDIR}" | cut -f1)"

# Roles live outside a database dump; without them a restored cluster has no
# fafnir_ingest to own anything. Costs a few kB, so it rides along every time.
if command -v pg_dumpall >/dev/null && [ "${MODE}" = full ]; then
    pg_dumpall -h "${PGHOST_ARG}" --globals-only > "${DUMPDIR}/globals.sql" 2>/dev/null \
        || echo "!! globals not captured (needs superuser) -- recreate roles from install_hetzner.md 3.5"
fi

# --- Upload -----------------------------------------------------------------
# Dropbox rate-limits (HTTP 429) an aggressive many-small-files writer, so keep
# concurrency modest and pack files large. Harmless on S3-style backends.
echo "==> restic backup --tag ${TAG}"
restic backup "${DUMPDIR}" \
    --tag "${TAG}" \
    --host fafnir \
    --compression auto \
    --pack-size 64 \
    --option rclone.connections=4 \
    --cleanup-cache

# --- Recycle ----------------------------------------------------------------
# `forget` only unlinks snapshots; `prune` is what reclaims space, and it is the
# expensive, API-chatty operation -- rewriting pack files, listing the whole
# repository. On Dropbox that is the step that trips rate limits, so it is not
# part of the weekly run: schedule it monthly with --prune.
echo "==> forget (${TAG}): ${KEEP}"
# shellcheck disable=SC2086
restic forget --tag "${TAG}" --host fafnir --group-by host,tags ${KEEP}

if [ "${DO_PRUNE}" -eq 1 ]; then
    echo "==> prune"
    restic prune --max-unused 10%
    # Reading a sample back is the only thing that distinguishes a backup from a
    # directory of files you have never opened.
    echo "==> check (5% of pack data re-read)"
    restic check --read-data-subset=5%
fi

rm -rf "${DUMPDIR}"

echo "==> snapshots now held:"
restic snapshots --host fafnir --compact
echo "==> backup_offsite finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
