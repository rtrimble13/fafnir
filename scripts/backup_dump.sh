#!/usr/bin/env bash
# backup_dump.sh -- nightly whole-database logical dump with retention.
#
# doc/install_hetzner.md §9.1. Run it from the fafnir-dump.timer, or by hand.
#
# Dumps EVERY schema, not just core/landing. Skipping `mart` because it is
# derived and `meta` because it is "only bookkeeping" gives you a restore that
# is not a working warehouse: meta.schema_migration is what stops
# `fafnir db migrate` re-applying every migration on the restored copy, and
# without the mart views there is nothing for `fafnir db refresh-marts` to
# refresh. The extra bytes are negligible -- mart is a screening snapshot, not a
# second copy of the price history.
#
# Role definitions live OUTSIDE a database dump. Recreate them from
# install_hetzner.md §3.5 on a new host, or capture them separately:
#   sudo -u postgres pg_dumpall --globals-only > globals.sql
# --globals is the flag that does that from here (needs a superuser connection).
#
# Environment:
#   FAFNIR_BACKUP_DIR          where dumps land (default /var/backups/fafnir)
#   FAFNIR_BACKUP_RETAIN_DAYS  delete dumps older than this (default 14)
#   FAFNIR_BACKUP_HOST         libpq host (default /var/run/postgresql, the
#                              socket + peer auth path of §3.6)
#   FAFNIR_BACKUP_DB           database name (default fafnir)
#   FAFNIR_BACKUP_ROLE         role to dump as (default fafnir_ingest)
#   FAFNIR_BACKUP_COMPRESS     pg_dump -Z level (default 6)
#
#   scripts/backup_dump.sh              # dump + prune
#   scripts/backup_dump.sh --globals    # also dump role definitions
#   scripts/backup_dump.sh --no-prune   # keep every dump
set -euo pipefail

BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"
RETAIN_DAYS="${FAFNIR_BACKUP_RETAIN_DAYS:-14}"
DB_HOST="${FAFNIR_BACKUP_HOST:-/var/run/postgresql}"
DB_NAME="${FAFNIR_BACKUP_DB:-fafnir}"
DB_ROLE="${FAFNIR_BACKUP_ROLE:-fafnir_ingest}"
COMPRESS="${FAFNIR_BACKUP_COMPRESS:-6}"
WITH_GLOBALS="no"
PRUNE="yes"

usage() {
    cat <<'USAGE'
Usage: scripts/backup_dump.sh [--globals] [--no-prune] [--dir PATH]
                              [--retain-days N]

Options:
  --globals        Also write globals_<date>.sql (pg_dumpall --globals-only).
                   Needs a superuser connection -- normally run as the postgres
                   OS user, not as fafnir.
  --no-prune       Keep every dump instead of expiring old ones.
  --dir PATH       Output directory (default $FAFNIR_BACKUP_DIR or
                   /var/backups/fafnir).
  --retain-days N  Expiry age in days (default $FAFNIR_BACKUP_RETAIN_DAYS or 14).
  -h, --help       This message.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --globals) WITH_GLOBALS="yes"; shift ;;
        --no-prune) PRUNE="no"; shift ;;
        --dir) BACKUP_DIR="${2:?--dir needs a value}"; shift 2 ;;
        --dir=*) BACKUP_DIR="${1#*=}"; shift ;;
        --retain-days) RETAIN_DAYS="${2:?--retain-days needs a value}"; shift 2 ;;
        --retain-days=*) RETAIN_DAYS="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! -d "${BACKUP_DIR}" ]]; then
    echo "ERROR: ${BACKUP_DIR} does not exist." >&2
    echo "  sudo install -d -o fafnir -g fafnir -m 0750 ${BACKUP_DIR}" >&2
    exit 1
fi
if [[ ! -w "${BACKUP_DIR}" ]]; then
    echo "ERROR: ${BACKUP_DIR} is not writable by $(id -un)." >&2
    exit 1
fi

STAMP=$(date -u +%F)
OUT="${BACKUP_DIR}/fafnir_${STAMP}.dump"

echo "==> Dumping ${DB_NAME} to ${OUT}"

# Write to a .partial and rename only on success. Without this, a dump killed by
# a full disk or TimeoutStartSec leaves a truncated file that looks exactly like
# a good one to the retention sweep and to the off-site copy -- and a truncated
# custom-format dump is not partially restorable, it is unrestorable.
TMP="${OUT}.partial"
trap 'rm -f "${TMP}"' EXIT

pg_dump -h "${DB_HOST}" -U "${DB_ROLE}" -d "${DB_NAME}" -Fc -Z"${COMPRESS}" -f "${TMP}"

# pg_restore -l on the finished file is a cheap read of the archive's table of
# contents: it fails loudly on a corrupt or truncated custom-format dump.
pg_restore -l "${TMP}" > /dev/null

mv "${TMP}" "${OUT}"
trap - EXIT
echo "    wrote ${OUT} ($(du -h "${OUT}" | cut -f1))"

if [[ "${WITH_GLOBALS}" == "yes" ]]; then
    GLOBALS="${BACKUP_DIR}/globals_${STAMP}.sql"
    echo "==> Dumping role definitions to ${GLOBALS}"
    pg_dumpall -h "${DB_HOST}" --globals-only > "${GLOBALS}"
    chmod 600 "${GLOBALS}"     # may carry role password hashes
    echo "    wrote ${GLOBALS}"
fi

if [[ "${PRUNE}" == "yes" ]]; then
    echo "==> Expiring dumps older than ${RETAIN_DAYS} days"
    # -print so the log says what went, not just that something did.
    find "${BACKUP_DIR}" -maxdepth 1 -name 'fafnir_*.dump' \
        -mtime "+${RETAIN_DAYS}" -print -delete
    find "${BACKUP_DIR}" -maxdepth 1 -name 'globals_*.sql' \
        -mtime "+${RETAIN_DAYS}" -print -delete
    # Leftovers from a previous interrupted run. Older than a day means no
    # currently-running dump owns it.
    find "${BACKUP_DIR}" -maxdepth 1 -name '*.partial' -mtime +1 -print -delete
fi

echo "==> Dumps on hand:"
ls -lh "${BACKUP_DIR}"/fafnir_*.dump 2>/dev/null || echo "    (none)"

echo "==> backup_dump finished"
