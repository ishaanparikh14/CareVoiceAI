#!/usr/bin/env bash
# Re-export from Neon and import in FK-safe order (no superuser needed).
# Arg 1 = Neon URL.
set -euo pipefail

NEON_URL="$1"
export PGPASSWORD="$(cat /home/ec2-user/.db_pass)"
LOCAL="psql -h 127.0.0.1 -U carevoice -d carevoice -v ON_ERROR_STOP=1"
TMP=/home/ec2-user/mig
mkdir -p "$TMP"

# Export (re-export in case previous CSVs were cleaned up).
for t in users patients rooms voice_notes; do
  psql "$NEON_URL" -c "\copy (SELECT * FROM \"$t\" ORDER BY 1) TO '$TMP/$t.csv' WITH (FORMAT csv, HEADER true)"
done

# Clean slate again (idempotent).
$LOCAL -c "TRUNCATE users, patients, rooms, voice_notes RESTART IDENTITY CASCADE;"

# Import parents first, then children (satisfies FKs without disabling triggers).
#  users  <- patients.user_id, voice_notes.sender_user_id
#  rooms  (independent; voice_notes.room_id references rooms.id? check FK) 
for t in users rooms patients voice_notes; do
  $LOCAL -c "\copy \"$t\" FROM '$TMP/$t.csv' WITH (FORMAT csv, HEADER true)"
  echo "loaded $t"
done

# Fix sequences.
for t in users patients rooms voice_notes; do
  $LOCAL -tAc "SELECT setval(pg_get_serial_sequence('$t','id'), COALESCE((SELECT MAX(id) FROM \"$t\"),1), true);" >/dev/null 2>&1 || true
done

echo "=== final counts ==="
for t in users patients rooms voice_notes; do
  echo "  $t: $($LOCAL -tAc "SELECT count(*) FROM \"$t\";")"
done
rm -rf "$TMP"
echo "REIMPORT_DONE"
