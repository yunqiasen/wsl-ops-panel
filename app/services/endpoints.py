from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.assets import AssetSnapshot

DEFAULT_TAILSCALE_IP = '100.126.43.55'


def enrich_service_endpoints(
    metadata: dict[str, Any],
    *,
    asset_id: str,
    asset_name: str,
    configured_endpoints: list[dict[str, Any]] | None = None,
    tailscale_ip: str = DEFAULT_TAILSCALE_IP,
) -> dict[str, Any]:
    endpoints = build_service_endpoints(
        metadata,
        asset_id=asset_id,
        asset_name=asset_name,
        configured_endpoints=configured_endpoints,
        tailscale_ip=tailscale_ip,
    )
    primary = primary_endpoint(endpoints)
    return {
        **metadata,
        'service_endpoints': endpoints,
        'primary_url': primary.get('tailscale_url', '') if primary else '',
        'endpoint_lines': format_endpoint_lines(endpoints),
    }


def build_service_endpoints(
    metadata: dict[str, Any],
    *,
    asset_id: str,
    asset_name: str,
    configured_endpoints: list[dict[str, Any]] | None = None,
    tailscale_ip: str = DEFAULT_TAILSCALE_IP,
) -> list[dict[str, str | bool]]:
    port_map = parse_port_mappings(str(metadata.get('ports') or ''))
    endpoints: list[dict[str, str | bool]] = []
    for index, item in enumerate(configured_endpoints or []):
        endpoint = _endpoint_from_config(item, port_map, tailscale_ip=tailscale_ip, primary=index == 0)
        if endpoint is not None:
            endpoints.append(endpoint)

    if endpoints:
        return _dedupe_endpoints(endpoints)

    port = str(metadata.get('primary_public_port') or '').strip()
    if not port and port_map:
        port = port_map[0]['host_port']
    if not port:
        return []

    label = _default_endpoint_label(asset_id, asset_name)
    path = _default_endpoint_path(asset_id)
    return [
        _build_endpoint(
            label=label,
            host_port=port,
            container_port=_container_port_for(port, port_map),
            path=path,
            tailscale_ip=tailscale_ip,
            primary=True,
        )
    ]


def service_urls(asset: AssetSnapshot, *, tailscale_ip: str = DEFAULT_TAILSCALE_IP) -> dict[str, str]:
    endpoints = _asset_endpoints(asset, tailscale_ip=tailscale_ip)
    primary = primary_endpoint(endpoints)
    cf_url = read_cf_url(asset) or ''
    primary_url = str(primary.get('tailscale_url') or '') if primary else ''
    return {
        'primary_url': primary_url,
        'tailscale_url': primary_url,
        'cf_url': cf_url,
        'endpoint_lines': format_endpoint_lines(endpoints),
    }


def _asset_endpoints(asset: AssetSnapshot, *, tailscale_ip: str) -> list[dict[str, str | bool]]:
    endpoints = asset.metadata.get('service_endpoints')
    if isinstance(endpoints, list) and endpoints:
        normalized = []
        for item in endpoints:
            if isinstance(item, dict):
                endpoint = _endpoint_from_config(item, parse_port_mappings(str(asset.metadata.get('ports') or '')), tailscale_ip=tailscale_ip)
                if endpoint is not None:
                    normalized.append(endpoint)
        if normalized:
            return normalized
    return build_service_endpoints(
        asset.metadata,
        asset_id=asset.object_id,
        asset_name=asset.name,
        tailscale_ip=tailscale_ip,
    )


def primary_endpoint(endpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    for endpoint in endpoints:
        if endpoint.get('primary') is True:
            return endpoint
    return endpoints[0] if endpoints else None


def format_endpoint_lines(endpoints: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for endpoint in endpoints:
        label = str(endpoint.get('label') or '入口')
        url = str(endpoint.get('tailscale_url') or '')
        host_port = str(endpoint.get('host_port') or '')
        container_port = str(endpoint.get('container_port') or '')
        suffix = ''
        if host_port and container_port and host_port != container_port:
            suffix = f'（{host_port} -> {container_port}）'
        elif host_port:
            suffix = f'（{host_port}）'
        lines.append(f'{label}：{url}{suffix}'.strip())
    return '\n'.join(lines)


def format_ports_for_display(ports: str | None) -> str:
    mappings = parse_port_mappings(ports or '')
    if not mappings:
        return ports or ''
    return ', '.join(_format_port_mapping(item['host_port'], item['container_port'], item['protocol']) for item in mappings)


def extract_primary_public_port(ports: str | None) -> str | None:
    mappings = parse_port_mappings(ports or '')
    if mappings:
        return mappings[0]['host_port']
    return None


def parse_port_mappings(ports: str) -> list[dict[str, str]]:
    mappings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_item in ports.split(','):
        item = raw_item.strip()
        if not item:
            continue
        host_port = ''
        container_port = ''
        protocol = 'tcp'
        if '->' in item:
            left, right = item.split('->', 1)
            host_port = _last_port(left)
            container_port, protocol = _split_target_port(right)
        else:
            host_port, protocol = _split_target_port(item)
            container_port = host_port
        if not host_port:
            continue
        key = (host_port, protocol)
        if key in seen:
            continue
        seen.add(key)
        mappings.append({'host_port': host_port, 'container_port': container_port or host_port, 'protocol': protocol})
    return mappings


def read_cf_url(asset: AssetSnapshot) -> str | None:
    capabilities = asset.metadata.get('capabilities')
    if isinstance(capabilities, dict):
        cf = capabilities.get('cf_tunnel')
        if isinstance(cf, dict):
            current_url = cf.get('current_url')
            if isinstance(current_url, str) and current_url.strip():
                return current_url.strip()
            domain_file = cf.get('domain_file')
            if isinstance(domain_file, str):
                path = Path(domain_file)
                if path.exists():
                    return path.read_text(encoding='utf-8', errors='replace').strip() or None
    for key in ('project_dir', 'path'):
        value = asset.metadata.get(key)
        if isinstance(value, str) and value.strip():
            domain_file = Path(value) / 'logs' / 'cftunnel-domain.txt'
            if domain_file.exists():
                return domain_file.read_text(encoding='utf-8', errors='replace').strip() or None
    return None


def _endpoint_from_config(
    item: dict[str, Any],
    port_map: list[dict[str, str]],
    *,
    tailscale_ip: str,
    primary: bool | None = None,
) -> dict[str, str | bool] | None:
    host_port = str(item.get('host_port') or item.get('port') or '').strip()
    container_port = str(item.get('container_port') or '').strip()
    if not host_port and container_port:
        host_port = _host_port_for(container_port, port_map)
    if not container_port and host_port:
        container_port = _container_port_for(host_port, port_map)
    if not host_port:
        return None
    endpoint = _build_endpoint(
        label=str(item.get('label') or '入口'),
        host_port=host_port,
        container_port=container_port,
        path=str(item.get('path') or ''),
        tailscale_ip=tailscale_ip,
        primary=bool(item.get('primary')) if primary is None else bool(item.get('primary', primary)),
    )
    if item.get('note'):
        endpoint['note'] = str(item['note'])
    return endpoint


def _build_endpoint(
    *,
    label: str,
    host_port: str,
    container_port: str,
    path: str,
    tailscale_ip: str,
    primary: bool = False,
) -> dict[str, str | bool]:
    clean_path = path if path.startswith('/') else f'/{path}' if path else ''
    base = f'http://{tailscale_ip}:{host_port}'
    return {
        'label': label,
        'host_port': host_port,
        'container_port': container_port or host_port,
        'path': clean_path,
        'tailscale_url': f'{base}{clean_path}',
        'primary': primary,
    }


def _dedupe_endpoints(endpoints: list[dict[str, str | bool]]) -> list[dict[str, str | bool]]:
    result: list[dict[str, str | bool]] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in endpoints:
        key = (str(endpoint.get('host_port') or ''), str(endpoint.get('path') or ''))
        if key in seen:
            continue
        seen.add(key)
        result.append(endpoint)
    if result and not any(item.get('primary') is True for item in result):
        result[0]['primary'] = True
    return result


def _last_port(value: str) -> str:
    parts = value.strip().rsplit(':', 1)
    candidate = parts[-1].strip().strip('[]')
    return candidate if candidate.isdigit() else ''


def _split_target_port(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    if '/' in cleaned:
        port, protocol = cleaned.split('/', 1)
        return port.strip(), protocol.strip() or 'tcp'
    port = _last_port(cleaned) if ':' in cleaned else cleaned
    return port.strip(), 'tcp'


def _host_port_for(container_port: str, port_map: list[dict[str, str]]) -> str:
    for item in port_map:
        if item['container_port'] == container_port:
            return item['host_port']
    return container_port


def _container_port_for(host_port: str, port_map: list[dict[str, str]]) -> str:
    for item in port_map:
        if item['host_port'] == host_port:
            return item['container_port']
    return host_port


def _format_port_mapping(public: str, target: str, protocol: str) -> str:
    target_label = f'{target}/{protocol}' if protocol else target
    if target == public:
        return target_label
    return f'{public} -> {target_label}'


def _default_endpoint_label(asset_id: str, asset_name: str) -> str:
    labels = {
        'cpa': 'API / 管理面板入口',
        'new_api': '管理后台 / API 入口',
        'sub2api': '管理后台 / API 入口',
        'searxng': '搜索页面入口',
        'openai_cpa': '注册系统管理后台',
    }
    return labels.get(asset_id, f'{asset_name} 访问入口')


def _default_endpoint_path(asset_id: str) -> str:
    paths = {
        'cpa': '/management.html',
        'new_api': '/',
        'sub2api': '/',
        'searxng': '/',
        'openai_cpa': '/',
    }
    return paths.get(asset_id, '/')
