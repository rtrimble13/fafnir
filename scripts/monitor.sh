#!/usr/bin/env bash
# monitor.sh -- one pass over everything doc/install_hetzner.md §10 tells you to
# watch, in one command. Read-only: it reports, it never changes anything.
#
# The §10 checks are spread across the fafnir CLI, the OS and psql, which makes
# them easy to half-do at 8am. This runs the lot and exits non-zero if any check
# tripped, so it also works as the body of a health probe or an alerting cron.
#
#   scripts/monitor.sh                    # every section
#   scripts/monitor.sh disk timers        # just those two
#   scripts/monitor.sh --list             # what the section names are
#   scripts/monitor.sh --quiet            # only the summary + anything that tripped
#
# Sourcing the env file: §10 runs each check as
#   sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; ...'
# so this script does that for you when FAFNIR_DSN is not already set and the
# file is readable. Under systemd the EnvironmentFile has already provided it and
# nothing is sourced.
set -uo pipefail          # NOT -e: a failing check must be reported, not fatal

ENV_FILE="${FAFNIR_ENV_FILE:-/etc/fafnir/fafnir.env}"
BACKUP_DIR="${FAFNIR_BACKUP_DIR:-/var/backups/fafnir}"
LOG_DIR="${FAFNIR_LOG_DIR:-/var/log/fafnir}"
DISK_WARN_PCT="${FAFNIR_DISK_WARN_PCT:-85}"
BACKUP_MAX_AGE_H="${FAFNIR_BACKUP_MAX_AGE_HOURS:-36}"
JOURNAL_SINCE="${FAFNIR_JOURNAL_SINCE:-2 days ago}"
QUIET="no"

ALL_SECTIONS=(status dq disk timers journal backups runs bandwidth slow)

usage() {
    cat <<'USAGE'
Usage: scripts/monitor.sh [SECTION...] [options]

Sections (default: all, in this order):
  status     `fafnir status` -- securities, price rows, latest date, renames,
             New (7d), open DQ count.
  dq         `fafnir dq list` -- the open quality queue by check and severity.
  disk       Free space on the Postgres data directory and the log/backup
             volumes. An out-of-space Postgres stops writing.
  timers     `systemctl list-timers 'fafnir-*'` plus any failed fafnir unit --
             did last night actually run?
  journal    Recent journal lines for the fafnir units.
  backups    Newest dump in the backup directory, and its age.
  runs       ops.ingestion_run rows in status failed/partial.
  bandwidth  bytes_downloaded per month against the 50 GB FMP budget.
  slow       Top queries by total time from pg_stat_statements.

Options:
  --list             Print the section names and exit.
  --quiet, -q        Suppress the healthy output; show only problems + summary.
  --env-file PATH    Env file to source when FAFNIR_DSN is unset
                     (default /etc/fafnir/fafnir.env).
  --since WHEN       journalctl --since window (default "2 days ago").
  -h, --help         This message.

Exit status: 0 if every section that ran was clean, 1 if any tripped.
USAGE
}

SECTIONS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --list) printf '%s\n' "${ALL_SECTIONS[@]}"; exit 0 ;;
        --quiet|-q) QUIET="yes"; shift ;;
        --env-file) ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
        --env-file=*) ENV_FILE="${1#*=}"; shift ;;
        --since) JOURNAL_SINCE="${2:?--since needs a value}"; shift 2 ;;
        --since=*) JOURNAL_SINCE="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) SECTIONS+=("$1"); shift ;;
    esac
done
[[ ${#SECTIONS[@]} -eq 0 ]] && SECTIONS=("${ALL_SECTIONS[@]}")

for s in "${SECTIONS[@]}"; do
    # shellcheck disable=SC2076
    if [[ ! " ${ALL_SECTIONS[*]} " =~ " ${s} " ]]; then
        echo "Unknown section: ${s}" >&2
        echo "Known: ${ALL_SECTIONS[*]}" >&2
        exit 2
    fi
done

if [[ -z "${FAFNIR_DSN:-}" && -r "${ENV_FILE}" ]]; then
    set -a; . "${ENV_FILE}"; set +a
fi

PROBLEMS=()
say()  { [[ "${QUIET}" == "yes" ]] || echo "$@"; }
head_() { say ""; say "==> $*"; }
warn() { echo "!!  $*"; PROBLEMS+=("$*"); }

# psql against FAFNIR_DSN. Returns 1 (without spewing) when there is no DSN, so
# the SQL sections can skip themselves instead of failing.
psql_q() {
    [[ -n "${FAFNIR_DSN:-}" ]] || return 1
    psql "${FAFNIR_DSN}" -X -q -v ON_ERROR_STOP=1 "$@"
}

# ---------------------------------------------------------------- status ----
sec_status() {
    head_ "fafnir status"
    local out
    if ! out=$(fafnir status 2>&1); then
        warn "fafnir status failed: ${out}"
        return
    fi
    say "${out}"
    # A week of zero new listings on a working FMP key means the security-master
    # step is not running and the universe is quietly going stale (§10).
    if grep -qE '^New \(7d\)[[:space:]]*:[[:space:]]*0[[:space:]]*$' <<<"${out}"; then
        warn "New (7d) is 0 -- the security-master step may not be running."
    fi
    local renames
    # The Renames line is only printed when the count is non-zero.
    renames=$(grep -E '^Renames' <<<"${out}" | grep -oE '[0-9]+' | head -1)
    if [[ -n "${renames}" && "${renames}" -gt 0 ]]; then
        warn "${renames} unapplied ticker rename(s) awaiting a decision."
    fi
}

# -------------------------------------------------------------------- dq ----
sec_dq() {
    head_ "Open data-quality flags"
    local out
    if ! out=$(fafnir dq list 2>&1); then
        warn "fafnir dq list failed: ${out}"
        return
    fi
    say "${out}"
    say ""
    say "    Open DQ counts problems, not runs: a standing condition stays one"
    say "    row until someone resolves it, so a RISING count means new problems."
    say "    Work it with: fafnir dq list --detail --check <name>"
    say "                  fafnir dq resolve --check <name> --note '...' --yes"
}

# ------------------------------------------------------------------ disk ----
sec_disk() {
    head_ "Disk (an out-of-space Postgres stops writing)"
    local mounts=() m
    for m in /var/lib/postgresql /mnt/fafnir-data "${BACKUP_DIR}" "${LOG_DIR}"; do
        [[ -d "${m}" ]] && mounts+=("${m}")
    done
    if [[ ${#mounts[@]} -eq 0 ]]; then
        say "    (none of the expected paths exist on this host)"
        return
    fi
    # Deduplicate on the mount point: those four paths are usually the same
    # filesystem, and four identical rows read like four problems.
    say "$(df -h "${mounts[@]}" | awk 'NR==1 || !seen[$NF]++')"
    # Read the used% per filesystem, deduplicated -- several of those paths are
    # usually the same mount, and one warning per mount is enough.
    local pct target
    while read -r pct target; do
        [[ -z "${pct}" ]] && continue
        if [[ "${pct}" -ge "${DISK_WARN_PCT}" ]]; then
            warn "${target} is ${pct}% full (threshold ${DISK_WARN_PCT}%)."
        fi
    done < <(df --output=pcent,target "${mounts[@]}" 2>/dev/null | tail -n +2 \
             | awk '{sub(/%/, "", $1); if (!seen[$2]++) print $1, $2}')
}

# ---------------------------------------------------------------- timers ----
sec_timers() {
    head_ "Timers (did last night actually run?)"
    if ! command -v systemctl >/dev/null 2>&1; then
        say "    (no systemd on this host)"
        return
    fi
    say "$(systemctl list-timers 'fafnir-*' --all --no-pager 2>&1)"

    if ! systemctl list-timers 'fafnir-*' --all --no-pager 2>/dev/null \
         | grep -q 'fafnir-'; then
        warn "No fafnir-* timers are installed. See scripts/install_timers.sh."
        return
    fi
    local failed
    failed=$(systemctl list-units 'fafnir-*' --state=failed --no-legend \
             --no-pager 2>/dev/null | awk '{print $1}')
    if [[ -n "${failed}" ]]; then
        while read -r u; do
            warn "unit ${u} is in the failed state (systemctl status ${u})"
        done <<<"${failed}"
    fi
    # A oneshot that exited non-zero is not left "failed" forever once it is
    # reset, so also check the last exit status each service recorded.
    local svc rc
    for svc in $(systemctl list-units 'fafnir-*.service' --all --no-legend \
                 --no-pager 2>/dev/null | awk '{print $1}'); do
        rc=$(systemctl show -p ExecMainStatus --value "${svc}" 2>/dev/null)
        [[ -n "${rc}" && "${rc}" != "0" ]] && \
            warn "${svc} last exited ${rc} (journalctl -u ${svc} -n 50)"
    done
}

# --------------------------------------------------------------- journal ----
sec_journal() {
    head_ "Journal since ${JOURNAL_SINCE}"
    if ! command -v journalctl >/dev/null 2>&1; then
        say "    (no journalctl on this host)"
        return
    fi
    local out
    out=$(journalctl -u 'fafnir-*' --since "${JOURNAL_SINCE}" \
          --no-pager -n 60 2>&1)
    if grep -q 'No journal files\|Failed to add match\|not.*permitted' <<<"${out}"; then
        say "    ${out}"
        say "    (reading the journal needs membership of adm or systemd-journal)"
        return
    fi
    say "${out:-    (nothing logged)}"
}

# --------------------------------------------------------------- backups ----
sec_backups() {
    head_ "Backups in ${BACKUP_DIR}"
    if [[ ! -d "${BACKUP_DIR}" ]]; then
        warn "${BACKUP_DIR} does not exist -- no logical dumps are being taken."
        return
    fi
    local newest
    newest=$(find "${BACKUP_DIR}" -maxdepth 1 -name 'fafnir_*.dump' \
             -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [[ -z "${newest}" ]]; then
        warn "No fafnir_*.dump in ${BACKUP_DIR}."
        return
    fi
    say "$(ls -lht "${BACKUP_DIR}" | head -8)"
    local age_h
    age_h=$(( ( $(date +%s) - $(stat -c %Y "${newest}") ) / 3600 ))
    say ""
    say "    newest: ${newest} (${age_h}h old)"
    if [[ "${age_h}" -gt "${BACKUP_MAX_AGE_H}" ]]; then
        warn "Newest dump is ${age_h}h old (threshold ${BACKUP_MAX_AGE_H}h)."
    fi
    # An untested backup is a hope, not a backup -- §9.4 has the restore drill.
    local partials
    partials=$(find "${BACKUP_DIR}" -maxdepth 1 -name '*.partial' | wc -l)
    [[ "${partials}" -gt 0 ]] && \
        warn "${partials} interrupted dump(s) (*.partial) in ${BACKUP_DIR}."
}

# ------------------------------------------------------------------ runs ----
sec_runs() {
    head_ "Failed / partial ingestion runs"
    if ! psql_q -c '\x off' >/dev/null 2>&1; then
        say "    (no FAFNIR_DSN -- skipped)"
        return
    fi
    local out
    out=$(psql_q -P pager=off <<'SQL'
SELECT started_at, endpoint, status, left(error_message, 80) AS error
FROM ops.ingestion_run
WHERE status IN ('failed', 'partial')
  AND started_at > now() - interval '7 days'
ORDER BY started_at DESC
LIMIT 20;
SQL
) || { warn "query for failed runs did not complete"; return; }
    say "${out}"
    local n
    n=$(psql_q -tAc "SELECT count(*) FROM ops.ingestion_run
                     WHERE status IN ('failed','partial')
                       AND started_at > now() - interval '7 days';" 2>/dev/null)
    [[ -n "${n}" && "${n}" -gt 0 ]] && \
        warn "${n} failed/partial ingestion run(s) in the last 7 days."
}

# ------------------------------------------------------------- bandwidth ----
sec_bandwidth() {
    head_ "FMP bandwidth by month (Professional budget: 50 GB/month)"
    if ! psql_q -c '\x off' >/dev/null 2>&1; then
        say "    (no FAFNIR_DSN -- skipped)"
        return
    fi
    say "$(psql_q -P pager=off <<'SQL'
SELECT to_char(date_trunc('month', started_at), 'YYYY-MM') AS month,
       pg_size_pretty(sum(bytes_downloaded)::bigint)       AS downloaded,
       count(*)                                            AS runs
FROM ops.ingestion_run
GROUP BY 1 ORDER BY 1 DESC LIMIT 6;
SQL
)"
    local gb
    gb=$(psql_q -tAc "SELECT coalesce(sum(bytes_downloaded),0)/1073741824.0
                      FROM ops.ingestion_run
                      WHERE started_at >= date_trunc('month', now());" 2>/dev/null)
    if [[ -n "${gb}" ]] && awk -v g="${gb}" 'BEGIN{exit !(g > 40)}'; then
        warn "$(printf '%.1f' "${gb}") GB downloaded this month -- close to the 50 GB budget."
    fi
}

# ------------------------------------------------------------------ slow ----
sec_slow() {
    head_ "Slowest queries (pg_stat_statements)"
    if ! psql_q -c '\x off' >/dev/null 2>&1; then
        say "    (no FAFNIR_DSN -- skipped)"
        return
    fi
    if [[ "$(psql_q -tAc "SELECT count(*) FROM pg_extension
                          WHERE extname='pg_stat_statements';" 2>/dev/null)" != "1" ]]; then
        say "    (pg_stat_statements not installed -- see install_hetzner.md §3.4/§3.5)"
        return
    fi
    say "$(psql_q -P pager=off <<'SQL'
SELECT calls, round(mean_exec_time) AS avg_ms, round(total_exec_time) AS total_ms,
       left(query, 90) AS query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
SQL
)"
}

echo "==> fafnir monitor -- $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
for s in "${SECTIONS[@]}"; do "sec_${s}"; done

echo ""
if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
    echo "==> All ${#SECTIONS[@]} section(s) clean."
    exit 0
fi
echo "==> ${#PROBLEMS[@]} thing(s) to look at:"
printf '    - %s\n' "${PROBLEMS[@]}"
exit 1
