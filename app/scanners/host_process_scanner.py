import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id

HostProcessCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
_PROCESS_RE = re.compile(r'"(?P<name>[^"]+)"(?:,pid=(?P<pid>\d+))?')
KNOWN_PORTS: dict[str, dict[str, str]] = {
    '8317': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA API / CLIProxyAPI 网页与 API 入口', 'owner_type': 'docker', 'target_container_name': 'cli-proxy-api'},
    '11451': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口', 'owner_type': 'docker', 'target_container_name': 'cli-proxy-api'},
    '51121': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口', 'owner_type': 'docker', 'target_container_name': 'cli-proxy-api'},
    '54545': {'service_hint': 'CPA / CLIProxyAPI', 'purpose': 'CPA 附加监听端口', 'owner_type': 'docker', 'target_container_name': 'cli-proxy-api'},
    '8128': {'service_hint': 'openai-cpa', 'purpose': 'openai-cpa 注册系统页面', 'owner_type': 'docker', 'target_container_name': 'wenfxl_codex_manager'},
    '45345': {'service_hint': 'regmail UI', 'purpose': 'regmail UI 页面', 'owner_type': 'docker'},
    '8420': {'service_hint': 'Sub2API', 'purpose': 'Sub2API OpenAI 兼容接口', 'owner_type': 'docker', 'target_container_name': 'sub2api'},
    '38217': {'service_hint': 'New API', 'purpose': 'New API 管理后台与模型转发入口', 'owner_type': 'docker', 'target_container_name': 'new-api'},
    '39281': {'service_hint': 'SearXNG', 'purpose': 'SearXNG 搜索服务入口', 'owner_type': 'docker', 'target_container_name': 'searxng'},
    '8312': {'service_hint': 'image2api', 'purpose': 'image2api / chatgpt2api 入口', 'owner_type': 'docker', 'target_container_name': 'image2api'},
    '8328': {'service_hint': 'WSL Ops Panel', 'purpose': 'WSL 维护更新管理面板', 'owner_type': 'systemd'},
    '8228': {'service_hint': 'freemail-proxy', 'purpose': 'freemail 反代入口', 'owner_type': 'docker', 'target_container_name': 'freemail-proxy'},
    '8787': {'service_hint': 'ip-convert-static', 'purpose': 'IP 转换静态页面', 'owner_type': 'docker', 'target_container_name': 'ip-convert-static'},
    '7890': {'service_hint': 'Clash / Mihomo', 'purpose': '本地代理 HTTP/SOCKS 入口', 'owner_type': 'process'},
    '2222': {'service_hint': 'SSH', 'purpose': 'SSH 远程登录入口', 'owner_type': 'systemd'},
    '50222': {'service_hint': 'Tailscale SSH', 'purpose': 'Tailscale SSH / WSL 远程访问入口', 'owner_type': 'systemd'},
}
DOCKER_PORT_TO_ASSET: dict[str, str] = {
    '8317': 'cpa',
    '11451': 'cpa',
    '51121': 'cpa',
    '54545': 'cpa',
    '8128': 'openai_cpa',
    '8420': 'sub2api',
    '38217': 'new_api',
    '39281': 'searxng',
    '8312': 'docker__image2api',
    '8228': 'docker__freemail-proxy',
    '8787': 'docker__ip-转换',
}
SYSTEMD_PORT_TO_UNIT: dict[str, str] = {
    '8328': 'wsl-ops-panel.service',
    '2222': 'ssh.service',
    '50222': 'tailscaled.service',
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
            'target_asset_id': DOCKER_PORT_TO_ASSET.get(port) if enrichment.get('owner_type') == 'docker' else None,
            'target_container_name': enrichment.get('target_container_name') if enrichment.get('owner_type') == 'docker' else None,
            'target_unit_name': SYSTEMD_PORT_TO_UNIT.get(port) if enrichment.get('owner_type') == 'systemd' else None,
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
    return assets


def _run_ss() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['ss', '-ltnp'],
        check=True,
        capture_output=True,
        text=True,
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
    owner_type = known.get('owner_type')
    if not owner_type and process_name:
        owner_type = 'docker' if process_name == 'docker-proxy' else 'process'
    return {
        'service_hint': known.get('service_hint', process_name or f'port:{port}'),
        'purpose': known.get('purpose', '未知端口。可通过 PID / systemd / docker 进一步确认。'),
        'owner_type': owner_type or 'unknown',
        'port_action': owner_type or 'unknown',
        'target_container_name': known.get('target_container_name', ''),
    }
