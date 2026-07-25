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
#   FAFNIR_DB_OWNER   role that owns the database (default: fafnir_ingest). The
#                     migrating role must own it: it creates the schemas, and
#                     `fafnir db ensure-horizon` later attaches partitions to
#                     core.daily_price, which only the owner may do.
set -euo pipefail

FAFNIR_DB="${FAFNIR_DB:-fafnir}"
FAFNIR_DB_OWNER="${FAFNIR_DB_OWNER:-fafnir_ingest}"
ADMIN_DSN="${FAFNIR_ADMIN_DSN:-}"

echo "==> fafnir database setup"

if [[ -n "${ADMIN_DSN}" ]]; then
    # Roles first: the database is owned by one of them, and pre-creating them lets
    # migration 0001 run as an ordinary (non-superuser) role. Passwords are assigned
    # out of band -- see doc/operations.md.
    echo "==> Ensuring roles exist (fafnir_ingest / fafnir_read / fafnir_app)"
    psql "${ADMIN_DSN}" -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_ingest') THEN
        CREATE ROLE fafnir_ingest LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_read') THEN
        CREATE ROLE fafnir_read LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fafnir_app') THEN
        CREATE ROLE fafnir_app LOGIN;
    END IF;
END
$$;
SQL

    echo "==> Ensuring database '${FAFNIR_DB}' exists (owner: ${FAFNIR_DB_OWNER})"
    if ! psql "${ADMIN_DSN}" -tAc "SELECT 1 FROM pg_database WHERE datname='${FAFNIR_DB}'" | grep -q 1; then
        psql "${ADMIN_DSN}" -c "CREATE DATABASE ${FAFNIR_DB} OWNER ${FAFNIR_DB_OWNER}"
        echo "    created."
    else
        current_owner=$(psql "${ADMIN_DSN}" -tAc \
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='${FAFNIR_DB}'")
        if [[ "${current_owner}" != "${FAFNIR_DB_OWNER}" ]]; then
            echo "    already present, but owned by '${current_owner}'."
            echo "    Migrations run as ${FAFNIR_DB_OWNER} and need ownership. Fix with:"
            echo "      ALTER DATABASE ${FAFNIR_DB} OWNER TO ${FAFNIR_DB_OWNER};"
        else
            echo "    already present."
        fi
    fi
else
    echo "==> Skipping CREATE ROLE / CREATE DATABASE (set FAFNIR_ADMIN_DSN to enable)."
    echo "    Assuming the database in FAFNIR_DSN exists and is owned by its role."
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
