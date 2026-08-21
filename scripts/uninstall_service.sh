#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="telegram-camera-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
sudo rm -f "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl reset-failed
echo "Removed ${SERVICE_NAME}. Project files and .venv were left in place."
