#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="telegram-camera-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "${RUN_USER}")"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "Missing ${PROJECT_DIR}/.env. Copy .env.example to .env and add BOT_TOKEN." >&2
  exit 1
fi

if ! python3 -c 'import picamera2' >/dev/null 2>&1; then
  echo "Picamera2 is missing. Install the OS dependencies first:" >&2
  echo "  sudo apt update && sudo apt install -y python3-picamera2 python3-gpiozero python3-lgpio python3-pil python3-venv ffmpeg" >&2
  exit 1
fi

if ! python3 -c 'import gpiozero, PIL' >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1; then
  echo "GPIO Zero, Pillow, or FFmpeg is missing. Install the OS dependencies first:" >&2
  echo "  sudo apt update && sudo apt install -y python3-picamera2 python3-gpiozero python3-lgpio python3-pil python3-venv ffmpeg" >&2
  exit 1
fi

python3 -m venv --system-site-packages "${PROJECT_DIR}/.venv"
"${PROJECT_DIR}/.venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
chmod 600 "${PROJECT_DIR}/.env"

sudo tee "${SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=Telegram Raspberry Pi Camera Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

if ! sudo systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "Service was installed but did not become active." >&2
  sudo systemctl status "${SERVICE_NAME}.service" --no-pager || true
  sudo journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager || true
  exit 1
fi

echo "Installed and started ${SERVICE_NAME}."
echo "View logs with: sudo journalctl -u ${SERVICE_NAME} -f"
