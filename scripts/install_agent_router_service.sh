#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="wsl-agent-router.service"
PROJECT_DIR="${WSL_OPS_PANEL_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
CONFIG_PATH="$PROJECT_DIR/data/agent/router.json"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
SERVICE_USER="${WSL_OPS_PANEL_USER:-$(stat -c '%U' "$PROJECT_DIR")}"
DEFAULT_SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found: $PYTHON_BIN" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Service user not found: $SERVICE_USER" >&2
  exit 1
fi

sudo install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$PROJECT_DIR/data/agent"
if [[ ! -f "$CONFIG_PATH" ]]; then
  sudo -u "$SERVICE_USER" env PYTHONPATH="$PROJECT_DIR" "$PYTHON_BIN" - <<PY
from app.services.agent_router_config import AgentRouterConfigStore
store = AgentRouterConfigStore(r"$PROJECT_DIR/data/agent")
store.update_global()
PY
fi
sudo chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_PATH"
sudo chmod 0600 "$CONFIG_PATH"

unit_file="$(mktemp)"
trap 'rm -f "$unit_file"' EXIT
cat >"$unit_file" <<UNIT
[Unit]
Description=WSL Agent Router
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/.venv/bin:$DEFAULT_SERVICE_PATH
Environment=PYTHONUNBUFFERED=1
Environment=AGENT_ROUTER_CONFIG=$CONFIG_PATH
ExecStart=$PYTHON_BIN -m app.agent_router.main
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

sudo install -m 0644 "$unit_file" "$UNIT_PATH"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

for _ in {1..30}; do
  if curl -fsS --max-time 1 http://127.0.0.1:7888/health >/dev/null; then
    break
  fi
  sleep 0.2
done

sudo systemctl status "$SERVICE_NAME" --no-pager --lines=20
curl -fsS http://127.0.0.1:7888/health
printf '\n'
