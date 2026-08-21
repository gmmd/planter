#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="telegram-camera-bot"
SERVICE_UNIT="${SERVICE_NAME}.service"
VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "Missing ${PROJECT_DIR}/.env. Copy .env.example to .env and configure it." >&2
  exit 1
fi

if ! sudo systemctl cat "${SERVICE_UNIT}" >/dev/null 2>&1; then
  echo "${SERVICE_UNIT} is not installed. Run scripts/install_service.sh first." >&2
  exit 1
fi

if ! python3 -c 'import picamera2, gpiozero, PIL' >/dev/null 2>&1; then
  echo "Required OS Python packages are missing. Install them first:" >&2
  echo "  sudo apt update && sudo apt install -y python3-picamera2 python3-gpiozero python3-lgpio python3-pil python3-venv ffmpeg" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is missing. Install it with: sudo apt install -y ffmpeg" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  python3 -m venv --system-site-packages "${VENV_DIR}"
fi

"${PIP_BIN}" install --upgrade -r "${PROJECT_DIR}/requirements.txt"
chmod 600 "${PROJECT_DIR}/.env"

UPDATE_CACHE_DIR="$(mktemp -d)"
trap 'rm -rf -- "${UPDATE_CACHE_DIR}"' EXIT
PYTHONPYCACHEPREFIX="${UPDATE_CACHE_DIR}" \
  "${PYTHON_BIN}" -m py_compile \
  "${PROJECT_DIR}/bot.py" \
  "${PROJECT_DIR}/automation.py" \
  "${PROJECT_DIR}/ai_client.py" \
  "${PROJECT_DIR}/sensors.py" \
  "${PROJECT_DIR}/photo_watermark.py"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_UNIT}" >/dev/null

if ! sudo systemctl restart "${SERVICE_UNIT}"; then
  echo "Service restart failed." >&2
  sudo systemctl status "${SERVICE_UNIT}" --no-pager || true
  sudo journalctl -u "${SERVICE_UNIT}" -n 50 --no-pager || true
  exit 1
fi

if ! sudo systemctl is-active --quiet "${SERVICE_UNIT}"; then
  echo "Service did not become active." >&2
  sudo systemctl status "${SERVICE_UNIT}" --no-pager || true
  sudo journalctl -u "${SERVICE_UNIT}" -n 50 --no-pager || true
  exit 1
fi

echo "Updated and restarted ${SERVICE_NAME}."
sudo systemctl status "${SERVICE_UNIT}" --no-pager --lines=10
