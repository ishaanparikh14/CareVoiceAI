#!/usr/bin/env bash
# Generate secrets and write the server .env on the instance.
set -euo pipefail
cd /home/ec2-user/carevoice-server

PY=/usr/bin/python3.11
DBPASS="$(cat /home/ec2-user/.db_pass)"
SECRET_KEY="$($PY -c 'import secrets; print(secrets.token_hex(32))')"
NURSE_ADMIN_KEY="$($PY -c 'import secrets; print(secrets.token_hex(16))')"
DASHED_IP="3-110-163-212"

cat > .env <<EOF
USE_STUB=false
HOST=127.0.0.1
PORT=8000
LOG_LEVEL=info
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
PG_HOST=localhost
PG_PORT=5432
PG_USER=carevoice
PG_PASSWORD=${DBPASS}
PG_DATABASE=carevoice
SECRET_KEY=${SECRET_KEY}
NURSE_ADMIN_KEY=${NURSE_ADMIN_KEY}
CORS_ORIGINS=["https://${DASHED_IP}.sslip.io"]
EOF

chmod 600 .env
echo "ENV_WRITTEN"
echo "NURSE_ADMIN_KEY=${NURSE_ADMIN_KEY}"
