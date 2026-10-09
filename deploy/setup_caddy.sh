#!/usr/bin/env bash
# Install Caddy config + service, prepare data dirs, start it.
set -euo pipefail

# Config dir + file
sudo mkdir -p /etc/caddy
sudo mv /home/ec2-user/Caddyfile /etc/caddy/Caddyfile
sudo chown root:root /etc/caddy/Caddyfile

# Caddy data/config dirs for certificate storage (owned by caddy user)
sudo mkdir -p /var/lib/caddy /var/log/caddy
sudo chown -R caddy:caddy /var/lib/caddy /var/log/caddy
# Caddy stores certs under $HOME/.local/share/caddy by default; set HOME via systemd env
sudo mkdir -p /etc/systemd/system/caddy.service.d
cat <<EOF | sudo tee /etc/systemd/system/caddy.service.d/env.conf >/dev/null
[Service]
Environment=HOME=/var/lib/caddy
Environment=XDG_DATA_HOME=/var/lib/caddy
Environment=XDG_CONFIG_HOME=/var/lib/caddy
EOF

# Service unit
sudo mv /home/ec2-user/caddy.service /etc/systemd/system/caddy.service
sudo chown root:root /etc/systemd/system/caddy.service

# Validate config before starting
sudo /usr/bin/caddy validate --config /etc/caddy/Caddyfile

sudo systemctl daemon-reload
sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl restart caddy
sleep 3
systemctl is-active caddy
echo "CADDY_SETUP_DONE"
