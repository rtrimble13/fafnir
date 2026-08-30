#!/usr/bin/env bash
# backup_offsite.sh -- push the local dump directory to off-server storage.
#
# doc/install_hetzner.md §9.2. A dump sitting on the same disk as the database
# does not survive losing the server; this is the step that turns §9.1 into an
# actual backup. Target a Hetzner Storage Box over SFTP/rsync, or any other host
# reachable by key-based ssh.
#
# Environment:
#   FAFNIR_BACKUP_DIR       local dump directory (default /var/backups/fafnir)
#   FAFNIR_BACKUP_REMOTE    rsync destination, REQUIRED, e.g.
#                           u123456@u123456.your-storagebox.de:/home/fafnir-backups/
#   FAFNIR_BACKUP_SSH_OPTS  ssh options (default: -o BatchMode=yes)
#   FAFNIR_BACKUP_MIRROR    "yes" (default) mirrors the local retention window
#                           with --delete; "no" lets the remote accumulate.
#
# Before enabling the timer, run this once by hand as the service user. The
# service runs with ProtectHome=read-only and BatchMode=yes, so it cannot accept
# an unknown host key or answer a passphrase prompt -- do both now, interactively:
#
#   sudo -u fafnir -H ssh-keygen -t ed25519 -N '' -f ~fafnir/.ssh/id_ed25519
#   sudo -u fafnir -H ssh-copy-id -s u123456@u123456.your-storagebox.de   # -s: Storage Box
#   sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; \
#       FAFNIR_BACKUP_REMOTE=... /opt/fafnir/scripts/backup_offsite.sh'
set -euo pipefail

BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"
REMOTE="${FAFNIR_BACKUP_REMOTE:-}"
SSH_OPTS="${FAFNIR_BACKUP_SSH_OPTS:--o BatchMode=yes}"
MIRROR="${FAFNIR_BACKUP_MIRROR:-yes}"
DRY_RUN="no"

usage() {
    cat <<'USAGE'
Usage: scripts/backup_offsite.sh [--remote DEST] [--dir PATH]
                                 [--no-mirror] [--dry-run]

Options:
  --remote DEST  rsync destination (default $FAFNIR_BACKUP_REMOTE).
                 e.g. u123456@u123456.your-storagebox.de:/home/fafnir-backups/
  --dir PATH     Local dump directory (default $FAFNIR_BACKUP_DIR or
                 /var/backups/fafnir).
  --no-mirror    Do not pass --delete: the remote keeps dumps that have already
                 expired locally. Costs remote space, buys a longer window.
  --dry-run      rsync --dry-run: list what would transfer, send nothing.
  -h, --help     This message.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote) REMOTE="${2:?--remote needs a value}"; shift 2 ;;
        --remote=*) REMOTE="${1#*=}"; shift ;;
        --dir) BACKUP_DIR="${2:?--dir needs a value}"; shift 2 ;;
        --dir=*) BACKUP_DIR="${1#*=}"; shift ;;
        --no-mirror) MIRROR="no"; shift ;;
        --dry-run|-n) DRY_RUN="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "${REMOTE}" ]]; then
    echo "ERROR: no destination. Set FAFNIR_BACKUP_REMOTE or pass --remote." >&2
    echo "  e.g. u123456@u123456.your-storagebox.de:/home/fafnir-backups/" >&2
    exit 2
fi
if [[ ! -d "${BACKUP_DIR}" ]]; then
    echo "ERROR: ${BACKUP_DIR} does not exist -- run scripts/backup_dump.sh first." >&2
    exit 1
fi

# The guard that makes --delete safe to automate. If tonight's dump failed and
# the retention sweep has since emptied the directory, a mirroring rsync would
# faithfully propagate "nothing" and wipe the off-site copy -- destroying the
# backup precisely when the local one is already gone. An empty source is never
# a legitimate state here, so refuse rather than sync.
shopt -s nullglob
DUMPS=("${BACKUP_DIR}"/fafnir_*.dump)
shopt -u nullglob
if [[ ${#DUMPS[@]} -eq 0 ]]; then
    echo "ERROR: no fafnir_*.dump in ${BACKUP_DIR}. Refusing to sync an empty" >&2
    echo "       directory -- with --delete that would erase the off-site copy." >&2
    echo "       Check the last dump: journalctl -u fafnir-dump.service -n 50" >&2
    exit 1
fi

RSYNC_ARGS=(-a --human-readable --stats -e "ssh ${SSH_OPTS}")
[[ "${MIRROR}" == "yes" ]] && RSYNC_ARGS+=(--delete)
[[ "${DRY_RUN}" == "yes" ]] && RSYNC_ARGS+=(--dry-run)
# A dump still being written is a .partial (see backup_dump.sh); never ship one.
RSYNC_ARGS+=(--exclude '*.partial')

echo "==> Copying ${#DUMPS[@]} dump(s) from ${BACKUP_DIR} to ${REMOTE}"
[[ "${MIRROR}" == "yes" ]] && echo "    mirroring (--delete): the remote matches local retention"
[[ "${DRY_RUN}" == "yes" ]] && echo "    DRY RUN -- nothing will be transferred"

# Trailing slash on the source: copy the directory's CONTENTS into the remote
# path, not the directory itself into a nested fafnir/ under it.
rsync "${RSYNC_ARGS[@]}" "${BACKUP_DIR}/" "${REMOTE}"

echo "==> backup_offsite finished"
