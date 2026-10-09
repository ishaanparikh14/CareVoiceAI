#!/usr/bin/env bash
# Create the carevoice PostgreSQL user + database with a generated password.
set -euo pipefail

PYBIN="$(command -v python3.11 || echo /usr/bin/python3.11)"
DBPASS="$("$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(24))')"

# Persist the password so later steps (and .env) can read it.
echo "$DBPASS" > /home/ec2-user/.db_pass
chmod 600 /home/ec2-user/.db_pass

# Create role + database (idempotent-ish: drop/recreate role only if absent).
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'carevoice') THEN
      CREATE ROLE carevoice LOGIN PASSWORD '${DBPASS}';
   ELSE
      ALTER ROLE carevoice PASSWORD '${DBPASS}';
   END IF;
END
\$\$;
SQL

# Create database if it does not already exist.
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='carevoice'" | grep -q 1; then
   sudo -u postgres createdb -O carevoice carevoice
fi

echo "DB_SETUP_DONE"
