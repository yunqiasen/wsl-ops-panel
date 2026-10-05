from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any
from urllib import request

import yaml
from pydantic import BaseModel, ConfigDict

from app.models.assets import AssetSnapshot
from app.services.endpoints import DEFAULT_TAILSCALE_IP, format_ports_for_display, service_urls
from app.services.source_links import source_url_from_links

DEFAULT_TEMPLATE = (
    '📌 {{ asset.name }}\n'
    '状态：{{ asset.status }}\n'
    '首选TS：{{ primary_url }}\n'
    'CF：{{ cf_url }}\n'
    '端点：\n{{ endpoint_lines }}\n'
    '来源：{{ source_url }}\n'
    '路径：{{ project_dir }}\n'
    '原始端口：{{ ports }}\n'
    '时间：{{ updated_at }}'
)
_TOKEN_RE = re.compile(r"TOKEN=['\"]([^'\"]+)['\"]")


class ProjectNotificationConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: bool = False
    title: str
    template: str


class NotificationPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')

    title: str
    content: str
    enabled: bool


class NotificationService:
    def __init__(self, config_root: Path | str = Path('config')) -> None:
        self.config_root = Path(config_root)
        self.config_path = self.config_root / 'notifications' / 'projects.yaml'

    def get_project_config(self, asset: AssetSnapshot) -> ProjectNotificationConfig:
        config = self._load_raw().get('assets', {}).get(asset.object_id, {})
        if not isinstance(config, dict):
            config = {}
        return ProjectNotificationConfig(
            enabled=bool(config.get('enabled', False)),
            title=str(config.get('title') or f'{asset.name} 访问信息'),
            template=str(config.get('template') or DEFAULT_TEMPLATE),
        )

    def save_project_config(self, asset_id: str, *, enabled: bool, title: str, template: str) -> ProjectNotificationConfig:
        raw = self._load_raw()
        assets = raw.setdefault('assets', {})
        if not isinstance(assets, dict):
            assets = {}
            raw['assets'] = assets
        assets[asset_id] = {'enabled': enabled, 'title': title, 'template': template}
        self._write_raw(raw)
        return ProjectNotificationConfig(enabled=enabled, title=title, template=template)

    def preview(self, asset: AssetSnapshot) -> NotificationPreview:
        config = self.get_project_config(asset)
        return NotificationPreview(
            title=config.title,
            content=render_notification_template(config.template, asset, tailscale_ip=self._tailscale_ip()),
            enabled=config.enabled,
        )

    def send(self, asset: AssetSnapshot, *, sender: Any | None = None) -> NotificationPreview:
        preview = self.preview(asset)
        token = self._pushplus_token()
        if not token:
            raise RuntimeError('PushPlus token is not configured')
        payload = json.dumps(
            {'token': token, 'title': preview.title, 'content': preview.content, 'template': 'html'},
            ensure_ascii=False,
        ).encode('utf-8')
        send_func = sender or _send_pushplus
        send_func(payload)
        return preview

    def _load_raw(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {'pushplus': {}, 'assets': {}}
        payload = yaml.safe_load(self.config_path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {'pushplus': {}, 'assets': {}}

    def _write_raw(self, payload: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')

    def _tailscale_ip(self) -> str:
        raw = self._load_raw()
        value = raw.get('tailscale_ip')
        return str(value) if value else DEFAULT_TAILSCALE_IP

    def _pushplus_token(self) -> str | None:
        raw = self._load_raw()
        pushplus = raw.get('pushplus') if isinstance(raw.get('pushplus'), dict) else {}
        token = pushplus.get('token') if isinstance(pushplus, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
        script_path = Path('/home/div/1_Project_dir/AI/scripts/startup-notify.sh')
        if not script_path.exists():
            return None
        match = _TOKEN_RE.search(script_path.read_text(encoding='utf-8', errors='replace'))
        return match.group(1).strip() if match else None


def render_notification_template(template: str, asset: AssetSnapshot, *, tailscale_ip: str = DEFAULT_TAILSCALE_IP) -> str:
    context = _notification_context(asset, tailscale_ip=tailscale_ip)
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace('{{ ' + key + ' }}', value).replace('{{' + key + '}}', value)
    return rendered


def asset_to_notification_json(asset: AssetSnapshot) -> str:
    return json.dumps(asset.model_dump(mode='json'), ensure_ascii=False)


def _notification_context(asset: AssetSnapshot, *, tailscale_ip: str) -> dict[str, str]:
    ports = str(asset.metadata.get('ports') or '')
    urls = service_urls(asset, tailscale_ip=tailscale_ip)
    source_links = asset.metadata.get('source_links') if isinstance(asset.metadata.get('source_links'), dict) else {}
    return {
        'asset.name': asset.name,
        'asset.status': asset.status,
        'primary_url': urls['primary_url'],
        'tailscale_url': urls['tailscale_url'],
        'cf_url': urls['cf_url'],
        'endpoint_lines': urls['endpoint_lines'],
        'source_url': source_url_from_links(source_links) or str(asset.metadata.get('git_remote_url') or ''),
        'image_repository': str(asset.metadata.get('image_repository') or ''),
        'project_dir': str(asset.metadata.get('project_dir') or asset.metadata.get('path') or ''),
        'ports': format_ports_for_display(ports),
        'updated_at': datetime.now(UTC).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'),
    }


def _send_pushplus(payload: bytes) -> None:
    req = request.Request(
        'https://www.pushplus.plus/send',
        data=payload,
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    with request.urlopen(req, timeout=15) as response:
        body = response.read().decode('utf-8', errors='replace')
        if response.status >= 400:
            raise RuntimeError(f'PushPlus failed: {response.status} {body}')


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('send', nargs='?')
    parser.add_argument('--config-root', default='config')
    parser.add_argument('--asset-id', required=True)
    parser.add_argument('--asset-json', required=True)
    args = parser.parse_args()
    asset = AssetSnapshot.model_validate_json(args.asset_json)
    if asset.object_id != args.asset_id:
        raise SystemExit('asset id mismatch')
    preview = NotificationService(args.config_root).send(asset)
    print(preview.model_dump_json())


if __name__ == '__main__':
    _main()
