import logging
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from subprocess import CalledProcessError

from app.models.assets import AssetSnapshot, DockerContainerSnapshot, PackageVersionInfo
from app.models.registry import ObjectDefinition, RegistrySnapshot
from app.models.recipes import DockerRecipe
from app.recipes.service import DockerRecipeService
from app.registry.service import RegistryService
from app.scanners.docker_scanner import scan_docker_containers
from app.scanners.host_process_scanner import scan_host_processes
from app.scanners.node_scanner import scan_node_packages
from app.scanners.python_scanner import scan_python_packages
from app.scanners.project_scanner import scan_projects
from app.scanners.system_scanner import scan_system_infrastructure
from app.scanners.systemd_scanner import scan_systemd_units
from app.services.asset_policies import AssetPolicyService
from app.services.docker_versions import DockerVersionService

LOGGER = logging.getLogger(__name__)
DockerScanner = Callable[[], list[DockerContainerSnapshot]]
SystemdScanner = Callable[[], list[AssetSnapshot]]
ReadonlyScanner = Callable[[], list[AssetSnapshot]]
DOCKER_SUPPORTED_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']
DOCKER_DISCOVERED_SUPPORTED_ACTIONS = [
    'update_latest',
    'deploy_version',
    'delete',
    'full_delete',
    'start',
    'stop',
    'autostart_enable',
    'autostart_disable',
    'cf_create',
    'cf_refresh',
    'cf_disable',
    'notify_send',
]
SYSTEMD_SUPPORTED_ACTIONS = ['delete']
READONLY_CATEGORY_IDS = ('node', 'python', 'project', 'host', 'system', 'agent_cli', 'agent')


class AssetService:
    def __init__(
        self,
        registry_service: RegistryService,
        *,
        docker_scanner: DockerScanner | None = None,
        systemd_scanner: SystemdScanner | None = None,
        node_scanner: ReadonlyScanner | None = None,
        python_scanner: ReadonlyScanner | None = None,
        host_process_scanner: ReadonlyScanner | None = None,
        system_infra_scanner: ReadonlyScanner | None = None,
        project_scanner: ReadonlyScanner | None = None,
        docker_recipe_service: DockerRecipeService | None = None,
        docker_version_service: DockerVersionService | None = None,
        config_root: Path | str = Path('config'),
    ) -> None:
        self._registry_service = registry_service
        self._docker_scanner = docker_scanner or scan_docker_containers
        self._systemd_scanner = systemd_scanner or scan_systemd_units
        self._node_scanner = node_scanner or scan_node_packages
        self._python_scanner = python_scanner or scan_python_packages
        self._host_process_scanner = host_process_scanner or scan_host_processes
        self._system_infra_scanner = system_infra_scanner or scan_system_infrastructure
        self._project_scanner = project_scanner or scan_projects
        self._docker_recipe_service = docker_recipe_service
        self._docker_version_service = docker_version_service or DockerVersionService()
        self._policy_service = AssetPolicyService(Path(config_root))

    def list_assets(self, category_id: str) -> list[AssetSnapshot]:
        snapshot = self._registry_service.snapshot
        return self._build_assets_for_category(snapshot, category_id)

    def get_asset(self, object_id: str) -> AssetSnapshot | None:
        snapshot = self._registry_service.snapshot
        object_map = {obj.id: obj for obj in snapshot.objects}
        target = object_map.get(object_id)
        if target is not None:
            assets = self._build_assets_for_category(snapshot, target.category)
            for asset in assets:
                if asset.object_id == object_id:
                    return asset
            return None

        for category_id in READONLY_CATEGORY_IDS:
            for asset in self._build_assets_for_category(snapshot, category_id):
                if asset.object_id == object_id:
                    return asset
        return None

    def _build_assets_for_category(self, snapshot: RegistrySnapshot, category_id: str) -> list[AssetSnapshot]:
        if category_id == 'docker':
            return build_docker_asset_snapshots(
                snapshot,
                self._scan_docker_containers(),
                recipe_service=self._docker_recipe_service,
                version_service=self._docker_version_service,
                resolve_remote_versions=False,
            )
        if category_id == 'systemd':
            scanned_units, scan_failed = self._scan_systemd_units()
            return build_systemd_asset_snapshots(snapshot, scanned_units, scan_failed=scan_failed)
        if category_id == 'node':
            return [self._policy_service.apply(asset) for asset in self._scan_readonly_assets(self._node_scanner, 'node')]
        if category_id == 'python':
            return [self._policy_service.apply(asset) for asset in self._scan_readonly_assets(self._python_scanner, 'python')]
        if category_id == 'project':
            return self._scan_readonly_assets(self._project_scanner, 'project')
        if category_id == 'host':
            return self._scan_readonly_assets(self._host_process_scanner, 'host')
        if category_id == 'system':
            return self._scan_readonly_assets(self._system_infra_scanner, 'system')
        if category_id in {'agent_cli', 'agent'}:
            return []
        return []

    def _scan_docker_containers(self) -> list[DockerContainerSnapshot]:
        try:
            return self._docker_scanner()
        except (CalledProcessError, FileNotFoundError):
            return []

    def _scan_systemd_units(self) -> tuple[list[AssetSnapshot], bool]:
        try:
            return self._systemd_scanner(), False
        except (CalledProcessError, FileNotFoundError) as exc:
            LOGGER.warning('systemd scan failed: %s', exc)
            return [], True

    def _scan_readonly_assets(self, scanner: ReadonlyScanner, category_id: str) -> list[AssetSnapshot]:
        try:
            return scanner()
        except (CalledProcessError, FileNotFoundError, ValueError) as exc:
            LOGGER.warning('%s scan failed: %s', category_id, exc)
            return []


def build_docker_asset_snapshots(
    registry_snapshot: RegistrySnapshot,
    containers: list[DockerContainerSnapshot],
    *,
    recipe_service: DockerRecipeService | None = None,
    version_service: DockerVersionService | None = None,
    resolve_remote_versions: bool = True,
) -> list[AssetSnapshot]:
    docker_version_service = version_service or DockerVersionService()
    containers_by_dir: dict[str, list[DockerContainerSnapshot]] = defaultdict(list)
    containers_by_name = {container.name: container for container in containers}

    for container in containers:
        if container.compose_working_dir:
            containers_by_dir[container.compose_working_dir].append(container)

    assets: list[AssetSnapshot] = []
    managed_project_dirs: set[str] = set()
    for obj in registry_snapshot.objects:
        if obj.category != 'docker' or not obj.enabled:
            continue

        object_containers = sorted(containers_by_dir.get(obj.config['project_dir'], []), key=lambda item: item.name)
        primary = _select_primary_container(obj, object_containers, containers_by_name)
        status = primary.status if primary is not None else 'not running'
        recipe = recipe_service.get(obj.config.get('recipe_id')) if recipe_service is not None else None
        lifecycle_strategy = obj.config.get('lifecycle_strategy', 'compose_pull')
        runtime = docker_version_service.build_runtime_version_info(primary)
        version_info = _build_docker_version_info(
            obj,
            primary,
            version_service=docker_version_service,
            recipe=recipe,
            lifecycle_strategy=lifecycle_strategy,
            resolve_remote_versions=resolve_remote_versions,
        )

        managed_project_dirs.add(obj.config['project_dir'])
        assets.append(
            AssetSnapshot(
                object_id=obj.id,
                category=obj.category,
                name=obj.name,
                status=status,
                current_version=version_info.current_version,
                latest_version=version_info.latest_version,
                supports_actions=DOCKER_SUPPORTED_ACTIONS.copy(),
                metadata={
                    'type': obj.type,
                    'project_dir': obj.config['project_dir'],
                    'compose_file': obj.config['compose_file'],
                    'primary_container': obj.config.get('primary_container'),
                    'compose_service': obj.config.get('compose_service'),
                    'recipe_id': recipe.id if recipe is not None else obj.config.get('recipe_id'),
                    'lifecycle_strategy': lifecycle_strategy,
                    'version_source': obj.config.get('version_source', 'registry_tags'),
                    'available_versions': version_info.versions,
                    'managed_services': version_info.managed_services,
                    'ignored_services': version_info.ignored_services,
                    'runtime': version_info.runtime.model_dump() if version_info.runtime is not None else None,
                    'source_status': version_info.source_status,
                    'error': version_info.error,
                    'version_source_status': version_info.source_status,
                    'runtime_image_tag': runtime.image_tag,
                    'runtime_oci_version': runtime.oci_version,
                    'runtime_oci_revision': runtime.oci_revision,
                },
                containers=object_containers,
                primary_container_name=primary.name if primary is not None else None,
            )
        )
    assets.extend(_build_runtime_discovered_docker_assets(containers_by_dir, managed_project_dirs, docker_version_service))
    return assets


def _build_runtime_discovered_docker_assets(
    containers_by_dir: dict[str, list[DockerContainerSnapshot]],
    managed_project_dirs: set[str],
    version_service: DockerVersionService,
) -> list[AssetSnapshot]:
    assets: list[AssetSnapshot] = []
    for project_dir, project_containers in sorted(containers_by_dir.items()):
        if project_dir in managed_project_dirs:
            continue
        containers = sorted(project_containers, key=lambda item: item.name)
        primary = containers[0] if containers else None
        if primary is None:
            continue
        project_path = Path(project_dir)
        compose_file = _detect_compose_file(project_path)
        compose_service = primary.compose_service or primary.name
        image_repository = _parse_image_repository(primary.image)
        runtime = version_service.build_runtime_version_info(primary)
        git_info = _read_git_info(project_path)
        capabilities = _build_discovered_capabilities(project_path, image_repository=image_repository, git_remote_url=git_info.get('git_remote_url'))
        name = project_path.name or primary.compose_project or primary.name
        assets.append(
            AssetSnapshot(
                object_id=f"docker__{_slugify(project_path.name or primary.compose_project or primary.name)}",
                category='docker',
                name=name,
                status=primary.status,
                current_version=primary.image_tag,
                supports_actions=DOCKER_DISCOVERED_SUPPORTED_ACTIONS.copy(),
                metadata={
                    'type': 'docker_compose_discovered',
                    'discovery_source': 'runtime_discovered',
                    'project_dir': project_dir,
                    'compose_file': compose_file,
                    'primary_container': primary.name,
                    'compose_service': compose_service,
                    'compose_project': primary.compose_project,
                    'image_repository': image_repository,
                    'lifecycle_strategy': 'compose_pull',
                    'version_source': 'registry_tags' if image_repository else 'unknown',
                    'available_versions': [],
                    'runtime': runtime.model_dump(),
                    'source_status': 'deferred',
                    'version_source_status': 'deferred',
                    'runtime_image_tag': runtime.image_tag,
                    'runtime_oci_version': runtime.oci_version,
                    'runtime_oci_revision': runtime.oci_revision,
                    'ports': primary.ports,
                    'capabilities': capabilities,
                    **git_info,
                },
                containers=containers,
                primary_container_name=primary.name,
            )
        )
    return assets


def _slugify(value: str) -> str:
    normalized = ''.join(ch.lower() if ch.isalnum() else '-' for ch in value.strip())
    normalized = '-'.join(part for part in normalized.split('-') if part)
    return normalized or 'discovered'


def _detect_compose_file(project_path: Path) -> str:
    for name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'):
        if (project_path / name).exists():
            return name
    return 'docker-compose.yml'


def _parse_image_repository(image: str | None) -> str | None:
    if not image:
        return None
    image = image.split('@', 1)[0]
    last_slash = image.rfind('/')
    last_colon = image.rfind(':')
    if last_colon > last_slash:
        return image[:last_colon]
    return image


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


def _build_discovered_capabilities(
    project_path: Path,
    *,
    image_repository: str | None,
    git_remote_url: str | None,
) -> dict[str, dict[str, object]]:
    cftunnel_script = project_path / 'scripts' / 'cftunnel-start.sh'
    domain_file = project_path / 'logs' / 'cftunnel-domain.txt'
    current_url = domain_file.read_text(encoding='utf-8', errors='replace').strip() if domain_file.exists() else None
    return {
        'runtime_control': {'enabled': True, 'supported_actions': ['start', 'stop']},
        'versioning': {'enabled': bool(image_repository), 'supported_actions': ['update_latest', 'deploy_version'] if image_repository else []},
        'repo_metadata': {'enabled': bool(git_remote_url), 'supported_actions': []},
        'cf_tunnel': {
            'enabled': cftunnel_script.exists(),
            'supported_actions': ['cf_create', 'cf_refresh', 'cf_disable'] if cftunnel_script.exists() else [],
            'script_path': str(cftunnel_script) if cftunnel_script.exists() else None,
            'domain_file': str(domain_file),
            'current_url': current_url,
        },
        'wechat_notify': {'enabled': False, 'supported_actions': ['notify_send']},
        'autostart': {'enabled': False, 'supported_actions': ['autostart_enable', 'autostart_disable']},
    }


def build_systemd_asset_snapshots(
    registry_snapshot: RegistrySnapshot,
    scanned_units: list[AssetSnapshot],
    *,
    scan_failed: bool = False,
) -> list[AssetSnapshot]:
    units_by_name = {unit.name: unit for unit in scanned_units}
    assets: list[AssetSnapshot] = []

    for obj in registry_snapshot.objects:
        if obj.category != 'systemd' or not obj.enabled:
            continue

        unit_name = obj.config['unit_name']
        scanned = units_by_name.get(unit_name)
        status = 'scan_failed' if scan_failed and scanned is None else (scanned.status if scanned is not None else 'inactive')
        metadata = {
            'type': obj.type,
            'unit_name': unit_name,
            'working_dir': obj.config['working_dir'],
            'sub': scanned.metadata.get('sub') if scanned else ('scan_failed' if scan_failed else 'unknown'),
            'description': scanned.metadata.get('description') if scanned else '',
        }
        managed_project_dirs.add(obj.config['project_dir'])
        assets.append(
            AssetSnapshot(
                object_id=obj.id,
                category=obj.category,
                name=obj.name,
                status=status,
                supports_actions=SYSTEMD_SUPPORTED_ACTIONS.copy(),
                metadata=metadata,
            )
        )
    return assets


def _select_primary_container(
    obj: ObjectDefinition,
    object_containers: list[DockerContainerSnapshot],
    containers_by_name: dict[str, DockerContainerSnapshot],
) -> DockerContainerSnapshot | None:
    configured_service = obj.config.get('compose_service')
    if configured_service:
        for container in object_containers:
            if container.compose_service == configured_service:
                return container

    configured_primary = obj.config.get('primary_container')
    if configured_primary:
        primary = containers_by_name.get(configured_primary)
        if primary is not None:
            return primary

    normalized_object_id = obj.id.replace('_', '-')
    for container in object_containers:
        if container.name == normalized_object_id:
            return container

    return object_containers[0] if object_containers else None


def _build_docker_version_info(
    obj: ObjectDefinition,
    primary: DockerContainerSnapshot | None,
    *,
    version_service: DockerVersionService,
    recipe: DockerRecipe | None,
    lifecycle_strategy: str,
    resolve_remote_versions: bool,
) -> PackageVersionInfo:
    runtime = version_service.build_runtime_version_info(primary)
    managed_services = list(recipe.managed_services) if recipe is not None else list(obj.config.get('managed_services', []))
    ignored_services = list(recipe.ignored_services) if recipe is not None else list(obj.config.get('ignored_services', []))
    if lifecycle_strategy == 'compose_local_build_git_tag' and recipe is not None:
        if not resolve_remote_versions:
            return PackageVersionInfo(
                current_version=primary.image_tag if primary is not None else None,
                runtime=runtime,
                lifecycle_strategy=lifecycle_strategy,
                version_source=obj.config.get('version_source', 'git_tags'),
                source_status='deferred',
                managed_services=managed_services,
                ignored_services=ignored_services,
            )
        return version_service.get_git_tag_version_info(recipe.repo_dir, fetch=False).model_copy(
            update={
                'runtime': runtime,
                'lifecycle_strategy': lifecycle_strategy,
                'version_source': obj.config.get('version_source', 'git_tags'),
                'managed_services': managed_services,
                'ignored_services': ignored_services,
            }
        )

    image_repository = None
    if primary is not None and primary.image:
        image_repository = primary.image.rsplit(':', 1)[0] if ':' in primary.image else primary.image
    if not image_repository:
        return PackageVersionInfo(
            current_version=primary.image_tag if primary is not None else None,
            runtime=runtime,
            lifecycle_strategy=lifecycle_strategy,
            version_source=obj.config.get('version_source', 'registry_tags'),
            managed_services=managed_services,
            ignored_services=ignored_services,
        )
    if not resolve_remote_versions:
        return PackageVersionInfo(
            current_version=primary.image_tag if primary is not None else None,
            runtime=runtime,
            lifecycle_strategy=lifecycle_strategy,
            version_source=obj.config.get('version_source', 'registry_tags'),
            source_status='deferred',
            managed_services=managed_services,
            ignored_services=ignored_services,
        )
    return version_service.get_registry_tag_version_info(
        image_repository,
        current_version=primary.image_tag if primary is not None else None,
    ).model_copy(
        update={
            'runtime': runtime,
            'lifecycle_strategy': lifecycle_strategy,
            'version_source': obj.config.get('version_source', 'registry_tags'),
            'managed_services': managed_services,
            'ignored_services': ignored_services,
        }
    )
