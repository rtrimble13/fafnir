#!/usr/bin/env bash
# reconcile.sh -- compare a sample of symbols' raw closes in the warehouse with
# FMP's, date by date, to catch silent drift (re-adjustments, late corrections) and,
# since the cutover, disagreement between the vendors. Reports; it does not
# overwrite. Intended for periodic (e.g. weekly) runs over a rotating sample.
#
# Each date is judged by whose bar the warehouse holds (fafnir source reconcile,
# #117): DRIFT when it is FMP's own and FMP now serves another, DIFFERS when it is
# another vendor's (Sharadar's, since the cutover) and FMP disagrees. Dates
# `vendor correct` replaced (ops.vendor_correction) and bars an operator changed are
# expected. A symbol FMP serves nothing for is compared with Sharadar over its last
# 5 stored sessions instead, when SHARADAR_API_KEY is set.
#
# Usage: scripts/reconcile.sh SYM1,SYM2,...  [FROM_DATE]
set -euo pipefail

SYMBOLS="${1:?usage: reconcile.sh SYM1,SYM2,... [FROM_DATE]}"
FROM_DATE="${2:-$(date -u -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -u +%Y-%m-%d)}"

exec fafnir source reconcile "${SYMBOLS}" --from "${FROM_DATE}"
