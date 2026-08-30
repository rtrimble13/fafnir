#!/usr/bin/env bash
# install_timers.sh -- install the fafnir systemd timers described in
# doc/install_hetzner.md §8-§9.
#
# Generates five service/timer pairs from the templates in etc/systemd/,
# verifies them, and enables the timers:
#
#   fafnir-daily            Mon-Fri 22:30 ET  scripts/daily_update.sh
#   fafnir-dq               Mon-Fri 23:00 ET  scripts/run_dq_checks.sh
#   fafnir-reconcile        Sun     06:00 ET  scripts/reconcile.sh
#   fafnir-dump             Mon-Sat 04:00 ET  scripts/backup_dump.sh
#   fafnir-backup-offsite   Mon-Sat 04:45 ET  scripts/backup_offsite.sh
#
# Every OnCalendar carries an explicit America/New_York. That is the point of
# using timers rather than the fixed-UTC crontab: the host clock stays UTC (§2.1)
# but the data settles on market time, which moves with DST. Cron would drift an
# hour twice a year and the weekday would shift, because a US evening slot is the
# next day in UTC.
#
#   sudo scripts/install_timers.sh --dry-run          # print the units, install nothing
#   sudo scripts/install_timers.sh                    # install + enable everything
#   sudo scripts/install_timers.sh daily dq           # just those two
#   sudo scripts/install_timers.sh --no-enable        # install, leave timers stopped
#   sudo scripts/install_timers.sh --uninstall        # disable + remove
#
# FINISH THE FULL BACKFILL (§7) BEFORE ENABLING fafnir-daily. The nightly
# `ingest securities` step pulls the rest of the universe on its first run, and
# since none of those securities has a price watermark, the price step that
# follows backfills full history for ~21k of them in one unattended pass --
# many hours and several GB against the 50 GB/month FMP budget, outside the
# resumable chunked path §7 gives you.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAFNIR_HOME="${FAFNIR_HOME:-/opt/fafnir}"
FAFNIR_USER="${FAFNIR_USER:-fafnir}"
FAFNIR_GROUP="${FAFNIR_GROUP:-fafnir}"
ENV_FILE="${FAFNIR_ENV_FILE:-/etc/fafnir/fafnir.env}"
LOG_DIR="${FAFNIR_LOG_DIR:-/var/log/fafnir}"
BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"
BACKUP_RETAIN_DAYS="${FAFNIR_BACKUP_RETAIN_DAYS:-14}"
BACKUP_REMOTE="${FAFNIR_BACKUP_REMOTE:-}"
DQ_EXCHANGE="${FAFNIR_DQ_EXCHANGE:-NASDAQ}"
RECONCILE_SYMBOLS="${FAFNIR_RECONCILE_SYMBOLS:-AAPL,MSFT,SPY,BRK-B}"

UNIT_DIR="${FAFNIR_UNIT_DIR:-/etc/systemd/system}"
TEMPLATE_DIR="${REPO_ROOT}/etc/systemd"

# unit name -> default OnCalendar / RandomizedDelaySec
declare -A SCHEDULE=(
    [fafnir-daily]="Mon..Fri 22:30 America/New_York"
    [fafnir-dq]="Mon..Fri 23:00 America/New_York"
    [fafnir-reconcile]="Sun 06:00 America/New_York"
    [fafnir-dump]="Mon..Sat 04:00 America/New_York"
    [fafnir-backup-offsite]="Mon..Sat 04:45 America/New_York"
)
declare -A JITTER=(
    [fafnir-daily]=300
    [fafnir-dq]=300
    [fafnir-reconcile]=900
    [fafnir-dump]=300
    [fafnir-backup-offsite]=600
)
# Short name (what you type) -> unit name.
declare -A UNIT_OF=(
    [daily]=fafnir-daily
    [dq]=fafnir-dq
    [reconcile]=fafnir-reconcile
    [dump]=fafnir-dump
    [offsite]=fafnir-backup-offsite
)
ALL_SHORT=(daily dq reconcile dump offsite)

DRY_RUN="no"
DO_ENABLE="yes"
UNINSTALL="no"
FORCE="no"

usage() {
    cat <<'USAGE'
Usage: sudo scripts/install_timers.sh [JOB...] [options]

Jobs (default: all):
  daily      fafnir-daily            Mon-Fri 22:30 ET  incremental update
  dq         fafnir-dq               Mon-Fri 23:00 ET  data-quality sweep
  reconcile  fafnir-reconcile        Sun     06:00 ET  weekly reconciliation
  dump       fafnir-dump             Mon-Sat 04:00 ET  nightly logical dump
  offsite    fafnir-backup-offsite   Mon-Sat 04:45 ET  off-server copy

Options:
  --dry-run       Print the generated units and stop. Touches nothing.
  --no-enable     Install and daemon-reload, but leave the timers stopped.
  --uninstall     Stop, disable and remove the selected units.
  --force         Install even if the preflight checks fail.
  -h, --help      This message.

Configuration (environment variables, all with the defaults from
doc/install_hetzner.md):
  FAFNIR_HOME=/opt/fafnir             checkout the units run from
  FAFNIR_USER=fafnir  FAFNIR_GROUP=fafnir
  FAFNIR_ENV_FILE=/etc/fafnir/fafnir.env
  FAFNIR_LOG_DIR=/var/log/fafnir
  FAFNIR_BACKUP_DIR=/var/backups/fafnir
  FAFNIR_BACKUP_RETAIN_DAYS=14
  FAFNIR_BACKUP_REMOTE=               required by the offsite job, e.g.
                                      u123456@u123456.your-storagebox.de:/home/fafnir-backups/
  FAFNIR_DQ_EXCHANGE=NASDAQ
  FAFNIR_RECONCILE_SYMBOLS=AAPL,MSFT,SPY,BRK-B
  FAFNIR_UNIT_DIR=/etc/systemd/system

Change a schedule after installation with a drop-in, not by editing the
generated unit (the next run of this script overwrites it):

  sudo systemctl edit fafnir-daily.timer
  [Timer]
  OnCalendar=
  OnCalendar=Mon..Fri 23:15 America/New_York

The empty OnCalendar= first is required -- timer settings are additive, so
without it the job runs at BOTH times.
USAGE
}

SELECTED=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n) DRY_RUN="yes"; shift ;;
        --no-enable) DO_ENABLE="no"; shift ;;
        --uninstall) UNINSTALL="yes"; shift ;;
        --force) FORCE="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) SELECTED+=("$1"); shift ;;
    esac
done
[[ ${#SELECTED[@]} -eq 0 ]] && SELECTED=("${ALL_SHORT[@]}")

for job in "${SELECTED[@]}"; do
    if [[ -z "${UNIT_OF[${job}]:-}" ]]; then
        echo "Unknown job: ${job}" >&2
        echo "Known: ${ALL_SHORT[*]}" >&2
        exit 2
    fi
done

if [[ "${DRY_RUN}" == "no" && "${EUID}" -ne 0 ]]; then
    echo "ERROR: this writes to ${UNIT_DIR} -- run it with sudo." >&2
    echo "       (or pass --dry-run to see what it would install)" >&2
    exit 1
fi

# --------------------------------------------------------------- generate ----
# Placeholder substitution, done with bash parameter expansion rather than sed or
# awk. Both of those give the replacement text its own metacharacters -- '/' and
# '&' -- and every value here is a filesystem path or an rsync destination, so a
# perfectly ordinary value could otherwise corrupt the unit it lands in. Bash
# replacement is literal.
render() {
    local template="$1" unit="$2" line
    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line//@FAFNIR_HOME@/${FAFNIR_HOME}}"
        line="${line//@FAFNIR_USER@/${FAFNIR_USER}}"
        line="${line//@FAFNIR_GROUP@/${FAFNIR_GROUP}}"
        line="${line//@ENV_FILE@/${ENV_FILE}}"
        line="${line//@LOG_DIR@/${LOG_DIR}}"
        line="${line//@ONCALENDAR@/${SCHEDULE[${unit}]}}"
        line="${line//@RANDOMIZED_DELAY@/${JITTER[${unit}]}}"
        line="${line//@BACKUP_DIR@/${BACKUP_DIR}}"
        line="${line//@BACKUP_RETAIN_DAYS@/${BACKUP_RETAIN_DAYS}}"
        line="${line//@BACKUP_REMOTE@/${BACKUP_REMOTE}}"
        line="${line//@DQ_EXCHANGE@/${DQ_EXCHANGE}}"
        line="${line//@RECONCILE_SYMBOLS@/${RECONCILE_SYMBOLS}}"
        printf '%s\n' "${line}"
    done < "${template}"
}

# -------------------------------------------------------------- uninstall ----
if [[ "${UNINSTALL}" == "yes" ]]; then
    for job in "${SELECTED[@]}"; do
        unit="${UNIT_OF[${job}]}"
        echo "==> Removing ${unit}"
        systemctl disable --now "${unit}.timer" 2>/dev/null || true
        systemctl stop "${unit}.service" 2>/dev/null || true
        rm -fv "${UNIT_DIR}/${unit}.service" "${UNIT_DIR}/${unit}.timer"
    done
    systemctl daemon-reload
    echo "==> Done. Logs under ${LOG_DIR} and dumps under ${BACKUP_DIR} were kept."
    exit 0
fi

# -------------------------------------------------------------- preflight ----
PROBLEMS=0
# Takes the test as a COMMAND, not as a pre-evaluated $?: under `set -e` a bare
# failing `[[ ... ]]` on its own line would abort the script instead of counting
# a problem, which is the opposite of what a preflight should do.
check() {                    # check <message> <command...>
    local msg="$1"; shift
    if ! "$@" >/dev/null 2>&1; then
        echo "!!  ${msg}" >&2
        PROBLEMS=$((PROBLEMS + 1))
    fi
}
echo "==> Preflight"
[[ -d "${TEMPLATE_DIR}" ]] || { echo "ERROR: no templates at ${TEMPLATE_DIR}" >&2; exit 1; }

check "service user '${FAFNIR_USER}' does not exist (install_hetzner.md §4.1)" \
    id "${FAFNIR_USER}"
check "${FAFNIR_HOME} does not exist (§4.1)" test -d "${FAFNIR_HOME}"
check "${ENV_FILE} is missing or unreadable (§4.4)" test -r "${ENV_FILE}"
check "${LOG_DIR} does not exist: sudo install -d -o ${FAFNIR_USER} -g adm -m 0750 ${LOG_DIR}" \
    test -d "${LOG_DIR}"

for job in "${SELECTED[@]}"; do
    case "${job}" in
        daily)     script="daily_update.sh" ;;
        dq)        script="run_dq_checks.sh" ;;
        reconcile) script="reconcile.sh" ;;
        dump)      script="backup_dump.sh" ;;
        offsite)   script="backup_offsite.sh" ;;
    esac
    check "${FAFNIR_HOME}/scripts/${script} is missing or not executable" \
        test -x "${FAFNIR_HOME}/scripts/${script}"
done

# The offsite job with no destination would install cleanly and then fail every
# night, which is the worst kind of backup: one you believe in.
if [[ " ${SELECTED[*]} " == *" offsite "* && -z "${BACKUP_REMOTE}" ]]; then
    check "the offsite job needs FAFNIR_BACKUP_REMOTE (see scripts/backup_offsite.sh)" false
fi
if [[ " ${SELECTED[*]} " == *" dump "* && ! -d "${BACKUP_DIR}" ]]; then
    echo "    ${BACKUP_DIR} does not exist -- creating it."
    [[ "${DRY_RUN}" == "no" ]] && \
        install -d -o "${FAFNIR_USER}" -g "${FAFNIR_GROUP}" -m 0750 "${BACKUP_DIR}"
fi

if [[ "${PROBLEMS}" -gt 0 ]]; then
    if [[ "${FORCE}" == "yes" ]]; then
        echo "    ${PROBLEMS} problem(s), continuing because --force was given."
    else
        echo "ERROR: ${PROBLEMS} preflight problem(s). Fix them, or pass --force." >&2
        exit 1
    fi
else
    echo "    ok"
fi

# ---------------------------------------------------------------- install ----
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

for job in "${SELECTED[@]}"; do
    unit="${UNIT_OF[${job}]}"
    for kind in service timer; do
        tpl="${TEMPLATE_DIR}/${unit}.${kind}.in"
        [[ -f "${tpl}" ]] || { echo "ERROR: missing template ${tpl}" >&2; exit 1; }
        render "${tpl}" "${unit}" > "${STAGE}/${unit}.${kind}"
    done
    if grep -q '@[A-Z_]*@' "${STAGE}/${unit}".{service,timer}; then
        echo "ERROR: unsubstituted placeholder left in ${unit}:" >&2
        grep -Hn '@[A-Z_]*@' "${STAGE}/${unit}".{service,timer} >&2
        exit 1
    fi
done

if [[ "${DRY_RUN}" == "yes" ]]; then
    for f in "${STAGE}"/*; do
        echo ""
        echo "----- ${UNIT_DIR}/$(basename "${f}") -----"
        cat "${f}"
    done
    echo ""
    echo "==> Dry run: nothing was installed."
    exit 0
fi

# Verify before installing. systemd-analyze needs the files where it can resolve
# them by name, so verify from the staging directory.
if command -v systemd-analyze >/dev/null 2>&1; then
    echo "==> Verifying units"
    # Non-fatal: verify also complains about units this host legitimately does
    # not have (postgresql.service on a remote-database host, say).
    (cd "${STAGE}" && systemd-analyze verify ./*.service ./*.timer) || \
        echo "    (systemd-analyze raised warnings above -- review them)"
fi

echo "==> Installing into ${UNIT_DIR}"
install -o root -g root -m 0644 "${STAGE}"/* "${UNIT_DIR}/"
ls -1 "${STAGE}" | sed 's/^/    /'

systemctl daemon-reload

if [[ "${DO_ENABLE}" == "yes" ]]; then
    echo "==> Enabling timers"
    for job in "${SELECTED[@]}"; do
        systemctl enable --now "${UNIT_OF[${job}]}.timer"
    done
else
    echo "==> Timers installed but NOT enabled (--no-enable). Start them with:"
    for job in "${SELECTED[@]}"; do
        echo "    sudo systemctl enable --now ${UNIT_OF[${job}]}.timer"
    done
fi

echo ""
systemctl list-timers 'fafnir-*' --all --no-pager || true

cat <<NEXT

==> Next
  Confirm a schedule without waiting for it:
    systemd-analyze calendar "${SCHEDULE[fafnir-daily]}"
  Run a unit now, rather than waiting for its timer:
    sudo systemctl start fafnir-daily.service
    sudo systemctl status fafnir-daily.service
    tail -40 ${LOG_DIR}/daily.log
  Rotate those logs so they cannot fill the disk:
    sudo scripts/install_logrotate.sh
  Check on the whole thing tomorrow morning:
    ${FAFNIR_HOME}/scripts/monitor.sh
NEXT
