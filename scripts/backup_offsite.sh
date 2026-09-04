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
#                           (u123456 is a PLACEHOLDER -- substitute the Storage
#                           Box username from the Hetzner console)
#   FAFNIR_BACKUP_SSH_PORT  ssh port; defaults to 23 for *.your-storagebox.de,
#                           else ssh's own default
#   FAFNIR_BACKUP_SSH_OPTS  extra ssh options (default: -o BatchMode=yes)
#   FAFNIR_BACKUP_KNOWN_HOSTS  known_hosts file (default ~/.ssh/known_hosts)
#   FAFNIR_BACKUP_MIRROR    "yes" (default) mirrors the local retention window
#                           with --delete; "no" lets the remote accumulate.
#
# ## Hetzner Storage Box: port 23, not 22
#
# A Storage Box answers on both, with different services:
#   port 22 -> SSH-2.0-mod_sftp       SFTP only. Cannot execute a remote binary.
#   port 23 -> SSH-2.0-OpenSSH_9.6p1  a real shell.
# rsync works by running an rsync process on the far end, so it needs 23. On 22
# it fails after the handshake with no useful message. The two ports also present
# DIFFERENT host keys, which is why known_hosts entries here are port-qualified.
#
# ## First run, by hand
#
# The timer runs with ProtectHome=read-only and BatchMode=yes: it can neither
# accept an unknown host key nor write known_hosts. Do that once, interactively,
# in this order -- the host key comes FIRST, because ssh-copy-id needs it too:
#
#   sudo -u fafnir -H ssh-keygen -t ed25519 -N '' -f ~fafnir/.ssh/id_ed25519
#   sudo -u fafnir -H /opt/fafnir/scripts/backup_offsite.sh \
#       --remote u123456@u123456.your-storagebox.de:/home/fafnir-backups/ \
#       --accept-host-key --dry-run
#   sudo -u fafnir -H ssh-copy-id -s -p 23 u123456@u123456.your-storagebox.de
set -euo pipefail

BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"
REMOTE="${FAFNIR_BACKUP_REMOTE:-}"
SSH_OPTS="${FAFNIR_BACKUP_SSH_OPTS:--o BatchMode=yes}"
SSH_PORT="${FAFNIR_BACKUP_SSH_PORT:-}"
MIRROR="${FAFNIR_BACKUP_MIRROR:-yes}"
DRY_RUN="no"
ACCEPT_HOST_KEY="no"

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
  --port N       ssh port (default 23 for *.your-storagebox.de, else ssh's own).
  --accept-host-key
                 Trust the remote's host key and record it in known_hosts, after
                 printing its fingerprint. Interactive first-run use only -- this
                 is a trust-on-first-use decision, so compare the fingerprint
                 against the one in the Hetzner console before relying on it.
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
        --port) SSH_PORT="${2:?--port needs a value}"; shift 2 ;;
        --port=*) SSH_PORT="${1#*=}"; shift ;;
        --accept-host-key) ACCEPT_HOST_KEY="yes"; shift ;;
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
# rsync only treats a destination as remote if it contains a colon. Without one,
# "u123456@u123456.your-storagebox.de" is a LOCAL RELATIVE PATH: rsync would
# cheerfully create a directory of that literal name in the working directory,
# copy the dumps into it, and exit 0. The backup would report success every night
# and never leave the server -- which you would discover only after losing it.
if [[ "${REMOTE}" != *:* ]]; then
    # An absolute path is an unambiguous, deliberate local destination -- a
    # mounted volume, or a test. Anything else without a colon is a mistyped
    # remote, and that one is dangerous rather than merely wrong.
    if [[ "${REMOTE}" == /* ]]; then
        echo "==> NOTE: ${REMOTE} is a local path. This copy does not leave the server;"
        echo "    it only protects you from losing the database, not the machine."
    else
        echo "ERROR: the destination has no ':path', so rsync would treat it as a LOCAL" >&2
        echo "       directory name and the backup would never leave this server --" >&2
        echo "       silently, exiting 0 every night, until you needed it." >&2
        echo "         got:      ${REMOTE}" >&2
        echo "         expected: ${REMOTE}:/home/fafnir-backups/" >&2
        echo "       (a remote destination is user@host:/path -- the colon is what" >&2
        echo "        makes it remote; pass an absolute path for a deliberate local copy)" >&2
        exit 2
    fi
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

# ------------------------------------------------------------- ssh setup ----
# Pull the ssh host out of an rsync destination. Returns 1 for a local path,
# which involves no ssh and so needs no host key.
remote_host() {
    local dest="$1" hostpart
    dest="${dest#rsync://}"
    case "${dest}" in
        \[*\]:*)  hostpart="${dest%%]:*}"; hostpart="${hostpart#[}" ;;   # [ipv6]:path
        *:*)      hostpart="${dest%%:*}"
                  # A colon that appears after a slash is part of a local path
                  # (/var/backups/odd:name), not a host separator.
                  [[ "${hostpart}" == */* ]] && return 1 ;;
        *)        return 1 ;;
    esac
    hostpart="${hostpart##*@}"
    [[ -n "${hostpart}" ]] || return 1
    printf '%s' "${hostpart}"
}

SSH_HOST="$(remote_host "${REMOTE}" || true)"

if [[ -n "${SSH_HOST}" ]]; then
    # A Storage Box answers on 22 with SFTP-only mod_sftp, which cannot execute
    # the remote rsync binary that rsync-over-ssh depends on. The real OpenSSH is
    # on 23. Defaulting this is worth the special case: on 22 the failure comes
    # after a successful handshake and says nothing useful.
    if [[ -z "${SSH_PORT}" && "${SSH_HOST}" == *.your-storagebox.de ]]; then
        SSH_PORT=23
        echo "==> Storage Box detected: using ssh port 23 (22 is SFTP-only, rsync needs a shell)"
    fi
fi

# Name the known_hosts file explicitly rather than letting ssh find it. ssh and
# ssh-keygen resolve ~ from the passwd entry, not from $HOME, so under a sudo
# that did not pass -H the preflight below would happily inspect one file while
# rsync's ssh consulted another -- and report a key as trusted that ssh then
# rejects. Being explicit makes the check and the connection agree by construction.
KNOWN_HOSTS="${FAFNIR_BACKUP_KNOWN_HOSTS:-${HOME}/.ssh/known_hosts}"
GLOBAL_KNOWN_HOSTS=/etc/ssh/ssh_known_hosts

SSH_CMD="ssh ${SSH_OPTS} -o UserKnownHostsFile=${KNOWN_HOSTS} -o GlobalKnownHostsFile=${GLOBAL_KNOWN_HOSTS}"
[[ -n "${SSH_PORT}" ]] && SSH_CMD="${SSH_CMD} -p ${SSH_PORT}"

# known_hosts keys a non-default port as [host]:port, and the two ports on a
# Storage Box really do present different keys -- so look up the one we will use.
if [[ -n "${SSH_HOST}" ]]; then
    if [[ -n "${SSH_PORT}" && "${SSH_PORT}" != "22" ]]; then
        KNOWN_HOSTS_KEY="[${SSH_HOST}]:${SSH_PORT}"
    else
        KNOWN_HOSTS_KEY="${SSH_HOST}"
    fi

    host_key_known() {
        local f
        for f in "${KNOWN_HOSTS}" "${GLOBAL_KNOWN_HOSTS}"; do
            [[ -r "${f}" ]] || continue
            ssh-keygen -f "${f}" -F "${KNOWN_HOSTS_KEY}" >/dev/null 2>&1 && return 0
        done
        return 1
    }

    if ! host_key_known; then
        if [[ "${ACCEPT_HOST_KEY}" == "yes" ]]; then
            echo "==> Host key for ${KNOWN_HOSTS_KEY} is not yet trusted. Fingerprints:"
            ssh-keyscan ${SSH_PORT:+-p "${SSH_PORT}"} "${SSH_HOST}" 2>/dev/null \
                | ssh-keygen -lf - | sed 's/^/    /'
            echo "    Compare these against the Hetzner console before relying on them."
            mkdir -p "$(dirname "${KNOWN_HOSTS}")"
            chmod 700 "$(dirname "${KNOWN_HOSTS}")"
            ssh-keyscan -H ${SSH_PORT:+-p "${SSH_PORT}"} "${SSH_HOST}" 2>/dev/null \
                >> "${KNOWN_HOSTS}"
            chmod 600 "${KNOWN_HOSTS}"
            if ! host_key_known; then
                echo "ERROR: ssh-keyscan returned nothing for ${KNOWN_HOSTS_KEY}." >&2
                echo "       Check the hostname and that port ${SSH_PORT:-22} is reachable." >&2
                exit 1
            fi
            echo "    Recorded in ${KNOWN_HOSTS}"
        else
            cat >&2 <<UNTRUSTED
ERROR: the host key for ${KNOWN_HOSTS_KEY} is not in known_hosts, and ssh runs
       here with BatchMode=yes, so it cannot stop and ask. rsync would fail with
       "Host key verification failed" and exit 255.

       This is trust-on-first-use, so it is a decision to make by hand, once, as
       the service user -- never from the timer, which runs ProtectHome=read-only
       and could not write known_hosts anyway:

         sudo -u fafnir -H $0 \\
             --remote '${REMOTE}' --accept-host-key --dry-run

       That prints the key's fingerprint for you to compare against the Hetzner
       console, then records it. Or do it yourself:

         sudo -u fafnir -H bash -c 'ssh-keyscan -H ${SSH_PORT:+-p ${SSH_PORT} }${SSH_HOST} >> ${KNOWN_HOSTS}'
UNTRUSTED
            exit 1
        fi
    fi
fi

RSYNC_ARGS=(-a --human-readable --stats -e "${SSH_CMD}")
[[ "${MIRROR}" == "yes" ]] && RSYNC_ARGS+=(--delete)
[[ "${DRY_RUN}" == "yes" ]] && RSYNC_ARGS+=(--dry-run)
# A dump still being written is a .partial (see backup_dump.sh); never ship one.
RSYNC_ARGS+=(--exclude '*.partial')

# BACKUP_DIR is normally a mount point of its own, and every ext4 filesystem has
# a root-owned, mode-0700 lost+found at its root. rsync runs as fafnir, cannot
# opendir it, and reports that as a sending-side I/O error. Two consequences,
# and the second is the dangerous one:
#   - the run exits 23 ("partial transfer due to error") and the unit fails,
#     even though every dump copied fine;
#   - an I/O error on the sending side makes rsync silently DISABLE --delete
#     (see rsync(1) under --delete), so the remote quietly stops honouring the
#     retention window and grows without bound until it hits the Storage Box
#     quota -- while each night still looks like a transfer that happened.
# The leading slash anchors this to the transfer root, so it excludes only the
# filesystem's own lost+found and not some future dump directory named that.
RSYNC_ARGS+=(--exclude '/lost+found')

echo "==> Copying ${#DUMPS[@]} dump(s) from ${BACKUP_DIR} to ${REMOTE}"
[[ -n "${SSH_PORT}" ]] && echo "    over ssh port ${SSH_PORT}"
[[ "${MIRROR}" == "yes" ]] && echo "    mirroring (--delete): the remote matches local retention"
[[ "${DRY_RUN}" == "yes" ]] && echo "    DRY RUN -- nothing will be transferred"

# Trailing slash on the source: copy the directory's CONTENTS into the remote
# path, not the directory itself into a nested fafnir/ under it.
# `rc=$?` after `if ! rsync ...` would read the status the `!` already inverted --
# i.e. 0 on failure -- so the hint below would never fire and, worse, the script
# would exit 0 on a backup that did not happen. Capture it with `|| rc=$?`, which
# both preserves the real status and keeps `set -e` from killing us first.
rc=0
rsync "${RSYNC_ARGS[@]}" "${BACKUP_DIR}/" "${REMOTE}" || rc=$?
if [[ ${rc} -ne 0 ]]; then
    if [[ ${rc} -eq 255 ]]; then
        cat >&2 <<HINT

!!  rsync exit 255 is an ssh-layer failure, not a transfer failure. The usual
    causes, in order:
      - host key not trusted        -> re-run with --accept-host-key
      - public key not installed    -> ssh-copy-id -s ${SSH_PORT:+-p ${SSH_PORT} }<user>@${SSH_HOST:-<host>}
      - wrong port                  -> a Storage Box needs 23; 22 is SFTP-only
      - placeholder username        -> 'u123456' is an example, not your box
HINT
    fi
    exit ${rc}
fi

echo "==> backup_offsite finished"
