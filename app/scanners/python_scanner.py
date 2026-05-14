import json
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id
from app.services.source_links import build_source_links

PythonCommandRunner = Callable[[], subprocess.CompletedProcess[str]]


def parse_pip_package(entry: dict) -> AssetSnapshot:
    name = entry.get('name')
    version = entry.get('version')
    if not isinstance(name, str) or not name:
        raise ValueError(f'invalid pip package entry: {entry!r}')
    if not isinstance(version, str) or not version:
        raise ValueError(f'invalid pip package entry: {entry!r}')

    return AssetSnapshot(
        object_id=make_encoded_asset_id('python', name),
        category='python',
        name=name,
        status='installed',
        current_version=version,
        metadata={'package_manager': 'pip', 'source_links': build_source_links(package_name=name, package_manager='pip')},
    )


def scan_python_packages(*, runner: PythonCommandRunner | None = None) -> list[AssetSnapshot]:
    completed = (runner or _run_pip_list)()
    stdout = completed.stdout.strip()
    if not stdout:
        return []

    payload = json.loads(stdout)
    if not isinstance(payload, list):
        raise ValueError('pip list output must be a JSON array')

    assets: list[AssetSnapshot] = []
    for entry in payload:
        try:
            assets.append(parse_pip_package(entry))
        except ValueError:
            continue
    return assets


def _run_pip_list() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['python3', '-m', 'pip', 'list', '--format=json'],
        check=True,
        capture_output=True,
        text=True,
    )
