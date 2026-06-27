#!/usr/bin/env bash
# reconcile.sh -- re-pull a sample of symbols from FMP and diff against stored
# values to catch silent drift (re-adjustments, late corrections). Reports
# differences; it does not auto-overwrite. Intended for periodic (e.g. weekly)
# runs over a rotating sample.
#
# Usage: scripts/reconcile.sh SYM1,SYM2,...  [FROM_DATE]
set -euo pipefail

SYMBOLS="${1:?provide comma-separated symbols}"
FROM_DATE="${2:-$(date -u -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -u +%Y-%m-%d)}"

echo "==> Reconciling ${SYMBOLS} from ${FROM_DATE}"
echo "    Re-pulling live and diffing against the warehouse (db)."

IFS=',' read -ra SYMS <<< "${SYMBOLS}"
for sym in "${SYMS[@]}"; do
    echo "---- ${sym} ----"
    # Compare close series from each source; differences indicate drift.
    diff <(duk -S db   ph "${sym}" -s "${FROM_DATE}" --close --csv 2>/dev/null) \
         <(duk -S live ph "${sym}" -s "${FROM_DATE}" --close --csv 2>/dev/null) \
         && echo "    OK: db matches live" \
         || echo "    DRIFT: db and live differ for ${sym} (review above)"
done
