#!/usr/bin/env bash
# Inspect the Neon source DB: list tables and row counts. Reads URL from arg 1.
set -euo pipefail
NEON_URL="$1"

echo "=== tables ==="
psql "$NEON_URL" -c "\dt"
echo "=== row counts ==="
for t in users patients rooms voice_notes alerts transcripts; do
  n=$(psql "$NEON_URL" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null || echo "n/a")
  echo "$t: $n"
done
