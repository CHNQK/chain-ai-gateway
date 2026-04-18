#!/usr/bin/env bash
# install.sh - Chain-AI-Gateway installer (requires root on Ubuntu)
set -euo pipefail

INSTALL_DIR="/opt/chain-ai-gateway"
SERVICE_NAME="chain-ai-gateway"
PYTHON="python3"

echo "==> Installing Chain-AI-Gateway..."

# 1. Install system dependencies
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv

# 2. Copy project files
mkdir -p "$INSTALL_DIR"
cp -r "$(dirname "$0")/." "$INSTALL_DIR/"

# 3. Create virtualenv and install Python deps
$PYTHON -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# 4. Write systemd service
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Chain-AI-Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=GATEWAY_CONFIG=${INSTALL_DIR}/config.yaml

[Install]
WantedBy=multi-user.target
EOF

# 5. Enable and start
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "==> Done. Service status:"
systemctl status "$SERVICE_NAME" --no-pager
echo ""
echo "Edit config: ${INSTALL_DIR}/config.yaml  (hot-reload, no restart needed)"
echo "Logs:        journalctl -u ${SERVICE_NAME} -f"
