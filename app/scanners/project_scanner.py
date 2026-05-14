import json
import re
from pathlib import Path

from app.models.assets import AssetSnapshot

DEFAULT_PROJECT_ROOT = Path('/home/div/1_Project_dir')
DOCKER_COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')
SKIP_NAME_PARTS = ('backup', 'backups', 'archive', 'tmp', 'cache')
_PORT_RE = re.compile(r'(?:--port\s+|PORT=)(\d{2,5})')


def scan_projects(*, root: Path | str = DEFAULT_PROJECT_ROOT) -> list[AssetSnapshot]:
    root_path = Path(root)
    if not root_path.is_dir():
        return []

    assets: list[AssetSnapshot] = []
    for child in sorted(root_path.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or _should_skip(child):
            continue
        if _has_docker_compose(child):
            continue
        stacks = _detect_stacks(child)
        if not stacks:
            continue
        git_info = _read_git_info(child)
        package_scripts = _read_package_scripts(child)
        start_command = package_scripts.get('start')
        web_ui = _detect_web_ui(child, start_command=start_command)
        capabilities = _build_project_capabilities(child, web_ui=web_ui)
        assets.append(
            AssetSnapshot(
                object_id=f'project__{_slugify(child.name)}',
                category='project',
                name=child.name,
                status='present',
                supports_actions=[],
                metadata={
                    'path': str(child),
                    'stacks': stacks,
                    'git_remote_url': git_info.get('git_remote_url'),
                    'git_branch': git_info.get('git_branch'),
                    'head_sha': git_info.get('head_sha'),
                    'discovery_source': 'filesystem_scan',
                    'package_scripts': package_scripts,
                    'start_command': start_command,
                    'web_ui': web_ui,
                    'capabilities': capabilities,
                },
            )
        )
    return assets


def _read_package_scripts(path: Path) -> dict[str, str]:
    package_json = path / 'package.json'
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
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
    return {'enabled': enabled, 'port': port, 'url_path': '/'}


def _build_project_capabilities(path: Path, *, web_ui: dict[str, object]) -> dict[str, dict[str, object]]:
    cftunnel_script = path / 'scripts' / 'cftunnel-start.sh'
    domain_file = path / 'logs' / 'cftunnel-domain.txt'
    current_url = domain_file.read_text(encoding='utf-8', errors='replace').strip() if domain_file.exists() else None
    return {
        'cf_tunnel': {
            'enabled': cftunnel_script.exists(),
            'script_path': str(cftunnel_script) if cftunnel_script.exists() else None,
            'domain_file': str(domain_file),
            'current_url': current_url,
        },
        'wechat_notify': {'enabled': False},
        'runtime_control': {'enabled': bool(web_ui.get('enabled'))},
        'autostart': {'enabled': False},
    }


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
    if (path / 'pyproject.toml').exists() or (path / 'requirements.txt').exists():
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
