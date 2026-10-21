#!/bin/sh
# Local dev only: runs once, on a fresh Postgres volume, from docker-entrypoint-initdb.d.
# Creates the dedicated API / worker logins (02-login-roles.psql) with the passwords from .env.
# Without the passwords it skips with a warning (the API then falls back to the owner login and
# warns); apply it later with the command in apps/api/README.md ("Database logins").
if [ -z "${CASELENS_API_DB_PASSWORD:-}" ] || [ -z "${CASELENS_WORKER_DB_PASSWORD:-}" ]; then
    echo "02-login-roles: CASELENS_API_DB_PASSWORD / CASELENS_WORKER_DB_PASSWORD not set; skipping" >&2
else
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        -v api_password="$CASELENS_API_DB_PASSWORD" \
        -v worker_password="$CASELENS_WORKER_DB_PASSWORD" \
        -f /docker-entrypoint-initdb.d/02-login-roles.psql
fi
