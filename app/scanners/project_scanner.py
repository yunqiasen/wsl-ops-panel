import json
import re
import shutil
import subprocess
from pathlib import Path
from collections.abc import Callable

from app.models.assets import AssetSnapshot
from app.services.asset_descriptions import resolve_asset_description
from app.services.source_links import source_links_from_package_json

DEFAULT_PROJECT_ROOT = Path('/home/div/1_Project_dir')
DEFAULT_PROJECT_ROOTS = (
    DEFAULT_PROJECT_ROOT,
    DEFAULT_PROJECT_ROOT / 'AI',
    DEFAULT_PROJECT_ROOT / 'Project',
    DEFAULT_PROJECT_ROOT / 'regmail-2api' / '资源' / 'oai-cpa-tools',
)
DEFAULT_SYSTEMD_UNIT_ROOTS = (
    Path('/etc/systemd/system'),
    Path('/lib/systemd/system'),
    Path.home() / '.config' / 'systemd' / 'user',
)
DOCKER_COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')
SKIP_NAME_PARTS = ('backup', 'backups', 'archive', 'tmp', 'cache')
_PORT_RE = re.compile(r'(?:--port\s+|PORT=)(\d{2,5})')
CommandRunner = Callable[[list[str], float], str]


def scan_projects(
    *,
    root: Path | str | None = None,
    roots: list[Path | str] | tuple[Path | str, ...] | None = None,
    systemd_unit_roots: list[Path | str] | tuple[Path | str, ...] | None = None,
    command_runner: CommandRunner | None = None,
) -> list[AssetSnapshot]:
    requested_roots = list(roots) if roots is not None else [root] if root is not None else list(DEFAULT_PROJECT_ROOTS)
    unit_roots = tuple(Path(item) for item in (systemd_unit_roots or DEFAULT_SYSTEMD_UNIT_ROOTS))
    runtime_units = _detect_project_systemd_units(unit_roots, command_runner or _run_command)
    assets_by_id: dict[str, AssetSnapshot] = {}
    for item in requested_roots:
        for asset in _scan_project_root(Path(item), runtime_units=runtime_units):
            assets_by_id.setdefault(asset.object_id, asset)
    return list(assets_by_id.values())


def _scan_project_root(root_path: Path, *, runtime_units: list[dict[str, object]]) -> list[AssetSnapshot]:
    if not root_path.is_dir():
        return []

    assets: list[AssetSnapshot] = []
    root_asset = _scan_project_path(root_path, runtime_units=runtime_units)
    if root_asset is not None:
        return [root_asset]
    for child in sorted(root_path.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or _should_skip(child):
            continue
        asset = _scan_project_path(child, runtime_units=runtime_units)
        if asset is not None:
            assets.append(asset)
    return assets


def _scan_project_path(path: Path, *, runtime_units: list[dict[str, object]]) -> AssetSnapshot | None:
    if _should_skip(path) or _has_docker_compose(path):
        return None
    stacks = _detect_stacks(path)
    if not stacks:
        return None
    git_info = _read_git_info(path)
    package_payload = _read_package_json(path)
    package_scripts = _read_package_scripts_from_payload(package_payload)
    start_command = package_scripts.get('start')
    runtime = _runtime_for_project(path, runtime_units)
    runtime_command = runtime.get('exec_start') or start_command
    web_ui = _detect_web_ui(path, start_command=str(runtime_command) if runtime_command else None)
    capabilities = _build_project_capabilities(path, web_ui=web_ui, runtime=runtime)
    port = str(web_ui.get('port') or '').strip()
    metadata = {
        'path': str(path),
        'description': resolve_asset_description(path),
        'stacks': stacks,
        'git_remote_url': git_info.get('git_remote_url'),
        'source_links': source_links_from_package_json(package_payload, git_remote_url=git_info.get('git_remote_url')),
        'git_branch': git_info.get('git_branch'),
        'head_sha': git_info.get('head_sha'),
        'discovery_source': 'filesystem_scan',
        'package_scripts': package_scripts,
        'start_command': start_command,
        'web_ui': web_ui,
        'capabilities': capabilities,
        **runtime,
    }
    if port:
        metadata['ports'] = f'{port}/tcp'
        metadata['display_ports'] = port
        metadata['primary_public_port'] = port
    return AssetSnapshot(
        object_id=f'project__{_slugify(path.name)}',
        category='project',
        name=path.name,
        status=str(runtime.get('status') or 'present'),
        supports_actions=[],
        metadata=metadata,
    )

def _read_package_json(path: Path) -> dict[str, object]:
    package_json = path / 'package.json'
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_package_scripts_from_payload(payload: dict[str, object]) -> dict[str, str]:
    scripts = payload.get('scripts')
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items() if isinstance(value, str)}


def _detect_web_ui(path: Path, *, start_command: str | None) -> dict[str, object]:
    port = None
    if start_command:
        match = _PORT_RE.search(start_command)
        if match:
            port = match.group(1)
    enabled = bool(port or (path / 'scripts' / 'cftunnel-start.sh').exists())
    return {'enabled': enabled, 'port': port, 'url_path': _detect_default_url_path(path)}


def _build_project_capabilities(path: Path, *, web_ui: dict[str, object], runtime: dict[str, str]) -> dict[str, dict[str, object]]:
    cftunnel_script = path / 'scripts' / 'cftunnel-start.sh'
    domain_file = path / 'logs' / 'cftunnel-domain.txt'
    current_url = domain_file.read_text(encoding='utf-8', errors='replace').strip() if domain_file.exists() else None
    has_service_unit = bool(runtime.get('service_unit'))
    return {
        'cf_tunnel': {
            'enabled': cftunnel_script.exists(),
            'script_path': str(cftunnel_script) if cftunnel_script.exists() else None,
            'domain_file': str(domain_file),
            'current_url': current_url,
            'supported_actions': ['cf_create', 'cf_refresh', 'cf_disable'] if cftunnel_script.exists() else [],
        },
        'wechat_notify': {'enabled': False},
        'runtime_control': {'enabled': has_service_unit or bool(web_ui.get('enabled'))},
        'autostart': {'enabled': has_service_unit},
    }


def _detect_project_systemd_units(
    unit_roots: tuple[Path, ...],
    runner: CommandRunner,
) -> list[dict[str, object]]:
    units: list[dict[str, object]] = []
    for root in unit_roots:
        if not root.is_dir():
            continue
        user_scope = _is_user_systemd_root(root)
        for unit_path in sorted(root.glob('*.service')):
            payload: dict[str, object] = _parse_systemd_unit(unit_path)
            project_dir = payload.get('working_directory')
            if not project_dir:
                continue
            unit_name = unit_path.name
            payload['service_unit'] = unit_name
            payload['service_unit_path'] = str(unit_path)
            payload['service_scope'] = 'user' if user_scope else 'system'
            command = ['systemctl', '--user', 'is-active', unit_name] if user_scope else ['systemctl', 'is-active', unit_name]
            status = _first_line(runner(command, 0.5)) or 'unknown'
            exec_start = str(payload.get('exec_start') or '')
            port_match = _PORT_RE.search(exec_start)
            if status not in {'active', 'running'} and port_match:
                port = port_match.group(1)
                if re.search(rf':{re.escape(port)}\b', runner(['ss', '-ltn'], 0.5)):
                    status = 'active'
                    payload['status_source'] = 'listening_port'
            payload['status'] = status
            units.append(payload)
    return units


def _is_user_systemd_root(path: Path) -> bool:
    normalized = path.as_posix().rstrip('/')
    return normalized.endswith('/.config/systemd/user') or '/systemd/user' in normalized

def _parse_systemd_unit(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    try:
        rows = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return payload
    for raw_row in rows:
        row = raw_row.strip()
        if not row or row.startswith('#') or '=' not in row:
            continue
        key, value = row.split('=', 1)
        if key == 'WorkingDirectory':
            payload['working_directory'] = value.strip()
        elif key == 'ExecStart':
            payload['exec_start'] = value.strip()
    return payload


def _runtime_for_project(path: Path, runtime_units: list[dict[str, object]]) -> dict[str, object]:
    resolved = str(path.resolve())
    direct_matches: list[dict[str, object]] = []
    related_matches: list[tuple[int, dict[str, object]]] = []
    for payload in runtime_units:
        unit_project_dir = str(payload.get('working_directory') or '')
        exec_start = str(payload.get('exec_start') or '')
        if unit_project_dir == resolved:
            direct_matches.append(payload)
            continue
        score = 0
        if unit_project_dir.startswith(resolved + '/'):
            score += 30
        if resolved in exec_start:
            score += 50
        unit_name = str(payload.get('service_unit') or '')
        if unit_name.removesuffix('.service') == path.name:
            score += 100
        if score:
            related_matches.append((score, payload))

    if direct_matches:
        selected = [(200, payload) for payload in direct_matches]
    else:
        selected = related_matches
    if not selected:
        return {}

    selected.sort(key=lambda item: (-item[0], str(item[1].get('service_unit') or '')))
    primary = dict(selected[0][1])
    primary['service_units'] = sorted(
        {str(payload.get('service_unit')) for _, payload in selected if payload.get('service_unit')}
    )
    return primary

def _detect_default_url_path(path: Path) -> str:
    router = path / 'ui' / 'frontend' / 'src' / 'router' / 'index.ts'
    try:
        text = router.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return '/'
    redirect_match = re.search(r"redirect:\s*['\"](/[^'\"]*)['\"]", text)
    if redirect_match:
        return redirect_match.group(1)
    match = re.search(r"path:\s*['\"](/[^'\"]*)['\"]", text)
    return match.group(1) if match else '/'


def _run_command(command: list[str], timeout: float = 1.0) -> str:
    if not command or shutil.which(command[0]) is None:
        return ''
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (completed.stdout or completed.stderr or '').strip()


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), '')


def _should_skip(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith('.'):
        return True
    return any(part in name for part in SKIP_NAME_PARTS)


def _has_docker_compose(path: Path) -> bool:
    return any((path / name).exists() for name in DOCKER_COMPOSE_FILES)


def _detect_stacks(path: Path) -> list[str]:
    stacks: list[str] = []
    if (path / 'package.json').exists():
        stacks.append('node')
    if (
        (path / 'pyproject.toml').exists()
        or (path / 'requirements.txt').exists()
        or any(path.glob('*.py'))
    ):
        stacks.append('python')
    if (path / 'go.mod').exists():
        stacks.append('go')
    if (path / 'Cargo.toml').exists():
        stacks.append('rust')
    if (path / '.git').exists():
        stacks.append('git')
    return stacks


def _read_git_info(project_path: Path) -> dict[str, str | None]:
    git_dir = project_path / '.git'
    if not git_dir.exists():
        return {'git_remote_url': None, 'git_branch': None, 'head_sha': None}

    remote_url = None
    config_path = git_dir / 'config'
    if config_path.exists():
        in_origin = False
        for raw_line in config_path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = raw_line.strip()
            if line.startswith('[remote "origin"'):
                in_origin = True
                continue
            if line.startswith('['):
                in_origin = False
            if in_origin and line.startswith('url ='):
                remote_url = line.split('=', 1)[1].strip()
                break

    branch = None
    head_sha = None
    head_path = git_dir / 'HEAD'
    if head_path.exists():
        head_value = head_path.read_text(encoding='utf-8', errors='replace').strip()
        if head_value.startswith('ref:'):
            ref = head_value.split(None, 1)[1].strip()
            branch = ref.removeprefix('refs/heads/')
            ref_path = git_dir / ref
            if ref_path.exists():
                head_sha = ref_path.read_text(encoding='utf-8', errors='replace').strip()
        else:
            head_sha = head_value or None
    return {'git_remote_url': remote_url, 'git_branch': branch, 'head_sha': head_sha}


def _slugify(value: str) -> str:
    normalized = ''.join(ch.lower() if ch.isalnum() else '-' for ch in value.strip())
    normalized = '-'.join(part for part in normalized.split('-') if part)
    return normalized or 'project'
