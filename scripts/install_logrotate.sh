#!/usr/bin/env bash
# install_logrotate.sh -- install /etc/logrotate.d/fafnir, per
# doc/install_hetzner.md §10.
#
# The systemd units append to /var/log/fafnir/*.log forever. On a small Hetzner
# instance the log volume is the same filesystem Postgres writes to, so an
# unrotated daily.log eventually stops the database, not just the logging.
#
#   sudo scripts/install_logrotate.sh              # install + dry-run verify
#   scripts/install_logrotate.sh --dry-run         # print the config, install nothing
#   sudo scripts/install_logrotate.sh --rotate 12 --frequency daily
#   sudo scripts/install_logrotate.sh --uninstall
#
# Environment: FAFNIR_LOG_DIR, FAFNIR_USER (defaults /var/log/fafnir, fafnir).
set -euo pipefail

LOG_DIR="${FAFNIR_LOG_DIR:-/var/log/fafnir}"
LOG_USER="${FAFNIR_USER:-fafnir}"
# adm, not the fafnir group: §4.1 creates the log directory fafnir:adm 0750 so
# that operators in adm can read the logs without being able to write them.
LOG_GROUP="${FAFNIR_LOG_GROUP:-adm}"
FREQUENCY="${FAFNIR_LOG_FREQUENCY:-weekly}"
ROTATE="${FAFNIR_LOG_ROTATE:-8}"
CONF="${FAFNIR_LOGROTATE_CONF:-/etc/logrotate.d/fafnir}"
DRY_RUN="no"
UNINSTALL="no"

usage() {
    cat <<'USAGE'
Usage: sudo scripts/install_logrotate.sh [options]

Options:
  --dir PATH         Log directory (default $FAFNIR_LOG_DIR or /var/log/fafnir).
  --frequency FREQ   daily | weekly | monthly (default weekly).
  --rotate N         How many rotations to keep (default 8, i.e. ~2 months
                     weekly).
  --size SIZE        Also rotate whenever a log exceeds SIZE (e.g. 100M),
                     regardless of the frequency. Worth setting if a backfill
                     might produce a very large daily.log between rotations.
  --dry-run          Print the config and stop.
  --uninstall        Remove the config.
  -h, --help         This message.
USAGE
}

SIZE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir) LOG_DIR="${2:?--dir needs a value}"; shift 2 ;;
        --dir=*) LOG_DIR="${1#*=}"; shift ;;
        --frequency) FREQUENCY="${2:?--frequency needs a value}"; shift 2 ;;
        --frequency=*) FREQUENCY="${1#*=}"; shift ;;
        --rotate) ROTATE="${2:?--rotate needs a value}"; shift 2 ;;
        --rotate=*) ROTATE="${1#*=}"; shift ;;
        --size) SIZE="${2:?--size needs a value}"; shift 2 ;;
        --size=*) SIZE="${1#*=}"; shift ;;
        --dry-run|-n) DRY_RUN="yes"; shift ;;
        --uninstall) UNINSTALL="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "${FREQUENCY}" in
    daily|weekly|monthly) ;;
    *) echo "ERROR: --frequency must be daily, weekly or monthly." >&2; exit 2 ;;
esac
if [[ ! "${ROTATE}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --rotate must be a number." >&2; exit 2
fi

if [[ "${DRY_RUN}" == "no" && "${EUID}" -ne 0 ]]; then
    echo "ERROR: this writes ${CONF} -- run it with sudo." >&2
    echo "       (or pass --dry-run to see what it would write)" >&2
    exit 1
fi

if [[ "${UNINSTALL}" == "yes" ]]; then
    echo "==> Removing ${CONF}"
    rm -fv "${CONF}"
    echo "==> Done. Already-rotated ${LOG_DIR}/*.gz files were kept."
    exit 0
fi

# `create`, not `copytruncate`: the units open their log fresh on every start
# (StandardOutput=append:), so there is no long-lived writer holding a stale
# descriptor across the rotation -- and `create` never loses the lines that land
# between a copy and a truncate.
#
# `su` names the directory's owner. logrotate refuses to act on a directory that
# is group-writable or not owned by root unless it is told whose privileges to
# drop to, and §4.1 creates this one as fafnir:adm.
build_config() {
    cat <<CONF_BODY
${LOG_DIR}/*.log {
    ${FREQUENCY}
    rotate ${ROTATE}
${SIZE:+    size ${SIZE}
}    compress
    delaycompress
    missingok
    notifempty
    create 0640 ${LOG_USER} ${LOG_GROUP}
    su ${LOG_USER} ${LOG_GROUP}
}
CONF_BODY
}

if [[ "${DRY_RUN}" == "yes" ]]; then
    echo "----- ${CONF} -----"
    build_config
    echo ""
    echo "==> Dry run: nothing was written."
    exit 0
fi

if [[ ! -d "${LOG_DIR}" ]]; then
    echo "!!  ${LOG_DIR} does not exist yet. Creating it (${LOG_USER}:${LOG_GROUP}, 0750)."
    install -d -o "${LOG_USER}" -g "${LOG_GROUP}" -m 0750 "${LOG_DIR}"
fi

if ! command -v logrotate >/dev/null 2>&1; then
    echo "!!  logrotate is not installed: sudo apt-get install -y logrotate" >&2
fi

echo "==> Writing ${CONF}"
build_config > "${CONF}"
chmod 0644 "${CONF}"
sed 's/^/    /' "${CONF}"

if command -v logrotate >/dev/null 2>&1; then
    echo ""
    echo "==> Verifying (logrotate --debug: parses and reports, changes nothing)"
    logrotate --debug "${CONF}" 2>&1 | sed 's/^/    /'
fi

cat <<NEXT

==> Installed. Rotation runs from the system logrotate timer/cron; nothing else
    to enable. Useful follow-ups:
      systemctl list-timers logrotate.timer     # when it next runs
      sudo logrotate --force ${CONF}   # rotate now, for real
      cat /var/lib/logrotate/status | grep fafnir
NEXT
