#!/usr/bin/env bash
# Restart carevoice and verify admin dispatch board + app version endpoint.
set -e
cd /home/ec2-user/carevoice-server

echo "=== restart ==="
sudo systemctl restart carevoice
sleep 55
systemctl is-active carevoice

echo "=== health ==="
curl -s -m 10 http://127.0.0.1:8000/health; echo

echo "=== app version manifest ==="
curl -s -m 10 http://127.0.0.1:8000/app/version; echo

echo "=== APK reachable (HEAD) ==="
curl -s -o /dev/null -w "apk HTTP %{http_code}, %{size_download} bytes header\n" -I -m 15 http://127.0.0.1:8000/static/carevoice-latest.apk

echo "=== admin login -> dispatch-board ==="
TOKEN=$(curl -s -m 10 -X POST http://127.0.0.1:8000/auth/login \
  -d "username=admin" -d "password=admin1234" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "got admin token: ${TOKEN:0:12}..."
curl -s -m 10 http://127.0.0.1:8000/admin/dispatch-board \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('nurses:', len(d['nurses']), '| pending_alerts:', len(d['pending_alerts'])); [print('  -', n['username'], 'online' if n['online'] else 'offline', 'BUSY' if n['busy'] else 'free') for n in d['nurses']]"
echo "VERIFY_DONE"
