#!/usr/bin/env bash
# collect_facts.sh -- gather one night's evidence about the fafnir automations
# into a single text block on stdout. Read-only: it reports, it never changes
# anything.
#
# Runs as the AGENT user (doc/agent.md), not as fafnir, which is what shapes it:
# every fafnir command goes through the one sudoers rule the agent has, and
# anything the agent cannot read is left out rather than guessed at. Pairs with
# nightly_report.sh, which feeds the output to the model. See
# doc/nightly_report.md.
#
#   scripts/collect_facts.sh                  # default 18h window
#   scripts/collect_facts.sh "2 days ago"     # wider journal window
set -uo pipefail          # NOT -e: a failing probe must be reported, not fatal

FAFNIR_HOME="${FAFNIR_HOME:-/opt/fafnir}"
FAFNIR_BIN="${FAFNIR_BIN:-$FAFNIR_HOME/.venv/bin/fafnir}"
SINCE="${1:-18 hours ago}"
FAF=(sudo -n -u fafnir "$FAFNIR_BIN")

section() { printf '\n===== %s =====\n' "$1"; }

printf 'fafnir nightly evidence -- collected %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'host: %s\n' "$(hostname)"

section "TIMERS (did last night fire, when is the next?)"
systemctl list-timers 'fafnir-*' --all --no-pager 2>&1

# The exit status of each unit, which is the load-bearing evidence: it is the
# only signal here that does not depend on the agent being able to read a log.
section "UNIT RESULT"
for u in fafnir-daily fafnir-dq fafnir-dump fafnir-backup-offsite fafnir-reconcile; do
    # Started AND finished: the journal is the only other place a duration can
    # come from, and it is exactly what the agent loses when it is not in `adm`.
    # Without both timestamps the model is asked for a duration it cannot derive,
    # which is an invitation to invent one.
    printf '%-24s active=%-10s result=%-10s status=%-3s started=%s finished=%s\n' \
        "$u" \
        "$(systemctl show -p ActiveState     --value "$u.service" 2>/dev/null)" \
        "$(systemctl show -p Result          --value "$u.service" 2>/dev/null)" \
        "$(systemctl show -p ExecMainStatus  --value "$u.service" 2>/dev/null)" \
        "$(systemctl show -p ExecMainStartTimestamp --value "$u.service" 2>/dev/null)" \
        "$(systemctl show -p ExecMainExitTimestamp  --value "$u.service" 2>/dev/null)"
done

section "JOURNAL (last ${SINCE})"
journalctl -q -u 'fafnir-*' --since "$SINCE" --no-pager -o short-iso 2>&1 | tail -200

section "WAREHOUSE STATUS"
"${FAF[@]}" status 2>&1

section "OPEN DQ QUEUE"
"${FAF[@]}" dq list 2>&1

# NOT the `backups` section: /var/backups/fafnir is 0750 fafnir:fafnir and this
# runs as the agent, so that check can only ever report a false "no dump found".
# The dump is covered honestly by fafnir-dump.service's exit status above.
#
# NOT --quiet, either: under it monitor.sh emits only warnings and a summary, so a
# clean night carries no numbers for the model to compare against tomorrow's -- and
# a section that skipped itself is indistinguishable from one that passed.
section "DISK / RUNS / BANDWIDTH"
if [[ -z "${FAFNIR_DSN:-}" ]]; then
    # monitor.sh sources /etc/fafnir/fafnir.env for a DSN, which is root:fafnir
    # 0640 and denied to the agent by design (doc/agent.md). Unset, `runs` and
    # `bandwidth` skip themselves and the run still summarises as "clean" -- so
    # say plainly that they did not run, rather than letting silence read as good
    # news. Set FAFNIR_DSN to the agent read role in /etc/fafnir/report.env.
    cat <<'NODSN'
!!  FAFNIR_DSN is unset, so the `runs` and `bandwidth` checks below did NOT run.
!!  Any "clean" summary line covers `disk` only. This is missing evidence, not a
!!  healthy result -- see doc/nightly_report.md step 2.
NODSN
fi
"$FAFNIR_HOME/scripts/monitor.sh" disk runs bandwidth 2>&1
