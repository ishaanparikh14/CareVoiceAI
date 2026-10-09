#!/usr/bin/env bash
# Version-independent migration from Neon (PG18) to local AWS Postgres (PG15),
# using psql \copy to CSV (no pg_dump version check). Arg 1 = Neon URL.
set -euo pipefail

NEON_URL="$1"
LOCAL_PASS="$(cat /home/ec2-user/.db_pass)"
export PGPASSWORD="$LOCAL_PASS"
LOCAL="psql -h 127.0.0.1 -U carevoice -d carevoice -v ON_ERROR_STOP=1"
TABLES=(users patients rooms voice_notes)
TMP=/home/ec2-user/mig
mkdir -p "$TMP"

echo "=== schema check (column lists must match) ==="
mismatch=0
for t in "${TABLES[@]}"; do
  nc=$(psql "$NEON_URL" -tAc "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='$t';")
  lc=$($LOCAL -tAc "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='$t';")
  if [ "$nc" != "$lc" ]; then
    echo "MISMATCH $t:"; echo "  neon : $nc"; echo "  local: $lc"; mismatch=1
  else
    echo "ok $t ($nc)"
  fi
done
if [ "$mismatch" = "1" ]; then
  echo "ABORT: schema mismatch — not migrating."; exit 1
fi

echo "=== exporting from Neon to CSV ==="
for t in "${TABLES[@]}"; do
  psql "$NEON_URL" -c "\copy (SELECT * FROM \"$t\" ORDER BY 1) TO '$TMP/$t.csv' WITH (FORMAT csv, HEADER true)"
  echo "  $t rows: $(( $(wc -l < "$TMP/$t.csv") - 1 ))"
done

echo "=== truncating local target tables ==="
$LOCAL -c "TRUNCATE users, patients, rooms, voice_notes RESTART IDENTITY CASCADE;"

echo "=== importing into local (FK triggers disabled for order independence) ==="
for t in "${TABLES[@]}"; do
  $LOCAL -c "SET session_replication_role = replica;" \
         -c "\copy \"$t\" FROM '$TMP/$t.csv' WITH (FORMAT csv, HEADER true)"
done

echo "=== fixing id sequences ==="
for t in "${TABLES[@]}"; do
  $LOCAL -tAc "SELECT setval(pg_get_serial_sequence('$t','id'), COALESCE((SELECT MAX(id) FROM \"$t\"),1), true);" >/dev/null 2>&1 || true
done

echo "=== final local row counts ==="
for t in "${TABLES[@]}"; do
  echo "  $t: $($LOCAL -tAc "SELECT count(*) FROM \"$t\";")"
done

rm -rf "$TMP"
echo "MIGRATION_DONE"
