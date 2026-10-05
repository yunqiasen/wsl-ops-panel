from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')


def main() -> None:
    parser = argparse.ArgumentParser(description='Control WSL Ops Panel known host ports through their owning docker compose asset.')
    parser.add_argument('action', choices=['start', 'stop'])
    parser.add_argument('--asset-id', required=True)
    parser.add_argument('--config-root', default='config')
    args = parser.parse_args()

    obj = _load_docker_object(Path(args.config_root), args.asset_id)
    project_dir = str(obj['config']['project_dir'])
    compose_file = str(obj['config'].get('compose_file') or _detect_compose_file(Path(project_dir)))
    service = str(obj['config'].get('compose_service') or obj['config'].get('primary_container') or '')
    if not service:
        raise SystemExit(f'asset {args.asset_id} has no compose_service')

    command = ['docker', 'compose', '-f', compose_file]
    if args.action == 'start':
        command += ['up', '-d', service]
    else:
        command += ['stop', service]
    print('$ ' + ' '.join(command))
    subprocess.run(command, cwd=project_dir, check=True)


def _load_docker_object(config_root: Path, asset_id: str) -> dict[str, Any]:
    objects_dir = config_root / 'objects'
    for path in sorted(objects_dir.glob('*.yaml')):
        payload = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        if not isinstance(payload, dict):
            continue
        if payload.get('id') != asset_id:
            continue
        if payload.get('category') != 'docker':
            raise SystemExit(f'asset {asset_id} is not a docker object')
        config = payload.get('config')
        if not isinstance(config, dict) or not config.get('project_dir'):
            raise SystemExit(f'asset {asset_id} has no project_dir')
        return payload
    raise SystemExit(f'asset {asset_id} not found under {objects_dir}')


def _detect_compose_file(project_dir: Path) -> str:
    for name in COMPOSE_FILES:
        if (project_dir / name).exists():
            return name
    return 'docker-compose.yml'


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
