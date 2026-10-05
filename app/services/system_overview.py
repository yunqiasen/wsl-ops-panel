from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic
from typing import Any

CommandRunner = Callable[[list[str], float], str]

_PORT_LINE_RE = re.compile(r'(?P<address>[^\s:]+|\[[^\]]+\]):(?P<port>\d{1,5})')
_PROCESS_RE = re.compile(r'"(?P<name>[^"]+)"(?:,pid=(?P<pid>\d+))?')
_DOCKER_PUBLISHED_PORT_RE = re.compile(r'(?P<address>[\d.]+|\[[^\]]+\]):(?P<host_port>\d+)->(?P<container_port>\d+)/tcp')
_LOCAL_NO_PROXY = {'localhost', '127.0.0.1', '::1', 'host.docker.internal', '172.17.0.1'}
_SERVICE_NAMES = {
    'docker': 'docker',
    'panel': 'wsl-ops-panel.service',
    'cloudflare_tunnel': 'wsl-xinghaihub-tunnel.service',
    'tailscaled': 'tailscaled',
}


def build_system_overview(
    *,
    command_runner: CommandRunner | None = None,
    env: Mapping[str, str] | None = None,
    proc_root: Path | str = Path('/proc'),
) -> dict[str, Any]:
    started = monotonic()
    runner = command_runner or _run_command
    environ = env or os.environ
    proc_path = Path(proc_root)
    ports = _list_ports(runner)
    docker_proxy = _list_docker_proxy_containers(runner)
    proxy = _build_proxy_snapshot(environ, ports, docker_proxy)
    return {
        'identity': _build_identity(runner, proc_path),
        'resources': _build_resources(proc_path),
        'network': _build_network(runner),
        'proxy': proxy,
        'ports': {'total': len(ports), 'items': ports[:12]},
        'services': _build_services(runner),
        'warnings': _build_warnings(proxy, ports),
        'scan': {'cost_ms': int((monotonic() - started) * 1000), 'mode': 'local-light'},
    }


def _run_command(command: list[str], timeout: float = 1.0) -> str:
    if not command or shutil.which(command[0]) is None:
        return ''
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return ''
    return (completed.stdout or completed.stderr or '').strip()


def _build_identity(runner: CommandRunner, proc_root: Path) -> dict[str, str]:
    uptime_seconds = _read_uptime_seconds(proc_root)
    return {
        'hostname': _first_line(runner(['hostname'], 0.5)) or platform.node() or 'unknown',
        'kernel': _first_line(runner(['uname', '-r'], 0.5)) or platform.release(),
        'uptime': _format_duration(uptime_seconds),
    }


def _build_resources(proc_root: Path) -> dict[str, str]:
    memory = _read_memory(proc_root)
    disk = shutil.disk_usage('/')
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0
    return {
        'load': f'{load1:.2f} / {load5:.2f} / {load15:.2f}',
        'memory': memory,
        'disk': f'{_format_bytes(disk.used)} / {_format_bytes(disk.total)}',
    }


def _build_network(runner: CommandRunner) -> dict[str, str]:
    route = _clean_default_route(_first_line(runner(['ip', 'route', 'show', 'default'], 0.7))) or 'unknown'
    windows_gateway = _default_gateway(route)
    dns_servers = _read_dns_servers(Path('/etc/resolv.conf'))
    tailscale_ip = _first_line(runner(['tailscale', 'ip', '-4'], 0.7)) or 'unknown'
    tailscale_status = _first_line(runner(['tailscale', 'status', '--self', '--peers=false'], 0.7)) or 'unknown'
    wsl_ip = _first_non_loopback_ip(runner(['hostname', '-I'], 0.5)) or 'unknown'
    host_docker_internal = _first_column(runner(['getent', 'hosts', 'host.docker.internal'], 0.5)) or 'unknown'
    return {
        'tailscale_ip': tailscale_ip,
        'tailscale_label': '当前 WSL Tailscale IP',
        'panel_tailscale_url': f'http://{tailscale_ip}:8328' if tailscale_ip != 'unknown' else 'unknown',
        'panel_tailscale_description': '当前 WSL 面板的 Tailscale 内网访问地址',
        'panel_cf_url': 'https://wsl.xinghaihub.com',
        'tailscale_status': tailscale_status,
        'default_route': route,
        'windows_gateway': windows_gateway,
        'wsl_ip': wsl_ip,
        'host_docker_internal': host_docker_internal,
        'dns': ', '.join(dns_servers[:3]) if dns_servers else 'unknown',
    }


def _build_proxy_snapshot(env: Mapping[str, str], ports: list[dict[str, str]], docker_proxy: dict[str, Any]) -> dict[str, Any]:
    http_proxy = env.get('HTTP_PROXY') or env.get('http_proxy') or ''
    https_proxy = env.get('HTTPS_PROXY') or env.get('https_proxy') or ''
    all_proxy = env.get('ALL_PROXY') or env.get('all_proxy') or ''
    no_proxy = env.get('NO_PROXY') or env.get('no_proxy') or ''
    no_proxy_items = {item.strip() for item in no_proxy.split(',') if item.strip()}
    local_proxy_ports = {item['port'] for item in ports if item.get('port') in {'7890', '7891', '7892', '1080', '10808', '20171'}}
    return {
        'enabled': bool(http_proxy or https_proxy or all_proxy or local_proxy_ports),
        'http_proxy': http_proxy or '未设置',
        'https_proxy': https_proxy or '未设置',
        'all_proxy': all_proxy or '未设置',
        'no_proxy': no_proxy or '未设置',
        'no_proxy_has_local': bool(_LOCAL_NO_PROXY.intersection(no_proxy_items)),
        'local_proxy_ports': ', '.join(sorted(local_proxy_ports)) if local_proxy_ports else '未发现',
        **docker_proxy,
    }


def _list_docker_proxy_containers(runner: CommandRunner) -> dict[str, Any]:
    output = runner(['docker', 'ps', '--format', '{{.Names}}\t{{.Ports}}'], 1.0)
    clash_names: list[str] = []
    proxy_ports: list[int] = []
    control_ports: list[int] = []
    gap_entries: list[str] = []
    for line in output.splitlines():
        name, _, port_text = line.partition('\t')
        if not name:
            continue
        normalized = name.lower()
        mappings = _parse_docker_port_mappings(port_text)
        if normalized.startswith('clash_') or 'mihomo' in normalized or 'clash' in normalized:
            if normalized.startswith('clash_') or normalized.startswith('clash-'):
                clash_names.append(name)
                for mapping in mappings:
                    if mapping['container_port'] == '7890':
                        proxy_ports.append(int(mapping['host_port']))
                    if mapping['container_port'] == '9090':
                        control_ports.append(int(mapping['host_port']))
        if normalized == 'gap-mihomo':
            gap_entries = [f"{item['address']}:{item['host_port']}" for item in mappings if item['container_port'] == '7890']
    return {
        'clash_container_count': len(clash_names),
        'clash_proxy_range': _format_port_range(proxy_ports),
        'clash_control_range': _format_port_range(control_ports),
        'gap_mihomo': ', '.join(gap_entries) if gap_entries else '未发现',
    }


def _parse_docker_port_mappings(value: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for match in _DOCKER_PUBLISHED_PORT_RE.finditer(value):
        items.append(
            {
                'address': match.group('address').strip('[]'),
                'host_port': match.group('host_port'),
                'container_port': match.group('container_port'),
            }
        )
    return items


def _format_port_range(values: list[int]) -> str:
    if not values:
        return '未发现'
    unique = sorted(set(values))
    if len(unique) == 1:
        return str(unique[0])
    if unique == list(range(unique[0], unique[-1] + 1)):
        return f'{unique[0]}-{unique[-1]}'
    return ', '.join(str(value) for value in unique[:8]) + (' ...' if len(unique) > 8 else '')


def _build_services(runner: CommandRunner) -> dict[str, str]:
    services: dict[str, str] = {}
    for key, unit in _SERVICE_NAMES.items():
        services[key] = _first_line(runner(['systemctl', 'is-active', unit], 0.7)) or 'unknown'
    return services


def _list_ports(runner: CommandRunner) -> list[dict[str, str]]:
    output = runner(['ss', '-ltnp'], 1.0)
    items: list[dict[str, str]] = []
    for line in output.splitlines():
        if 'LISTEN' not in line:
            continue
        match = _PORT_LINE_RE.search(line)
        if not match:
            continue
        process_match = _PROCESS_RE.search(line)
        items.append(
            {
                'address': match.group('address').strip('[]'),
                'port': match.group('port'),
                'process': process_match.group('name') if process_match else 'unknown',
                'pid': process_match.group('pid') if process_match and process_match.group('pid') else '',
                'scope': _port_scope(match.group('address')),
            }
        )
    priority = {'8328': 0, '8317': 1, '8420': 2, '7890': 3, '7891': 4, '7892': 5}
    return sorted(items, key=lambda item: (priority.get(item['port'], 50), int(item['port'])))


def _build_warnings(proxy: dict[str, Any], ports: list[dict[str, str]]) -> list[str]:
    warnings: list[str] = []
    if proxy['enabled'] and not proxy['no_proxy_has_local']:
        warnings.append('代理已启用，但 NO_PROXY 没有覆盖 localhost / host.docker.internal')
    if any(item['port'] == '8328' and item['scope'] == 'public' for item in ports):
        warnings.append('面板监听 0.0.0.0:8328，可通过 Tailscale 访问')
    return warnings[:5]


def _read_uptime_seconds(proc_root: Path) -> float | None:
    try:
        return float((proc_root / 'uptime').read_text(encoding='utf-8').split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_memory(proc_root: Path) -> str:
    try:
        rows = (proc_root / 'meminfo').read_text(encoding='utf-8').splitlines()
    except OSError:
        return 'unknown'
    values: dict[str, int] = {}
    for row in rows:
        key, _, rest = row.partition(':')
        if key in {'MemTotal', 'MemAvailable'}:
            try:
                values[key] = int(rest.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    if not total or available is None:
        return 'unknown'
    return f'{_format_bytes(total - available)} / {_format_bytes(total)}'


def _read_dns_servers(path: Path) -> list[str]:
    try:
        rows = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [row.split()[1] for row in rows if row.startswith('nameserver ') and len(row.split()) >= 2]


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), '')


def _first_column(value: str) -> str:
    line = _first_line(value)
    return line.split()[0] if line.split() else ''


def _first_non_loopback_ip(value: str) -> str:
    for item in value.split():
        if item.startswith(('127.', '::1', '169.254.')):
            continue
        if ':' in item:
            continue
        return item
    return ''


def _default_gateway(route: str) -> str:
    parts = route.split()
    if len(parts) >= 3 and parts[0] == 'default' and parts[1] == 'via':
        return parts[2]
    return 'unknown'


def _clean_default_route(value: str) -> str:
    parts = value.split()
    if len(parts) >= 5 and parts[0] == 'default' and parts[1] == 'via' and parts[3] == 'dev':
        return ' '.join(parts[:5])
    return value


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return 'unknown'
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f'{days}天 {hours}小时'
    if hours:
        return f'{hours}小时 {minutes}分'
    return f'{minutes}分'


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024
    return f'{size:.1f} TiB'


def _port_scope(address: str) -> str:
    cleaned = address.strip('[]')
    if cleaned in {'0.0.0.0', '::', '*'}:
        return 'public'
    if cleaned in {'127.0.0.1', '::1', 'localhost'}:
        return 'loopback'
    return 'bound'
