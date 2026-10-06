#!/usr/bin/env bash
# Restart carevoice and exercise the admin console endpoints end to end.
set -e
cd /home/ec2-user/carevoice-server
B=http://127.0.0.1:8000

sudo systemctl restart carevoice
sleep 55
echo "service: $(systemctl is-active carevoice)"
echo "health:  $(curl -s -m 10 $B/health)"
echo "version: $(curl -s -m 10 $B/app/version | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["versionName"], "code", d["versionCode"])')"

TOKEN=$(curl -s -m 10 -X POST $B/auth/login -d username=admin -d password=admin1234 \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

echo "--- board ---"
curl -s -m 10 $B/admin/dispatch-board -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys,json; d=json.load(sys.stdin)
for n in d["nurses"]: print(" ", n["username"], "online" if n["online"] else "offline", "BUSY" if n["busy"] else "free", repr(n["busy_reason"]))
print("  pending:", len(d["pending_alerts"]))'

echo "--- mark nurse_ben occupied ---"
curl -s -m 10 -X POST $B/admin/nurses/nurse_ben/busy -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"busy": true}'; echo
curl -s -m 10 $B/admin/dispatch-board -H "Authorization: Bearer $TOKEN" | python3 -c '
import sys,json; d=json.load(sys.stdin)
b=[n for n in d["nurses"] if n["username"]=="nurse_ben"][0]
print("  nurse_ben busy =", b["busy"], "reason =", repr(b["busy_reason"]))'

echo "--- mark nurse_ben free again ---"
curl -s -m 10 -X POST $B/admin/nurses/nurse_ben/busy -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"busy": false}'; echo

echo "--- non-admin is rejected ---"
NT=$(curl -s -m 10 -X POST $B/auth/login -d username=nurse_anna -d password=pass123 \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
echo "  nurse -> board: HTTP $(curl -s -o /dev/null -w '%{http_code}' -m 10 $B/admin/dispatch-board -H "Authorization: Bearer $NT")"
echo "  bad token -> board: HTTP $(curl -s -o /dev/null -w '%{http_code}' -m 10 $B/admin/dispatch-board -H 'Authorization: Bearer junk')"
echo VERIFY_DONE
