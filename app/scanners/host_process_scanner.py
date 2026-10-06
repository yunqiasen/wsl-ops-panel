import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id
from app.services.host_ownership import attach_owner, inspect_containers

HostProcessCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
_PROCESS_RE = re.compile(r'"(?P<name>[^"]+)"(?:,pid=(?P<pid>\d+))?')
KNOWN_PORTS: dict[str, dict[str, str]] = {
    '8317': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA API / CLIProxyAPI 网页与 API 入口'},
    '11451': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口'},
    '51121': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口'},
    '54545': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口'},
    '8128': {'service_hint': 'openai-cpa', 'purpose': 'openai-cpa 注册系统页面'},
    '45345': {'service_hint': 'regmail UI', 'purpose': 'regmail UI 页面'},
    '8420': {'service_hint': 'Sub2API', 'purpose': 'Sub2API OpenAI 兼容接口'},
    '38217': {'service_hint': 'New API', 'purpose': 'New API 管理后台与模型转发入口'},
    '39281': {'service_hint': 'SearXNG', 'purpose': 'SearXNG 搜索服务入口'},
    '8312': {'service_hint': 'image2api', 'purpose': 'image2api / chatgpt2api 入口'},
    '8328': {'service_hint': 'WSL Ops Panel', 'purpose': 'WSL 维护更新管理面板'},
    '8228': {'service_hint': 'freemail-proxy', 'purpose': 'freemail 反代入口'},
    '8787': {'service_hint': 'ip-convert-static', 'purpose': 'IP 转换静态页面'},
    '7890': {'service_hint': 'Clash / Mihomo', 'purpose': '本地代理 HTTP/SOCKS 入口'},
    '2222': {'service_hint': 'SSH', 'purpose': 'SSH 远程登录入口'},
    '50222': {'service_hint': 'Tailscale SSH', 'purpose': 'Tailscale SSH / WSL 远程访问入口'},
}


def parse_listening_socket(raw: str) -> AssetSnapshot:
    parts = raw.split(maxsplit=5)
    if len(parts) < 5:
        raise ValueError(f'invalid ss output line: {raw!r}')

    _state, _recv_q, _send_q, local_address, _peer_address, *rest = parts
    process_info = rest[0] if rest else ''
    address, port = _split_address_port(local_address)
    process_name, pid = _parse_process_info(process_info)
    display_name = process_name or f'port:{port}'
    enrichment = _enrich_port(port, process_name)

    return AssetSnapshot(
        object_id=make_encoded_asset_id('host', f'{address}|{port}|{pid or ""}|{process_name or ""}'),
        category='host',
        name=display_name,
        status='listening',
        metadata={
            'local_address': address,
            'port': port,
            'process_name': process_name,
            'pid': pid,
            **enrichment,
            'target_asset_id': None,
            'target_container_name': None,
            'target_unit_name': None,
            'raw': raw,
        },
    )


def scan_host_processes(*, runner: HostProcessCommandRunner | None = None) -> list[AssetSnapshot]:
    completed = (runner or _run_ss)()
    stdout = completed.stdout.strip()
    if not stdout:
        return []

    assets: list[AssetSnapshot] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('State '):
            continue
        try:
            assets.append(parse_listening_socket(stripped))
        except ValueError:
            continue
    containers = []
    if any(a.metadata.get('process_name') == 'docker-proxy' for a in assets):
        try:
            containers = inspect_containers()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    return [attach_owner(asset, containers=containers) for asset in assets]


def _run_ss() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['ss', '-ltnp'],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _split_address_port(value: str) -> tuple[str, str]:
    if value.startswith('['):
        match = re.match(r'^\[(?P<address>.*)\]:(?P<port>\d+)$', value)
        if not match:
            raise ValueError(f'invalid local address: {value!r}')
        return match.group('address'), match.group('port')

    if ':' not in value:
        raise ValueError(f'invalid local address: {value!r}')
    address, port = value.rsplit(':', 1)
    return address, port


def _parse_process_info(value: str) -> tuple[str | None, int | None]:
    match = _PROCESS_RE.search(value)
    if not match:
        return None, None
    pid = int(match.group('pid')) if match.group('pid') else None
    return match.group('name'), pid


def _enrich_port(port: str, process_name: str | None) -> dict[str, str]:
    known = KNOWN_PORTS.get(port, {})
    owner_type = 'unknown' if not process_name or process_name == 'docker-proxy' else 'process'
    return {
        'service_hint': known.get('service_hint', process_name or f'port:{port}'),
        'purpose': known.get('purpose', '未知端口。可通过 PID / systemd / docker 进一步确认。'),
        'owner_type': owner_type,
        'port_action': owner_type,
        'target_container_name': '',
    }
