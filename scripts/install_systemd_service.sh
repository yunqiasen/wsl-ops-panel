#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="wsl-ops-panel.service"
PROJECT_DIR="/home/div/1_Project_dir/AI/wsl-ops-panel"
UVICORN_BIN="$PROJECT_DIR/.venv/bin/uvicorn"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"

if [[ ! -x "$UVICORN_BIN" ]]; then
  echo "uvicorn not found: $UVICORN_BIN" >&2
  exit 1
fi

cat <<UNIT | sudo tee "$UNIT_PATH" >/dev/null
[Unit]
Description=WSL Ops Panel
After=network.target docker.service

[Service]
Type=simple
User=div
WorkingDirectory=$PROJECT_DIR
ExecStart=$UVICORN_BIN app.main:app --host 127.0.0.1 --port 8328
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager --lines=20
