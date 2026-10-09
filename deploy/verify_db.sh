#!/usr/bin/env bash
# Ensure password auth over TCP works for the carevoice role, then verify login.
set -euo pipefail

HBA="/var/lib/pgsql/data/pg_hba.conf"

# Make local TCP (127.0.0.1 / ::1) use scram-sha-256 password auth.
if ! sudo grep -qE '^\s*host\s+all\s+all\s+127\.0\.0\.1/32\s+scram-sha-256' "$HBA"; then
  sudo sed -i -E 's|^(host\s+all\s+all\s+127\.0\.0\.1/32\s+).*$|\1scram-sha-256|' "$HBA"
  sudo sed -i -E 's|^(host\s+all\s+all\s+::1/128\s+).*$|\1scram-sha-256|' "$HBA"
  sudo systemctl reload postgresql
fi

DBPASS="$(cat /home/ec2-user/.db_pass)"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc "SELECT current_user || ' @ ' || current_database();"
