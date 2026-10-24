#!/usr/bin/env bash
# Restart carevoice and verify the central department-routing agent end to end.
# Safe to run on the live box: it only READS state and confirms shadow mode is
# ON (so no patient-facing behaviour changes on deploy).
set -e
cd /home/ec2-user/carevoice-server
B=http://127.0.0.1:8000

sudo systemctl restart carevoice
sleep 55
echo "service: $(systemctl is-active carevoice)"
echo "health:  $(curl -s -m 10 $B/health)"

echo "--- department staff accounts seeded ---"
DBPASS="$(cat /home/ec2-user/.db_pass)"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
  "SELECT username, role, department, floors FROM users WHERE department IS NOT NULL ORDER BY username;"

echo "--- room floors + patient diets seeded ---"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
  "SELECT room_number, floor FROM rooms ORDER BY room_number;"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
  "SELECT room_number, diet_restriction FROM patients WHERE is_discharged=false ORDER BY room_number;"

echo "--- /dept dashboard page served ---"
echo "  /dept: HTTP $(curl -s -o /dev/null -w '%{http_code}' -m 10 $B/dept)"

echo "--- department login works ---"
CANTEEN=$(curl -s -m 10 -X POST $B/auth/login -d username=canteen_cathy -d password=dept1234 \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("role"), d.get("department"))')
echo "  canteen_cathy -> role/department: $CANTEEN"

echo "--- SHADOW MODE must be ON (no live routing yet) ---"
TOKEN=$(curl -s -m 10 -X POST $B/auth/login -d username=admin -d password=admin1234 \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
curl -s -m 10 $B/admin/department/config -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys,json; d=json.load(sys.stdin)
print("  config:", d)
assert d.get("shadow") is True, "EXPECTED shadow mode ON for a safe deploy!"
print("  OK: shadow mode is ON — existing nurse flow unchanged.")'

echo "--- department routes registered ---"
echo "  /department/me (no auth -> 401 expected): HTTP $(curl -s -o /dev/null -w '%{http_code}' -m 10 $B/department/me)"
echo VERIFY_DEPARTMENT_DONE
