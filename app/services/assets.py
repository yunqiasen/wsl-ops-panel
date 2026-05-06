import logging
from collections import defaultdict
from collections.abc import Callable
from subprocess import CalledProcessError

from app.models.assets import AssetSnapshot, DockerContainerSnapshot
from app.models.registry import ObjectDefinition, RegistrySnapshot
from app.registry.service import RegistryService
from app.scanners.docker_scanner import scan_docker_containers
from app.scanners.host_process_scanner import scan_host_processes
from app.scanners.node_scanner import scan_node_packages
from app.scanners.python_scanner import scan_python_packages
from app.scanners.system_scanner import scan_system_infrastructure
from app.scanners.systemd_scanner import scan_systemd_units

LOGGER = logging.getLogger(__name__)
DockerScanner = Callable[[], list[DockerContainerSnapshot]]
SystemdScanner = Callable[[], list[AssetSnapshot]]
ReadonlyScanner = Callable[[], list[AssetSnapshot]]
DOCKER_SUPPORTED_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']
SYSTEMD_SUPPORTED_ACTIONS = ['delete']
READONLY_CATEGORY_IDS = ('node', 'python', 'host', 'system', 'agent_cli', 'agent')


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
    ) -> None:
        self._registry_service = registry_service
        self._docker_scanner = docker_scanner or scan_docker_containers
        self._systemd_scanner = systemd_scanner or scan_systemd_units
        self._node_scanner = node_scanner or scan_node_packages
        self._python_scanner = python_scanner or scan_python_packages
        self._host_process_scanner = host_process_scanner or scan_host_processes
        self._system_infra_scanner = system_infra_scanner or scan_system_infrastructure

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
            return build_docker_asset_snapshots(snapshot, self._scan_docker_containers())
        if category_id == 'systemd':
            scanned_units, scan_failed = self._scan_systemd_units()
            return build_systemd_asset_snapshots(snapshot, scanned_units, scan_failed=scan_failed)
        if category_id == 'node':
            return self._scan_readonly_assets(self._node_scanner, 'node')
        if category_id == 'python':
            return self._scan_readonly_assets(self._python_scanner, 'python')
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
) -> list[AssetSnapshot]:
    containers_by_dir: dict[str, list[DockerContainerSnapshot]] = defaultdict(list)
    containers_by_name = {container.name: container for container in containers}

    for container in containers:
        if container.compose_working_dir:
            containers_by_dir[container.compose_working_dir].append(container)

    assets: list[AssetSnapshot] = []
    for obj in registry_snapshot.objects:
        if obj.category != 'docker' or not obj.enabled:
            continue

        object_containers = sorted(containers_by_dir.get(obj.config['project_dir'], []), key=lambda item: item.name)
        primary = _select_primary_container(obj, object_containers, containers_by_name)
        status = primary.status if primary is not None else 'not running'
        current_version = primary.image_tag if primary is not None else None

        assets.append(
            AssetSnapshot(
                object_id=obj.id,
                category=obj.category,
                name=obj.name,
                status=status,
                current_version=current_version,
                supports_actions=DOCKER_SUPPORTED_ACTIONS.copy(),
                metadata={
                    'type': obj.type,
                    'project_dir': obj.config['project_dir'],
                    'compose_file': obj.config['compose_file'],
                    'primary_container': obj.config.get('primary_container'),
                    'compose_service': obj.config.get('compose_service'),
                },
                containers=object_containers,
                primary_container_name=primary.name if primary is not None else None,
            )
        )
    return assets


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
