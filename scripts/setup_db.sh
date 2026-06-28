#!/usr/bin/env bash
# setup_db.sh -- create the database, apply migrations, and seed reference data.
#
# Idempotent: safe to re-run. Requires a reachable PostgreSQL server and a
# superuser/owner connection for the initial CREATE DATABASE.
#
# Environment:
#   FAFNIR_DSN        libpq DSN the `fafnir` CLI uses (ingest role).
#   FAFNIR_ADMIN_DSN  optional admin connection for CREATE ROLE/DATABASE
#                     (defaults to a maintenance connection to the 'postgres' db).
#   FAFNIR_DB         database name to create (default: fafnir).
set -euo pipefail

FAFNIR_DB="${FAFNIR_DB:-fafnir}"
ADMIN_DSN="${FAFNIR_ADMIN_DSN:-}"

echo "==> fafnir database setup"

if [[ -n "${ADMIN_DSN}" ]]; then
    echo "==> Ensuring database '${FAFNIR_DB}' exists"
    if ! psql "${ADMIN_DSN}" -tAc "SELECT 1 FROM pg_database WHERE datname='${FAFNIR_DB}'" | grep -q 1; then
        psql "${ADMIN_DSN}" -c "CREATE DATABASE ${FAFNIR_DB}"
        echo "    created."
    else
        echo "    already present."
    fi
else
    echo "==> Skipping CREATE DATABASE (set FAFNIR_ADMIN_DSN to enable). "
    echo "    Assuming the database in FAFNIR_DSN already exists."
fi

echo "==> Applying migrations"
fafnir db migrate

echo "==> Seeding reference data + trading calendar"
fafnir db seed

echo "==> Ensuring price partitions + calendar to the rolling horizon"
fafnir db ensure-horizon

echo "==> Migration status"
fafnir db status

echo "==> Done. Next: fafnir ingest securities"
