#!/usr/bin/env bash
# Migrate users/patients/rooms/voice_notes from Neon into the local AWS Postgres.
# Data-only copy: the AWS schema already exists (created by the app on first run).
# Arg 1 = Neon connection URL.
set -euo pipefail

NEON_URL="$1"
LOCAL_PASS="$(cat /home/ec2-user/.db_pass)"
LOCAL_URL="postgresql://carevoice:${LOCAL_PASS}@127.0.0.1:5432/carevoice"
DUMP=/home/ec2-user/neon_data.sql

TABLES=(users patients rooms voice_notes)

echo "=== Dumping data-only from Neon ==="
# --data-only: keep AWS schema. --column-inserts: portable, order-independent.
# Dump in FK-safe order (users/rooms before patients/voice_notes handled by
# disabling triggers during load instead).
pg_dump "$NEON_URL" \
  --data-only --no-owner --no-privileges \
  $(printf -- '--table=public.%s ' "${TABLES[@]}") \
  > "$DUMP"
echo "dump size: $(wc -c < "$DUMP") bytes"

echo "=== Truncating target tables on AWS (removes freshly-seeded rows) ==="
PGPASSWORD="$LOCAL_PASS" psql -h 127.0.0.1 -U carevoice -d carevoice -v ON_ERROR_STOP=1 \
  -c "TRUNCATE users, patients, rooms, voice_notes RESTART IDENTITY CASCADE;"

echo "=== Loading Neon data into AWS (triggers disabled for FK order) ==="
# session_replication_role=replica disables FK trigger checks so insert order
# across tables does not matter.
PGPASSWORD="$LOCAL_PASS" psql -h 127.0.0.1 -U carevoice -d carevoice -v ON_ERROR_STOP=1 \
  -c "SET session_replication_role = replica;" \
  -f "$DUMP" \
  -c "SET session_replication_role = default;"

echo "=== Fixing sequences (so new signups get fresh ids) ==="
for t in "${TABLES[@]}"; do
  PGPASSWORD="$LOCAL_PASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
    "SELECT setval(pg_get_serial_sequence('$t','id'), COALESCE((SELECT MAX(id) FROM \"$t\"), 1), true);" >/dev/null 2>&1 || true
done

echo "=== Verify row counts on AWS ==="
for t in "${TABLES[@]}"; do
  n=$(PGPASSWORD="$LOCAL_PASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc "SELECT count(*) FROM \"$t\";")
  echo "$t: $n"
done

rm -f "$DUMP"
echo "MIGRATION_DONE"
