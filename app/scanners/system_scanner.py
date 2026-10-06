import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id

SystemCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
SYSTEM_COMMANDS: dict[str, list[str]] = {
    'apt_packages': ['bash', '-lc', 'apt list --upgradable 2>/dev/null | tail -n +2 | wc -l'],
    'docker': ['docker', '--version'],
    'docker_compose': ['docker', 'compose', 'version', '--short'],
    'tailscale': ['tailscale', 'version'],
    'cloudflared': ['/home/div/.cftunnel/bin/cloudflared', '--version'],
    'node': ['node', '--version'],
    'python': ['python3', '--version'],
    'bun': ['bun', '--version'],
    'systemd': ['systemctl', '--version'],
}
SYSTEM_UPDATE_METADATA: dict[str, dict[str, object]] = {
    'apt_packages': {
        'display_name': 'APT / 系统软件包',
        'update_supported': True,
        'supports_actions': ['update_latest'],
        'update_command': 'sudo apt update && apt list --upgradable',
        'upgrade_command': 'sudo apt upgrade -y',
        'update_note': '面板内默认只刷新软件包索引并列出可升级项，不直接执行 apt upgrade -y。',
    },
    'cloudflared': {
        'update_supported': True,
        'supports_actions': ['update_latest', 'deploy_version'],
        'update_command': '/home/div/.cftunnel/bin/cloudflared update',
        'version_command': '/home/div/.cftunnel/bin/cloudflared update --version <version>',
    },
    'bun': {
        'update_supported': True,
        'supports_actions': ['update_latest'],
        'update_command': 'bun upgrade',
    },
    'tailscale': {
        'update_supported': True,
        'supports_actions': ['update_latest', 'deploy_version'],
        'update_command': 'tailscale update --yes',
        'version_command': 'tailscale update --yes --version <version>',
        'update_note': '系统级更新，执行前确认当前网络连接。',
    },
    'docker': {'update_supported': False, 'update_note': 'Docker Engine 更新风险较高，先只展示版本。'},
    'docker_compose': {'update_supported': False, 'update_note': 'Docker Compose 跟随 Docker 安装来源，先只展示版本。'},
    'node': {'update_supported': False, 'update_note': '需要先识别 nvm/系统包来源。'},
    'python': {'update_supported': False, 'update_note': '不建议面板更新系统 Python。'},
    'systemd': {'update_supported': False, 'update_note': 'systemd 不作为面板内更新对象。'},
}
_VERSION_RE = re.compile(r'v?\d+(?:\.\d+)+(?:[-+][^\s,]+)?')


def parse_version_output(name: str, output: str) -> AssetSnapshot:
    normalized = output.strip()
    if not normalized:
        raise ValueError(f'empty version output for {name}')
    metadata = SYSTEM_UPDATE_METADATA.get(name, {})
    display_name = str(metadata.get('display_name') or name)
    if name == 'apt_packages':
        match = re.search(r'\d+', normalized)
        version = f'{match.group(0)} upgradable' if match else 'unknown'
    else:
        match = _VERSION_RE.search(normalized)
        version = match.group(0).lstrip('v') if match else None
    return AssetSnapshot(
        object_id=make_encoded_asset_id('system', name),
        category='system',
        name=display_name,
        status='available',
        current_version=version,
        supports_actions=list(metadata.get('supports_actions', [])),
        metadata={'raw_output': normalized, 'system_key': name, **metadata},
    )


def scan_system_infrastructure(
    *,
    runners: dict[str, SystemCommandRunner] | None = None,
) -> list[AssetSnapshot]:
    assets: list[AssetSnapshot] = []
    for name, command in SYSTEM_COMMANDS.items():
        runner = runners.get(name) if runners else None
        try:
            completed = runner() if runner is not None else _run_command(command)
            output = (completed.stdout or completed.stderr).strip()
            assets.append(parse_version_output(name, output))
        except FileNotFoundError:
            assets.append(
                AssetSnapshot(
                    object_id=make_encoded_asset_id('system', name),
                    category='system',
                    name=name,
                    status='missing',
                    metadata={'command': command, **SYSTEM_UPDATE_METADATA.get(name, {})},
                )
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            assets.append(
                AssetSnapshot(
                    object_id=make_encoded_asset_id('system', name),
                    category='system',
                    name=name,
                    status='error',
                    metadata={'command': command, 'error': str(exc), **SYSTEM_UPDATE_METADATA.get(name, {})},
                )
            )
    return assets


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
