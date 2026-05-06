import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id

SystemCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
SYSTEM_COMMANDS: dict[str, list[str]] = {
    'docker': ['docker', '--version'],
    'docker_compose': ['docker', 'compose', 'version', '--short'],
    'tailscale': ['tailscale', 'version'],
    'cloudflared': ['/home/div/.cftunnel/bin/cloudflared', '--version'],
    'node': ['node', '--version'],
    'python': ['python3', '--version'],
    'bun': ['bun', '--version'],
    'systemd': ['systemctl', '--version'],
}
_VERSION_RE = re.compile(r'v?\d+(?:\.\d+)+(?:[-+][^\s,]+)?')


def parse_version_output(name: str, output: str) -> AssetSnapshot:
    normalized = output.strip()
    if not normalized:
        raise ValueError(f'empty version output for {name}')
    match = _VERSION_RE.search(normalized)
    version = match.group(0).lstrip('v') if match else None
    return AssetSnapshot(
        object_id=make_encoded_asset_id('system', name),
        category='system',
        name=name,
        status='available',
        current_version=version,
        metadata={'raw_output': normalized},
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
                    metadata={'command': command},
                )
            )
        except (subprocess.CalledProcessError, ValueError) as exc:
            assets.append(
                AssetSnapshot(
                    object_id=make_encoded_asset_id('system', name),
                    category='system',
                    name=name,
                    status='error',
                    metadata={'command': command, 'error': str(exc)},
                )
            )
    return assets


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)
