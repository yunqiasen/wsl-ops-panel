#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="wsl-ops-panel.service"
PROJECT_DIR="/home/div/1_Project_dir/AI/wsl-ops-panel"
UVICORN_BIN="$PROJECT_DIR/.venv/bin/uvicorn"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
PYTHON_BIN_DIR="$(dirname "$(readlink -f "$(command -v python3)")")"
DEFAULT_SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"

if [[ ! -x "$UVICORN_BIN" ]]; then
  echo "uvicorn not found: $UVICORN_BIN" >&2
  exit 1
fi

if command -v node >/dev/null 2>&1; then
  NODE_BIN_DIR="$(dirname "$(readlink -f "$(command -v node)")")"
  SERVICE_PATH="$PYTHON_BIN_DIR:$NODE_BIN_DIR:$DEFAULT_SERVICE_PATH"
else
  SERVICE_PATH="$PYTHON_BIN_DIR:$DEFAULT_SERVICE_PATH"
fi

cat <<UNIT | sudo tee "$UNIT_PATH" >/dev/null
[Unit]
Description=WSL Ops Panel
After=network.target docker.service

[Service]
Type=simple
User=div
Environment=PATH=$SERVICE_PATH
WorkingDirectory=$PROJECT_DIR
ExecStart=$UVICORN_BIN app.main:app --host 0.0.0.0 --port 8328
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager --lines=20
