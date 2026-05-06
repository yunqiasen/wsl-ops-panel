import re
import subprocess
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.scanners._ids import make_encoded_asset_id

HostProcessCommandRunner = Callable[[], subprocess.CompletedProcess[str]]
_PROCESS_RE = re.compile(r'"(?P<name>[^"]+)"(?:,pid=(?P<pid>\d+))?')


def parse_listening_socket(raw: str) -> AssetSnapshot:
    parts = raw.split(maxsplit=5)
    if len(parts) < 5:
        raise ValueError(f'invalid ss output line: {raw!r}')

    _state, _recv_q, _send_q, local_address, _peer_address, *rest = parts
    process_info = rest[0] if rest else ''
    address, port = _split_address_port(local_address)
    process_name, pid = _parse_process_info(process_info)
    display_name = process_name or f'port:{port}'

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
