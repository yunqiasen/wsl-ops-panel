import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id
from app.services.source_links import build_source_links

NodeCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
_TREE_PREFIX_RE = re.compile(r'^[\s│├└─]+')


def parse_npm_package(raw: str) -> AssetSnapshot:
    normalized = _normalize_npm_line(raw)
    if not normalized or normalized.startswith('/'):
        raise ValueError(f'invalid npm package line: {raw!r}')

    split_at = normalized.rfind('@')
    if split_at <= 0 or split_at == len(normalized) - 1:
        raise ValueError(f'invalid npm package line: {raw!r}')

    name = normalized[:split_at]
    version = normalized[split_at + 1 :]
    return AssetSnapshot(
        object_id=make_encoded_asset_id('node', name),
        category='node',
        name=name,
        status='installed',
        current_version=version,
        metadata={'package_manager': 'npm', 'source_links': build_source_links(package_name=name, package_manager='npm')},
    )


def scan_node_packages(*, runner: NodeCommandRunner | None = None) -> list[AssetSnapshot]:
    completed = (runner or _run_npm_list)()
    stdout = completed.stdout.strip()
    if not stdout:
        return []

    assets: list[AssetSnapshot] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            assets.append(parse_npm_package(stripped))
        except ValueError:
            continue
    return assets


def _run_npm_list() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['npm', 'list', '-g', '--depth=0', '--parseable=false', '--silent'],
        check=True,
        capture_output=True,
        text=True,
    )


def _normalize_npm_line(raw: str) -> str:
    return _TREE_PREFIX_RE.sub('', raw.strip())
