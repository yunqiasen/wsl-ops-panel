from pathlib import Path

from app.models.assets import AssetSnapshot

DEFAULT_PROJECT_ROOT = Path('/home/div/1_Project_dir')
DOCKER_COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')
SKIP_NAME_PARTS = ('backup', 'backups', 'archive', 'tmp', 'cache')


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
                },
            )
        )
    return assets


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
