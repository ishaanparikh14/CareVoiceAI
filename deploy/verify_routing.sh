#!/usr/bin/env bash
# Restart carevoice; verify routing columns, dispatch board, and status/escalation endpoints.
set -e
cd /home/ec2-user/carevoice-server
B=http://127.0.0.1:8000

sudo systemctl restart carevoice
sleep 55
echo "service: $(systemctl is-active carevoice)"
echo "health:  $(curl -s -m 10 $B/health)"
echo "version: $(curl -s -m 10 $B/app/version | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["versionName"],"code",d["versionCode"])')"

echo "--- routing columns present ---"
DBPASS="$(cat /home/ec2-user/.db_pass)"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
  "SELECT username, competencies, status, is_supervisor FROM users WHERE role='nurse' ORDER BY username;"
PGPASSWORD="$DBPASS" psql -h 127.0.0.1 -U carevoice -d carevoice -tAc \
  "SELECT room_number, acuity FROM patients WHERE is_discharged=false ORDER BY room_number;"

TOKEN=$(curl -s -m 10 -X POST $B/auth/login -d username=admin -d password=admin1234 \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

echo "--- dispatch board (nurses w/ status+competency) ---"
curl -s -m 10 $B/admin/dispatch-board -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys,json; d=json.load(sys.stdin)
for n in d["nurses"]:
  print(" ", n["username"], "| status="+n["status"], "| sup="+str(n["is_supervisor"]), "| comp="+",".join(n["competencies"]))
print("  pending:", len(d["pending_alerts"]))'

echo "--- admin sets nurse_carol On Break ---"
curl -s -m 10 -X POST $B/admin/nurses/nurse_carol/status -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"status":"on_break"}'; echo
curl -s -m 10 $B/admin/dispatch-board -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys,json; d=json.load(sys.stdin)
c=[n for n in d["nurses"] if n["username"]=="nurse_carol"][0]
print("  nurse_carol status =", c["status"])'
echo "--- reset nurse_carol to available ---"
curl -s -m 10 -X POST $B/admin/nurses/nurse_carol/status -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"status":"available"}'; echo

echo "--- admin sets Room 4B acuity to ESI 1 ---"
curl -s -m 10 -X POST $B/admin/rooms/4B/acuity -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"acuity":1}'; echo
echo VERIFY_DONE
