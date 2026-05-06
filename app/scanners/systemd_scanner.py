import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot

SystemdCommandRunner = Callable[[], subprocess.CompletedProcess[str]]


def parse_systemctl_line(line: str) -> AssetSnapshot:
    normalized = line.strip()
    if normalized.startswith('● '):
        normalized = normalized[2:].strip()

    parts = normalized.split(maxsplit=4)
    if len(parts) < 4:
        raise ValueError(f'invalid systemctl line: {line!r}')

    unit, load_state, active_state, sub_state, *rest = parts
    description = rest[0] if rest else ''
    return AssetSnapshot(
        object_id=unit,
        category='systemd',
        name=unit,
        status=active_state,
        metadata={
            'load_state': load_state,
            'sub': sub_state,
            'description': description,
        },
    )


def scan_systemd_units(*, runner: SystemdCommandRunner | None = None) -> list[AssetSnapshot]:
    completed = (runner or _run_systemctl_list_units)()
    stdout = completed.stdout.strip()
    if not stdout:
        return []

    assets: list[AssetSnapshot] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            assets.append(parse_systemctl_line(stripped))
        except ValueError:
            continue
    return assets


def _run_systemctl_list_units() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['systemctl', 'list-units', '--type=service', '--all', '--plain', '--no-legend', '--no-pager'],
        check=True,
        capture_output=True,
        text=True,
    )
