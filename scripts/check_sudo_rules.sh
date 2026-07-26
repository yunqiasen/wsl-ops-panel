#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mapfile -t SYSTEMD_UNITS < <(
python3 - <<'PY' "$ROOT_DIR/config"
from pathlib import Path
import sys
import yaml

config_root = Path(sys.argv[1])
objects_dir = config_root / 'objects'
for path in sorted(objects_dir.glob('*.yaml')):
    payload = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    if payload.get('category') != 'systemd':
        continue
    config = payload.get('config') or {}
    unit_name = config.get('unit_name')
    if unit_name:
        print(unit_name)
PY
)

if [ "${#SYSTEMD_UNITS[@]}" -eq 0 ]; then
  SYSTEMD_UNITS=("wsl-ops-panel.service")
fi

for unit in "${SYSTEMD_UNITS[@]}"; do
  for action in start stop restart; do
    sudo -n -l systemctl "$action" "$unit" >/dev/null
  done
  sudo -n -l systemctl disable --now "$unit" >/dev/null
done

sudo -n -l docker ps >/dev/null
